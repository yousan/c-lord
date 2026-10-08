"""A refused message is reported to the owner by DM, never in the channel (#346).

Before #346, a message from someone the allowlist does not cover was dropped in
``ClaudeChatCog.on_message`` with no reply, no reaction — and no log line. On
2026-09-26 a guest's request was dropped that way twice, and neither the guest
nor the owner could tell.

What is pinned here:

* the person who was refused sees **nothing** (no reply, no reaction, no thread);
* the bot's owner gets **one DM** naming who, where, what (first lines), a link
  to the message, and how to allow them;
* the same person is throttled — in memory, not in the DB;
* bots are logged, never DMed (webhooks are authorized by construction);
* every refusal is logged with ``log_ctx``;
* ``CLORD_NOTIFY_DENIED=0`` turns the DM off (the log stays);
* an owner who refuses DMs does not break the bot;
* only places this instance would act on count (a thread it owns), so ordinary
  chat elsewhere in the guild never turns into a DM.
"""

from __future__ import annotations

import logging
from unittest.mock import AsyncMock, MagicMock

import discord
import pytest

import c_lord.denied_notice as dn
from c_lord.denied_notice import DeniedNotifier, build_notice, notify_enabled
from c_lord.discord_ui.authorization import set_fallback_owner_ids

OWNER = 4242
OUTSIDER = 99
THREAD = 5555
PARENT = 6666
BOT_SELF = 1111


@pytest.fixture(autouse=True)
def _reset(monkeypatch: pytest.MonkeyPatch):
    monkeypatch.delenv("CLORD_NOTIFY_DENIED", raising=False)
    set_fallback_owner_ids(None)
    yield
    set_fallback_owner_ids(None)


def _thread() -> MagicMock:
    thread = MagicMock(spec=discord.Thread)
    thread.id = THREAD
    thread.parent_id = PARENT
    thread.name = "作業スレッド"
    thread.mention = f"<#{THREAD}>"
    thread.guild = MagicMock()
    thread.guild.name = "Test Guild"
    thread.send = AsyncMock()
    return thread


def _message(
    *,
    author_id: int = OUTSIDER,
    bot: bool = False,
    content: str = "これをやって\n2行目\n3行目\n4行目",
    channel: MagicMock | None = None,
) -> MagicMock:
    msg = MagicMock(spec=discord.Message)
    msg.id = 777
    msg.webhook_id = None
    msg.type = discord.MessageType.default
    msg.content = content
    msg.attachments = []
    msg.jump_url = f"https://discord.com/channels/1/{THREAD}/777"
    msg.author = MagicMock(spec=discord.Member)
    msg.author.id = author_id
    msg.author.bot = bot
    msg.author.name = "babeln"
    msg.author.display_name = "babeln"
    msg.channel = channel if channel is not None else _thread()
    msg.add_reaction = AsyncMock()
    msg.reply = AsyncMock()
    return msg


def _bot(*, owner_user: MagicMock | None = None) -> MagicMock:
    bot = MagicMock()
    bot.user = MagicMock()
    bot.user.id = BOT_SELF
    bot.channel_id = 123
    ctx = MagicMock()
    ctx.valid = False
    bot.get_context = AsyncMock(return_value=ctx)
    app = MagicMock()
    app.team = None
    app.owner = MagicMock()
    app.owner.id = OWNER
    bot.application = app
    user = owner_user or MagicMock()
    if owner_user is None:
        user.send = AsyncMock()
    bot.get_user = MagicMock(return_value=user)
    bot.fetch_user = AsyncMock(return_value=user)
    return bot


def _owned(monkeypatch: pytest.MonkeyPatch, owned: bool = True) -> None:
    monkeypatch.setattr(dn, "owns_channel", AsyncMock(return_value=owned))


