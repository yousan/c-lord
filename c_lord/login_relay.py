"""Log Claude Code back in from the owner's DM (#1310).

#1309 tells the owner by DM that the login has run out. The cure used to be
"go to the host and run ``/login``" — which is exactly what the owner, away
from the host, cannot do. This module brings the login to the DM:

1. When an outage's DM goes out, ``claude auth login`` is started in a window
   of its own (``clord-login``) — never in a thread's window — inside the tmux
   session of the first thread that stopped, so it sees the same environment
   (the same ``CLAUDE_CONFIG_DIR``) as the claude that is logged out.
2. The sign-in link it prints is read off the pane (``capture-pane -J`` joins a
   link the terminal wrapped; a wrapped capture is joined here as well) and put
   into the same DM.
3. **The only thing typed into that window is a DM reply from the owner, and
   only while the window is waiting for one.** Anyone else's DM, and the owner's
   outside that window, is never typed anywhere.
4. Success or failure is answered in the DM. On success each stopped thread is
   told 「ログインが戻りました。もう一度送ってください」 — nothing is resent on
   anyone's behalf.
5. The link expires: after :data:`WAIT_SECONDS` the window is closed and the DM
   says so. A 🔄 button on the DM starts a fresh login at any time.

**The link and the code never leave the DM**: no thread, channel or log line
carries either. The log says that a code arrived and how long it was.

A stopped thread's claude comes back on its own once the login is renewed: on
2026-11-09 the claude refused at 09:17 in window ``@122`` answered the next
message at 09:23 in the same window after a ``/login`` elsewhere (journal of
``c-lord.service``), so nothing is restarted here.

See ``docs/specs/login-alert.md``.
"""

from __future__ import annotations

import asyncio
import logging
import os
import re
import shlex
import shutil
import time
from collections.abc import Callable
from typing import Any

import discord
from discord.ext import commands

from .discord_failures import DISCORD_CALL_FAILURES
from .login_alert import LoginAlert, host_label, login_alert
from .tmux import SENSITIVE_ENV_KEYS

logger = logging.getLogger(__name__)

LOGIN_WINDOW = "clord-login"
#: How long the window waits for the owner's code before it is closed.
WAIT_SECONDS = 15 * 60
#: How long ``claude auth login`` may take to print its link.
URL_TIMEOUT = 45.0
#: How long it may take to answer a code.
RESULT_TIMEOUT = 90.0
POLL_INTERVAL = 1.0
#: Printed by the window after ``claude auth login`` exits, with its exit code.
EXIT_MARKER = "__CLORD_LOGIN_EXIT__="
REISSUE_CUSTOM_ID = "clord:login-reissue"
ENV_VAR = "CLORD_LOGIN_RELAY"
_FALSE_VALUES = ("0", "false", "no", "off")

# What an authorization code looks like: one token, no spaces. Generous on
# purpose — the CLI is the judge; this only keeps chat out of the pane.
_CODE_RE = re.compile(r"^[A-Za-z0-9_\-#.~+/=]{8,1024}$")
_URL_START_RE = re.compile(r"https://\S+")
_URL_CONT_RE = re.compile(r"^[A-Za-z0-9%&=._~:/?#\-+]+$")
# OSC 8 hyperlinks (``ESC ] 8 ; ; url ESC \``) and other escapes, in case a
# capture carries them.
_ESCAPE_RE = re.compile(r"\x1b\]8;;[^\x1b\x07]*(?:\x1b\\|\x07)|\x1b\[[0-9;?]*[A-Za-z]")
_FAILED_RE = re.compile(r"Login failed:[^\n]*")
_SUCCESS_RE = re.compile(r"Login successful")


def relay_enabled() -> bool:
    """On by default; ``CLORD_LOGIN_RELAY=0`` keeps #1309's DM without the login."""
    return os.getenv(ENV_VAR, "").strip().lower() not in _FALSE_VALUES


