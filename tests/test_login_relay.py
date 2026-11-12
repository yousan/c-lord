"""Logging Claude Code back in from the owner's DM (#1310).

``docs/specs/login-alert.md`` (「DM からログインし直す」) is the "should" here.
The fixtures under ``tests/fixtures/login/`` are real ``claude auth login``
captures (CLI 2.1.319) with the one-time values replaced.
"""

from __future__ import annotations

import logging
from pathlib import Path
from unittest.mock import AsyncMock, MagicMock

import discord
import pytest

from c_lord.login_alert import LoginAlert
from c_lord.login_relay import (
    EXIT_MARKER,
    LOGIN_WINDOW,
    LoginRelay,
    TmuxLoginWindow,
    extract_login_url,
    is_code_shaped,
    login_outcome,
)

FIXTURES = Path(__file__).parent / "fixtures" / "login"
OWNER = 4242
STRANGER = 5151
CODE = "AbCdEf0123456789_-xyz#FAKEstateFAKEstate"
FAKE_URL_END = "state=FAKEstateFAKEstateFAKEstateFAKEstateFAKEsta"
FAILED = (
    "Paste code here if prompted > Login failed: That code wasn't accepted. It may be "
    "incomplete, expired, already used, or from an earlier sign-in attempt."
)


# ── reading the pane ─────────────────────────────────────────────────────────


def test_link_from_a_joined_capture() -> None:
    url = extract_login_url((FIXTURES / "auth_login_joined.txt").read_text())
    assert url is not None
    assert url.startswith("https://claude.com/cai/oauth/authorize?code=true&client_id=")
    assert url.endswith(FAKE_URL_END)


def test_link_wrapped_at_the_pane_width_is_joined_back() -> None:
    """AC1: a capture without -J has the link cut every 80 columns."""
    wrapped = (FIXTURES / "auth_login_wrapped_80.txt").read_text()
    joined = extract_login_url((FIXTURES / "auth_login_joined.txt").read_text())
    assert extract_login_url(wrapped) == joined
    assert "\n" not in (joined or "")


def test_osc8_hyperlink_escapes_are_dropped() -> None:
    url = "https://claude.com/cai/oauth/authorize?code=true&state=x"
    osc = f"\x1b]8;;{url}\x1b\\{url}\x1b]8;;\x1b\\"
    pane = f"If the browser didn't open, visit: {osc}\nPaste code here if prompted >"
    assert extract_login_url(pane) == url


def test_no_link_yet() -> None:
    assert extract_login_url("Opening browser to sign in…\n") is None


def test_outcome() -> None:
    assert login_outcome("Paste code here if prompted >") is None
    ok, line = login_outcome(FAILED) or (True, "")
    assert not ok and line.startswith("Login failed: That code wasn't accepted")
    assert login_outcome("Login successful.\n") == (True, "Login successful")
    assert login_outcome(f"{EXIT_MARKER}0") == (True, "claude auth login exited with 0")
    assert login_outcome(f"{EXIT_MARKER}1") == (False, "claude auth login exited with 1")


def test_code_shape() -> None:
    assert is_code_shaped(CODE)
    assert not is_code_shaped("ありがとう、いま開いた")
    assert not is_code_shaped("two words")
    assert not is_code_shaped("short")


def test_window_command_unsets_secrets_and_opens_no_browser() -> None:
    cmd = TmuxLoginWindow("claude").command()
    assert "-u DISCORD_BOT_TOKEN" in cmd
    assert "BROWSER=true" in cmd
    assert "auth login" in cmd
    assert EXIT_MARKER in cmd


# ── the relay ────────────────────────────────────────────────────────────────


