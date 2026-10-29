"""Pin the REST API's error paths and status codes that callers depend on.

``c_lord/ext/api_server.py`` is the control plane (CLAUDE.md, Key Design
Decision 7): Claude sessions, CI and ``scripts/staging.sh`` call it with
``curl $CLORD_API_URL/api/...`` and branch on the status code. The endpoint
table and auth rules live in ``docs/COMMANDS.md`` → "REST API". The happy paths
are pinned in ``tests/test_api_server.py`` / ``test_api_tasks.py`` /
``test_api_peer_auth.py``; this file pins the refusals and fallbacks next to
them, each against the docstring or issue that defines it.

Every server here is an in-process ``aiohttp`` ``TestServer`` on an unused
loopback port — no production port, DB, ``.env`` or Discord.
"""

from __future__ import annotations

import logging
import os
from pathlib import Path
from types import SimpleNamespace
from unittest.mock import AsyncMock, MagicMock

import discord
import pytest
from aiohttp.test_utils import TestClient, TestServer

import c_lord.ext.api_server as api_mod
from c_lord.database.notification_repo import NotificationRepository
from c_lord.database.task_repo import TaskRepository
from c_lord.ext.api_server import ApiServer, _bound_port

pytestmark = pytest.mark.skipif(
    not hasattr(os, "getuid"), reason="POSIX-only: the API gate keys on Unix UIDs"
)


@pytest.fixture
async def repo(tmp_path: Path) -> NotificationRepository:
    r = NotificationRepository(str(tmp_path / "notifications.db"))
    await r.init_db()
    return r


@pytest.fixture
async def task_repo(tmp_path: Path) -> TaskRepository:
    r = TaskRepository(str(tmp_path / "tasks.db"))
    await r.init_db()
    return r


@pytest.fixture
def channel() -> MagicMock:
    ch = MagicMock(spec=discord.Thread)
    ch.id = 777000111
    ch.send = AsyncMock()
    return ch


@pytest.fixture
def bot(channel: MagicMock) -> MagicMock:
    b = MagicMock()
    b.get_channel.return_value = channel
    b.fetch_channel = AsyncMock(return_value=channel)
    b.cogs = {}
    return b


@pytest.fixture
async def make_client():
    clients: list[TestClient] = []

    async def _make(api: ApiServer) -> TestClient:
        client = TestClient(TestServer(api.app))
        await client.start_server()
        clients.append(client)
        return client

    yield _make
    for c in clients:
        await c.close()


@pytest.fixture
def foreign_uid(monkeypatch: pytest.MonkeyPatch) -> int:
    """Make the server believe it runs as another Unix user (same trick as
    ``tests/test_api_peer_auth.py``): the real loopback caller is now foreign."""
    real_uid = os.getuid()
    assert real_uid != 0, "run the suite as a non-root user; root is allowed by design"
    monkeypatch.setattr(os, "getuid", lambda: real_uid + 1)
    return real_uid + 1


# ---------------------------------------------------------------------------
# Auth (#457) — docs/COMMANDS.md "Auth (#457)"
# ---------------------------------------------------------------------------


