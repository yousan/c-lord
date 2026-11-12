"""The owner hears about a logged-out Claude Code by DM — once per outage (#1309).

``docs/specs/login-alert.md`` is the "should" for every test here.
"""

from __future__ import annotations

# <<REDACTED 5 lines>>
from c_lord.claude.tmux_runner import LOGIN_REQUIRED_ERROR_PREFIX
from c_lord.claude.types import MessageType, StreamEvent
from c_lord.cogs.event_processor import EventProcessor
from c_lord.cogs.run_config import RunConfig
from c_lord.login_alert import LoginAlert, build_alert, login_alert

OWNER = 4242


def _thread(thread_id: int) -> MagicMock:
    thread = MagicMock(spec=discord.Thread)
    thread.id = thread_id
    thread.jump_url = f"https://discord.com/channels/1/{thread_id}"
    thread.send = AsyncMock(return_value=MagicMock(spec=discord.Message))
    return thread


def _bot(*, fail: Exception | None = None) -> tuple[MagicMock, MagicMock]:
    """A bot whose owner is ``OWNER``; returns ``(bot, the DM message)``."""
    dm = MagicMock()
    dm.id = 777
    dm.channel.id = 888
    dm.edit = AsyncMock()
    user = MagicMock()
    user.send = AsyncMock(side_effect=fail) if fail else AsyncMock(return_value=dm)
    bot = MagicMock()
    bot.owner_id = OWNER
    bot.get_user = MagicMock(return_value=user)
    bot.fetch_user = AsyncMock(return_value=user)
    return bot, dm


def _user(bot: MagicMock) -> MagicMock:
    return bot.get_user.return_value


def _alert(tmp_path, bot: MagicMock) -> LoginAlert:
    alert = LoginAlert(state_path=tmp_path / "login-alert.json")
    alert.bind(bot)
    return alert


@pytest.mark.asyncio
async def test_first_refusal_dms_the_owner_with_host_thread_and_next_step(
    tmp_path, monkeypatch
) -> None:
    """AC1: one DM — the host, the stopped thread's link, what to do."""
    monkeypatch.setenv("CLORD_HOST_LABEL", "tachikoma")
    bot, _ = _bot()
    alert = _alert(tmp_path, bot)
    await alert.report(_thread(1))

    bot.get_user.assert_called_with(OWNER)
    _user(bot).send.assert_awaited_once()
    body = _user(bot).send.await_args.args[0]
    assert body.startswith("[tachikoma] 🔑")
    assert "https://discord.com/channels/1/1" in body
    assert "claude auth login" in body
    assert alert.is_open


@pytest.mark.asyncio
async def test_more_threads_in_the_same_outage_edit_the_one_dm(tmp_path) -> None:
    """AC2: a second (and third) stopped thread adds its link; no second DM."""
    bot, dm = _bot()
    alert = _alert(tmp_path, bot)
    await alert.report(_thread(1))
    await alert.report(_thread(2))
    await alert.report(_thread(2))  # the same thread again: nothing new

    _user(bot).send.assert_awaited_once()
    dm.edit.assert_awaited_once()
    edited = dm.edit.await_args.kwargs["content"]
    assert "/1/1" in edited and "/1/2" in edited
    assert "（2）" in edited


@pytest.mark.asyncio
async def test_a_clean_turn_ends_the_outage_and_the_next_refusal_dms_again(tmp_path) -> None:
    """AC3: logged back in, then out again → a new DM."""
    bot, _ = _bot()
    alert = _alert(tmp_path, bot)
    await alert.report(_thread(1))
    await alert.recovered()
    assert not alert.is_open
    await alert.report(_thread(3))

    assert _user(bot).send.await_count == 2
    second = _user(bot).send.await_args.args[0]
    assert "/1/3" in second and "/1/1" not in second


@pytest.mark.asyncio
async def test_a_restart_mid_outage_sends_no_second_dm(tmp_path) -> None:
    """AC4: the outage is on disk; a new process edits the DM it finds again."""
    bot, _ = _bot()
    await _alert(tmp_path, bot).report(_thread(1))

    bot2, _ = _bot()
    old_dm = MagicMock()
    old_dm.edit = AsyncMock()
    channel = MagicMock()
    channel.fetch_message = AsyncMock(return_value=old_dm)
    bot2.get_channel = MagicMock(return_value=channel)
    alert2 = _alert(tmp_path, bot2)
    await alert2.report(_thread(2))

    _user(bot2).send.assert_not_awaited()
    channel.fetch_message.assert_awaited_once_with(777)
    assert "/1/2" in old_dm.edit.await_args.kwargs["content"]


@pytest.mark.asyncio
async def test_a_dm_that_cannot_be_sent_is_logged(tmp_path, caplog) -> None:
    """AC5: the owner closed DMs → WARNING with the reason; nothing raises."""
    forbidden = discord.Forbidden(MagicMock(status=403, reason="Forbidden"), "Cannot send")
    bot, _ = _bot(fail=forbidden)
    alert = _alert(tmp_path, bot)
    with caplog.at_level("WARNING"):
        await alert.report(_thread(1))
    assert "could not DM owner 4242 (Forbidden" in caplog.text
    assert "owner DM NOT sent" in caplog.text


@pytest.mark.asyncio
async def test_without_owner_id_the_application_owner_is_used(tmp_path, monkeypatch) -> None:
    """No ``DISCORD_OWNER_ID`` → the #713 owner, as for the #346 DM."""
    bot, _ = _bot()
    bot.owner_id = None
    monkeypatch.setattr("c_lord.login_alert.get_fallback_owner_ids", lambda: {99})
    await _alert(tmp_path, bot).report(_thread(1))
    bot.get_user.assert_called_with(99)


def test_long_outages_list_the_first_threads_and_count_the_rest() -> None:
    urls = [f"https://discord.com/channels/1/{i}" for i in range(40)]
    body = build_alert("h", urls)
    assert "ほか 25 本" in body
    assert len(body) < 2000


# ── the wiring: EventProcessor reports, a clean turn recovers ───────────────


def _result(error: str | None) -> StreamEvent:
    return StreamEvent(message_type=MessageType.RESULT, is_complete=True, error=error)


async def _process(thread: MagicMock, error: str | None) -> None:
    config = RunConfig(thread=thread, runner=MagicMock(), prompt="hi")
    await EventProcessor(config).process(_result(error))


@pytest.mark.asyncio
async def test_a_refused_turn_reaches_the_owner_and_a_clean_turn_closes_it() -> None:
    """AC1-3 through the real path: the ❌ embed is still posted in the thread."""
    bot, _ = _bot()
    login_alert.bind(bot)
    thread = _thread(5)
    await _process(thread, f"{LOGIN_REQUIRED_ERROR_PREFIX} Claude Code needs /login.")

    thread.send.assert_awaited_once()  # the #812 embed, unchanged
    _user(bot).send.assert_awaited_once()
    assert login_alert.is_open

    await _process(_thread(6), None)
    assert not login_alert.is_open


@pytest.mark.asyncio
async def test_other_errors_neither_dm_nor_close() -> None:
    bot, _ = _bot()
    login_alert.bind(bot)
    await _process(_thread(5), f"{LOGIN_REQUIRED_ERROR_PREFIX} x")
    await _process(_thread(6), "Something else broke")
    _user(bot).send.assert_awaited_once()
    assert login_alert.is_open
