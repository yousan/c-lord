"""Shared fixtures for E2E tests.

E2E tests post messages via Discord webhook to a real, running c-lord bot
and verify the bot's responses by polling Discord's REST API.

Required env (in `.env` at project root):
    DISCORD_BOT_TOKEN       — for authenticated REST reads
    DISCORD_CHANNEL_ID      — channel that webhook posts to
    E2E_TEST_WEBHOOK_URL    — webhook URL for the channel above

When any of these is missing, all e2e tests are SKIPPED (not failed) so
CI / contributors without Discord credentials are not blocked.

Tests are marked `@pytest.mark.e2e` and excluded from default `pytest`
runs (see `pyproject.toml`). To run E2E tests:

    uv run pytest -m e2e tests/e2e/

The bot must be running locally or remotely against the same channel.
"""

from __future__ import annotations

import json
import time
import urllib.error
import urllib.request
from pathlib import Path
from typing import Any

import pytest

UA = "DiscordBot (https://github.com/yousan/c-lord, 1.0)"
DISCORD_API = "https://discord.com/api/v10"


def _load_env() -> dict[str, str]:
    env_path = Path(__file__).resolve().parents[2] / ".env"
    env: dict[str, str] = {}
    if not env_path.exists():
        return env
    for raw in env_path.read_text().splitlines():
        line = raw.strip()
        if not line or line.startswith("#"):
            continue
        key, _, value = line.partition("=")
        env[key.strip()] = value.strip()
    return env


@pytest.fixture(scope="session")
def e2e_env() -> dict[str, str]:
    """Load .env and skip the E2E session if required keys are absent."""
    env = _load_env()
    required = ("DISCORD_BOT_TOKEN", "DISCORD_CHANNEL_ID", "E2E_TEST_WEBHOOK_URL")
    missing = [k for k in required if not env.get(k)]
    if missing:
        pytest.skip(f"E2E env not configured: missing {', '.join(missing)}")
    return env


class DiscordE2EClient:
    """Tiny REST + webhook client for E2E tests.

    Uses urllib only — no extra deps so the e2e suite stays light.
    """

    def __init__(self, env: dict[str, str]) -> None:
        self._token = env["DISCORD_BOT_TOKEN"]
        self._channel_id = env["DISCORD_CHANNEL_ID"]
        self._webhook = env["E2E_TEST_WEBHOOK_URL"]

    @property
    def channel_id(self) -> str:
        return self._channel_id

    def _get(self, path: str) -> Any:
        req = urllib.request.Request(f"{DISCORD_API}{path}")
        req.add_header("User-Agent", UA)
        req.add_header("Authorization", f"Bot {self._token}")
        with urllib.request.urlopen(req) as resp:
            return json.loads(resp.read())

    def _post(self, url: str, payload: dict[str, Any], *, auth: bool = True) -> Any:
        data = json.dumps(payload).encode()
        req = urllib.request.Request(url, data=data, method="POST")
        req.add_header("User-Agent", UA)
        req.add_header("Content-Type", "application/json")
        if auth:
            req.add_header("Authorization", f"Bot {self._token}")
        with urllib.request.urlopen(req) as resp:
            return json.loads(resp.read())

    def bot_user(self) -> dict[str, Any]:
        return self._get("/users/@me")

    def create_seed_message(self, content: str) -> dict[str, Any]:
        return self._post(
            f"{DISCORD_API}/channels/{self._channel_id}/messages",
            {"content": content},
        )

    def create_thread(self, seed_message_id: str, name: str) -> dict[str, Any]:
        return self._post(
            f"{DISCORD_API}/channels/{self._channel_id}/messages/{seed_message_id}/threads",
            {"name": name, "auto_archive_duration": 60},
        )

    def webhook_post(
        self,
        content: str,
        *,
        thread_id: str | None = None,
        username: str = "E2E Tester",
    ) -> dict[str, Any]:
        url = f"{self._webhook}?wait=true"
        if thread_id:
            url += f"&thread_id={thread_id}"
        return self._post(url, {"content": content, "username": username}, auth=False)

    def fetch_messages(
        self,
        channel_or_thread_id: str,
        *,
        after: str | None = None,
        limit: int = 20,
    ) -> list[dict[str, Any]]:
        path = f"/channels/{channel_or_thread_id}/messages?limit={limit}"
        if after:
            path += f"&after={after}"
        return self._get(path)

    def wait_for_bot_reply(
        self,
        thread_id: str,
        *,
        after_message_id: str,
        bot_id: str,
        contains: str | None = None,
        timeout: float = 120.0,
        poll: float = 3.0,
    ) -> dict[str, Any] | None:
        """Poll until a bot reply (optionally containing `contains`) appears.

        Returns the matching message or None on timeout.
        """
        deadline = time.monotonic() + timeout
        while time.monotonic() < deadline:
            try:
                msgs = self.fetch_messages(thread_id, after=after_message_id)
            except urllib.error.HTTPError:
                time.sleep(poll)
                continue
            for m in msgs:
                if m["author"]["id"] != bot_id:
                    continue
                if m.get("webhook_id"):
                    continue
                if contains is None or contains in m.get("content", ""):
                    return m
            time.sleep(poll)
        return None


@pytest.fixture(scope="session")
def discord_client(e2e_env: dict[str, str]) -> DiscordE2EClient:
    return DiscordE2EClient(e2e_env)


@pytest.fixture(scope="session")
def bot_id(discord_client: DiscordE2EClient) -> str:
    return discord_client.bot_user()["id"]


@pytest.fixture(scope="session")
def attached_thread_id(e2e_env: dict[str, str]) -> str:
    """ID of a thread already bound to a tmux session (via /clord-attach).

    Tests that exercise the message → Claude flow need to post into a thread
    the bot already knows about. Creating a fresh thread inside a test is
    insufficient because the bot's `_handle_thread_reply` requires a session
    record (`repo.get(thread_id)`) before invoking Claude.

    Set `E2E_TEST_THREAD_ID` in `.env` to the ID of a thread you've already
    attached. Tests requiring it are skipped if absent.
    """
    thread_id = e2e_env.get("E2E_TEST_THREAD_ID")
    if not thread_id:
        pytest.skip(
            "E2E_TEST_THREAD_ID not set in .env — "
            "create + attach a thread (/clord-attach) and add its ID to .env"
        )
    return thread_id