def extract_login_url(pane: str) -> str | None:
    """The sign-in link ``claude auth login`` printed on *pane*, joined if wrapped.

    ``capture-pane -J`` already joins a wrapped line; a capture without it has
    the link cut at the pane width, so continuation lines (nothing but URL
    characters) are appended until the first line that is not one.
    """
    lines = _ESCAPE_RE.sub("", pane).splitlines()
    for i, line in enumerate(lines):
        if "visit:" not in line:
            continue
        m = _URL_START_RE.search(line)
        if m is None:
            continue
        url = m.group(0)
        for nxt in lines[i + 1 :]:
            piece = nxt.strip()
            if not piece or not _URL_CONT_RE.match(piece):
                break
            url += piece
        return url
    return None


def login_outcome(pane: str) -> tuple[bool, str] | None:
    """``(True, line)`` / ``(False, line)`` once ``auth login`` answered, else None."""
    text = _ESCAPE_RE.sub("", pane)
    failed = _FAILED_RE.findall(text)
    if failed:
        return False, failed[-1].strip()
    if _SUCCESS_RE.search(text):
        return True, "Login successful"
    m = re.search(re.escape(EXIT_MARKER) + r"(\d+)", text)
    if m is not None:
        code = int(m.group(1))
        return (code == 0), f"claude auth login exited with {code}"
    return None


def is_code_shaped(text: str) -> bool:
    """Whether *text* could be an authorization code (one token, no spaces)."""
    return bool(_CODE_RE.match(text.strip()))


class TmuxLoginWindow:
    """The tmux side: one ``clord-login`` window running ``claude auth login``."""

    def __init__(self, claude_command: str = "claude") -> None:
        self._claude = shutil.which(claude_command) or claude_command

    async def _tmux(self, *args: str) -> tuple[int, str]:
        proc = await asyncio.create_subprocess_exec(
            "tmux",
            *args,
            stdout=asyncio.subprocess.PIPE,
            stderr=asyncio.subprocess.PIPE,
        )
        out, err = await proc.communicate()
        if proc.returncode != 0:
            logger.debug("tmux %s failed: %s", args[0], err.decode(errors="replace").strip())
        return proc.returncode or 0, out.decode(errors="replace")

    def command(self) -> str:
        """The shell line the window runs. Discord secrets are unset, no browser."""
        parts = ["env", "-u", "CLAUDECODE"]
        for key in SENSITIVE_ENV_KEYS:
            parts += ["-u", key]
        # Opening a browser on the host helps nobody: the owner is not there.
        parts += ["BROWSER=true", self._claude, "auth", "login"]
        line = " ".join(shlex.quote(p) for p in parts)
        return f'{line}; echo "{EXIT_MARKER}$?"; sleep 60'

    async def open(self, session: str) -> str | None:
        """Start the login in a new window of *session*; returns its window id."""
        await self.close_named(session)
        rc, out = await self._tmux(
            "new-window",
            "-d",
            "-P",
            "-F",
            "#{window_id}",
            "-t",
            f"={session}:",
            "-n",
            LOGIN_WINDOW,
            self.command(),
        )
        window = out.strip()
        if rc != 0 or not window.startswith("@"):
            return None
        # Wide enough that the link mostly fits on one line.
        await self._tmux("resize-window", "-t", window, "-x", "250", "-y", "50")
        return window

    async def capture(self, window: str) -> str:
        _, out = await self._tmux("capture-pane", "-p", "-J", "-t", window)
        return out

    async def type_code(self, window: str, code: str) -> bool:
        rc, _ = await self._tmux("send-keys", "-t", window, "-l", "--", code)
        if rc != 0:
            return False
        rc, _ = await self._tmux("send-keys", "-t", window, "Enter")
        return rc == 0

    async def close(self, window: str) -> None:
        await self._tmux("kill-window", "-t", window)

    async def close_named(self, session: str) -> None:
        """Close any ``clord-login`` window left in *session* (e.g. by a restart)."""
        rc, out = await self._tmux(
            "list-windows", "-t", f"={session}", "-F", "#{window_id} #{window_name}"
        )
        if rc != 0:
            return
        for line in out.splitlines():
            wid, _, name = line.partition(" ")
            if name == LOGIN_WINDOW and wid.startswith("@"):
                await self.close(wid)


