"""Who the 「復元できません」 notice is *for* — #556.

#538 replaced a silent ``return`` with a reply, so that a person who sent a
message into a thread whose ``sessions`` row was missing stopped being ignored.
The guard on it asked ``_is_our_thread`` — "is this channel mine?" — which is a
different question from "is this thread mine?", and every thread under a
``/clord-init``-bound channel answers yes.

In production that meant Grafana's server-alert thread: a thread yousan created
by hand under a bound channel, into which a webhook posts alerts. From the #545
deploy onward, every alert got a ⚠️ reaction and c-lord answered it with a wall
of text about restoring a session — in the one thread whose whole job is being
readable during an incident.

The first half of the fix is the cheap one, and it is what stops the bleeding:
**a webhook is not waiting for an answer.** Nothing that arrives from one earns a
reply, a reaction, or a notice.
"""

from __future__ import annotations

import logging
from unittest.mock import AsyncMock, MagicMock

import discord
import pytest

from c_lord.cogs.claude_chat import ClaudeChatCog

CHANNEL_ID = 999
THREAD_ID = 1541272541102870588  # the real サーバアラート thread


def _make_cog():
    bot = MagicMock()
    bot.channel_id = CHANNEL_ID
    bot.settings_repo = None
    ctx = MagicMock()
    ctx.valid = False
    bot.get_context = AsyncMock(return_value=ctx)
    bot.get_cog = MagicMock(return_value=None)
    repo = MagicMock()
    repo.get = AsyncMock(return_value=None)  # no sessions row — the #538 path
    repo.save = AsyncMock()
    runner = MagicMock()
    runner.clone = MagicMock(return_value=MagicMock())
    return ClaudeChatCog(bot=bot, repo=repo, runner=runner)


def _message(*, webhook: bool):
    """A message in a human-made thread under a c-lord-bound channel."""
    thread = MagicMock(spec=discord.Thread)
    thread.id = THREAD_ID
    thread.parent_id = CHANNEL_ID
    thread.send = AsyncMock()

    message = MagicMock(spec=discord.Message)
    message.channel = thread
    message.content = "## 🧪 テンプレート描画確認"
    message.attachments = []
    message.author = MagicMock()
    message.author.bot = webhook
    message.author.id = 4242
    message.webhook_id = 123456789 if webhook else None
    message.type = discord.MessageType.default
    message.add_reaction = AsyncMock()
    return message, thread


class TestWebhookMessagesAreLeftAlone:
    async def test_no_notice_is_posted(self) -> None:
        """AC1: the alert thread stays readable during an incident."""
        cog = _make_cog()
        message, thread = _message(webhook=True)

        await cog._handle_untracked_thread(message, thread)

        thread.send.assert_not_awaited()

    async def test_no_reaction_is_added(self) -> None:
        """The notice is once per process; the ⚠️ was on *every* alert."""
        cog = _make_cog()
        message, thread = _message(webhook=True)

        await cog._handle_untracked_thread(message, thread)

        message.add_reaction.assert_not_awaited()

    async def test_it_is_still_logged(self, caplog: pytest.LogCaptureFixture) -> None:
        """Quiet in Discord, not invisible — #538's AC1 was that the drop leaves
        a greppable trace, and that still holds for webhooks."""
        cog = _make_cog()
        message, thread = _message(webhook=True)

        with caplog.at_level(logging.DEBUG, logger="c_lord.cogs.claude_chat"):
            await cog._handle_untracked_thread(message, thread)

        assert str(THREAD_ID) in " ".join(r.getMessage() for r in caplog.records)

    async def test_a_human_in_the_same_thread_still_gets_answered(self) -> None:
        """AC4 guard: this must not silently undo #538 for people.

        A human who types into a thread with no row is the case #538 exists for —
        they are waiting for a reply that will never come unless we say so.
        """
        cog = _make_cog()
        message, thread = _message(webhook=False)

        await cog._handle_untracked_thread(message, thread)

        thread.send.assert_awaited_once()
        message.add_reaction.assert_awaited_once()
