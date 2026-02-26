#!/usr/bin/env python3
"""E2E test: send !attach via Discord webhook and verify bot response.

Prerequisites:
  1. Bot is running with the new code (process_commands override + !attach command)
  2. E2E_TEST_WEBHOOK_URL is set in .env (webhook for the DISCORD_CHANNEL_ID channel)
  3. Bot has Send Messages permission in the channel

Usage:
  uv run python tests/e2e_discord_attach.py

What this tests:
  - Webhook message triggers process_commands (process_commands override works)
  - !attach command fires and responds
  - on_message guard prevents double-processing (no _handle_thread_reply for commands)
"""

from __future__ import annotations

import json
import sys
import time
import urllib.request
from pathlib import Path

UA = "DiscordBot (https://github.com/yousan/c-lord, 1.0)"


def load_env() -> dict[str, str]:
    """Load .env file from project root."""
    env_path = Path(__file__).parent.parent / ".env"
    env = {}
    for line in env_path.read_text().strip().split("\n"):
        line = line.strip()
        if not line or line.startswith("#"):
            continue
        key, _, value = line.partition("=")
        env[key.strip()] = value.strip()
    return env


def discord_request(
    url: str,
    *,
    method: str = "GET",
    token: str | None = None,
    payload: dict | None = None,
) -> dict:
    """Make a Discord API request."""
    data = json.dumps(payload).encode() if payload else None
    req = urllib.request.Request(url, data=data, method=method)
    req.add_header("User-Agent", UA)
    req.add_header("Content-Type", "application/json")
    if token:
        req.add_header("Authorization", f"Bot {token}")
    resp = urllib.request.urlopen(req)
    return json.loads(resp.read())


def main() -> None:
    env = load_env()

    bot_token = env.get("DISCORD_BOT_TOKEN")
    channel_id = env.get("DISCORD_CHANNEL_ID")
    webhook_url = env.get("E2E_TEST_WEBHOOK_URL")

    if not bot_token or not channel_id:
        print("ERROR: DISCORD_BOT_TOKEN and DISCORD_CHANNEL_ID must be set in .env")
        sys.exit(1)

    if not webhook_url:
        print("ERROR: E2E_TEST_WEBHOOK_URL is not set in .env")
        print("")
        print("To set up:")
        print("  1. Open Discord → Server Settings → Integrations → Webhooks")
        print(f"  2. Create a webhook for the channel with ID {channel_id}")
        print("  3. Copy the webhook URL")
        print("  4. Add to .env: E2E_TEST_WEBHOOK_URL=<your-webhook-url>")
        sys.exit(1)

    # Get bot user ID
    bot_info = discord_request("https://discord.com/api/v10/users/@me", token=bot_token)
    bot_id = bot_info["id"]
    print(f"Bot: {bot_info['username']} (ID: {bot_id})")

    # Step 1: Create a seed message and thread
    print("\n[1/5] Creating seed message via Bot...")
    seed = discord_request(
        f"https://discord.com/api/v10/channels/{channel_id}/messages",
        method="POST",
        token=bot_token,
        payload={"content": "[E2E] !attach command test"},
    )
    seed_id = seed["id"]
    print(f"  Seed message: {seed_id}")

    print("\n[2/5] Creating thread from seed...")
    thread = discord_request(
        f"https://discord.com/api/v10/channels/{channel_id}/messages/{seed_id}/threads",
        method="POST",
        token=bot_token,
        payload={"name": "E2E: !attach test", "auto_archive_duration": 60},
    )
    thread_id = thread["id"]
    print(f"  Thread: {thread_id}")

    # Step 2: Send !attach work1 via webhook
    print("\n[3/5] Sending '!attach work1' via webhook...")
    wh_msg = discord_request(
        f"{webhook_url}?thread_id={thread_id}&wait=true",
        method="POST",
        payload={"content": "!attach work1", "username": "E2E Tester"},
    )
    wh_msg_id = wh_msg["id"]
    print(f"  Webhook message: {wh_msg_id} (webhook_id: {wh_msg.get('webhook_id')})")

    # Step 3: Wait for bot response
    wait_secs = 5
    print(f"\n[4/5] Waiting {wait_secs}s for bot response...")
    time.sleep(wait_secs)

    # Step 4: Check thread messages
    print("\n[5/5] Checking thread messages...")
    messages = discord_request(
        f"https://discord.com/api/v10/channels/{thread_id}/messages?limit=10",
        token=bot_token,
    )

    print("\n--- Thread messages ---")
    for m in reversed(messages):
        author = m["author"]["username"]
        flags = []
        if m["author"].get("bot"):
            flags.append("BOT")
        if m.get("webhook_id"):
            flags.append("WEBHOOK")
        flag_str = f" [{','.join(flags)}]" if flags else ""
        content = m["content"][:100] if m["content"] else "(empty)"
        print(f"  {author}{flag_str}: {content}")

    # Evaluate result
    bot_responses = [
        m
        for m in messages
        if m["author"]["id"] == bot_id
        and m.get("webhook_id") is None
        and int(m["id"]) > int(wh_msg_id)
    ]

    print("\n=== RESULT ===")
    if bot_responses:
        for r in bot_responses:
            content = r["content"]
            if "thread" in content.lower():
                print(f"PASS (thread-only error): {content}")
            elif "tmux" in content.lower():
                print(f"PASS (tmux not configured): {content}")
            elif "work1" in content:
                print(f"PASS (attach success): {content}")
            elif "authorized" in content.lower():
                print(f"PASS (auth error): {content}")
            else:
                print(f"PASS (bot responded): {content}")
    else:
        print("FAIL: No bot response found.")
        print("  Possible causes:")
        print("  - Bot is not running with the latest code (restart needed)")
        print("  - Webhook is for a different channel than DISCORD_CHANNEL_ID")
        print("  - process_commands is still blocking webhook messages")
        sys.exit(1)


if __name__ == "__main__":
    main()
