"""idle-sleep の安全網 — #922 / #964 (関連 #906) の取りこぼしを塞ぐテスト。

``tests/test_idle_sleep.py`` が主経路 (シェルが生きている / transcript が新しい /
mtime だけ動いた) を押さえている。ここはその周りの「判定材料が欠けたとき・壊れて
いるとき」にどちらへ倒れるかを固定する:

* 材料が**無い**（working_dir 無し・transcript 無し）→ 従来どおり ``last_used_at``
  だけで判定し、眠らせる。#922 の修正前の挙動であり、#572 の省メモリを殺さない。
* 材料が**読めない**（``_count_tool_shells`` が例外）→ 眠らせない。殺せば #922 の
  40 分の書き出しと同じことが起きる。しかも次の tick で必ず見直す（visited にしない）。

transcript の形は Claude Code が実際に書くもの: ``timestamp`` は ``message`` の
**後ろ**にあり (このホストの実 transcript のキー順)、サブエージェントは
``<project>/<session-uuid>/subagents/agent-<hex>.jsonl``（横に ``.meta.json``）。
"""

from __future__ import annotations

import datetime
import json
import logging
import os
from pathlib import Path
from unittest.mock import AsyncMock, MagicMock

import pytest

from c_lord.database.repository import SessionRecord
from c_lord.idle_sleep import IdleSleepLoop, last_transcript_write
from c_lord.transcript.resolver import derive_project_dir

FIXTURES = Path(__file__).parent / "fixtures" / "transcripts"
NOW = datetime.datetime(2026, 10, 29, 9, 0, 0)
SESSION = "8f079a5d-89a5-4007-8edd-52ac83e86b77"


def _utc(moment: datetime.datetime) -> str:
    """Local naive → Claude Code's ``2026-10-28T12:25:35.337Z`` form."""
    return moment.astimezone(datetime.timezone.utc).strftime("%Y-%m-%dT%H:%M:%S.%f")[:-3] + "Z"  # noqa: UP017 — 3.10


def _local(stamp: str) -> datetime.datetime:
    return (
        datetime.datetime.fromisoformat(stamp.replace("Z", "+00:00"))
        .astimezone()
        .replace(tzinfo=None)
    )


def _assistant(moment: datetime.datetime, *, text: str = "ok") -> dict:
    """Key order as Claude Code writes it: ``timestamp`` after ``message``."""
    return {
        "parentUuid": "p",
        "isSidechain": False,
        "message": {"role": "assistant", "content": [{"type": "text", "text": text}]},
        "type": "assistant",
        "uuid": "u",
        "timestamp": _utc(moment),
        "cwd": "/w",
        "sessionId": SESSION,
        "version": "2.1.301",
    }


def _write(path: Path, events: list[dict], *, mtime: datetime.datetime | None = None) -> Path:
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text("".join(json.dumps(e, ensure_ascii=False) + "\n" for e in events), "utf-8")
    if mtime is not None:
        os.utime(path, (mtime.timestamp(), mtime.timestamp()))
    return path


def _ago(hours: float) -> datetime.datetime:
    return NOW - datetime.timedelta(hours=hours)


# ── last_transcript_write: what counts as "Claude last wrote" (#922/#964) ──


