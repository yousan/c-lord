"""Peer-UID authorization for the control-plane REST API (#457).

``127.0.0.1`` is a *network* boundary, not a *UID* boundary: any Unix user on
the host can connect to the loopback port and — before this gate existed —
reach ``POST /api/spawn``, which starts a Claude Code session as the bot's
user. These tests pin the boundary the API is supposed to enforce.

The "foreign peer" tests move the *server's* idea of its own UID rather than
the client's, because a test cannot become another Unix user. The connection
is a real TCP connection over loopback and the UID behind it is resolved the
same way as in production (``/proc/net/tcp``), so the gate itself is exercised
end to end — only the expectation is inverted.
"""

from __future__ import annotations

import os
import tempfile
from pathlib import Path
from unittest.mock import AsyncMock, MagicMock

import pytest
from aiohttp.test_utils import TestClient, TestServer

from c_lord.database.notification_repo import NotificationRepository
from c_lord.database.task_repo import TaskRepository
from c_lord.ext.api_server import ApiServer

pytestmark = pytest.mark.skipif(
    not hasattr(os, "getuid"), reason="POSIX-only: the API gate keys on Unix UIDs"
)


@pytest.fixture
async def repo() -> NotificationRepository:
    fd, path = tempfile.mkstemp(suffix=".db")
    os.close(fd)
    repo = NotificationRepository(path)
    await repo.init_db()
    yield repo
    os.unlink(path)


@pytest.fixture
async def task_repo() -> TaskRepository:
    fd, path = tempfile.mkstemp(suffix=".db")
    os.close(fd)
    repo = TaskRepository(path)
    await repo.init_db()
    yield repo
    os.unlink(path)


@pytest.fixture
def bot() -> MagicMock:
    b = MagicMock()
    channel = MagicMock()
    channel.send = AsyncMock()
    b.get_channel.return_value = channel
    b.cogs = {}
    return b


async def _client(api: ApiServer) -> TestClient:
    server = TestServer(api.app)
    client = TestClient(server)
    await client.start_server()
    return client


@pytest.fixture
def foreign_uid(monkeypatch: pytest.MonkeyPatch) -> int:
    """Make the server believe it runs as a *different* Unix user.

    The test process keeps its real UID, so a real loopback request now looks
    like it comes from another user on the host — the #457 threat.
    """
    real_uid = os.getuid()
    assert real_uid != 0, "run the suite as a non-root user; root is allowed by design"
    monkeypatch.setattr(os, "getuid", lambda: real_uid + 1)
    return real_uid + 1


class TestForeignPeerIsRejected:
    """A connection from another UID must not reach any handler."""

    async def test_spawn_is_rejected_before_validation(
        self, repo: NotificationRepository, bot: MagicMock, foreign_uid: int
    ) -> None:
        """The exact request measured on production 2026-09-14.

        ``POST /api/spawn`` with an empty body answered ``400 prompt is
        required`` — i.e. an unauthenticated caller got *through* the front
        door and into request validation. It must stop at the door instead.
        """
        client = await _client(ApiServer(repo=repo, bot=bot, default_channel_id=12345))
        try:
            resp = await client.post("/api/spawn", json={})
            assert resp.status == 403
            assert (await resp.json())["error"]
        finally:
            await client.close()

    async def test_tasks_are_not_readable(
        self,
        repo: NotificationRepository,
        task_repo: TaskRepository,
        bot: MagicMock,
        foreign_uid: int,
    ) -> None:
        """``GET /api/tasks`` leaks scheduled-task prompt bodies."""
        client = await _client(
            ApiServer(repo=repo, bot=bot, default_channel_id=12345, task_repo=task_repo)
        )
        try:
            resp = await client.get("/api/tasks")
            assert resp.status == 403
        finally:
            await client.close()

    async def test_health_does_not_advertise_the_bot(
        self, repo: NotificationRepository, bot: MagicMock, foreign_uid: int
    ) -> None:
        """Health answered 200 to anyone, so a port scan located each c-lord."""
        client = await _client(ApiServer(repo=repo, bot=bot, default_channel_id=12345))
        try:
            resp = await client.get("/api/health")
            assert resp.status == 403
        finally:
            await client.close()

    async def test_notify_cannot_post_to_discord(
        self, repo: NotificationRepository, bot: MagicMock, foreign_uid: int
    ) -> None:
        """``/api/notify`` let another user speak as the bot."""
        client = await _client(ApiServer(repo=repo, bot=bot, default_channel_id=12345))
        try:
            resp = await client.post("/api/notify", json={"message": "hi"})
            assert resp.status == 403
            bot.get_channel.return_value.send.assert_not_called()
        finally:
            await client.close()


