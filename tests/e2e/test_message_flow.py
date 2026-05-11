"""E2E: thread message → Claude response, with TUI-leak regression checks.

Verifies the on_message → _handle_thread_reply → _run_claude path end-to-end,
and asserts that the response contains no TUI chrome (regression for #32 /
#30 / #29 / #27) and URL embeds are suppressed (PR #33).

These tests post into a pre-attached thread (`E2E_TEST_THREAD_ID`) — fresh
threads are skipped by the bot since they have no session record yet.
"""

from __future__ import annotations

import pytest

from .conftest import DiscordE2EClient

# Substrings that must NEVER appear in a streaming bot reply
# (they are TUI chrome that earlier bugs leaked into Discord).
CHROME_BLACKLIST = (
    "Model: ",  # ccstatusline row 1
    "Cost: $",  # ccstatusline row 2
    "⎇ ",  # ccstatusline branch row
    "Tip: Use Plan Mode",  # TUI hint
    "skill descriptions dropped",  # /doctor indicator
    "-- INSERT --",  # vim status bar
    "· /effort",  # Issue #50: effort/model footer ("◐ medium · /effort")
)


@pytest.mark.e2e
def test_simple_message_gets_response_without_chrome(
    discord_client: DiscordE2EClient,
    bot_id: str,
    attached_thread_id: str,
) -> None:
    """A single message → bot response containing no TUI chrome leakage."""
    marker = "e2e-flow-ok-z9k3"
    wh = discord_client.webhook_post(
        f"次の文字列を一言だけ返答に含めて: '{marker}'。説明は不要。",
        thread_id=attached_thread_id,
    )

    reply = discord_client.wait_for_bot_reply(
        attached_thread_id,
        after_message_id=wh["id"],
        bot_id=bot_id,
        contains=marker,
        timeout=180.0,
        poll=4.0,
    )
    assert reply is not None, f"Bot did not reply with marker {marker!r} within 180s"

    content = reply["content"]
    for chunk in CHROME_BLACKLIST:
        assert chunk not in content, (
            f"TUI chrome leak detected: {chunk!r} appeared in reply: {content[:300]!r}"
        )
    for line in content.splitlines():
        assert line.strip() != "❯", f"Bare prompt char leaked: {content[:300]!r}"


@pytest.mark.e2e
def test_url_in_reply_has_embeds_suppressed(
    discord_client: DiscordE2EClient,
    bot_id: str,
    attached_thread_id: str,
) -> None:
    """Replies containing URLs must have SUPPRESS_EMBEDS flag (PR #33)."""
    marker = "e2e-url-ok-q7m2"
    wh = discord_client.webhook_post(
        f"`{marker}` と `https://example.com` の 2 つを応答に含めて。説明は不要。",
        thread_id=attached_thread_id,
    )

    reply = discord_client.wait_for_bot_reply(
        attached_thread_id,
        after_message_id=wh["id"],
        bot_id=bot_id,
        contains=marker,
        timeout=180.0,
        poll=4.0,
    )
    assert reply is not None, "Bot did not reply within 180s"

    # Discord MessageFlags.SUPPRESS_EMBEDS == 1 << 2 == 4
    flags = reply.get("flags", 0)
    assert flags & 4, f"SUPPRESS_EMBEDS flag missing (flags={flags}). PR #33 regression."
    assert not reply.get("embeds"), "Reply contains embed cards despite SUPPRESS_EMBEDS"


@pytest.mark.e2e
def test_bullet_list_leading_indent_is_normalized(
    discord_client: DiscordE2EClient,
    bot_id: str,
    attached_thread_id: str,
) -> None:
    """Issue #43: flat bullet lists with leading spaces must be flattened.

    Claude is asked to echo a bullet list with 2-space leading indent. The
    bot's reply must contain the bullets at column 0, not indented (otherwise
    Discord renders them as nested under nothing — the regression we fixed).
    """
    marker = "e2e-43-indent-zk9"
    wh = discord_client.webhook_post(
        "以下を返答にそのままコピーして含めてください。説明不要、装飾不要、"
        f"marker `{marker}` を含めること:\n\n"
        f"result {marker}:\n\n  - alpha\n  - beta\n  - gamma",
        thread_id=attached_thread_id,
    )

    reply = discord_client.wait_for_bot_reply(
        attached_thread_id,
        after_message_id=wh["id"],
        bot_id=bot_id,
        contains=marker,
        timeout=180.0,
        poll=4.0,
    )
    assert reply is not None, f"Bot did not reply with marker {marker!r} within 180s"

    content = reply["content"]
    # Each bullet must appear at column 0, not preceded by spaces — otherwise
    # Discord (CommonMark) renders them as a nested list with no parent.
    for token in ("- alpha", "- beta", "- gamma"):
        assert f"\n{token}" in content or content.startswith(token), (
            f"Bullet {token!r} not at column 0 in reply: {content!r}"
        )
        assert f"  {token}" not in content, (
            f"Bullet {token!r} still preceded by indent in reply: {content!r}"
        )
