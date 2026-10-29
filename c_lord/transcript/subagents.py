"""What a thread's sub-agents did, on Discord (#971).

Claude Code writes each sub-agent's conversation to
``<project_dir>/<session-id>/subagents/agent-<id>.jsonl`` (with a sibling
``agent-<id>.meta.json``), not to the session's own transcript — so the mirror,
which tails only ``<session-id>.jsonl``, never saw any of it. The reader got the
parent's one-sentence paraphrase of a 5 KB report, and an agent that finished
mid-turn left no trace at all (#969).

:class:`SubagentLog` reads those files — the same "read what Claude Code already
wrote" rule as the mirror (#71/#712); c-lord starts, steers and waits for no
agent itself. Per run of a sub-agent it posts:

- a start line (``-# 🤖 サブエージェント「…」開始``), whose attachment is replaced
  while the agent works (every ``CLORD_SUBAGENT_PROGRESS_SECONDS``, only when
  something changed);
- an end line with ``subagent-*.md``: the final report in full, the assignment,
  and every tool call.

With ``CLORD_SUBAGENT_VOICES=1`` it also posts, under the speaker's name, the
parent's assignments, every ``SendMessage`` between the parent and its agents
(and between agents), and each final report. Tool calls never become messages.

Sources (measured on CLI 2.1.312, see ``docs/specs/subagent-log.md``):

- a run ends with the parent's ``queue-operation`` ``enqueue`` of a
  ``<task-notification>`` (written even when the turn absorbs it); a resumed
  agent is notified again, so every notification closes one run. In-process
  teammates report ``idle_notification`` instead.
- a message is counted on the **sending** side only (the ``SendMessage``
  ``tool_use``); the receiving side's ``peer`` row is the same message again.
"""

from __future__ import annotations

import asyncio
import contextlib
import json
import logging
import os
import re
import tempfile
import time
from collections.abc import Callable
from dataclasses import dataclass, field
from datetime import datetime
from pathlib import Path
from typing import Protocol

from ..utils.logger import log_ctx
from .claim import read_claim
from .formatter import render_event

logger = logging.getLogger(__name__)

#: What the parent is called on Discord — it is "main" / "team-lead" to Claude.
PARENT_NAME = "親"
_PARENT_ADDRESSES = frozenset({"main", "team-lead", "lead", "parent"})

DEFAULT_PROGRESS_SECONDS = 60.0
#: Discord allows ~5 edits per 5 s per channel; a floor keeps several agents
#: well clear of it.
MIN_PROGRESS_SECONDS = 10.0
#: The attachment's ceiling. Discord takes 10 MB unboosted; this is far below so
#: a runaway agent does not turn every edit into a megabyte upload.
MAX_FILE_BYTES = 1_000_000
#: A message body longer than this is cut, the rest stays in the attachment.
MAX_VOICE_CHARS = 5400
#: One progress line, so a giant tool result cannot crowd out the rest.
MAX_LINE_CHARS = 4000
#: How long the loop waits with nothing running before looking again anyway. A
#: new agent always comes with a row of the parent's (which wakes the loop), and
#: every thread on the host has one of these loops, so the fallback is slow.
IDLE_WAKE_SECONDS = 300.0
#: A run whose file has been quiet this long is closed if its meta says finished
#: (the notice can be missing — a foreground call, a parent that died).
FINISHED_QUIET_SECONDS = 30.0
#: ... and closed regardless after this long, so a dead agent does not read
#: "作業中" forever.
STALLED_SECONDS = 1800.0


def _env_flag(name: str, default: bool) -> bool:
    raw = os.environ.get(name, "").strip().lower()
    if not raw:
        return default
    return raw not in ("0", "false", "no", "off")


def subagent_log_enabled() -> bool:
    """``CLORD_SUBAGENT_LOG`` — the start/end lines and attachments (default on)."""
    return _env_flag("CLORD_SUBAGENT_LOG", True)


def subagent_voices_enabled() -> bool:
    """``CLORD_SUBAGENT_VOICES`` — named messages and reports (default off)."""
    return _env_flag("CLORD_SUBAGENT_VOICES", False)


def subagent_progress_seconds() -> float:
    """``CLORD_SUBAGENT_PROGRESS_SECONDS`` — how often the live file is replaced."""
    raw = os.environ.get("CLORD_SUBAGENT_PROGRESS_SECONDS", "").strip()
    try:
        value = float(raw) if raw else DEFAULT_PROGRESS_SECONDS
    except ValueError:
        value = DEFAULT_PROGRESS_SECONDS
    return max(MIN_PROGRESS_SECONDS, value)