class TestAuthGate:
    async def test_bearer_is_ignored_when_no_secret_is_configured(
        self, repo: NotificationRepository, bot: MagicMock, foreign_uid: int, make_client
    ) -> None:
        """Without CLORD_API_SECRET there is no token to hold, so a foreign
        caller's ``Bearer`` header must not open the gate (#457: the secret is
        "the explicit way to let someone else in" — no secret, no way in)."""
        client = await make_client(ApiServer(repo=repo, bot=bot, default_channel_id=1))
        resp = await client.post(
            "/api/notify",
            json={"message": "x"},
            headers={"Authorization": "Bearer anything"},
        )
        assert resp.status == 403
        bot.get_channel.return_value.send.assert_not_called()

    async def test_empty_secret_is_the_same_as_no_secret(
        self, repo: NotificationRepository, bot: MagicMock, foreign_uid: int, make_client
    ) -> None:
        """``CLORD_API_SECRET=`` (empty) must not make ``Bearer `` (empty token)
        a valid credential for a foreign peer (#457 / ``_authorize``: the bearer
        branch only runs ``if self.api_secret``)."""
        client = await make_client(
            ApiServer(repo=repo, bot=bot, default_channel_id=1, api_secret="")
        )
        resp = await client.get("/api/health", headers={"Authorization": "Bearer "})
        assert resp.status == 403

    async def test_non_bearer_scheme_from_a_foreign_peer_is_403(
        self, repo: NotificationRepository, bot: MagicMock, foreign_uid: int, make_client
    ) -> None:
        """Only ``Authorization: Bearer <secret>`` is a credential (docs/COMMANDS.md).
        ``Basic <secret>`` from another user is just an unauthenticated caller."""
        client = await make_client(
            ApiServer(repo=repo, bot=bot, default_channel_id=1, api_secret="s3cret")
        )
        resp = await client.get("/api/health", headers={"Authorization": "Basic s3cret"})
        assert resp.status == 403

    async def test_wrong_bearer_is_401_even_for_the_bots_own_user(
        self, repo: NotificationRepository, bot: MagicMock, make_client
    ) -> None:
        """A presented-but-wrong token is checked before the UID (``_authorize``):
        the caller asked to be judged by the token, and gets 401 — not silently
        let in because it happens to share the UID. Current behaviour since #457."""
        client = await make_client(
            ApiServer(repo=repo, bot=bot, default_channel_id=1, api_secret="s3cret")
        )
        resp = await client.get("/api/health", headers={"Authorization": "Bearer nope"})
        assert resp.status == 401
        assert (await resp.json()) == {"error": "Invalid token"}

    async def test_refusal_body_does_not_identify_c_lord(
        self, repo: NotificationRepository, bot: MagicMock, foreign_uid: int, make_client
    ) -> None:
        """#457: "a foreign caller should not be able to tell a c-lord from any
        other 403 on the host" — the body is a bare ``Forbidden``."""
        client = await make_client(ApiServer(repo=repo, bot=bot, default_channel_id=1))
        resp = await client.get("/api/health")
        assert resp.status == 403
        assert (await resp.json()) == {"error": "Forbidden"}

    async def test_denial_is_logged_for_the_operator(
        self,
        repo: NotificationRepository,
        bot: MagicMock,
        foreign_uid: int,
        make_client,
        caplog: pytest.LogCaptureFixture,
    ) -> None:
        """#457 / #678: the refusal says nothing to the caller, so the operator
        gets a WARNING naming the path and how to let another user in."""
        client = await make_client(ApiServer(repo=repo, bot=bot, default_channel_id=1))
        with caplog.at_level(logging.WARNING, logger="c_lord.ext.api_server"):
            await client.post("/api/spawn", json={"prompt": "x"})
        msgs = [r.getMessage() for r in caplog.records]
        assert any("REST API denied POST /api/spawn" in m for m in msgs)
        assert any("CLORD_API_SECRET" in m for m in msgs)

    def test_request_without_a_transport_is_unprovable_and_denied(
        self, repo: NotificationRepository, bot: MagicMock
    ) -> None:
        """No socket → no peer UID → fail closed (#457, ``_peer_uid`` docstring:
        "``None`` when unprovable")."""
        api = ApiServer(repo=repo, bot=bot, default_channel_id=1)
        request = MagicMock()
        request.headers = {}
        request.transport = None
        request.method = "GET"
        request.path = "/api/health"
        denial = api._authorize(request)
        assert denial is not None
        assert denial.status == 403


