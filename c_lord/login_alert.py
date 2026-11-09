"""Tell the owner by DM that Claude Code's login has run out (#1309).

When Claude Code is logged out, every turn is refused with one line —
``Login expired · Please run /login`` — and #812 turns that into a ❌ embed in
the thread. That embed only reaches someone who opens the thread, and the cure
(``/login``) is on the host, which the owner is usually away from: on
2026-11-09 the login ran out at 05:31 and nobody logged back in until 09:22.

What happens now:

* **The first refused turn of an outage DMs the owner once**: which host (the
  DM starts with ``[<host>]`` so a second machine can be told apart — #899),
  which thread stopped (a link), and what to do next.
* **Every further thread that stops during the same outage edits that DM** to
  add its link. However many threads stop, the owner gets one DM.
* **The outage ends when a turn finishes without an error** — that turn could
  only have run logged in. The next refusal after that is a new outage and a
  new DM. A successful re-login from the DM (#1310) ends it too.
* **The outage is kept on disk** (``~/.c-lord/<instance>/login-alert.json``), so
  a restart in the middle of one does not send a second DM.
* **A DM that cannot be sent** (the owner does not accept DMs, Discord is down)
  changes nothing in the thread — the #812 embed is posted regardless — and is
  logged as a WARNING with the reason.

The recipient is ``DISCORD_OWNER_ID`` when it is set, else the Discord
application's owner (the #713 fallback, same as the #346 DM).

``CLORD_HOST_LABEL`` replaces the host name shown in the DM (default: the
machine's host name). See ``docs/specs/login-alert.md``.
"""

from __future__ import annotations

import asyncio
import contextlib
import json
import logging
import os
import socket
import time
from collections.abc import Callable
from pathlib import Path
from typing import Any

import discord

from .clord_home import clord_home, instance_name
from .discord_failures import DISCORD_CALL_FAILURES
from .discord_ui.authorization import get_fallback_owner_ids, read_application_owners
from .utils.logger import log_ctx

logger = logging.getLogger(__name__)

HOST_LABEL_ENV = "CLORD_HOST_LABEL"
_STATE_FILE = "login-alert.json"
#: How many stopped threads the DM lists before it says "+N more" — a DM is
#: capped at 2000 characters and a thread link is ~85.
MAX_LISTED_THREADS = 15


def host_label() -> str:
    """What the DM calls this machine: ``$CLORD_HOST_LABEL``, else the host name."""
    label = os.getenv(HOST_LABEL_ENV, "").strip()
    return label or socket.gethostname() or "?"


def default_state_path() -> Path:
    """``~/.c-lord/<instance>/login-alert.json`` — per instance, like its sessions."""
    return clord_home() / instance_name() / _STATE_FILE


def build_alert(label: str, thread_urls: list[str], *, extra: str | None = None) -> str:
    """The DM body for an outage that has stopped *thread_urls*."""
    shown = thread_urls[:MAX_LISTED_THREADS]
    lines = [
        f"[{label}] 🔑 **Claude Code のログインが切れました**",
        f"止まったスレッド（{len(thread_urls)}）:",
        *(f"• {url}" for url in shown),
    ]
    if len(thread_urls) > len(shown):
        lines.append(f"• …ほか {len(thread_urls) - len(shown)} 本")
    lines += [
        f"次の手: ホスト `{label}` で `claude auth login`（または `claude` を開いて "
        "`/login`）を実行してください。",
        "戻ったら、止まったスレッドでもう一度送ってください（自動では再送しません）。",
    ]
    if extra:
        lines += ["", extra]
    lines.append("-# 同じ切れの間に止まったスレッドは、この DM に書き足します（通知は 1 回だけ）。")
    return "\n".join(lines)


