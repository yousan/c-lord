"""E2E: discord-reply Skill → c-lord REST API → Discord (#52 Phase 1).

These tests assume the bot under test was started with ``USE_SKILL_REPLY=true``
so that ``inject_skills`` ran when the session dir was created.  They are
skipped otherwise.

To run::

    USE_SKILL_REPLY=true uv run python -m c_lord.main &
    uv run pytest -m e2e tests/e2e/test_skill_reply.py
"""

from __future__ import annotations

import os

import pytest

from .conftest import DiscordE2EClient


def _skill_reply_enabled() -> bool:
    return os.getenv("USE_SKILL_REPLY", "").strip().lower() in {"1", "true", "yes", "on"}


@pytest.mark.e2e
@pytest.mark.skipif(
    not _skill_reply_enabled(),
    reason="USE_SKILL_REPLY not enabled — Skill is not injected on this bot",
)
def test_claude_uses_discord_reply_skill_to_post_marker(
    discord_client: DiscordE2EClient,
    bot_id: str,
    attached_thread_id: str,
) -> None:
    """Ask Claude to use the discord-reply skill and verify the marker arrives.

    The skill's curl command is the only way the marker can reach Discord
    (we don't ask Claude to print to stdout). If the marker shows up, the
    full Skill → REST API → bot.send pipeline is working.
    """
    marker = "e2e-skill-reply-fa9k2"
    prompt = (
        "あなたがいる作業ディレクトリの .claude/skills/discord-reply/SKILL.md を "
        "読み、その手順 (curl /api/reply への POST) のみを使って次のマーカーを "
        f"このスレッドに返答してください: {marker}\n\n"
        "応答本文は他に何も含めないこと。説明、装飾、進捗ログ不要。"
    )
    wh = discord_client.webhook_post(prompt, thread_id=attached_thread_id)

    reply = discord_client.wait_for_bot_reply(
        attached_thread_id,
        after_message_id=wh["id"],
        bot_id=bot_id,
        contains=marker,
        timeout=240.0,
        poll=4.0,
    )
    assert reply is not None, f"Bot did not reply with marker {marker!r} within 240s"

    content = reply["content"]
    # The skill posts as a plain message — no `-# 💻 (cli)` prefix like
    # /api/threads/{id}/messages would add.
    assert not content.startswith("-# "), (
        f"Skill reply should be plain text, but got CLI-style prefix: {content[:80]!r}"
    )
    assert marker in content