class TestGateSummaryAtStart:
    """#457: "who may drive the control plane is never left implicit — an
    opened gate says so at every start". ``start()`` binds a real port here,
    so the port is OS-chosen (0) on loopback."""

    async def _start_and_log(
        self, api: ApiServer, caplog: pytest.LogCaptureFixture
    ) -> list[logging.LogRecord]:
        api.port = 0
        with caplog.at_level(logging.INFO, logger="c_lord.ext.api_server"):
            await api.start()
            await api.stop()
        return [r for r in caplog.records if "REST API access" in r.getMessage()]

    async def test_allow_any_peer_is_a_warning(
        self, repo: NotificationRepository, bot: MagicMock, caplog: pytest.LogCaptureFixture
    ) -> None:
        api = ApiServer(repo=repo, bot=bot, host="127.0.0.1", allow_any_peer=True)
        (rec,) = await self._start_and_log(api, caplog)
        assert rec.levelno == logging.WARNING
        assert "CLORD_API_ALLOW_ANY_PEER=1" in rec.getMessage()

    async def test_unverifiable_platform_is_a_warning(
        self,
        repo: NotificationRepository,
        bot: MagicMock,
        caplog: pytest.LogCaptureFixture,
        monkeypatch: pytest.MonkeyPatch,
    ) -> None:
        """No ``os.getuid`` (``_current_uid`` → None): only secret holders are served."""
        monkeypatch.setattr(api_mod, "_current_uid", lambda: None)
        api = ApiServer(repo=repo, bot=bot, host="127.0.0.1")
        (rec,) = await self._start_and_log(api, caplog)
        assert rec.levelno == logging.WARNING
        assert "peer UID cannot be verified" in rec.getMessage()

    async def test_normal_gate_is_info_and_names_the_uid(
        self, repo: NotificationRepository, bot: MagicMock, caplog: pytest.LogCaptureFixture
    ) -> None:
        api = ApiServer(repo=repo, bot=bot, host="127.0.0.1", api_secret="s")
        (rec,) = await self._start_and_log(api, caplog)
        assert rec.levelno == logging.INFO
        assert f"serving uid={os.getuid()} only, or holding CLORD_API_SECRET" in rec.getMessage()


def test_bound_port_is_none_for_a_site_that_is_not_listening() -> None:
    """#258: ``_bound_port`` reports the real port; a site with no sockets has none
    (the caller then falls back to the port it asked for)."""
    site = SimpleNamespace(_server=None)
    assert _bound_port(site) is None  # type: ignore[arg-type]


# ---------------------------------------------------------------------------
# /api/notify — docs/COMMANDS.md "Send a notification"
# ---------------------------------------------------------------------------


class TestNotifyErrors:
    async def test_uncached_channel_is_fetched(
        self, repo: NotificationRepository, bot: MagicMock, channel: MagicMock, make_client
    ) -> None:
        """A channel not in the gateway cache is fetched over REST, then posted to."""
        bot.get_channel.return_value = None
        client = await make_client(ApiServer(repo=repo, bot=bot, default_channel_id=42))
        resp = await client.post(
            "/api/notify", json={"message": "Build complete!", "title": "CI", "color": 0x123456}
        )
        assert resp.status == 200
        bot.fetch_channel.assert_awaited_once_with(42)
        embed = channel.send.call_args.kwargs["embed"]
        assert embed.title == "CI"
        assert embed.description == "Build complete!"
        assert embed.color.value == 0x123456

    async def test_unreachable_channel_is_500_with_the_reason(
        self, repo: NotificationRepository, bot: MagicMock, make_client
    ) -> None:
        bot.get_channel.return_value = None
        bot.fetch_channel = AsyncMock(side_effect=RuntimeError("Unknown Channel"))
        client = await make_client(ApiServer(repo=repo, bot=bot, default_channel_id=42))
        resp = await client.post("/api/notify", json={"message": "x"})
        assert resp.status == 500
        assert (await resp.json()) == {"error": "Unknown Channel"}

    async def test_non_messageable_channel_is_400(
        self, repo: NotificationRepository, bot: MagicMock, make_client
    ) -> None:
        bot.get_channel.return_value = object()  # e.g. a CategoryChannel: no .send
        client = await make_client(ApiServer(repo=repo, bot=bot, default_channel_id=42))
        resp = await client.post("/api/notify", json={"message": "x"})
        assert resp.status == 400
        assert (await resp.json()) == {"error": "Channel is not messageable"}

    async def test_default_embed_title_and_colour(
        self, repo: NotificationRepository, bot: MagicMock, channel: MagicMock, make_client
    ) -> None:
        client = await make_client(ApiServer(repo=repo, bot=bot, default_channel_id=42))
        await client.post("/api/notify", json={"message": "x"})
        embed = channel.send.call_args.kwargs["embed"]
        assert embed.title == "Notification"
        assert embed.color.value == 0x00BFFF