class TestLastTranscriptWrite:
    def test_a_project_dir_claude_never_created_is_none(self, tmp_path: Path) -> None:
        """Claude Code creates the project dir lazily (resolver docstring)."""
        assert last_transcript_write(tmp_path / "never-created") is None

    def test_a_dir_with_no_jsonl_is_none(self, tmp_path: Path) -> None:
        (tmp_path / SESSION / "subagents").mkdir(parents=True)
        (tmp_path / SESSION / "subagents" / "agent-a313f018869c2688a.meta.json").write_text("{}")
        assert last_transcript_write(tmp_path) is None

    def test_a_real_transcript_answers_its_newest_event(self, tmp_path: Path) -> None:
        """#964: the answer is an event time, read from the file's content.

        i961_a is a real production transcript (2026-10-28, pytest wait); its
        queue-operation lines are stamped out of order, so this also pins
        "newest stamp", not "stamp of the last line".
        """
        path = tmp_path / f"{SESSION}.jsonl"
        path.write_bytes((FIXTURES / "i961_a_pytest_wait.jsonl").read_bytes())
        assert last_transcript_write(tmp_path) == _local("2026-10-28T12:55:15.525Z")

    def test_out_of_order_stamps_take_the_newest(self, tmp_path: Path) -> None:
        """queue-operation enqueue/dequeue are written with stamps older than the
        user event before them (i961 fixtures, line 4 vs 5)."""
        _write(
            tmp_path / f"{SESSION}.jsonl",
            [
                {"type": "queue-operation", "timestamp": _utc(_ago(1)), "operation": "enqueue"},
                {"type": "queue-operation", "timestamp": _utc(_ago(5)), "operation": "dequeue"},
            ],
        )
        assert last_transcript_write(tmp_path) == _ago(1).replace(microsecond=0)

    def test_a_subagent_transcript_counts(self, tmp_path: Path) -> None:
        """#922: W33 #917 was slept while waiting on a subagent — the parent
        transcript was quiet, the subagent's was not."""
        _write(tmp_path / f"{SESSION}.jsonl", [_assistant(_ago(6))], mtime=_ago(6))
        _write(
            tmp_path / SESSION / "subagents" / "agent-a89685a62f09b55d5.jsonl",
            [_assistant(_ago(0.5))],
            mtime=_ago(0.5),
        )
        assert last_transcript_write(tmp_path) == _ago(0.5).replace(microsecond=0)

    def test_an_older_file_cannot_hide_a_newer_one_in_either_order(self, tmp_path: Path) -> None:
        """The mtime pre-filter only skips files that cannot be newer."""
        _write(tmp_path / f"{SESSION}.jsonl", [_assistant(_ago(0.2))], mtime=_ago(0.2))
        _write(
            tmp_path / SESSION / "subagents" / "agent-a1.jsonl",
            [_assistant(_ago(7))],
            mtime=_ago(7),
        )
        assert last_transcript_write(tmp_path) == _ago(0.2).replace(microsecond=0)

    def test_an_hourly_touch_does_not_move_it(self, tmp_path: Path) -> None:
        """#906/#964: the CLI bumps mtime about once an hour without writing."""
        _write(tmp_path / f"{SESSION}.jsonl", [_assistant(_ago(15))], mtime=_ago(0.1))
        assert last_transcript_write(tmp_path) == _ago(15).replace(microsecond=0)

    def test_the_stamp_after_a_huge_message_is_still_found(self, tmp_path: Path) -> None:
        """Only the last 256 KiB is read. Claude Code writes ``timestamp`` after
        ``message``, so even a record bigger than that window (a long tool
        result — prod transcripts reach ~100 MB, #537) yields its own stamp."""
        _write(
            tmp_path / f"{SESSION}.jsonl",
            [_assistant(_ago(9)), _assistant(_ago(1), text="x" * (400 * 1024))],
        )
        assert last_transcript_write(tmp_path) == _ago(1).replace(microsecond=0)

    def test_metadata_lines_without_a_stamp_fall_back_to_the_last_event(
        self, tmp_path: Path
    ) -> None:
        """mode / ai-title / last-prompt / api-request-blob carry no timestamp
        (real key sets on this host); they must not erase the event before them."""
        _write(
            tmp_path / f"{SESSION}.jsonl",
            [
                _assistant(_ago(3)),
                {"type": "last-prompt", "lastPrompt": "…", "sessionId": SESSION},
                {"type": "ai-title", "aiTitle": "…", "sessionId": SESSION},
                {"type": "api-request-blob", "sessionId": SESSION, "hash": "h", "message": {}},
            ],
        )
        assert last_transcript_write(tmp_path) == _ago(3).replace(microsecond=0)

    def test_naive_and_unparsable_stamps_are_ignored(self, tmp_path: Path) -> None:
        """Defensive: a stamp without a zone cannot be placed on the local clock,
        and a garbage one cannot be placed at all — neither may win.

        (Every top-level and nested ``"timestamp"`` in 300 recent real
        transcripts here is ISO-8601 with ``Z``; this pins the guard, not a
        shape Claude Code is known to write.)"""
        _write(
            tmp_path / f"{SESSION}.jsonl",
            [
                _assistant(_ago(6)),
                {"type": "queue-operation", "timestamp": "2026-10-29T08:59:00"},
                {"type": "queue-operation", "timestamp": "not-a-date"},
            ],
        )
        assert last_transcript_write(tmp_path) == _ago(6).replace(microsecond=0)

    def test_a_file_that_vanishes_mid_listing_is_skipped(self, tmp_path: Path) -> None:
        """A dangling ``*.jsonl`` (removed between listing and stat) is skipped,
        not fatal — the other transcripts still answer."""
        _write(tmp_path / f"{SESSION}.jsonl", [_assistant(_ago(2))])
        (tmp_path / "gone.jsonl").symlink_to(tmp_path / "does-not-exist.jsonl")
        assert last_transcript_write(tmp_path) == _ago(2).replace(microsecond=0)

    def test_an_unlistable_dir_is_none(self, tmp_path: Path, monkeypatch) -> None:
        def _boom(self, pattern):  # noqa: ANN001
            raise PermissionError(13, "Permission denied")

        monkeypatch.setattr(Path, "rglob", _boom)
        assert last_transcript_write(tmp_path) is None