class TestDm:
    async def test_dm_goes_to_owner_and_nothing_to_channel(self, monkeypatch) -> None:
        _owned(monkeypatch)
        bot = _bot()
        msg = _message()
        await DeniedNotifier().report(bot, msg)

        owner = bot.get_user.return_value
        bot.get_user.assert_called_with(OWNER)
        owner.send.assert_awaited_once()
        # The refused person sees nothing: no reply, no reaction, no post.
        msg.reply.assert_not_called()
        msg.add_reaction.assert_not_called()
        msg.channel.send.assert_not_called()

    async def test_owner_comes_from_application_even_with_allowlist(self, monkeypatch) -> None:
        # With DISCORD_OWNER_ID set, #713's fallback is never resolved; the DM
        # still goes to the application owner.
        _owned(monkeypatch)
        bot = _bot()
        bot.application = None
        app = MagicMock()
        app.team = None
        app.owner = MagicMock()
        app.owner.id = OWNER
        bot.application_info = AsyncMock(return_value=app)
        await DeniedNotifier().report(bot, _message())
        bot.get_user.assert_called_with(OWNER)

    async def test_resolved_fallback_owner_is_used(self, monkeypatch) -> None:
        _owned(monkeypatch)
        set_fallback_owner_ids({OWNER + 1})
        bot = _bot()
        await DeniedNotifier().report(bot, _message())
        bot.get_user.assert_called_with(OWNER + 1)

    async def test_dm_does_not_ping_anyone(self, monkeypatch) -> None:
        _owned(monkeypatch)
        bot = _bot()
        await DeniedNotifier().report(bot, _message(content="@everyone <@1>"))
        kwargs = bot.get_user.return_value.send.await_args.kwargs
        mentions = kwargs["allowed_mentions"]
        assert mentions.everyone is False
        assert mentions.users is False
        assert mentions.roles is False


class TestNoticeBody:
    def test_names_who_where_what_link_and_how(self) -> None:
        body = build_notice(_message())
        assert f"<@{OUTSIDER}>" in body
        assert str(OUTSIDER) in body
        assert "babeln" in body
        assert "Test Guild" in body
        assert "作業スレッド" in body
        assert "https://discord.com/channels/1/5555/777" in body
        assert "これをやって" in body
        assert "CLORD_ALLOWED_ROLE" in body

    def test_only_first_lines(self) -> None:
        body = build_notice(_message())
        assert "3行目" in body
        assert "4行目" not in body

    def test_long_line_is_cut(self) -> None:
        body = build_notice(_message(content="x" * 5000))
        assert len(body) < 1500

    def test_no_allowlist_contents(self, monkeypatch) -> None:
        # The DM must not disclose who else is allowed.
        monkeypatch.setenv("DISCORD_OWNER_ID", "31415926535")
        monkeypatch.setenv("CLORD_ALLOWED_ROLE", "secret-role-name")
        body = build_notice(_message())
        assert "31415926535" not in body
        assert "secret-role-name" not in body

    def test_attachment_only(self) -> None:
        msg = _message(content="")
        msg.attachments = [MagicMock(), MagicMock()]
        assert "添付 2 件" in build_notice(msg)


class TestThrottle:
    async def test_same_person_is_dmed_once_per_interval(self, monkeypatch) -> None:
        _owned(monkeypatch)
        now = [1000.0]
        notifier = DeniedNotifier(interval=3600, clock=lambda: now[0])
        bot = _bot()
        send = bot.get_user.return_value.send
        await notifier.report(bot, _message())
        await notifier.report(bot, _message())
        assert send.await_count == 1
        now[0] += 3601
        await notifier.report(bot, _message())
        assert send.await_count == 2

    async def test_other_person_is_not_throttled(self, monkeypatch) -> None:
        _owned(monkeypatch)
        notifier = DeniedNotifier()
        bot = _bot()
        await notifier.report(bot, _message(author_id=1))
        await notifier.report(bot, _message(author_id=2))
        assert bot.get_user.return_value.send.await_count == 2

    def test_throttle_is_in_memory_not_db(self) -> None:
        # No repository is accepted or reached: the record is process memory.
        import inspect

        src = inspect.getsource(dn)
        assert "repository" not in src
        assert "aiosqlite" not in src


class TestNotDmed:
    async def test_bot_is_logged_not_dmed(self, monkeypatch, caplog) -> None:
        _owned(monkeypatch)
        bot = _bot()
        caplog.set_level(logging.INFO, logger="c_lord.denied_notice")
        await DeniedNotifier().report(bot, _message(author_id=8888, bot=True))
        bot.get_user.return_value.send.assert_not_called()
        assert f"thread={THREAD}" in caplog.text
        assert "8888" in caplog.text

    async def test_own_messages_are_ignored(self, monkeypatch, caplog) -> None:
        _owned(monkeypatch)
        bot = _bot()
        caplog.set_level(logging.INFO, logger="c_lord.denied_notice")
        await DeniedNotifier().report(bot, _message(author_id=BOT_SELF, bot=True))
        bot.get_user.return_value.send.assert_not_called()
        assert caplog.text == ""

    async def test_place_we_do_not_own_is_ignored(self, monkeypatch) -> None:
        _owned(monkeypatch, owned=False)
        bot = _bot()
        await DeniedNotifier().report(bot, _message())
        bot.get_user.return_value.send.assert_not_called()

    async def test_non_thread_channel_is_ignored(self, monkeypatch) -> None:
        # Nobody's channel post runs a turn, so it is not a refusal.
        _owned(monkeypatch)
        bot = _bot()
        channel = MagicMock(spec=discord.TextChannel)
        channel.id = PARENT
        await DeniedNotifier().report(bot, _message(channel=channel))
        bot.get_user.return_value.send.assert_not_called()

    async def test_system_message_is_ignored(self, monkeypatch) -> None:
        _owned(monkeypatch)
        bot = _bot()
        msg = _message()
        msg.type = discord.MessageType.thread_created
        await DeniedNotifier().report(bot, msg)
        bot.get_user.return_value.send.assert_not_called()

    async def test_text_command_is_left_to_the_command(self, monkeypatch) -> None:
        _owned(monkeypatch)
        bot = _bot()
        bot.get_context.return_value.valid = True
        await DeniedNotifier().report(bot, _message(content="!clord-status"))
        bot.get_user.return_value.send.assert_not_called()