class TestScheduleErrors:
    async def test_invalid_json_is_400(
        self, repo: NotificationRepository, bot: MagicMock, make_client
    ) -> None:
        client = await make_client(ApiServer(repo=repo, bot=bot))
        resp = await client.post(
            "/api/schedule", data="not json", headers={"Content-Type": "application/json"}
        )
        assert resp.status == 400
        assert (await resp.json()) == {"error": "Invalid JSON"}

    async def test_scheduled_at_is_normalised_to_seconds(
        self, repo: NotificationRepository, bot: MagicMock, make_client
    ) -> None:
        """Stored as ``%Y-%m-%dT%H:%M:%S`` so the poller can compare strings."""
        client = await make_client(ApiServer(repo=repo, bot=bot))
        resp = await client.post(
            "/api/schedule",
            json={"message": "m", "scheduled_at": "2030-01-02T03:04:05.678"},
        )
        assert resp.status == 200
        listed = await (await client.get("/api/scheduled")).json()
        assert listed["notifications"][0]["scheduled_at"] == "2030-01-02T03:04:05"


# ---------------------------------------------------------------------------
# /api/tasks — docs/COMMANDS.md table; Key Design Decision 8 (runtime tasks)
# ---------------------------------------------------------------------------


class TestTasksWithoutScheduler:
    """``_require_task_repo``: an instance without SchedulerCog answers 503 on
    every task route, so ``curl`` gets a reason instead of a crash."""

    @pytest.mark.parametrize(
        ("method", "path"),
        [
            ("POST", "/api/tasks"),
            ("GET", "/api/tasks"),
            ("DELETE", "/api/tasks/1"),
            ("PATCH", "/api/tasks/1"),
        ],
    )
    async def test_503(
        self,
        repo: NotificationRepository,
        bot: MagicMock,
        make_client,
        method: str,
        path: str,
    ) -> None:
        client = await make_client(ApiServer(repo=repo, bot=bot))
        resp = await client.request(method, path, json={})
        assert resp.status == 503
        assert "task_repo is None" in (await resp.json())["error"]