# --- parsing ----------------------------------------------------------------

_TASK_ID_RE = re.compile(r"<task-id>\s*([\w-]+)\s*</task-id>")
_STATUS_RE = re.compile(r"<status>\s*(\w+)\s*</status>")
_IDLE_RE = re.compile(
    r'<teammate-message teammate_id="([^"]+)"[^>]*>\s*(\{.*?\})\s*</teammate-message>', re.DOTALL
)
_WRAPPER_RE = re.compile(
    r"^\s*<(teammate-message|agent-message)\b[^>]*>\s*(.*?)\s*</\1>", re.DOTALL
)
_INTERNAL_PATH_RE = re.compile(r"/tmp/claude-\d+/[^\s\"'`<>)]*")


def _content(event: dict) -> object:
    message = event.get("message")
    return message.get("content") if isinstance(message, dict) else None


def task_notifications(event: dict) -> list[tuple[str, str]]:
    """``(agent id, status)`` of each run *event* reports finished.

    Only the ``enqueue`` row counts: the ``remove`` row, the absorbed
    ``queued_command`` attachment and the user row that opens a new turn are the
    same notice again.
    """
    if event.get("type") != "queue-operation" or event.get("operation") != "enqueue":
        return []
    content = event.get("content")
    if not isinstance(content, str) or "<task-notification>" not in content:
        return []
    task = _TASK_ID_RE.search(content)
    if task is None:
        return []
    status = _STATUS_RE.search(content)
    return [(task.group(1), status.group(1) if status else "completed")]


def teammate_idles(event: dict) -> list[str]:
    """Names of the in-process teammates *event* reports idle (Agent Teams)."""
    if event.get("type") != "user" or event.get("isSidechain"):
        return []
    content = _content(event)
    if not isinstance(content, str) or "idle_notification" not in content:
        return []
    names: list[str] = []
    for name, payload in _IDLE_RE.findall(content):
        with contextlib.suppress(ValueError):
            if json.loads(payload).get("type") == "idle_notification":
                names.append(name)
    return names


def sent_messages(event: dict) -> list[tuple[str, str]]:
    """``(to, text)`` of each ``SendMessage`` call in an assistant *event*."""
    if event.get("type") != "assistant":
        return []
    content = _content(event)
    if not isinstance(content, list):
        return []
    out: list[tuple[str, str]] = []
    for block in content:
        if not isinstance(block, dict) or block.get("type") != "tool_use":
            continue
        if block.get("name") != "SendMessage":
            continue
        inp = block.get("input") or {}
        to = inp.get("to") or inp.get("recipient")
        text = inp.get("message") or inp.get("content")
        if isinstance(to, str) and isinstance(text, str) and text.strip():
            out.append((to, text.strip()))
    return out


def _tool_uses(event: dict) -> int:
    content = _content(event)
    if event.get("type") != "assistant" or not isinstance(content, list):
        return 0
    return sum(1 for b in content if isinstance(b, dict) and b.get("type") == "tool_use")


def _assistant_text(event: dict) -> str:
    content = _content(event)
    if event.get("type") != "assistant" or not isinstance(content, list):
        return ""
    parts = [b.get("text", "") for b in content if isinstance(b, dict) and b.get("type") == "text"]
    return "\n".join(p for p in parts if p.strip()).strip()


def _unwrap(text: str) -> str:
    match = _WRAPPER_RE.match(text)
    return match.group(2).strip() if match else text.strip()


def _timestamp(event: dict) -> float | None:
    raw = event.get("timestamp")
    if not isinstance(raw, str):
        return None
    try:
        return datetime.fromisoformat(raw.replace("Z", "+00:00")).timestamp()
    except ValueError:
        return None


def _duration(seconds: float) -> str:
    seconds = max(0, int(seconds))
    if seconds >= 3600:
        return f"{seconds // 3600}:{seconds % 3600 // 60:02d}:{seconds % 60:02d}"
    return f"{seconds // 60}:{seconds % 60:02d}"


def _clip(text: str, limit: int) -> str:
    if len(text) <= limit:
        return text
    return text[:limit].rstrip() + "\n…（続きは添付）"


# --- state ------------------------------------------------------------------


