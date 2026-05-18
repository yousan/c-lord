"""Per-thread JSONL transcript → Discord sink pipe.

A :class:`TranscriptMirror` owns one asyncio task that tails the active
Claude Code session jsonl for a project directory and pushes each rendered
event to an awaitable ``sink`` callback (typically ``discord.Thread.send``).
The task survives transient sink errors so a flaky Discord call does not
permanently silence the mirror.
"""

from __future__ import annotations

import asyncio
import contextlib
import logging
import os
from collections.abc import Awaitable, Callable
from pathlib import Path

from .formatter import RenderedEvent, render_event
from .tail import tail_events

logger = logging.getLogger(__name__)

Sink = Callable[[str], Awaitable[None]]


def bridge_mode_jsonl() -> bool:
    """Return True iff ``CLORD_BRIDGE_MODE`` is set to ``jsonl``.

    Defaults to False (the legacy skill-based reply path stays in charge).
    """
    return os.getenv("CLORD_BRIDGE_MODE", "skill").strip().lower() == "jsonl"


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
        poll_interval: float = 0.5,
    ) -> None:
        self.thread_id = thread_id
        self.project_dir = project_dir
        self._sink = sink
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
            "TranscriptMirror starting: thread=%d project_dir=%s",
            self.thread_id,
            self.project_dir,
        )
        try:
            async for event in tail_events(self.project_dir, poll_interval=self._poll_interval):
                rendered = render_event(event)
                if rendered is None:
                    continue
                body = _format_body(rendered)
                try:
                    await self._sink(body)
                except asyncio.CancelledError:
                    raise
                except Exception:
                    # Discord post failure must not kill the mirror — log and
                    # carry on so a transient HTTPException doesn't silence the
                    # whole session.
                    logger.warning(
                        "TranscriptMirror sink failed for thread=%d",
                        self.thread_id,
                        exc_info=True,
                    )
        except asyncio.CancelledError:
            pass
        finally:
            logger.info("TranscriptMirror stopped: thread=%d", self.thread_id)