class FakeWindow:
    """Stands in for tmux: what the pane shows, and what was typed into it."""

    def __init__(self, pane: str = "") -> None:
        self.pane = pane or (FIXTURES / "auth_login_joined.txt").read_text()
        self.opened: list[str] = []
        self.typed: list[str] = []
        self.closed: list[str] = []
        self.after_code = "Login successful.\n"

    async def open(self, session: str) -> str | None:
        self.opened.append(session)
        return f"@{len(self.opened)}"

    async def capture(self, window: str) -> str:
        return self.pane

    async def type_code(self, window: str, code: str) -> bool:
        self.typed.append(code)
        self.pane += self.after_code
        return True

    async def close(self, window: str) -> None:
        self.closed.append(window)


def _bot() -> tuple[MagicMock, MagicMock]:
    dm = MagicMock()
    dm.id = 777
    dm.channel.id = 888
    dm.edit = AsyncMock()
    owner = MagicMock()
    owner.send = AsyncMock(return_value=dm)
    bot = MagicMock()
    bot.owner_id = OWNER
    bot.get_user = MagicMock(return_value=owner)
    thread_channel = MagicMock()
    thread_channel.send = AsyncMock()
    bot.get_channel = MagicMock(return_value=thread_channel)
    return bot, dm


def _thread(thread_id: int) -> MagicMock:
    t = MagicMock(spec=discord.Thread)
    t.id = thread_id
    t.jump_url = f"https://discord.com/channels/1/{thread_id}"
    return t


def _dm_message(author_id: int, content: str) -> MagicMock:
    msg = MagicMock()
    msg.channel = MagicMock(spec=discord.DMChannel)
    msg.channel.send = AsyncMock()
    msg.author.id = author_id
    msg.author.bot = False
    msg.content = content
    return msg


async def _outage(tmp_path, window: FakeWindow, **kw) -> tuple[LoginRelay, MagicMock, MagicMock]:
    bot, dm = _bot()
    alert = LoginAlert(state_path=tmp_path / "login-alert.json")
    alert.bind(bot)
    await alert.report(_thread(1), session="c-lord")
    relay = LoginRelay(bot, alert, window=window, poll_interval=0, **kw)  # type: ignore[arg-type]
    await relay.start()
    return relay, bot, dm


def _last_dm_text(dm: MagicMock) -> str:
    return dm.edit.await_args.kwargs["content"]


def _replies(msg: MagicMock) -> str:
    return "\n".join(c.args[0] for c in msg.channel.send.await_args_list)


@pytest.mark.asyncio
async def test_the_link_goes_into_the_same_dm(tmp_path) -> None:
    """AC1: the window opens in the stopped thread's session; the link is in the DM."""
    window = FakeWindow()
    relay, bot, dm = await _outage(tmp_path, window)

    assert window.opened == ["c-lord"]
    text = _last_dm_text(dm)
    assert "https://claude.com/cai/oauth/authorize" in text and FAKE_URL_END in text
    assert dm.edit.await_args.kwargs["view"] is relay.view
    assert bot.get_user.return_value.send.await_count == 1  # still one DM
    assert relay.waiting


@pytest.mark.asyncio
async def test_only_the_owner_while_waiting_reaches_the_window(tmp_path) -> None:
    """AC2: a stranger's DM, and the owner's before/after the wait, are not typed."""
    window = FakeWindow()
    clock = [1000.0]
    relay, _, _ = await _outage(tmp_path, window, clock=lambda: clock[0], wait_seconds=60)

    stranger = _dm_message(STRANGER, CODE)
    assert await relay.handle_dm(stranger) is False
    stranger.channel.send.assert_not_awaited()

    chat = _dm_message(OWNER, "ありがとう、いま開いた")
    assert await relay.handle_dm(chat) is True
    assert "コードの形ではありません" in _replies(chat)

    clock[0] += 61  # the wait is over
    late = _dm_message(OWNER, CODE)
    await relay.handle_dm(late)
    assert "待っていません" in _replies(late)
    assert window.typed == []