# ── the loop: which way it falls when evidence is missing or unreadable ──


def _rec(tid: int = 1, *, hours_ago: float = 9, working_dir: str | None = "/w") -> SessionRecord:
    ts = _ago(hours_ago).strftime("%Y-%m-%d %H:%M:%S")
    return SessionRecord(
        thread_id=tid,
        session_id="a" * 32,
        working_dir=working_dir,  # type: ignore[arg-type]
        model="opus",
        origin="discord",
        summary=None,
        created_at=ts,
        last_used_at=ts,
        closed_at=None,
    )


class _Cog:
    """What the loop asks of SessionManageCog (the real one has both — see
    ``test_idle_sleep.py::test_the_cog_really_has_the_shell_count``)."""

    def __init__(self, shells: object = 0) -> None:
        self.shells = shells
        self._sleep_workspace_impl = AsyncMock(return_value=True)

    async def _count_tool_shells(self, channel: object) -> int:
        if isinstance(self.shells, BaseException):
            raise self.shells
        return self.shells  # type: ignore[return-value]


def _bot(cog: object) -> MagicMock:
    bot = MagicMock()
    bot.get_cog = MagicMock(return_value=cog)
    bot.get_channel = MagicMock(side_effect=lambda t: MagicMock(id=t))
    return bot


def _repo(*records: SessionRecord) -> MagicMock:
    repo = MagicMock()
    repo.list_all = AsyncMock(return_value=list(records))
    return repo


def _loop(cog: object, repo: MagicMock, tmp_path: Path, now=NOW) -> IdleSleepLoop:
    loop = IdleSleepLoop(_bot(cog), repo, threshold_hours=4, now_fn=lambda: now)
    loop._projects_root = tmp_path / "projects"
    return loop


class TestMissingEvidenceStillSleeps:
    """#922 AC2: with nothing showing Claude at work, #572's 4h rule applies."""

    @pytest.mark.asyncio
    async def test_a_record_without_working_dir(self, tmp_path: Path) -> None:
        cog = _Cog()
        assert await _loop(cog, _repo(_rec(working_dir=None)), tmp_path).tick() == 1
        cog._sleep_workspace_impl.assert_awaited_once()

    @pytest.mark.asyncio
    async def test_a_workspace_with_no_transcript_at_all(self, tmp_path: Path) -> None:
        """Claude never wrote (or its project dir was cleaned up)."""
        cog = _Cog()
        rec = _rec(working_dir=str(tmp_path / "w"))
        assert await _loop(cog, _repo(rec), tmp_path).tick() == 1


