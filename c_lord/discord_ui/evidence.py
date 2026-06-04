"""Auto-evidence: render the current Discord thread to a PNG on completion (#243).

The c-lord DoD wants *Discord-side* evidence (the result a user actually sees)
for PRs/Issues, but a server-side AI has no GUI to screenshot the Discord
client — and automating the real client is a self-bot (forbidden by Discord's
ToS). So when a Claude turn finishes, c-lord self-renders the thread from data
it already holds in-process (``channel.history``) via
:mod:`c_lord.discord_ui.conversation_renderer`, saves the PNG under
``<working_dir>/.evidence/`` for the AI to attach to a PR, and (by default)
posts it back to the thread.

Everything is env-gated so consumers get it by updating the package alone
(zero-config) yet can opt out without code changes:

* ``CLORD_AUTO_SCREENSHOT=0``       — disable the feature entirely
* ``CLORD_AUTO_SCREENSHOT_POST=0``  — keep saving the file, stop posting it
* ``CLORD_AUTO_SCREENSHOT_ENGINE``  — ``pillow`` (default) / ``html`` / ``auto``
* ``CLORD_AUTO_SCREENSHOT_LIMIT``   — how many recent messages to render
* ``CLORD_AUTO_SCREENSHOT_DEBOUNCE``— min seconds between captures per thread

Rendering can take a moment and depends on the optional ``c-lord[table]`` extra
(Pillow); this module never raises on failure — a screenshot problem must not
break the Claude run.
"""

from __future__ import annotations

import asyncio
import contextlib
import logging
import os
import time
from datetime import datetime
from io import BytesIO
from pathlib import Path

from .conversation_renderer import fetch_conversation, render_conversation

logger = logging.getLogger(__name__)

_ENV_ENABLE = "CLORD_AUTO_SCREENSHOT"
_ENV_POST = "CLORD_AUTO_SCREENSHOT_POST"
_ENV_ENGINE = "CLORD_AUTO_SCREENSHOT_ENGINE"
_ENV_LIMIT = "CLORD_AUTO_SCREENSHOT_LIMIT"
_ENV_DEBOUNCE = "CLORD_AUTO_SCREENSHOT_DEBOUNCE"

_DEFAULT_LIMIT = 25
_DEFAULT_DEBOUNCE_S = 12.0

# thread_id → monotonic timestamp of the last capture (debounce state).
# Bounded so a long-lived bot that sees many threads can't grow it without end.
_last_capture: dict[int, float] = {}
_MAX_TRACKED = 4096


def _truthy(val: str | None, *, default: bool) -> bool:
    """Interpret an env flag; absent → *default*, ``0/false/no/off`` → False."""
    if val is None:
        return default
    return val.strip().lower() not in ("0", "false", "no", "off", "")


def auto_screenshot_enabled() -> bool:
    """True unless ``CLORD_AUTO_SCREENSHOT`` is explicitly disabled."""
    return _truthy(os.getenv(_ENV_ENABLE), default=True)


def _post_enabled() -> bool:
    return _truthy(os.getenv(_ENV_POST), default=True)


def _engine() -> str:
    return os.getenv(_ENV_ENGINE) or "pillow"


def _limit() -> int:
    raw = os.getenv(_ENV_LIMIT)
    if not raw:
        return _DEFAULT_LIMIT
    try:
        return max(1, int(raw))
    except ValueError:
        return _DEFAULT_LIMIT


def _debounce_s() -> float:
    raw = os.getenv(_ENV_DEBOUNCE)
    if not raw:
        return _DEFAULT_DEBOUNCE_S
    try:
        return max(0.0, float(raw))
    except ValueError:
        return _DEFAULT_DEBOUNCE_S


def should_capture(thread_id: int, *, now: float | None = None) -> bool:
    """Debounce: True at most once per ``CLORD_AUTO_SCREENSHOT_DEBOUNCE`` window."""
    ts = time.monotonic() if now is None else now
    last = _last_capture.get(thread_id)
    if last is not None and ts - last < _debounce_s():
        return False
    # Bound the debounce map: when it gets large, drop the oldest half so a bot
    # that has touched thousands of threads doesn't leak memory.
    if len(_last_capture) >= _MAX_TRACKED and thread_id not in _last_capture:
        oldest = sorted(_last_capture, key=lambda tid: _last_capture[tid])[: _MAX_TRACKED // 2]
        for tid in oldest:
            del _last_capture[tid]
    _last_capture[thread_id] = ts
    return True


def _ensure_git_excluded(repo_root: Path) -> None:
    """Keep ``.evidence/`` out of git status via the clone's local-only exclude.

    Writes to ``.git/info/exclude`` (never committed, never touches a tracked
    file) so the generated PNGs don't pollute the session clone's working tree
    or the AI's own ``git diff`` self-review. Best-effort; silent on failure.
    """
    try:
        exclude = repo_root / ".git" / "info" / "exclude"
        if not exclude.parent.is_dir():
            return  # not a git checkout (or no .git/info) — nothing to do
        existing = exclude.read_text(encoding="utf-8") if exclude.exists() else ""
        if ".evidence/" in existing:
            return
        with exclude.open("a", encoding="utf-8") as fh:
            if existing and not existing.endswith("\n"):
                fh.write("\n")
            fh.write(".evidence/\n")
    except OSError:
        pass


def _save(png: bytes, working_dir: str | None) -> str | None:
    """Save *png* under ``<working_dir>/.evidence/`` and return the path."""
    if not working_dir:
        return None
    try:
        base = Path(working_dir)
        out_dir = base / ".evidence"
        out_dir.mkdir(parents=True, exist_ok=True)
        _ensure_git_excluded(base)
        stamp = datetime.now().strftime("%Y%m%d-%H%M%S")
        path = out_dir / f"discord-{stamp}.png"
        path.write_bytes(png)
        return str(path)
    except OSError:
        logger.warning("auto-screenshot: could not save evidence file", exc_info=True)
        return None


async def capture_evidence(
    thread: object, working_dir: str | None = None, *, title: str | None = None
) -> str | None:
    """Render *thread* to a PNG, save it, and (by default) post it back.

    Returns the saved file path, or ``None`` when nothing was saved (no
    ``working_dir``, empty thread, renderer unavailable, or any error). Never
    raises — a screenshot failure must not break the Claude run.
    """
    import discord

    try:
        messages = await fetch_conversation(thread, limit=_limit())
        if not messages:
            return None

        png = await asyncio.to_thread(render_conversation, messages, engine=_engine(), title=title)
        if png is None:
            logger.debug(
                "auto-screenshot: renderer unavailable (install c-lord[table] for Pillow, "
                "or a headless browser for engine=html)"
            )
            return None

        saved = _save(png, working_dir)

        if _post_enabled():
            filename = os.path.basename(saved) if saved else "discord-evidence.png"
            with contextlib.suppress(Exception):
                await thread.send(file=discord.File(BytesIO(png), filename=filename))  # type: ignore[attr-defined]

        return saved
    except Exception:  # noqa: BLE001 — evidence must never break the run
        logger.warning("auto-screenshot failed", exc_info=True)
        return None