@dataclass(frozen=True)
class AgentMeta:
    """What ``agent-<id>.meta.json`` says about one agent."""

    name: str
    teammate: bool = False
    finished: bool = False


def _read_meta(path: Path) -> AgentMeta | None:
    try:
        raw = json.loads(path.read_text(encoding="utf-8"))
    except (OSError, ValueError):
        return None
    if not isinstance(raw, dict):
        return None
    name = raw.get("name") or raw.get("description") or raw.get("agentType") or ""
    return AgentMeta(
        name=str(name).strip()[:80] or "サブエージェント",
        teammate=raw.get("taskKind") == "in_process_teammate",
        finished=raw.get("finished") is True,
    )


@dataclass
class _Run:
    started_at: float | None
    resumed: bool
    started_clock: float = 0.0
    last_row_clock: float = 0.0
    tools: int = 0
    report: str = ""
    handle: object | None = None
    last_upload: float = 0.0
    uploaded_len: int = -1


@dataclass
class _Agent:
    agent_id: str
    path: Path
    offset: int = 0
    # The file's first row is the parent's assignment; later prompts are
    # messages, already shown from the sender's side.
    read_any: bool = False
    meta: AgentMeta | None = None
    assignment: str = ""
    lines: list[str] = field(default_factory=list)
    run: _Run | None = None
    runs: int = 0
    final_status: str = ""
    # 1, 2, … in the order this log met the agents: tells attachments apart
    # when the name has no ASCII to put in the filename.
    ordinal: int = 0


@dataclass(frozen=True)
class StatusFile:
    """An attachment to put on a status line: a local *path* sent as *filename*."""

    path: str
    filename: str


class SubagentPoster(Protocol):
    """Discord side of the log; :class:`TranscriptMirrorCog` supplies the real one."""

    async def post_status(self, text: str, file: StatusFile | None) -> object | None: ...

    async def edit_status(self, handle: object, text: str, file: StatusFile | None) -> None: ...

    async def post_voice(self, speaker: str, text: str) -> None: ...