class TestTasksErrors:
    @pytest.fixture
    async def client(
        self, repo: NotificationRepository, task_repo: TaskRepository, bot: MagicMock, make_client
    ) -> TestClient:
        return await make_client(ApiServer(repo=repo, bot=bot, task_repo=task_repo))

    async def _create(self, client: TestClient, name: str = "daily-review") -> int:
        resp = await client.post(
            "/api/tasks",
            json={
                "name": name,
                "prompt": "Check for open PRs and post a summary",
                "interval_seconds": 86400,
                "channel_id": 123456789,
            },
        )
        assert resp.status == 201
        return (await resp.json())["id"]

    @pytest.mark.parametrize("method", ["DELETE", "PATCH"])
    async def test_non_integer_id_is_400(self, client: TestClient, method: str) -> None:
        resp = await client.request(method, "/api/tasks/abc", json={"enabled": False})
        assert resp.status == 400
        assert (await resp.json()) == {"error": "Invalid ID"}

    async def test_patch_invalid_json_is_400(self, client: TestClient) -> None:
        task_id = await self._create(client)
        resp = await client.patch(
            f"/api/tasks/{task_id}", data="{", headers={"Content-Type": "application/json"}
        )
        assert resp.status == 400
        assert (await resp.json()) == {"error": "Invalid JSON"}

    async def test_patch_interval_and_working_dir(
        self, client: TestClient, task_repo: TaskRepository
    ) -> None:
        """docs/COMMANDS.md: "Update a task (enable/disable, prompt, interval)"."""
        task_id = await self._create(client)
        resp = await client.patch(
            f"/api/tasks/{task_id}", json={"interval_seconds": 3600, "working_dir": "/srv/repo"}
        )
        assert resp.status == 200
        (task,) = await task_repo.get_all()
        assert task["interval_seconds"] == 3600
        assert task["working_dir"] == "/srv/repo"

    async def test_patch_with_no_known_field_is_404(self, client: TestClient) -> None:
        """Nothing to update reads as "not found" — current behaviour: the
        handler only knows whether a row changed."""
        task_id = await self._create(client)
        resp = await client.patch(f"/api/tasks/{task_id}", json={"colour": "red"})
        assert resp.status == 404

    async def test_create_with_zero_interval_is_rejected_as_missing(
        self, client: TestClient
    ) -> None:
        """``interval_seconds: 0`` would fire every 30 s master-loop tick; the
        required-field check treats it as missing (``not data.get(field)``)."""
        resp = await client.post(
            "/api/tasks",
            json={"name": "n", "prompt": "p", "interval_seconds": 0, "channel_id": 1},
        )
        assert resp.status == 400
        assert (await resp.json()) == {"error": "interval_seconds is required"}

    async def test_run_immediately_false_is_stored(
        self, client: TestClient, task_repo: TaskRepository
    ) -> None:
        """``run_immediately`` (default true) — false defers the first run by one
        interval (``create_task`` docstring)."""
        resp = await client.post(
            "/api/tasks",
            json={
                "name": "later",
                "prompt": "p",
                "interval_seconds": 600,
                "channel_id": 1,
                "run_immediately": False,
            },
        )
        assert resp.status == 201
        (task,) = await task_repo.get_all()
        assert task["next_run_at"] > task["created_at"] + 500


# ---------------------------------------------------------------------------
# /api/spawn — docs/COMMANDS.md "Spawn a new session"
# ---------------------------------------------------------------------------