class ReissueView(discord.ui.View):
    """The 🔄 button on the outage DM. Persistent: it survives a restart."""

    def __init__(self, relay: LoginRelay) -> None:
        super().__init__(timeout=None)
        self._relay = relay

    @discord.ui.button(
        label="リンクを出し直す", emoji="🔄", style=discord.ButtonStyle.secondary,
        custom_id=REISSUE_CUSTOM_ID,
    )  # fmt: skip
    async def reissue(self, interaction: discord.Interaction, button: discord.ui.Button) -> None:
        if not await self._relay.is_owner(interaction.user.id):
            await interaction.response.send_message(
                "⛔ この操作は owner だけができます。", ephemeral=True
            )
            return
        await interaction.response.defer()
        await self._relay.start(reason="reissue")


class LoginRelay:
    """Drives one re-login at a time for the current outage (#1310)."""

    def __init__(
        self,
        bot: Any,
        alert: LoginAlert = login_alert,
        *,
        window: TmuxLoginWindow | None = None,
        clock: Callable[[], float] = time.time,
        wait_seconds: float = WAIT_SECONDS,
        url_timeout: float = URL_TIMEOUT,
        result_timeout: float = RESULT_TIMEOUT,
        poll_interval: float = POLL_INTERVAL,
    ) -> None:
        self._bot = bot
        self._alert = alert
        self._window_driver = window or TmuxLoginWindow(
            getattr(bot, "claude_command", None) or "claude"
        )
        self._clock = clock
        self._wait_seconds = wait_seconds
        self._url_timeout = url_timeout
        self._result_timeout = result_timeout
        self._poll = poll_interval
        self._lock = asyncio.Lock()
        self._window: str | None = None
        self._deadline: float | None = None
        self._expiry: asyncio.Task[None] | None = None
        self.view = ReissueView(self)

    # -- who may answer ---------------------------------------------------

    async def is_owner(self, user_id: int) -> bool:
        try:
            return user_id in await self._alert.recipients()
        except Exception:
            logger.warning("login relay: could not read the owner (#1310)", exc_info=True)
            return False

    @property
    def waiting(self) -> bool:
        """Whether a login window is waiting for the owner's code right now."""
        return (
            self._window is not None
            and self._deadline is not None
            and self._clock() < self._deadline
        )

    # -- starting -----------------------------------------------------------

    def _session(self) -> str | None:
        for t in self._alert.stopped_threads:
            if isinstance(t.get("session"), str):
                return t["session"]
        manager = getattr(self._bot, "tmux_manager", None)
        name = getattr(manager, "session_name", None)
        return name if isinstance(name, str) else None

    async def start(self, *, reason: str = "outage") -> None:
        """Open a login window and put its link in the DM. Never raises."""
        try:
            async with self._lock:
                await self._start(reason)
        except Exception:
            logger.exception("login relay: could not start a login (#1310)")
            await self._alert.set_extra(
                "⚠️ DM からのログインを始められませんでした（ログを見てください）。"
                "🔄 で、もう一度試せます。",
                view=self.view,
            )

    async def _start(self, reason: str) -> None:
        if not self._alert.is_open:
            logger.info("login relay: no outage open — not starting a login (%s)", reason)
            return
        await self._close_window()
        session = self._session()
        if session is None:
            logger.warning("login relay: no tmux session to open the login window in (#1310)")
            await self._alert.set_extra(
                "⚠️ ログイン用の窓を開ける tmux セッションが見つかりませんでした。", view=self.view
            )
            return
        window = await self._window_driver.open(session)
        if window is None:
            logger.warning("login relay: could not open a login window in %s (#1310)", session)
            await self._alert.set_extra(
                "⚠️ ログイン用の窓を開けませんでした。🔄 で、もう一度試せます。", view=self.view
            )
            return
        self._window = window
        url = await self._wait_for_url(window)
        if url is None:
            logger.warning(
                "login relay: claude auth login printed no link in %.0fs (window %s) (#1310)",
                self._url_timeout,
                window,
            )
            await self._close_window()
            await self._alert.set_extra(
                "⚠️ ログイン用のリンクを取り出せませんでした。🔄 で、もう一度試せます。",
                view=self.view,
            )
            return
        self._deadline = self._clock() + self._wait_seconds
        until = time.strftime("%H:%M", time.localtime(self._deadline))
        logger.info(
            "login relay: login window %s in session %s is waiting for the owner's code "
            "until %s (%s) (#1310)",
            window,
            session,
            until,
            reason,
        )
        await self._alert.set_extra(
            "\n".join(
                [
                    "**DM からログインし直す**",
                    f"1. このリンクを開いてログイン: <{url}>",
                    "2. 表示されたコードを、この DM にそのまま返信してください"
                    f"（{until} まで受け付けます）",
                    "-# リンクが古くなったら 🔄 で出し直せます。",
                ]
            ),
            view=self.view,
        )
        self._arm_expiry()

    async def _wait_for_url(self, window: str) -> str | None:
        deadline = time.monotonic() + self._url_timeout
        while True:
            url = extract_login_url(await self._window_driver.capture(window))
            if url is not None or time.monotonic() >= deadline:
                return url
            await asyncio.sleep(self._poll)

    def _arm_expiry(self) -> None:
        if self._expiry is not None:
            self._expiry.cancel()
        self._expiry = asyncio.create_task(self._expire_later(self._wait_seconds))

    async def _expire_later(self, delay: float) -> None:
        await asyncio.sleep(delay)
        async with self._lock:
            if self._window is None:
                return
            logger.info("login relay: no code within %.0fs — login window closed (#1310)", delay)
            await self._close_window()
        await self._alert.set_extra(
            "⌛ リンクの受付時間が過ぎたので、ログイン用の窓を閉じました。"
            "🔄 で新しいリンクを出せます。",
            view=self.view,
        )

    async def _close_window(self) -> None:
        window, self._window, self._deadline = self._window, None, None
        if window is not None:
            await self._window_driver.close(window)

    # -- the owner's reply --------------------------------------------------

    async def handle_dm(self, message: Any) -> bool:
        """Take an owner's DM reply as the code. ``True`` when it was handled.

        The code itself is never logged; only that one arrived and its length.
        """
        if not isinstance(message.channel, discord.DMChannel):
            return False
        author = message.author
        if getattr(author, "bot", False) or not await self.is_owner(author.id):
            return False
        text = (message.content or "").strip()
        if not self.waiting:
            if self._alert.is_open:
                await self._reply(
                    message, "いまはコードを待っていません。🔄 を押すと新しいリンクを出します。"
                )
                return True
            return False
        if not is_code_shaped(text):
            await self._reply(
                message,
                "コードの形ではありません。ブラウザに表示されたコードだけを、そのまま貼ってください。",
            )
            return True
        async with self._lock:
            window = self._window
            if window is None or not self.waiting:
                await self._reply(message, "いまはコードを待っていません。")
                return True
            logger.info("login relay: code received from the owner (%d chars) (#1310)", len(text))
            if not await self._window_driver.type_code(window, text):
                await self._reply(message, "⚠️ コードをログイン用の窓に送れませんでした。")
                return True
            await self._reply(message, "⏳ コードを送りました。確かめています…")
            outcome = await self._wait_for_outcome(window)
            await self._close_window()
            if self._expiry is not None:
                self._expiry.cancel()
                self._expiry = None
        await self._finish(message, outcome)
        return True

    async def _wait_for_outcome(self, window: str) -> tuple[bool, str] | None:
        deadline = time.monotonic() + self._result_timeout
        while True:
            outcome = login_outcome(await self._window_driver.capture(window))
            if outcome is not None or time.monotonic() >= deadline:
                return outcome
            await asyncio.sleep(self._poll)

    async def _finish(self, message: Any, outcome: tuple[bool, str] | None) -> None:
        label = host_label()
        if outcome is None:
            logger.warning("login relay: no answer from claude auth login (#1310)")
            await self._reply(message, "⚠️ ログインの結果が分かりませんでした。🔄 でやり直せます。")
            await self._alert.set_extra(
                "⚠️ 前回のログインは結果が分からないまま閉じました。🔄 で新しいリンクを出せます。",
                view=self.view,
            )
            return
        ok, line = outcome
        if not ok:
            logger.info("login relay: login failed — %s (#1310)", line)
            await self._reply(message, f"❌ [{label}] ログインできませんでした: {line}")
            await self._alert.set_extra(
                "❌ コードが通りませんでした。🔄 で新しいリンクを出して、やり直してください。",
                view=self.view,
            )
            return
        threads = self._alert.stopped_threads
        logger.info(
            "login relay: Claude Code logged back in from the DM — telling %d thread(s) (#1310)",
            len(threads),
        )
        await self._reply(
            message,
            f"✅ [{label}] ログインが戻りました。"
            "止まっていたスレッドには「もう一度送って」と出しました。",
        )
        at = time.strftime("%H:%M", time.localtime(self._clock()))
        await self._alert.recovered(f"✅ DM からログインし直しました（{at}）。")
        for t in threads:
            await self._tell_thread(t.get("id"))

    async def _tell_thread(self, thread_id: Any) -> None:
        if not isinstance(thread_id, int):
            return
        try:
            channel = self._bot.get_channel(thread_id) or await self._bot.fetch_channel(thread_id)
            await channel.send(
                "🔑 Claude Code のログインが戻りました。"
                "止まっていたメッセージは自動では送り直しません。もう一度送ってください。"
            )
        except DISCORD_CALL_FAILURES as exc:
            logger.warning(
                "login relay: could not tell thread %s that the login is back (%s) (#1310)",
                thread_id,
                exc,
            )

    async def _reply(self, message: Any, text: str) -> None:
        try:
            await message.channel.send(text, allowed_mentions=discord.AllowedMentions.none())
        except DISCORD_CALL_FAILURES as exc:
            logger.warning("login relay: could not answer in the DM (%s) (#1310)", exc)

    async def on_outage_closed(self) -> None:
        """The outage ended some other way (a turn ran): drop the waiting login."""
        async with self._lock:
            if self._window is not None:
                logger.info("login relay: outage over — closing the login window (#1310)")
            await self._close_window()
            if self._expiry is not None:
                self._expiry.cancel()
                self._expiry = None


class LoginRelayCog(commands.Cog):
    """Wires :class:`LoginRelay` to the outage DM and to the owner's DM replies."""

    def __init__(self, bot: commands.Bot, relay: LoginRelay | None = None) -> None:
        self.bot = bot
        self.relay = relay or LoginRelay(bot)

    async def cog_load(self) -> None:
        self.bot.add_view(self.relay.view)
        login_alert.on_open(self._on_open)
        login_alert.on_close(self.relay.on_outage_closed)

    def _on_open(self) -> None:
        # Not awaited by the alert: printing the link takes seconds, and the
        # turn that reported the outage must not wait for it.
        asyncio.create_task(self.relay.start())

    @commands.Cog.listener()
    async def on_message(self, message: discord.Message) -> None:
        if message.guild is not None:
            return
        try:
            await self.relay.handle_dm(message)
        except Exception:
            logger.exception("login relay: DM handling failed (#1310)")