class TestOwnUidIsAllowed:
    """Zero-Config: the bot's own user keeps calling the API with no headers."""

    async def test_health_ok(self, repo: NotificationRepository, bot: MagicMock) -> None:
        client = await _client(ApiServer(repo=repo, bot=bot, default_channel_id=12345))
        try:
            resp = await client.get("/api/health")
            assert resp.status == 200
            assert (await resp.json())["status"] == "ok"
        finally:
            await client.close()

    async def test_notify_ok_without_any_credentials(
        self, repo: NotificationRepository, bot: MagicMock
    ) -> None:
        client = await _client(ApiServer(repo=repo, bot=bot, default_channel_id=12345))
        try:
            resp = await client.post("/api/notify", json={"message": "hello"})
            assert resp.status == 200
        finally:
            await client.close()

    async def test_spawn_still_reaches_validation(
        self, repo: NotificationRepository, bot: MagicMock
    ) -> None:
        """Same UID keeps the pre-#457 behaviour (400, not 403)."""
        client = await _client(ApiServer(repo=repo, bot=bot, default_channel_id=12345))
        try:
            resp = await client.post("/api/spawn", json={})
            assert resp.status == 400
        finally:
            await client.close()


class TestSecretOpensTheGate:
    """``CLORD_API_SECRET`` stays the explicit way to let other users in."""

    async def test_valid_bearer_allows_foreign_peer(
        self, repo: NotificationRepository, bot: MagicMock, foreign_uid: int
    ) -> None:
        client = await _client(
            ApiServer(repo=repo, bot=bot, default_channel_id=12345, api_secret="s3cret")
        )
        try:
            resp = await client.get("/api/health", headers={"Authorization": "Bearer s3cret"})
            assert resp.status == 200
        finally:
            await client.close()

    async def test_wrong_bearer_is_401(
        self, repo: NotificationRepository, bot: MagicMock, foreign_uid: int
    ) -> None:
        client = await _client(
            ApiServer(repo=repo, bot=bot, default_channel_id=12345, api_secret="s3cret")
        )
        try:
            resp = await client.get("/api/tasks", headers={"Authorization": "Bearer nope"})
            assert resp.status == 401
        finally:
            await client.close()

    async def test_non_ascii_bearer_is_refused_not_a_500(
        self, repo: NotificationRepository, bot: MagicMock, foreign_uid: int
    ) -> None:
        """``hmac.compare_digest`` rejects non-ASCII strings by raising.

        The token comes straight off the wire, so without a guard any caller
        could turn ``Bearer ñ`` into a 500 and a traceback in the log.
        """
        client = await _client(
            ApiServer(repo=repo, bot=bot, default_channel_id=12345, api_secret="s3cret")
        )
        try:
            resp = await client.get("/api/health", headers={"Authorization": "Bearer ñ"})
            assert resp.status == 401
        finally:
            await client.close()

    async def test_own_uid_needs_no_bearer_even_when_a_secret_is_set(
        self, repo: NotificationRepository, bot: MagicMock
    ) -> None:
        """Same UID can already read the secret, so requiring it buys nothing."""
        client = await _client(
            ApiServer(repo=repo, bot=bot, default_channel_id=12345, api_secret="s3cret")
        )
        try:
            resp = await client.get("/api/health")
            assert resp.status == 200
        finally:
            await client.close()


class TestEscapeHatch:
    """Opening the gate on purpose is possible, and never silent."""

    async def test_allow_any_peer_lets_a_foreign_peer_in(
        self, repo: NotificationRepository, bot: MagicMock, foreign_uid: int
    ) -> None:
        client = await _client(
            ApiServer(repo=repo, bot=bot, default_channel_id=12345, allow_any_peer=True)
        )
        try:
            resp = await client.get("/api/health")
            assert resp.status == 200
        finally:
            await client.close()

    async def test_unresolvable_peer_is_denied_by_default(
        self,
        repo: NotificationRepository,
        bot: MagicMock,
        monkeypatch: pytest.MonkeyPatch,
    ) -> None:
        """No ``/proc`` (non-Linux) means no proof of identity — fail closed."""
        import c_lord.ext.api_server as api_mod

        monkeypatch.setattr(api_mod, "resolve_peer_uid", lambda *a, **kw: None)
        client = await _client(ApiServer(repo=repo, bot=bot, default_channel_id=12345))
        try:
            resp = await client.get("/api/health")
            assert resp.status == 403
        finally:
            await client.close()