class TestSpawnErrors:
    @pytest.fixture
    def text_channel(self) -> MagicMock:
        ch = MagicMock(spec=discord.TextChannel)
        ch.send = AsyncMock()
        return ch

    @pytest.fixture
    def cog(self) -> MagicMock:
        thread = MagicMock()
        thread.id = 1
        thread.name = "t"
        c = MagicMock()
        c.spawn_session = AsyncMock(return_value=thread)
        return c

    async def test_non_integer_channel_id_is_400(
        self, repo: NotificationRepository, bot: MagicMock, cog: MagicMock, make_client
    ) -> None:
        bot.cogs = {"ClaudeChatCog": cog}
        client = await make_client(ApiServer(repo=repo, bot=bot))
        resp = await client.post("/api/spawn", json={"prompt": "p", "channel_id": "general"})
        assert resp.status == 400
        assert (await resp.json()) == {"error": "channel_id must be an integer"}
        cog.spawn_session.assert_not_called()

    async def test_uncached_channel_is_fetched(
        self,
        repo: NotificationRepository,
        bot: MagicMock,
        cog: MagicMock,
        text_channel: MagicMock,
        make_client,
    ) -> None:
        bot.cogs = {"ClaudeChatCog": cog}
        bot.get_channel.return_value = None
        bot.fetch_channel = AsyncMock(return_value=text_channel)
        client = await make_client(ApiServer(repo=repo, bot=bot))
        resp = await client.post("/api/spawn", json={"prompt": "p", "channel_id": "55"})
        assert resp.status == 201
        bot.fetch_channel.assert_awaited_once_with(55)
        assert cog.spawn_session.call_args.args[0] is text_channel

    async def test_unreachable_channel_is_500(
        self, repo: NotificationRepository, bot: MagicMock, cog: MagicMock, make_client
    ) -> None:
        bot.cogs = {"ClaudeChatCog": cog}
        bot.get_channel.return_value = None
        bot.fetch_channel = AsyncMock(side_effect=RuntimeError("Missing Access"))
        client = await make_client(ApiServer(repo=repo, bot=bot, default_channel_id=55))
        resp = await client.post("/api/spawn", json={"prompt": "p"})
        assert resp.status == 500
        assert (await resp.json()) == {"error": "Missing Access"}

    async def test_a_thread_is_not_a_spawn_target(
        self, repo: NotificationRepository, bot: MagicMock, cog: MagicMock, make_client
    ) -> None:
        """Spawn creates a thread, so the parent must be a TextChannel. Passing a
        thread id (an easy mistake from inside a session) is a 400, not a crash."""
        bot.cogs = {"ClaudeChatCog": cog}
        client = await make_client(ApiServer(repo=repo, bot=bot, default_channel_id=55))
        resp = await client.post("/api/spawn", json={"prompt": "p"})
        assert resp.status == 400
        assert "text channel" in (await resp.json())["error"]
        cog.spawn_session.assert_not_called()

    async def test_spawn_failure_is_500_and_logged(
        self,
        repo: NotificationRepository,
        bot: MagicMock,
        cog: MagicMock,
        text_channel: MagicMock,
        make_client,
        caplog: pytest.LogCaptureFixture,
    ) -> None:
        bot.cogs = {"ClaudeChatCog": cog}
        bot.get_channel.return_value = text_channel
        cog.spawn_session = AsyncMock(side_effect=RuntimeError("tmux window failed"))
        client = await make_client(ApiServer(repo=repo, bot=bot, default_channel_id=55))
        with caplog.at_level(logging.ERROR, logger="c_lord.ext.api_server"):
            resp = await client.post("/api/spawn", json={"prompt": "p"})
        assert resp.status == 500
        assert (await resp.json()) == {"error": "tmux window failed"}
        assert any("spawn_session failed" in r.getMessage() for r in caplog.records)


# ---------------------------------------------------------------------------
# /api/mark-resume — docstring: session_id auto-resolved from sessions table
# ---------------------------------------------------------------------------


class TestMarkResumeSessionLookup:
    @pytest.fixture
    def resume_repo(self) -> MagicMock:
        r = MagicMock()
        r.mark = AsyncMock(return_value=7)
        return r

    async def test_session_id_is_filled_from_the_sessions_table(
        self, repo: NotificationRepository, bot: MagicMock, resume_repo: MagicMock, make_client
    ) -> None:
        """So the restarted bot resumes with ``--resume <uuid>`` (#773) even when
        the caller only knew the thread."""
        session_repo = MagicMock()
        session_repo.get = AsyncMock(return_value=SimpleNamespace(session_id="abc-123"))
        client = await make_client(
            ApiServer(repo=repo, bot=bot, resume_repo=resume_repo, session_repo=session_repo)
        )
        resp = await client.post("/api/mark-resume", json={"thread_id": "42"})
        assert resp.status == 201
        assert (await resp.json()) == {"status": "marked", "id": 7}
        session_repo.get.assert_awaited_once_with(42)
        assert resume_repo.mark.call_args.kwargs["session_id"] == "abc-123"
        assert resume_repo.mark.call_args.kwargs["reason"] == "self_restart"

    async def test_lookup_failure_still_marks_without_session(
        self,
        repo: NotificationRepository,
        bot: MagicMock,
        resume_repo: MagicMock,
        make_client,
        caplog: pytest.LogCaptureFixture,
    ) -> None:
        """The marker matters more than the session id: a DB hiccup is a WARNING,
        not a lost resume."""
        session_repo = MagicMock()
        session_repo.get = AsyncMock(side_effect=RuntimeError("database is locked"))
        client = await make_client(
            ApiServer(repo=repo, bot=bot, resume_repo=resume_repo, session_repo=session_repo)
        )
        with caplog.at_level(logging.WARNING, logger="c_lord.ext.api_server"):
            resp = await client.post("/api/mark-resume", json={"thread_id": 42})
        assert resp.status == 201
        assert resume_repo.mark.call_args.kwargs["session_id"] is None
        assert any("auto-resolve session_id" in r.getMessage() for r in caplog.records)

    async def test_explicit_session_id_skips_the_lookup(
        self, repo: NotificationRepository, bot: MagicMock, resume_repo: MagicMock, make_client
    ) -> None:
        session_repo = MagicMock()
        session_repo.get = AsyncMock()
        client = await make_client(
            ApiServer(repo=repo, bot=bot, resume_repo=resume_repo, session_repo=session_repo)
        )
        await client.post("/api/mark-resume", json={"thread_id": 42, "session_id": "given"})
        session_repo.get.assert_not_called()
        assert resume_repo.mark.call_args.kwargs["session_id"] == "given"


