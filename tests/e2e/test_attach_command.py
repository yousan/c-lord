"""E2E: !attach text command via webhook.

Replaces the standalone `tests/e2e_discord_attach.py` script with a
pytest-integrated equivalent. Verifies:
- Webhook messages reach the bot (process_commands override works)
- !attach command fires and the bot replies
"""

from __future__ import annotations

import pytest

from .conftest import DiscordE2EClient


@pytest.mark.e2e
def test_attach_command_via_webhook(
    discord_client: DiscordE2EClient,
    bot_id: str,
) -> None:
    seed = discord_client.create_seed_message("[E2E] !attach command test")
    thread = discord_client.create_thread(seed["id"], "E2E: !attach test")
    thread_id = thread["id"]

    wh_msg = discord_client.webhook_post("!attach work-e2e", thread_id=thread_id)

    reply = discord_client.wait_for_bot_reply(
        thread_id,
        after_message_id=wh_msg["id"],
        bot_id=bot_id,
        timeout=30.0,
        poll=2.0,
    )
    assert reply is not None, "Bot did not reply to !attach within 30s"
    # Any non-empty bot reply is acceptable — the command fired and the bot
    # responded (success / "thread-only" / "tmux not configured" / auth error
    # are all evidence that the message reached the cog).
    assert reply["content"], "Bot reply was empty"