class LoginAlert:
    """One DM per login outage, edited as more threads stop (#1309)."""

    def __init__(
        self,
        *,
        state_path: Path | None = None,
        clock: Callable[[], float] = time.time,
    ) -> None:
        self._state_path = state_path
        self._clock = clock
        self._bot: Any = None
        self._lock = asyncio.Lock()
        self._state: dict[str, Any] | None = None
        # The DM message, once fetched or sent — editing needs the object.
        self._dm: Any = None
        # Whoever is interested in an outage's start / end (#1310's re-login).
        self._on_open: list[Callable[[], Any]] = []
        self._on_close: list[Callable[[], Any]] = []

    # -- wiring -------------------------------------------------------------

    def bind(self, bot: Any) -> None:
        """Give the alert the bot it sends DMs with (``setup_bridge``)."""
        self._bot = bot

    def on_open(self, callback: Callable[[], Any]) -> None:
        """Call *callback* (sync or async) when a new outage's DM has been sent."""
        self._on_open.append(callback)

    def on_close(self, callback: Callable[[], Any]) -> None:
        """Call *callback* (sync or async) when an outage ends."""
        self._on_close.append(callback)

    @property
    def path(self) -> Path:
        return self._state_path or default_state_path()

    # -- state on disk --------------------------------------------------------

    def _load(self) -> dict[str, Any]:
        if self._state is not None:
            return self._state
        state: dict[str, Any] = {"open": False}
        try:
            raw = json.loads(self.path.read_text(encoding="utf-8"))
            if isinstance(raw, dict):
                state = raw
        except FileNotFoundError:
            pass
        except (OSError, ValueError):
            logger.warning("could not read %s — starting with no outage (#1309)", self.path)
        state.setdefault("open", False)
        state.setdefault("threads", [])
        self._state = state
        return state

    def _save(self) -> None:
        state = self._load()
        try:
            self.path.parent.mkdir(parents=True, exist_ok=True)
            tmp = self.path.with_suffix(".tmp")
            tmp.write_text(json.dumps(state, ensure_ascii=False), encoding="utf-8")
            os.replace(tmp, self.path)
        except OSError:
            # Losing the file only costs a second DM after a restart.
            logger.warning("could not write %s (#1309)", self.path, exc_info=True)

    @property
    def is_open(self) -> bool:
        """Whether a login outage is in progress (a refusal seen, no success since)."""
        return bool(self._load().get("open"))

    @property
    def stopped_threads(self) -> list[dict[str, Any]]:
        """``[{"id": thread_id, "url": jump_url}, …]`` for the current outage."""
        return list(self._load().get("threads", []))

    # -- the two events -------------------------------------------------------

    async def report(self, thread: Any) -> None:
        """A turn in *thread* was refused because Claude Code is logged out. Never raises."""
        try:
            async with self._lock:
                await self._report(thread)
        except Exception:
            logger.exception("login alert failed (#1309) — thread %s", getattr(thread, "id", "?"))

    async def recovered(self) -> None:
        """A turn ran — Claude Code is logged in. Ends the outage, if any. Never raises."""
        try:
            async with self._lock:
                state = self._load()
                if not state.get("open"):
                    return
                threads = len(state.get("threads", []))
                self._state = {"open": False, "threads": []}
                self._dm = None
                self._save()
                logger.info(
                    "Claude Code login is back — outage over (%d thread(s) had stopped) (#1309)",
                    threads,
                )
            await self._fire(self._on_close)
        except Exception:
            logger.exception("login alert: could not close the outage (#1309)")

    async def set_extra(self, extra: str | None) -> None:
        """Replace the DM's trailing section (the re-login link — #1310). Never raises."""
        try:
            async with self._lock:
                state = self._load()
                if not state.get("open"):
                    return
                state["extra"] = extra
                self._save()
                await self._edit_dm(state)
        except Exception:
            logger.exception("login alert: could not update the DM (#1309)")

    # -- internals ------------------------------------------------------------

    async def _report(self, thread: Any) -> None:
        state = self._load()
        thread_id = getattr(thread, "id", None)
        url = str(getattr(thread, "jump_url", "") or thread_id)
        ctx = log_ctx(thread_id=thread_id)
        known = {t.get("id") for t in state["threads"]}
        added = thread_id not in known
        if added:
            state["threads"].append({"id": thread_id, "url": url})

        if state.get("open"):
            self._save()
            if added:
                await self._edit_dm(state)
            logger.info(
                "%s Claude Code login required — outage already reported, "
                "%d thread(s) stopped so far, no new DM (#1309)",
                ctx,
                len(state["threads"]),
            )
            return

        state["open"] = True
        state["since"] = self._clock()
        state["extra"] = None
        self._save()
        sent = await self._send_dm(state)
        logger.log(
            logging.INFO if sent else logging.WARNING,
            "%s Claude Code login required — %s (#1309)",
            ctx,
            sent or "owner DM NOT sent; the thread's own notice is all there is",
        )
        if sent:
            await self._fire(self._on_open)

    async def recipients(self) -> set[int]:
        """``DISCORD_OWNER_ID``, else the application owner (#713)."""
        bot = self._bot
        owner_id = getattr(bot, "owner_id", None)
        if isinstance(owner_id, int):
            return {owner_id}
        resolved = get_fallback_owner_ids()
        if resolved:
            return set(resolved)
        owner_ids, _ = await read_application_owners(bot)
        return set(owner_ids)

    async def _send_dm(self, state: dict[str, Any]) -> str | None:
        if self._bot is None:
            logger.warning("login alert: no bot bound, cannot DM the owner (#1309)")
            return None
        try:
            owners = await self.recipients()
        except Exception:
            logger.warning("login alert: could not read the owner (#1309)", exc_info=True)
            return None
        if not owners:
            logger.warning("login alert: no owner known to DM (#1309)")
            return None
        body = self._body(state)
        for owner_id in sorted(owners):
            try:
                user = self._bot.get_user(owner_id) or await self._bot.fetch_user(owner_id)
                msg = await user.send(
                    body,
                    allowed_mentions=discord.AllowedMentions.none(),
                    suppress_embeds=True,
                )
            except DISCORD_CALL_FAILURES as exc:
                logger.warning(
                    "login alert: could not DM owner %s (%s: %s) (#1309)",
                    owner_id,
                    type(exc).__name__,
                    exc,
                )
                continue
            self._dm = msg
            state["dm"] = {"user": owner_id, "channel": msg.channel.id, "message": msg.id}
            self._save()
            return f"owner DM sent to {owner_id}"
        return None

    def _body(self, state: dict[str, Any]) -> str:
        urls = [str(t.get("url")) for t in state.get("threads", [])]
        return build_alert(host_label(), urls, extra=state.get("extra"))

    async def _dm_message(self, state: dict[str, Any]) -> Any:
        if self._dm is not None:
            return self._dm
        ref = state.get("dm")
        if not ref or self._bot is None:
            return None
        # After a restart: find the DM again from the ids on disk.
        try:
            channel = self._bot.get_channel(ref["channel"])
            if channel is None:
                user = self._bot.get_user(ref["user"]) or await self._bot.fetch_user(ref["user"])
                channel = await user.create_dm()
            self._dm = await channel.fetch_message(ref["message"])
        except DISCORD_CALL_FAILURES as exc:
            logger.warning("login alert: could not find the DM again (%s) (#1309)", exc)
            return None
        return self._dm

    @property
    def dm_channel_id(self) -> int | None:
        """The DM channel of the current outage's alert, or ``None``."""
        ref = self._load().get("dm")
        return ref.get("channel") if ref else None

    async def _edit_dm(self, state: dict[str, Any]) -> None:
        msg = await self._dm_message(state)
        if msg is None:
            return
        with contextlib.suppress(*DISCORD_CALL_FAILURES):
            await msg.edit(
                content=self._body(state), allowed_mentions=discord.AllowedMentions.none()
            )

    async def _fire(self, callbacks: list[Callable[[], Any]]) -> None:
        for cb in callbacks:
            try:
                result = cb()
                if asyncio.iscoroutine(result):
                    await result
            except Exception:
                logger.exception("login alert: callback failed (#1309)")


#: The process's alert. ``EventProcessor`` reports to it; ``setup_bridge`` binds it.
login_alert = LoginAlert()