class TestUnreadableEvidenceKeepsItAwake:
    """#922: 2026-10-17 00:38 the window was killed under a 40-minute render.
    Not being able to look for running commands must never read as "none"."""

    @pytest.mark.asyncio
    async def test_a_failing_shell_count_does_not_sleep(self, tmp_path: Path, caplog) -> None:
        cog = _Cog(shells=RuntimeError("tmux: no server running"))
        loop = _loop(cog, _repo(_rec()), tmp_path)

        with caplog.at_level(logging.WARNING, logger="c_lord.idle_sleep"):
            assert await loop.tick() == 0

        cog._sleep_workspace_impl.assert_not_awaited()
        assert "could not look for running commands in thread=1" in caplog.text

    @pytest.mark.asyncio
    async def test_and_it_is_looked_at_again_next_tick(self, tmp_path: Path) -> None:
        """Not marked visited: a busy skip that stuck would mean the workspace
        never sleeps until a human speaks again (the ``_busy`` comment)."""
        cog = _Cog(shells=RuntimeError("boom"))
        loop = _loop(cog, _repo(_rec()), tmp_path)
        assert await loop.tick() == 0
        assert 1 not in loop._visited

        cog.shells = 0
        assert await loop.tick() == 1
        cog._sleep_workspace_impl.assert_awaited_once()

    @pytest.mark.asyncio
    async def test_a_running_command_is_logged_with_its_count(self, tmp_path: Path, caplog) -> None:
        """The INFO line ``docs/specs/workspace-sleep.md`` promises for a skip."""
        cog = _Cog(shells=2)
        with caplog.at_level(logging.INFO, logger="c_lord.idle_sleep"):
            assert await _loop(cog, _repo(_rec()), tmp_path).tick() == 0
        assert "thread=1 not slept — 2 command(s) Claude started are still running" in caplog.text

    @pytest.mark.asyncio
    async def test_a_recent_transcript_is_logged(self, tmp_path: Path, caplog) -> None:
        rec = _rec(working_dir=str(tmp_path / "w"))
        project = derive_project_dir(str(tmp_path / "w"), projects_root=tmp_path / "projects")
        _write(project / f"{SESSION}.jsonl", [_assistant(_ago(1))], mtime=_ago(1))
        cog = _Cog()
        loop = _loop(cog, _repo(rec), tmp_path)
        with caplog.at_level(logging.INFO, logger="c_lord.idle_sleep"):
            assert await loop.tick() == 0
        assert "thread=1 not slept — Claude wrote its transcript at" in caplog.text
        assert 1 not in loop._visited


class TestCogUnavailable:
    @pytest.mark.asyncio
    async def test_no_session_cog_sleeps_nothing_and_does_not_raise(
        self, tmp_path: Path, caplog
    ) -> None:
        """There is one implementation of "sleep" (#538); without it the loop
        must do nothing rather than improvise."""
        with caplog.at_level(logging.WARNING, logger="c_lord.idle_sleep"):
            assert await _loop(None, _repo(_rec()), tmp_path).tick() == 0
        assert "SessionManageCog unavailable — skipping thread=1" in caplog.text

    @pytest.mark.asyncio
    async def test_a_cog_without_a_shell_count_still_sleeps(self, tmp_path: Path) -> None:
        """``_count_tool_shells`` is looked up, not required — a consumer's cog
        built before #922 keeps the #572 behaviour."""
        cog = MagicMock(spec=["_sleep_workspace_impl"])
        cog._sleep_workspace_impl = AsyncMock(return_value=True)
        assert await _loop(cog, _repo(_rec()), tmp_path).tick() == 1


class TestVisitedFilter:
    """``_needs_action``: a slept row keeps matching for up to a week (no
    ``closed_at``); re-probing it every 10 minutes is a fetch_channel each."""

    @pytest.mark.asyncio
    async def test_a_workspace_already_handled_is_not_probed_again(self, tmp_path: Path) -> None:
        cog = _Cog()
        bot = _bot(cog)
        loop = IdleSleepLoop(bot, _repo(_rec()), threshold_hours=4, now_fn=lambda: NOW)
        loop._projects_root = tmp_path / "projects"
        assert await loop.tick() == 1
        assert await loop.tick() == 0
        assert bot.get_channel.call_count == 1
        cog._sleep_workspace_impl.assert_awaited_once()

    @pytest.mark.asyncio
    async def test_a_human_speaking_again_makes_it_eligible_again(self, tmp_path: Path) -> None:
        cog = _Cog()
        repo = _repo(_rec(hours_ago=9))
        loop = _loop(cog, repo, tmp_path)
        assert await loop.tick() == 1

        repo.list_all = AsyncMock(return_value=[_rec(hours_ago=5)])
        assert await loop.tick() == 1
        assert cog._sleep_workspace_impl.await_count == 2