# ---------------------------------------------------------------------------
# /api/threads/{id}/messages — #14, docs/COMMANDS.md "Forward CLI input"
# ---------------------------------------------------------------------------


class TestThreadMessage:
    async def test_uncached_thread_is_fetched_and_prefixed(
        self, repo: NotificationRepository, bot: MagicMock, channel: MagicMock, make_client
    ) -> None:
        bot.get_channel.return_value = None
        client = await make_client(ApiServer(repo=repo, bot=bot))
        resp = await client.post(
            "/api/threads/123456789/messages", json={"content": "  check coverage  "}
        )
        assert resp.status == 200
        bot.fetch_channel.assert_awaited_once_with(123456789)
        channel.send.assert_awaited_once_with("-# \U0001f4bb (cli) check coverage")

    async def test_blank_source_falls_back_to_cli(
        self, repo: NotificationRepository, bot: MagicMock, channel: MagicMock, make_client
    ) -> None:
        client = await make_client(ApiServer(repo=repo, bot=bot))
        await client.post("/api/threads/1/messages", json={"content": "hi", "source": "   "})
        channel.send.assert_awaited_once_with("-# \U0001f4bb (cli) hi")

    async def test_whitespace_only_content_is_400(
        self, repo: NotificationRepository, bot: MagicMock, channel: MagicMock, make_client
    ) -> None:
        client = await make_client(ApiServer(repo=repo, bot=bot))
        resp = await client.post("/api/threads/1/messages", json={"content": " \n\t "})
        assert resp.status == 400
        channel.send.assert_not_called()


# ---------------------------------------------------------------------------
# /api/reply (#52/#235) and /api/prompt-choice (#63) — "400 on validation errors"
# ---------------------------------------------------------------------------


class TestReplyErrors:
    async def test_non_integer_thread_id_is_400(
        self, repo: NotificationRepository, bot: MagicMock, make_client
    ) -> None:
        client = await make_client(ApiServer(repo=repo, bot=bot))
        resp = await client.post("/api/reply", json={"thread_id": "abc", "content": "x"})
        assert resp.status == 400
        assert (await resp.json()) == {"error": "thread_id must be an integer"}

    async def test_non_messageable_is_400(
        self, repo: NotificationRepository, bot: MagicMock, make_client
    ) -> None:
        bot.get_channel.return_value = object()
        client = await make_client(ApiServer(repo=repo, bot=bot))
        resp = await client.post("/api/reply", json={"thread_id": 1, "content": "x"})
        assert resp.status == 400
        assert (await resp.json()) == {"error": "Channel is not messageable"}

    async def test_several_files_ride_on_the_last_chunk_as_files(
        self,
        repo: NotificationRepository,
        bot: MagicMock,
        channel: MagicMock,
        make_client,
        tmp_path: Path,
        monkeypatch: pytest.MonkeyPatch,
    ) -> None:
        """Table images (#82) plus progress.txt (#38) are sent together via
        ``files=`` — Discord's ``file=`` takes only one."""
        import c_lord.discord_ui.table_renderer as tr

        monkeypatch.setattr(tr, "get_table_images", lambda content: [("table1.png", b"PNG")])
        progress = tmp_path / "progress.txt"
        progress.write_text("detail")
        api = ApiServer(repo=repo, bot=bot)
        api.session_dir_base = str(tmp_path)  # progress_file は session dir の下だけ (#1039)
        client = await make_client(api)
        resp = await client.post(
            "/api/reply",
            json={"thread_id": 1, "content": "answer", "progress_file": str(progress)},
        )
        assert resp.status == 200
        kwargs = channel.send.call_args.kwargs
        assert "file" not in kwargs
        assert [f.filename for f in kwargs["files"]] == ["table1.png", "progress.txt"]