class SubagentLog:
    """Follow one thread's sub-agents and show their work (#971)."""

    def __init__(
        self,
        *,
        thread_id: int,
        project_dir: Path,
        poster: SubagentPoster,
        voices: bool = False,
        progress_seconds: float = DEFAULT_PROGRESS_SECONDS,
        poll_interval: float = 1.0,
        clock: Callable[[], float] = time.monotonic,
    ) -> None:
        self.thread_id = thread_id
        self.project_dir = project_dir
        self._poster = poster
        self._voices = voices
        self._progress_seconds = progress_seconds
        self._poll_interval = poll_interval
        self._clock = clock
        self._session_id: str | None = None
        self._agents: dict[str, _Agent] = {}
        # Files that were already there when we started: what they hold was
        # shown (or not) by the previous process, never again by this one.
        self._baseline: dict[Path, int] = {}
        # Sessions whose directory has been baselined. One named only later by
        # a parent row (no claim file) is baselined when first seen.
        self._baselined: set[str] = set()
        self._primed = False
        self._parent: list[dict] = []
        self._wake = asyncio.Event()
        self._task: asyncio.Task[None] | None = None
        self._ctx = log_ctx(thread_id=thread_id)

    # --- lifecycle ---------------------------------------------------------

    def start(self) -> None:
        if self._task is None or self._task.done():
            self._task = asyncio.create_task(self._loop(), name=f"subagent-log-{self.thread_id}")

    async def stop(self) -> None:
        task, self._task = self._task, None
        if task is None:
            return
        task.cancel()
        with contextlib.suppress(asyncio.CancelledError, Exception):
            await task

    async def _loop(self) -> None:
        await self.prime()
        while True:
            try:
                await self.step()
            except asyncio.CancelledError:
                raise
            except Exception:
                logger.warning("%s SubagentLog: step failed", self._ctx, exc_info=True)
            if self._running():
                await asyncio.sleep(self._poll_interval)
                continue
            self._wake.clear()
            with contextlib.suppress(asyncio.TimeoutError):
                await asyncio.wait_for(self._wake.wait(), timeout=IDLE_WAKE_SECONDS)

    def _running(self) -> bool:
        return any(a.run is not None for a in self._agents.values())

    async def prime(self) -> None:
        """Remember what is already on disk, so a restart shows none of it again."""
        if self._primed:
            return
        self._primed = True
        self._session_id = await asyncio.to_thread(read_claim, self.project_dir)
        await self._baseline_session()

    async def _baseline_session(self) -> None:
        if not self._session_id or self._session_id in self._baselined:
            return
        self._baselined.add(self._session_id)
        self._baseline.update(await asyncio.to_thread(self._sizes))

    def _subagents_dir(self) -> Path | None:
        if not self._session_id:
            return None
        return self.project_dir / self._session_id / "subagents"

    def _sizes(self) -> dict[Path, int]:
        directory = self._subagents_dir()
        if directory is None:
            return {}
        try:
            return {p: p.stat().st_size for p in directory.glob("agent-*.jsonl") if p.is_file()}
        except OSError:
            return {}

    # --- input -------------------------------------------------------------

    def observe_parent(self, event: dict) -> None:
        """Hand the log one row of the parent's transcript. Never blocks."""
        session = event.get("sessionId")
        if (
            isinstance(session, str)
            and session != self._session_id
            and not event.get("isSidechain")
        ):
            if self._session_id is not None:
                logger.info("%s SubagentLog: following session %s", self._ctx, session[:8])
            self._session_id = session
        if task_notifications(event) or teammate_idles(event) or sent_messages(event):
            self._parent.append(event)
        self._wake.set()

    async def step(self) -> None:
        """Read what is new, post what it means. One poll of the loop."""
        await self._baseline_session()
        await self._read_agents()
        parent, self._parent = self._parent, []
        for event in parent:
            await self._on_parent(event)
        await self._refresh_running()

    # --- children ----------------------------------------------------------

    async def _read_agents(self) -> None:
        directory = self._subagents_dir()
        if directory is None:
            return
        try:
            # A meta file can come first; it is what names the agent a message
            # is addressed to.
            ids = sorted(
                {p.name.removesuffix(".meta.json") for p in directory.glob("agent-*.meta.json")}
                | {p.name.removesuffix(".jsonl") for p in directory.glob("agent-*.jsonl")}
            )
        except OSError:
            return
        # Every agent is known by name before any row is read: a message in one
        # file is addressed to an agent whose file sorts after it.
        for stem in ids:
            agent_id = stem.removeprefix("agent-")
            agent = self._agents.get(agent_id)
            if agent is None:
                path = directory / f"{stem}.jsonl"
                start = self._baseline.get(path, 0)
                agent = _Agent(
                    agent_id=agent_id,
                    path=path,
                    offset=start,
                    read_any=start > 0,
                    ordinal=len(self._agents) + 1,
                )
                self._agents[agent_id] = agent
            if agent.meta is None:
                agent.meta = await asyncio.to_thread(_read_meta, directory / f"{stem}.meta.json")
        # One timeline across the files, so a question is shown before its answer.
        batch: list[tuple[float, int, _Agent, dict]] = []
        for agent in list(self._agents.values()):
            for row in await asyncio.to_thread(self._read_rows, agent):
                batch.append((_timestamp(row) or 0.0, len(batch), agent, row))
        for _, _, agent, row in sorted(batch, key=lambda item: (item[0], item[1])):
            await self._on_agent_row(agent, row)

    @staticmethod
    def _read_rows(agent: _Agent) -> list[dict]:
        try:
            with agent.path.open("rb") as f:
                f.seek(agent.offset)
                data = f.read()
        except OSError:
            return []
        end = data.rfind(b"\n")
        if end < 0:
            return []
        agent.offset += end + 1
        rows: list[dict] = []
        for line in data[: end + 1].splitlines():
            with contextlib.suppress(ValueError):
                row = json.loads(line)
                if isinstance(row, dict):
                    rows.append(row)
        return rows

    def _name(self, address: str) -> str:
        if address in _PARENT_ADDRESSES:
            return PARENT_NAME
        agent = self._agents.get(address)
        if agent is not None and agent.meta is not None:
            return agent.meta.name
        for other in self._agents.values():
            if other.meta is not None and other.meta.name == address:
                return other.meta.name
        return address

    def _agent_for(self, address: str) -> _Agent | None:
        if address in self._agents:
            return self._agents[address]
        for agent in self._agents.values():
            if agent.meta is not None and agent.meta.name == address:
                return agent
        return None

    def _scrub(self, text: str) -> str:
        """Agent ids and harness paths are Claude's bookkeeping, not the reader's."""
        for agent in self._agents.values():
            name = agent.meta.name if agent.meta is not None else "サブエージェント"
            text = re.sub(rf"\b{re.escape(agent.agent_id[:7])}[\w-]*", name, text)
        return _INTERNAL_PATH_RE.sub("…", text)

    def _label(self, agent: _Agent) -> str:
        kind = (
            "チームメイト" if agent.meta is not None and agent.meta.teammate else "サブエージェント"
        )
        name = agent.meta.name if agent.meta is not None else "?"
        return f"{kind}「{name}」"

    async def _on_agent_row(self, agent: _Agent, row: dict) -> None:
        if row.get("type") not in ("user", "assistant"):
            return
        if agent.run is None:
            await self._begin(agent, row)
        assert agent.run is not None
        agent.run.last_row_clock = self._clock()
        content = _content(row)
        first = not agent.read_any
        agent.read_any = True
        if row.get("type") == "user":
            origin = row.get("origin") or {}
            if isinstance(content, str) and row.get("isMeta"):
                if isinstance(origin, dict) and origin.get("kind") == "peer":
                    sender = self._name(str(origin.get("from") or origin.get("name") or "?"))
                    body = str(origin.get("body") or _unwrap(content))
                    agent.lines.append(f"📨 ← {sender}: {body}")
                return
            if isinstance(content, str):
                text = _unwrap(content)
                if first:
                    agent.assignment = text
                    if self._voices:
                        await self._voice(
                            PARENT_NAME, f"→ **{self._label_name(agent)}**（依頼）\n{text}"
                        )
                else:
                    agent.lines.append(f"📨 {text}")
                return
        for to, text in sent_messages(row):
            agent.lines.append(f"✉️ → {self._name(to)}: {text}")
            if self._voices:
                await self._voice(self._label_name(agent), f"→ **{self._name(to)}**\n{text}")
        agent.run.tools += _tool_uses(row)
        said = _assistant_text(row)
        if said:
            agent.run.report = said
        rendered = render_event(row)
        if rendered is not None and rendered.kind != "user_input":
            prefix = {"tool_result": "↳ ", "thinking": "💭 "}.get(rendered.kind, "")
            if rendered.kind == "assistant_text":
                prefix = "💬 "
            body = rendered.body
            if len(body) > MAX_LINE_CHARS:
                body = body[:MAX_LINE_CHARS] + " …"
            agent.lines.append(prefix + body)

    @staticmethod
    def _label_name(agent: _Agent) -> str:
        return agent.meta.name if agent.meta is not None else "サブエージェント"

    async def _begin(self, agent: _Agent, row: dict) -> None:
        resumed = agent.read_any
        now = self._clock()
        agent.run = _Run(
            started_at=_timestamp(row),
            resumed=resumed,
            started_clock=now,
            last_upload=now,
            last_row_clock=now,
        )
        agent.runs += 1
        word = "再開" if resumed else "開始"
        logger.info(
            "%s SubagentLog: %s %s (run %d)", self._ctx, self._label(agent), word, agent.runs
        )
        agent.run.handle = await self._safe(
            self._poster.post_status(f"-# 🤖 {self._label(agent)}{word}", None)
        )

    # --- parent ------------------------------------------------------------

    async def _on_parent(self, event: dict) -> None:
        for agent_id, status in task_notifications(event):
            agent = self._agents.get(agent_id)
            if agent is not None:
                await self._finish(agent, status, _timestamp(event))
        for name in teammate_idles(event):
            agent = self._agent_for(name)
            if agent is not None:
                await self._finish(agent, "idle", _timestamp(event))
        if self._voices:
            for to, text in sent_messages(event):
                agent = self._agent_for(to)
                if agent is not None:
                    await self._voice(PARENT_NAME, f"→ **{self._label_name(agent)}**\n{text}")

    async def _finish(self, agent: _Agent, status: str, at: float | None) -> None:
        run = agent.run
        if run is None:
            # Finished before we started, or a notice for a run we never saw.
            return
        # The notice can overtake the last rows of the agent's own file.
        for row in await asyncio.to_thread(self._read_rows, agent):
            await self._on_agent_row(agent, row)
        agent.run = None
        agent.final_status = status
        if at is not None and run.started_at is not None:
            elapsed = at - run.started_at
        else:
            elapsed = self._clock() - run.started_clock
        how = {
            "failed": "（失敗）",
            "killed": "（停止）",
            "stopped": "（停止）",
            "stalled": "（記録が止まりました）",
        }.get(status, "")
        summary = f"{_duration(elapsed)} · ツール {run.tools} 件"
        if run.handle is not None:
            await self._safe(
                self._poster.edit_status(
                    run.handle,
                    f"-# 🤖 {self._label(agent)}{'再開' if run.resumed else '開始'}（終了は下）",
                    None,
                )
            )
        await self._with_file(
            agent,
            run,
            f"終了{how}",
            lambda file: self._poster.post_status(
                f"-# 🤖 {self._label(agent)}終了{how} · {summary}", file
            ),
        )
        logger.info("%s SubagentLog: %s 終了 status=%s", self._ctx, self._label(agent), status)
        if self._voices and run.report:
            await self._voice(self._label_name(agent), f"📝 **最終報告**\n{run.report}")

    async def _refresh_running(self) -> None:
        now = self._clock()
        for agent in list(self._agents.values()):
            run = agent.run
            if run is None:
                continue
            quiet = now - run.last_row_clock
            if quiet >= FINISHED_QUIET_SECONDS:
                meta = await asyncio.to_thread(
                    _read_meta, agent.path.with_name(agent.path.stem + ".meta.json")
                )
                if meta is not None and meta.finished:
                    await self._finish(agent, "completed", None)
                    continue
            if quiet >= STALLED_SECONDS:
                await self._finish(agent, "stalled", None)
                continue
            if run.handle is None:
                continue
            if now - run.last_upload < self._progress_seconds:
                continue
            size = len(agent.lines) + len(run.report)
            if size == run.uploaded_len:
                continue
            run.last_upload = now
            run.uploaded_len = size
            elapsed = now - run.started_clock
            text = (
                f"-# 🤖 {self._label(agent)}作業中 · {_duration(elapsed)} · ツール {run.tools} 件"
            )
            await self._with_file(
                agent,
                run,
                "作業中",
                lambda file, handle=run.handle, text=text: self._poster.edit_status(
                    handle, text, file
                ),
            )

    # --- output ------------------------------------------------------------

    def _document(self, agent: _Agent, run: _Run, state: str) -> str:
        meta = agent.meta
        parts = [f"# {self._label(agent)}", "", f"- 状態: {state}", f"- ツール: {run.tools} 件"]
        if meta is not None and meta.teammate:
            parts.append("- 種類: Agent Teams の in-process teammate")
        if run.report:
            parts += ["", "## 最終報告", "", run.report]
        if agent.assignment:
            parts += ["", "## 依頼（親から）", "", agent.assignment]
        parts += ["", "## 途中経過", ""]
        body = "\n".join(parts) + "\n" + "\n".join(agent.lines) + "\n"
        body = self._scrub(body)
        data = body.encode("utf-8")
        if len(data) > MAX_FILE_BYTES:
            head = data[: MAX_FILE_BYTES // 5].decode("utf-8", "ignore")
            tail = data[-(MAX_FILE_BYTES * 3 // 4) :].decode("utf-8", "ignore")
            body = f"{head}\n\n…（中略 — 大きすぎるため）…\n\n{tail}"
        return body

    async def _with_file(self, agent: _Agent, run: _Run, state: str, send) -> object | None:
        tmp: str | None = None
        try:
            with tempfile.NamedTemporaryFile(
                "w",
                suffix=".md",
                prefix=f"clord_subagent_{self.thread_id}_",
                delete=False,
                encoding="utf-8",
            ) as f:
                f.write(self._document(agent, run, state))
                tmp = f.name
            return await self._safe(send(StatusFile(tmp, _filename(agent))))
        finally:
            if tmp is not None:
                with contextlib.suppress(OSError):
                    os.unlink(tmp)

    async def _voice(self, speaker: str, text: str) -> None:
        await self._safe(
            self._poster.post_voice(self._scrub(speaker), _clip(self._scrub(text), MAX_VOICE_CHARS))
        )

    async def _safe(self, call) -> object | None:
        try:
            return await call
        except asyncio.CancelledError:
            raise
        except Exception:
            logger.warning("%s SubagentLog: Discord call failed", self._ctx, exc_info=True)
            return None


_SLUG_RE = re.compile(r"[^A-Za-z0-9._-]+")


def _filename(agent: _Agent) -> str:
    name = agent.meta.name if agent.meta is not None else ""
    slug = _SLUG_RE.sub("-", name).strip("-.")[:40]
    return f"subagent-{agent.ordinal}-{slug}.md" if slug else f"subagent-{agent.ordinal}.md"
