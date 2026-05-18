"""Per-thread JSONL transcript → Discord sink pipe.

A :class:`TranscriptMirror` owns one asyncio task that tails the active
Claude Code session jsonl for a project directory and pushes each rendered
event to an awaitable ``sink`` callback (typically ``discord.Thread.send``).
The task survives transient sink errors so a flaky Discord call does not
permanently silence the mirror.

Verbosity modes (``CLORD_MIRROR_VERBOSITY`` env var, default ``minimal``):

- ``minimal``: only final ``assistant_text`` events reach Discord.
  ``tool_use`` / ``tool_result`` events are buffered and written to a
  temporary ``progress.txt`` file that is attached to the assistant reply
  via ``file_sink``.  When ``file_sink`` is ``None``, the assistant text is
  posted via the plain ``sink`` (graceful degradation).
- ``full``: all rendered events are posted to ``sink`` in real time
  (original behaviour, useful for debugging).
"""

from __future__ import annotations

import asyncio
import contextlib
import logging
import os
import tempfile
from collections.abc import Awaitable, Callable
from pathlib import Path

from .formatter import RenderedEvent, render_event
from .tail import tail_events

logger = logging.getLogger(__name__)

Sink = Callable[[str], Awaitable[None]]
FileSink = Callable[[str, str], Awaitable[None]]

# Kinds that are buffered (not posted individually) in minimal mode.
_BUFFERED_KINDS = frozenset({"tool_use", "tool_result"})


def bridge_mode_jsonl() -> bool:
    """Return True iff ``CLORD_BRIDGE_MODE`` is set to ``jsonl``."""
    return os.getenv("CLORD_BRIDGE_MODE", "skill").strip().lower() == "jsonl"


def verbosity_mode() -> str:
    """Return the mirror verbosity mode from ``CLORD_MIRROR_VERBOSITY``.

    Defaults to ``"minimal"`` (only final assistant text reaches Discord).
    Set to ``"full"`` for the original behaviour (all events posted live).
    """
    return os.getenv("CLORD_MIRROR_VERBOSITY", "minimal").strip().lower()


_KIND_PREFIX = {
    "assistant_text": "",
    "tool_use": "",  # tool_use bodies already start with the 🔧 emoji
    "tool_result": "↳ ",
    "user_input": "👤 ",
}


def _format_body(rendered: RenderedEvent) -> str:
    prefix = _KIND_PREFIX.get(rendered.kind, "")
    return f"{prefix}{rendered.body}"


class TranscriptMirror:
    """Tail one project's jsonl and forward rendered events to ``sink``."""

    def __init__(
        self,
        *,
        thread_id: int,
        project_dir: Path,
        sink: Sink,
        file_sink: FileSink | None = None,
        verbosity: str = "minimal",
        poll_interval: float = 0.5,
    ) -> None:
        self.thread_id = thread_id
        self.project_dir = project_dir
        self._sink = sink
        self._file_sink = file_sink
        self._verbosity = verbosity
        self._poll_interval = poll_interval
        self._task: asyncio.Task[None] | None = None

    def start(self) -> None:
        """Spawn the tail task.  Idempotent."""
        if self._task is not None and not self._task.done():
            return
        self._task = asyncio.create_task(self._run(), name=f"transcript-mirror-{self.thread_id}")

    async def stop(self) -> None:
        """Cancel the tail task and wait for it to settle.  Safe to call repeatedly."""
        task = self._task
        if task is None:
            return
        task.cancel()
        with contextlib.suppress(asyncio.CancelledError, Exception):
            await task
        self._task = None

    async def _run(self) -> None:
        logger.info(
            "TranscriptMirror starting: thread=%d project_dir=%s verbosity=%s",
            self.thread_id,
            self.project_dir,
            self._verbosity,
        )
        # Buffer for tool_use / tool_result lines in minimal mode.
        progress_buf: list[str] = []

        try:
            async for event in tail_events(self.project_dir, poll_interval=self._poll_interval):
                rendered = render_event(event)
                if rendered is None:
                    continue

                if self._verbosity == "minimal":
                    await self._handle_minimal(rendered, progress_buf)
                else:
                    await self._post(rendered)

        except asyncio.CancelledError:
            pass
        finally:
            logger.info("TranscriptMirror stopped: thread=%d", self.thread_id)

    async def _handle_minimal(
        self, rendered: RenderedEvent, progress_buf: list[str]
    ) -> None:
        """Route one event in minimal mode."""
        if rendered.kind in _BUFFERED_KINDS:
            progress_buf.append(_format_body(rendered))
            return

        if rendered.kind == "assistant_text":
            body = _format_body(rendered)
            if progress_buf and self._file_sink is not None:
                await self._flush_with_progress(body, progress_buf)
            else:
                await self._try_sink(body)
            progress_buf.clear()
            return

        # user_input and any future kinds: post directly.
        await self._post(rendered)

    async def _flush_with_progress(self, body: str, progress_buf: list[str]) -> None:
        """Write buffered tool lines to a tempfile and call file_sink."""
        assert self._file_sink is not None
        tmp_path: str | None = None
        try:
            with tempfile.NamedTemporaryFile(
                mode="w",
                suffix=".txt",
                prefix=f"clord_progress_{self.thread_id}_",
                delete=False,
                encoding="utf-8",
            ) as f:
                f.write("\n".join(progress_buf))
                tmp_path = f.name
            try:
                await self._file_sink(body, tmp_path)
            except asyncio.CancelledError:
                raise
            except Exception:
                logger.warning(
                    "TranscriptMirror file_sink failed for thread=%d",
                    self.thread_id,
                    exc_info=True,
                )
        finally:
            if tmp_path is not None:
                with contextlib.suppress(OSError):
                    os.unlink(tmp_path)

    async def _post(self, rendered: RenderedEvent) -> None:
        """Format and send a rendered event via plain sink."""
        await self._try_sink(_format_body(rendered))

    async def _try_sink(self, body: str) -> None:
        try:
            await self._sink(body)
        except asyncio.CancelledError:
            raise
        except Exception:
            logger.warning(
                "TranscriptMirror sink failed for thread=%d",
                self.thread_id,
                exc_info=True,
            )
