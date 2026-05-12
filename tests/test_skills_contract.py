"""Contract test: SKILL.md examples must work against /api/reply.

Each curl example in the discord-reply SKILL.md is parsed back into the JSON
payload it would send, then POSTed to a real ``ApiServer`` instance. If the
endpoint and the template ever drift (e.g. someone switches the API to
multipart again, or adds a required field), this test breaks loudly.
"""

from __future__ import annotations

import json
import os
import re
import tempfile
from unittest.mock import AsyncMock, MagicMock

import pytest
from aiohttp.test_utils import TestClient, TestServer

from c_lord.database.notification_repo import NotificationRepository
from c_lord.ext.api_server import ApiServer
from c_lord.skills import render_discord_reply_skill

THREAD_ID = 444555666


def _extract_json_payloads(skill_md: str) -> list[dict]:
    """Pull every ``-d @- <<JSON ... JSON`` heredoc body out of SKILL.md.

    The here-doc delimiter may be quoted (``<<'JSON'``) or unquoted (``<<JSON``)
    — both forms appear in the template. Returns a list of parsed JSON dicts
    in the order they appear.
    """
    # Match heredocs:  <<['"]?JSON['"]?  ... JSON  (delimiter "JSON" both ends)
    pattern = re.compile(
        r"<<['\"]?JSON['\"]?\n(?P<body>.*?)\nJSON\b",
        re.DOTALL,
    )
    return [json.loads(m.group("body")) for m in pattern.finditer(skill_md)]


def _curl_blocks(skill_md: str) -> list[str]:
    """Return every fenced ``bash`` block containing ``curl``."""
    return re.findall(r"```bash\n(.*?curl.*?)\n```", skill_md, re.DOTALL)


# ---------------------------------------------------------------------------
# Static contract checks (no network)
# ---------------------------------------------------------------------------


class TestSkillMdStaticContract:
    def test_every_curl_targets_api_reply(self) -> None:
        body = render_discord_reply_skill(thread_id=THREAD_ID, api_url="http://x")
        blocks = _curl_blocks(body)
        assert blocks, "no curl blocks found in SKILL.md"
        for block in blocks:
            assert "/api/reply" in block, f"curl example targets wrong endpoint: {block[:80]!r}"
            # Must be JSON, never multipart.
            assert " -F " not in block, f"multipart leaked into SKILL.md: {block[:120]!r}"
            assert "Content-Type: application/json" in block

    def test_payloads_parse_as_json(self) -> None:
        body = render_discord_reply_skill(thread_id=THREAD_ID, api_url="http://x")
        payloads = _extract_json_payloads(body)
        assert len(payloads) >= 2, "expected at least plain + attachment examples"
        for payload in payloads:
            assert payload["thread_id"] == THREAD_ID
            assert isinstance(payload["content"], str) and payload["content"]
            if "progress_file" in payload:
                assert payload["progress_file"].startswith("/"), (
                    f"progress_file must be an absolute path: {payload['progress_file']!r}"
                )

    def test_bearer_block_present_when_secret_set(self) -> None:
        body = render_discord_reply_skill(
            thread_id=THREAD_ID, api_url="http://x", api_secret="sk-abc"
        )
        for block in _curl_blocks(body):
            assert "Authorization: Bearer sk-abc" in block, (
                "every curl example must include the Bearer header when api_secret is set"
            )


# ---------------------------------------------------------------------------
# Live roundtrip: payload from SKILL.md → real /api/reply
# ---------------------------------------------------------------------------


@pytest.fixture
async def repo() -> NotificationRepository:
    fd, path = tempfile.mkstemp(suffix=".db")
    os.close(fd)
    repo = NotificationRepository(path)
    await repo.init_db()
    yield repo
    os.unlink(path)


@pytest.fixture
def thread_mock() -> MagicMock:
    import discord

    thread = MagicMock(spec=discord.Thread)
    thread.id = THREAD_ID
    thread.send = AsyncMock()
    return thread


@pytest.fixture
def bot_with_thread(thread_mock: MagicMock) -> MagicMock:
    b = MagicMock()
    b.get_channel.return_value = thread_mock
    return b


@pytest.fixture
async def client(
    repo: NotificationRepository, bot_with_thread: MagicMock
) -> TestClient:
    api = ApiServer(repo=repo, bot=bot_with_thread, default_channel_id=1)
    server = TestServer(api.app)
    client = TestClient(server)
    await client.start_server()
    yield client
    await client.close()


@pytest.fixture
async def auth_client(
    repo: NotificationRepository, bot_with_thread: MagicMock
) -> TestClient:
    api = ApiServer(
        repo=repo, bot=bot_with_thread, default_channel_id=1, api_secret="sk-abc"
    )
    server = TestServer(api.app)
    client = TestClient(server)
    await client.start_server()
    yield client
    await client.close()


class TestSkillMdRoundtripsAgainstEndpoint:
    @pytest.mark.asyncio
    async def test_plain_payload_accepted(
        self, client: TestClient, thread_mock: MagicMock
    ) -> None:
        body = render_discord_reply_skill(thread_id=THREAD_ID, api_url="http://x")
        plain, *_ = _extract_json_payloads(body)

        resp = await client.post("/api/reply", json=plain)
        assert resp.status == 200, await resp.text()
        thread_mock.send.assert_called()

    @pytest.mark.asyncio
    async def test_attachment_payload_accepted(
        self,
        client: TestClient,
        thread_mock: MagicMock,
        tmp_path,
    ) -> None:
        body = render_discord_reply_skill(thread_id=THREAD_ID, api_url="http://x")
        payloads = _extract_json_payloads(body)
        attachment_payload = next(p for p in payloads if "progress_file" in p)

        # The template uses /tmp/progress-{thread_id}.txt — create the file so
        # the endpoint can read it.
        real_path = tmp_path / "progress.txt"
        real_path.write_text("contract test detail")
        attachment_payload["progress_file"] = str(real_path)

        resp = await client.post("/api/reply", json=attachment_payload)
        assert resp.status == 200, await resp.text()
        kwargs = thread_mock.send.call_args.kwargs
        assert "file" in kwargs

    @pytest.mark.asyncio
    async def test_bearer_payload_authenticated(
        self, auth_client: TestClient, thread_mock: MagicMock
    ) -> None:
        body = render_discord_reply_skill(
            thread_id=THREAD_ID, api_url="http://x", api_secret="sk-abc"
        )
        plain, *_ = _extract_json_payloads(body)

        # Without the header → 401
        unauth = await auth_client.post("/api/reply", json=plain)
        assert unauth.status == 401

        # With the header (matches what SKILL.md tells Claude to send) → 200
        resp = await auth_client.post(
            "/api/reply",
            json=plain,
            headers={"Authorization": "Bearer sk-abc"},
        )
        assert resp.status == 200, await resp.text()
