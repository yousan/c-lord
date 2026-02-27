#!/usr/bin/env python3
"""E2E test: verify bot does NOT respond to threads without DB session.

This reproduces the original problem where the bot auto-responded to ALL
threads in the monitored channel, even those not created via /clord or !attach.

Prerequisites:
  1. Bot is running with the opt-in thread change
  2. E2E_TEST_WEBHOOK_URL is set in .env
  3. DISCORD_BOT_TOKEN and DISCORD_CHANNEL_ID are set in .env

Usage:
  uv run python tests/e2e_optin_thread.py

Expected result:
  PASS — bot does NOT respond to a manually created thread.
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
    env: dict[str, str] = {}
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
        sys.exit(1)

    # Get bot user ID
    bot_info = discord_request("https://discord.com/api/v10/users/@me", token=bot_token)
    bot_id = bot_info["id"]
    print(f"Bot: {bot_info['username']} (ID: {bot_id})")

    # Step 1: Create a seed message and thread (manually, NOT via /clord)
    print("\n[1/4] Creating seed message via Bot API (simulating manual thread)...")
    seed = discord_request(
        f"https://discord.com/api/v10/channels/{channel_id}/messages",
        method="POST",
        token=bot_token,
        payload={"content": "[E2E] Opt-in thread test — this thread has NO DB session"},
    )
    seed_id = seed["id"]
    print(f"  Seed message: {seed_id}")

    print("  Creating thread from seed...")
    thread = discord_request(
        f"https://discord.com/api/v10/channels/{channel_id}/messages/{seed_id}/threads",
        method="POST",
        token=bot_token,
        payload={"name": "E2E: opt-in test (no session)", "auto_archive_duration": 60},
    )
    thread_id = thread["id"]
    print(f"  Thread: {thread_id}")

    # Step 2: Send a normal message via webhook (simulating a user message)
    print("\n[2/4] Sending normal message via webhook (not a command)...")
    wh_msg = discord_request(
        f"{webhook_url}?thread_id={thread_id}&wait=true",
        method="POST",
        payload={
            "content": "Hello, is anyone there? This should be ignored by the bot.",
            "username": "E2E Tester",
        },
    )
    wh_msg_id = wh_msg["id"]
    print(f"  Webhook message: {wh_msg_id}")

    # Step 3: Wait for potential bot response
    wait_secs = 8
    print(f"\n[3/4] Waiting {wait_secs}s for potential bot response...")
    time.sleep(wait_secs)

    # Step 4: Check thread messages
    print("\n[4/4] Checking thread messages...")
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

    # Evaluate: bot should NOT have responded
    bot_responses = [
        m
        for m in messages
        if m["author"]["id"] == bot_id
        and m.get("webhook_id") is None
        and int(m["id"]) > int(wh_msg_id)
    ]

    print("\n=== RESULT ===")
    if bot_responses:
        print("FAIL: Bot responded to a thread with no DB session!")
        print("  The opt-in change is not working correctly.")
        for r in bot_responses:
            print(f"  Bot said: {r['content'][:200]}")
        sys.exit(1)
    else:
        print("PASS: Bot correctly ignored the thread (no DB session, no response).")


if __name__ == "__main__":
    main()
