"""E2E: text / mention command twins via webhook (#209).

Slash commands cannot be invoked by bots or webhooks, so the webhook-based
E2E harness can only reach them through their ``!``-prefix / mention twins.
These tests verify each twin fires and the bot replies when triggered the way
a slash command never could be.

Covered: !clord, !skill, !stop, !clear, the read-only info twins
(!model-show, !sessions, !session-dirs, !tmux-list, !resume-info), and a
mention-prefixed command.
"""

from __future__ import annotations

import pytest

from .conftest import DiscordE2EClient


@pytest.mark.e2e
def test_clord_twin_via_webhook(
    discord_client: DiscordE2EClient,
    bot_id: str,
) -> None:
    """!clord in a channel creates a thread + starts a session (like /clord)."""
    wh_msg = discord_client.webhook_post("!clord [E2E] say hello and stop")

    reply = discord_client.wait_for_bot_reply(
        discord_client.channel_id,
        after_message_id=wh_msg["id"],
        bot_id=bot_id,
        timeout=30.0,
        poll=2.0,
    )
    assert reply is not None, "Bot did not reply to !clord within 30s"
    # New-thread mode posts "Session started → <#thread>" (or an unbound-channel
    # / not-authorized notice). Any non-empty reply means the command fired.
    assert reply["content"], "Bot reply was empty"


@pytest.mark.e2e
def test_skill_twin_via_webhook(
    discord_client: DiscordE2EClient,
    bot_id: str,
) -> None:
    """!skill reaches SkillCommandCog (an unknown skill returns "not found")."""
    seed = discord_client.create_seed_message("[E2E] !skill command test")
    thread = discord_client.create_thread(seed["id"], "E2E: !skill test")
    thread_id = thread["id"]

    wh_msg = discord_client.webhook_post("!skill __e2e_nonexistent_skill__", thread_id=thread_id)

    reply = discord_client.wait_for_bot_reply(
        thread_id,
        after_message_id=wh_msg["id"],
        bot_id=bot_id,
        timeout=30.0,
        poll=2.0,
    )
    assert reply is not None, "Bot did not reply to !skill within 30s"
    assert "not found" in reply["content"].lower()


@pytest.mark.e2e
def test_stop_twin_via_webhook(
    discord_client: DiscordE2EClient,
    bot_id: str,
) -> None:
    """!stop reaches ClaudeChatCog (no active runner → informative reply)."""
    seed = discord_client.create_seed_message("[E2E] !stop command test")
    thread = discord_client.create_thread(seed["id"], "E2E: !stop test")
    thread_id = thread["id"]

    wh_msg = discord_client.webhook_post("!stop", thread_id=thread_id)

    reply = discord_client.wait_for_bot_reply(
        thread_id,
        after_message_id=wh_msg["id"],
        bot_id=bot_id,
        timeout=30.0,
        poll=2.0,
    )
    assert reply is not None, "Bot did not reply to !stop within 30s"
    assert reply["content"], "Bot reply was empty"


@pytest.mark.e2e
def test_clear_twin_via_webhook(
    discord_client: DiscordE2EClient,
    bot_id: str,
) -> None:
    """!clear reaches ClaudeChatCog and replies (cleared / nothing to clear)."""
    seed = discord_client.create_seed_message("[E2E] !clear command test")
    thread = discord_client.create_thread(seed["id"], "E2E: !clear test")
    thread_id = thread["id"]

    wh_msg = discord_client.webhook_post("!clear", thread_id=thread_id)

    reply = discord_client.wait_for_bot_reply(
        thread_id,
        after_message_id=wh_msg["id"],
        bot_id=bot_id,
        timeout=30.0,
        poll=2.0,
    )
    assert reply is not None, "Bot did not reply to !clear within 30s"
    assert reply["content"], "Bot reply was empty"