@pytest.mark.asyncio
async def test_a_good_code_logs_in_and_tells_the_thread(tmp_path, caplog) -> None:
    """AC3 + AC5: typed into the window, ✅ in the DM, the thread is told; no resend."""
    window = FakeWindow()
    relay, bot, dm = await _outage(tmp_path, window)
    msg = _dm_message(OWNER, CODE)
    with caplog.at_level(logging.DEBUG):
        assert await relay.handle_dm(msg) is True

    assert window.typed == [CODE]
    assert "✅" in _replies(msg)
    assert window.closed == ["@1"]
    told = bot.get_channel.return_value.send.await_args.args[0]
    assert "ログインが戻りました" in told and "もう一度送ってください" in told
    assert "✅ DM からログインし直しました" in _last_dm_text(dm)
    assert dm.edit.await_args.kwargs["view"] is None
    assert not relay._alert.is_open
    # The code never reaches a log line; the link neither.
    assert CODE not in caplog.text and "oauth/authorize" not in caplog.text
    assert "code received from the owner (40 chars)" in caplog.text


@pytest.mark.asyncio
async def test_a_bad_code_says_so_and_offers_a_new_link(tmp_path) -> None:
    """AC3: failure is answered in the DM; the outage stays open."""
    window = FakeWindow()
    window.after_code = FAILED + "\n"
    relay, _, dm = await _outage(tmp_path, window)
    msg = _dm_message(OWNER, CODE)
    await relay.handle_dm(msg)

    assert "❌" in _replies(msg) and "That code wasn't accepted" in _replies(msg)
    assert "🔄" in _last_dm_text(dm)
    assert relay._alert.is_open
    assert not relay.waiting


@pytest.mark.asyncio
async def test_reissue_opens_a_fresh_window(tmp_path) -> None:
    """AC4: 🔄 closes the old window and puts a new link in the DM."""
    window = FakeWindow()
    relay, _, _ = await _outage(tmp_path, window)
    await relay.start(reason="reissue")
    assert window.opened == ["c-lord", "c-lord"]
    assert window.closed == ["@1"]
    assert relay.waiting


@pytest.mark.asyncio
async def test_the_wait_expires_and_closes_the_window(tmp_path) -> None:
    """AC4: no code within the wait → the window is closed, the DM says so."""
    window = FakeWindow()
    relay, _, dm = await _outage(tmp_path, window, wait_seconds=0.01)
    assert relay._expiry is not None
    await relay._expiry
    assert window.closed == ["@1"]
    assert "⌛" in _last_dm_text(dm)
    assert not relay.waiting


@pytest.mark.asyncio
async def test_a_turn_that_runs_ends_the_wait(tmp_path) -> None:
    """Someone logged in on the host: the outage closes and the login window goes."""
    window = FakeWindow()
    relay, _, dm = await _outage(tmp_path, window)
    relay._alert.on_close(relay.on_outage_closed)
    await relay._alert.recovered()
    assert window.closed == ["@1"]
    assert not relay.waiting
    assert "✅ ログインが戻りました" in _last_dm_text(dm)


@pytest.mark.asyncio
async def test_no_link_means_no_wait(tmp_path) -> None:
    window = FakeWindow(pane="Opening browser to sign in…\n")
    relay, _, dm = await _outage(tmp_path, window, url_timeout=0)
    assert not relay.waiting
    assert window.closed == ["@1"]
    assert "取り出せませんでした" in _last_dm_text(dm)


@pytest.mark.asyncio
async def test_nothing_starts_without_an_outage(tmp_path) -> None:
    bot, _ = _bot()
    alert = LoginAlert(state_path=tmp_path / "login-alert.json")
    alert.bind(bot)
    window = FakeWindow()
    relay = LoginRelay(bot, alert, window=window, poll_interval=0)  # type: ignore[arg-type]
    await relay.start()
    assert window.opened == []
    assert await relay.handle_dm(_dm_message(OWNER, CODE)) is False


def test_window_name() -> None:
    assert LOGIN_WINDOW == "clord-login"