class TestLogAndSwitch:
    async def test_every_refusal_is_logged_with_ctx(self, monkeypatch, caplog) -> None:
        _owned(monkeypatch)
        notifier = DeniedNotifier()
        bot = _bot()
        caplog.set_level(logging.INFO, logger="c_lord.denied_notice")
        await notifier.report(bot, _message())
        await notifier.report(bot, _message())  # throttled DM, still logged
        lines = [r for r in caplog.records if "not authorized" in r.getMessage()]
        assert len(lines) == 2
        for r in lines:
            assert f"thread={THREAD} channel={PARENT}" in r.getMessage()
            assert str(OUTSIDER) in r.getMessage()

    async def test_switch_off_sends_no_dm_but_logs(self, monkeypatch, caplog) -> None:
        _owned(monkeypatch)
        monkeypatch.setenv("CLORD_NOTIFY_DENIED", "0")
        bot = _bot()
        caplog.set_level(logging.INFO, logger="c_lord.denied_notice")
        await DeniedNotifier().report(bot, _message())
        bot.get_user.return_value.send.assert_not_called()
        assert "not authorized" in caplog.text

    def test_switch_default_on(self) -> None:
        assert notify_enabled() is True

    @pytest.mark.parametrize("value", ["0", "false", "off", "no"])
    def test_switch_values(self, monkeypatch, value) -> None:
        monkeypatch.setenv("CLORD_NOTIFY_DENIED", value)
        assert notify_enabled() is False

    async def test_owner_refusing_dms_does_not_raise(self, monkeypatch, caplog) -> None:
        _owned(monkeypatch)
        owner = MagicMock()
        owner.send = AsyncMock(
            side_effect=discord.Forbidden(MagicMock(status=403), "Cannot send messages")
        )
        bot = _bot(owner_user=owner)
        caplog.set_level(logging.INFO, logger="c_lord.denied_notice")
        await DeniedNotifier().report(bot, _message())  # must not raise
        assert "DM" in caplog.text

    async def test_unknown_owner_does_not_raise(self, monkeypatch, caplog) -> None:
        _owned(monkeypatch)
        bot = _bot()
        bot.application = None
        bot.application_info = AsyncMock(side_effect=RuntimeError("boom"))
        caplog.set_level(logging.INFO, logger="c_lord.denied_notice")
        await DeniedNotifier().report(bot, _message())
        bot.get_user.return_value.send.assert_not_called()


class TestOnMessageWiring:
    async def test_on_message_reports_refused_message(self) -> None:
        from c_lord.cogs.claude_chat import ClaudeChatCog

        bot = MagicMock()
        bot.authorizer = None
        bot.session_registry = MagicMock()
        bot.channel_id = 123
        cog = ClaudeChatCog(bot=bot, repo=MagicMock(), runner=MagicMock(), allowed_user_ids={OWNER})
        cog._denied_notifier = MagicMock()
        cog._denied_notifier.report = AsyncMock()
        cog._handle_thread_reply = AsyncMock()
        msg = _message()
        await cog.on_message(msg)
        cog._denied_notifier.report.assert_awaited_once_with(bot, msg)
        cog._handle_thread_reply.assert_not_called()

    async def test_on_message_does_not_report_allowed_message(self) -> None:
        from c_lord.cogs.claude_chat import ClaudeChatCog

        bot = MagicMock()
        bot.authorizer = None
        bot.session_registry = MagicMock()
        bot.channel_id = 123
        cog = ClaudeChatCog(bot=bot, repo=MagicMock(), runner=MagicMock(), allowed_user_ids={OWNER})
        cog._denied_notifier = MagicMock()
        cog._denied_notifier.report = AsyncMock()
        cog._is_runnable_request = AsyncMock(return_value=False)
        await cog.on_message(_message(author_id=OWNER))
        cog._denied_notifier.report.assert_not_called()