@pytest.mark.e2e
@pytest.mark.parametrize(
    "command",
    ["!model-show", "!sessions", "!session-dirs", "!tmux-list", "!resume-info"],
)
def test_readonly_info_twins_via_webhook(
    discord_client: DiscordE2EClient,
    bot_id: str,
    command: str,
) -> None:
    """Each read-only info twin replies when fired from a webhook (like its slash)."""
    seed = discord_client.create_seed_message(f"[E2E] {command} test")
    thread = discord_client.create_thread(seed["id"], f"E2E: {command}")
    thread_id = thread["id"]

    wh_msg = discord_client.webhook_post(command, thread_id=thread_id)

    reply = discord_client.wait_for_bot_reply(
        thread_id,
        after_message_id=wh_msg["id"],
        bot_id=bot_id,
        timeout=30.0,
        poll=2.0,
    )
    assert reply is not None, f"Bot did not reply to {command} within 30s"
    # A reply (embed or text) means the command reached the cog. Embeds have
    # empty content, so accept either content or an embed.
    assert reply["content"] or reply.get("embeds"), f"Bot reply to {command} was empty"


@pytest.mark.e2e
@pytest.mark.parametrize(
    "command",
    ["!clord-init", "!clord-thread-init", "!model-set"],
)
def test_config_twins_non_mutating_via_webhook(
    discord_client: DiscordE2EClient,
    bot_id: str,
    command: str,
) -> None:
    """Config twins fire from a webhook (non-mutating forms: show / usage)."""
    # Bare forms don't change state: clord-init/-thread-init "show", model-set "usage".
    seed = discord_client.create_seed_message(f"[E2E] {command} test")
    thread = discord_client.create_thread(seed["id"], f"E2E: {command}")
    thread_id = thread["id"]

    wh_msg = discord_client.webhook_post(command, thread_id=thread_id)

    reply = discord_client.wait_for_bot_reply(
        thread_id,
        after_message_id=wh_msg["id"],
        bot_id=bot_id,
        timeout=30.0,
        poll=2.0,
    )
    assert reply is not None, f"Bot did not reply to {command} within 30s"
    assert reply["content"] or reply.get("embeds"), f"Bot reply to {command} was empty"


@pytest.mark.e2e
def test_session_cleanup_dryrun_twin_via_webhook(
    discord_client: DiscordE2EClient,
    bot_id: str,
) -> None:
    """!session-cleanup dry (preview, non-destructive) fires from a webhook."""
    wh_msg = discord_client.webhook_post("!session-cleanup dry")
    reply = discord_client.wait_for_bot_reply(
        discord_client.channel_id,
        after_message_id=wh_msg["id"],
        bot_id=bot_id,
        timeout=30.0,
        poll=2.0,
    )
    assert reply is not None, "Bot did not reply to !session-cleanup dry within 30s"
    assert reply["content"] or reply.get("embeds")


@pytest.mark.e2e
def test_workspace_delete_twin_via_webhook(
    discord_client: DiscordE2EClient,
    bot_id: str,
) -> None:
    """!workspace-delete fires in a fresh thread (no workspace → harmless reply)."""
    seed = discord_client.create_seed_message("[E2E] !workspace-delete test")
    thread = discord_client.create_thread(seed["id"], "E2E: !workspace-delete")
    thread_id = thread["id"]

    wh_msg = discord_client.webhook_post("!workspace-delete", thread_id=thread_id)
    reply = discord_client.wait_for_bot_reply(
        thread_id,
        after_message_id=wh_msg["id"],
        bot_id=bot_id,
        timeout=30.0,
        poll=2.0,
    )
    assert reply is not None, "Bot did not reply to !workspace-delete within 30s"
    assert reply["content"] or reply.get("embeds")


@pytest.mark.e2e
def test_mention_prefix_via_webhook(
    discord_client: DiscordE2EClient,
    bot_id: str,
) -> None:
    """A bot-mention prefix (when_mentioned_or) triggers the same text twins."""
    seed = discord_client.create_seed_message("[E2E] mention command test")
    thread = discord_client.create_thread(seed["id"], "E2E: mention test")
    thread_id = thread["id"]

    wh_msg = discord_client.webhook_post(f"<@{bot_id}> stop", thread_id=thread_id)

    reply = discord_client.wait_for_bot_reply(
        thread_id,
        after_message_id=wh_msg["id"],
        bot_id=bot_id,
        timeout=30.0,
        poll=2.0,
    )
    assert reply is not None, "Bot did not reply to mention-prefixed command within 30s"
    assert reply["content"], "Bot reply was empty"