class TestPromptChoiceErrors:
    @pytest.fixture
    async def client(self, repo: NotificationRepository, bot: MagicMock, make_client) -> TestClient:
        return await make_client(ApiServer(repo=repo, bot=bot))

    async def test_non_integer_thread_id_is_400(self, client: TestClient) -> None:
        resp = await client.post(
            "/api/prompt-choice",
            json={"thread_id": "x", "question": "q", "choices": [{"label": "a"}]},
        )
        assert resp.status == 400
        assert (await resp.json()) == {"error": "thread_id must be an integer"}

    async def test_choice_that_is_not_an_object_is_400(self, client: TestClient) -> None:
        resp = await client.post(
            "/api/prompt-choice",
            json={"thread_id": 1, "question": "q", "choices": [{"label": "a"}, "b"]},
        )
        assert resp.status == 400
        assert (await resp.json()) == {"error": "choices[1] must be an object"}

    async def test_choice_without_label_is_400(
        self, client: TestClient, channel: MagicMock
    ) -> None:
        resp = await client.post(
            "/api/prompt-choice",
            json={"thread_id": 1, "question": "q", "choices": [{"description": "d"}]},
        )
        assert resp.status == 400
        assert (await resp.json()) == {"error": "choices[0].label is required"}
        channel.send.assert_not_called()

    async def test_non_messageable_is_400(
        self, repo: NotificationRepository, bot: MagicMock, make_client
    ) -> None:
        bot.get_channel.return_value = object()
        client = await make_client(ApiServer(repo=repo, bot=bot))
        resp = await client.post(
            "/api/prompt-choice",
            json={"thread_id": 1, "question": "q", "choices": [{"label": "a"}]},
        )
        assert resp.status == 400


# ---------------------------------------------------------------------------
# /api/lounge forwarding (#758: no component posts here, but the route exists)
# ---------------------------------------------------------------------------


class TestLoungeForwarding:
    @pytest.fixture
    def lounge_repo(self) -> MagicMock:
        r = MagicMock()
        r.post = AsyncMock(
            return_value=SimpleNamespace(
                id=1, label="AI", message="hi", posted_at="2026-10-29T12:34:56"
            )
        )
        return r

    async def test_message_is_forwarded_with_hh_mm(
        self,
        repo: NotificationRepository,
        bot: MagicMock,
        channel: MagicMock,
        lounge_repo: MagicMock,
        make_client,
    ) -> None:
        client = await make_client(
            ApiServer(repo=repo, bot=bot, lounge_repo=lounge_repo, lounge_channel_id=9)
        )
        resp = await client.post("/api/lounge", json={"message": "hi"})
        assert resp.status == 201
        bot.get_channel.assert_called_with(9)
        channel.send.assert_awaited_once_with("**[AI]** hi *(12:34)*")

    async def test_discord_failure_does_not_fail_the_post(
        self,
        repo: NotificationRepository,
        bot: MagicMock,
        lounge_repo: MagicMock,
        make_client,
    ) -> None:
        """The message is already stored; a Discord outage is a WARNING, not a 500."""
        bot.get_channel.return_value = None
        bot.fetch_channel = AsyncMock(side_effect=RuntimeError("down"))
        client = await make_client(
            ApiServer(repo=repo, bot=bot, lounge_repo=lounge_repo, lounge_channel_id=9)
        )
        resp = await client.post("/api/lounge", json={"message": "hi"})
        assert resp.status == 201