class TestResolvePeerUid:
    """The ``/proc/net/tcp`` lookup itself."""

    def test_resolves_a_real_loopback_connection_to_our_own_uid(self) -> None:
        """End-to-end against the live kernel table, no fixtures."""
        import socket

        from c_lord.ext.peer_uid import resolve_peer_uid

        listener = socket.socket(socket.AF_INET, socket.SOCK_STREAM)
        listener.bind(("127.0.0.1", 0))
        listener.listen(1)
        client = socket.create_connection(listener.getsockname())
        conn, peer = listener.accept()
        try:
            uid = resolve_peer_uid(peer, conn.getsockname())
            assert uid == os.getuid()
        finally:
            conn.close()
            client.close()
            listener.close()

    def test_hex_encoding_matches_the_kernel_format(self) -> None:
        from c_lord.ext.peer_uid import encode_address

        assert encode_address("127.0.0.1", 19789) == "0100007F:4D4D"
        assert encode_address("::1", 80) == "00000000000000000000000001000000:0050"
        assert encode_address("::ffff:127.0.0.1", 80) == "0000000000000000FFFF00000100007F:0050"

    def test_unknown_connection_resolves_to_none(self, tmp_path: Path) -> None:
        from c_lord.ext.peer_uid import resolve_peer_uid

        table = tmp_path / "tcp"
        table.write_text(
            "  sl  local_address rem_address   st tx_queue rx_queue tr tm->when "
            "retrnsmt   uid  timeout inode\n"
            "   0: 0100007F:4D4D 0100007F:1F91 01 00000000:00000000 00:00000000 "
            "00000000  1000        0 12345 1 0000000000000000 20 0 0 10 -1\n"
        )
        assert resolve_peer_uid(("127.0.0.1", 19789), ("127.0.0.1", 8081), tables=(table,)) == 1000
        # Same client port, different server port — not our connection.
        assert resolve_peer_uid(("127.0.0.1", 19789), ("127.0.0.1", 9999), tables=(table,)) is None

    def test_a_shared_client_port_is_not_enough_to_identify_a_user(self, tmp_path: Path) -> None:
        """Only the full 4-tuple identifies a connection.

        Another user's socket may hold the same *local* port toward a different
        peer; matching on the client port alone would hand that user's request
        our own UID.
        """
        from c_lord.ext.peer_uid import resolve_peer_uid

        table = tmp_path / "tcp"
        table.write_text(
            "  sl  local_address rem_address   st tx_queue rx_queue tr tm->when "
            "retrnsmt   uid  timeout inode\n"
            "   0: 0100007F:4D4D 0200007F:1F91 01 00000000:00000000 00:00000000 "
            "00000000  1000        0 12345 1 0000000000000000 20 0 0 10 -1\n"
        )
        assert resolve_peer_uid(("127.0.0.1", 19789), ("127.0.0.1", 8081), tables=(table,)) is None

    def test_disagreeing_rows_resolve_to_none(self, tmp_path: Path) -> None:
        """Never guess an identity: disagreement is failure, not a majority vote."""
        from c_lord.ext.peer_uid import resolve_peer_uid

        table = tmp_path / "tcp"
        table.write_text(
            "  sl  local_address rem_address   st tx_queue rx_queue tr tm->when "
            "retrnsmt   uid  timeout inode\n"
            "   0: 0100007F:4D4D 0100007F:1F91 01 00000000:00000000 00:00000000 "
            "00000000  1000        0 12345 1 0000000000000000 20 0 0 10 -1\n"
            "   1: 0100007F:4D4D 0100007F:1F91 01 00000000:00000000 00:00000000 "
            "00000000  1005        0 12346 1 0000000000000000 20 0 0 10 -1\n"
        )
        assert resolve_peer_uid(("127.0.0.1", 19789), ("127.0.0.1", 8081), tables=(table,)) is None
