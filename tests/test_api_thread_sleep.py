"""POST /api/threads/{thread_id}/sleep — staging の release が検証窓を寝かせる入口 (#955).

検証が終わっても、検証用スレッドの Claude は 4 時間スリープ (#572) まで居座る。
``staging.sh release`` はこの API で、bot 自身のスリープ
(``SessionManageCog._sleep_workspace_impl``) を呼ぶ。kill を自前でやらないのは、
ミラーを先に止める (#379) ことと、``set_slept`` で次の発言を無言で復元すること
を、二つ目の実装に写さないため。
"""

from __future__ import annotations

import os
import tempfile
from unittest.mock import AsyncMock, MagicMock

import discord
import pytest
from aiohttp.test_utils import TestClient, TestServer

from c_lord.database.notification_repo import NotificationRepository
from c_lord.ext.api_server import ApiServer
from c_lord.workspace_notice import WorkspaceReason

THREAD = 1565200000000000001


@pytest.fixture
async def repo() -> NotificationRepository:
    fd, path = tempfile.mkstemp(suffix=".db")
    os.close(fd)
    repo = NotificationRepository(path)
    await repo.init_db()
    yield repo
    os.unlink(path)


def _thread() -> MagicMock:
    thread = MagicMock(spec=discord.Thread)
    thread.id = THREAD
    return thread


def _bot(*, impl: AsyncMock | None, thread: object | None) -> MagicMock:
    bot = MagicMock()
    bot.cogs = {}
    if impl is not None:
        cog = MagicMock()
        cog._sleep_workspace_impl = impl
        bot.cogs["SessionManageCog"] = cog
    bot.get_channel.return_value = thread
    bot.fetch_channel = AsyncMock(side_effect=discord.NotFound(MagicMock(status=404), "gone"))
    return bot


async def _client(repo: NotificationRepository, bot: MagicMock) -> TestClient:
    api = ApiServer(repo=repo, bot=bot, default_channel_id=1, host="127.0.0.1", port=0)
    client = TestClient(TestServer(api.app))
    await client.start_server()
    return client


async def test_sleeps_the_thread_through_the_shared_implementation(repo) -> None:
    impl = AsyncMock(return_value=True)
    thread = _thread()
    client = await _client(repo, _bot(impl=impl, thread=thread))
    try:
        resp = await client.post(f"/api/threads/{THREAD}/sleep")
        assert resp.status == 200
        assert (await resp.json()) == {"status": "slept", "thread_id": str(THREAD)}
    finally:
        await client.close()
    impl.assert_awaited_once()
    assert impl.await_args.kwargs["channel"] is thread
    assert impl.await_args.kwargs["reason"] is WorkspaceReason.MANUAL


async def test_a_thread_with_no_window_is_reported_not_resident(repo) -> None:
    client = await _client(repo, _bot(impl=AsyncMock(return_value=False), thread=_thread()))
    try:
        resp = await client.post(f"/api/threads/{THREAD}/sleep")
        assert resp.status == 200
        assert (await resp.json())["status"] == "not_resident"
    finally:
        await client.close()


async def test_an_unreachable_thread_is_404(repo) -> None:
    impl = AsyncMock(return_value=True)
    client = await _client(repo, _bot(impl=impl, thread=None))
    try:
        resp = await client.post(f"/api/threads/{THREAD}/sleep")
        assert resp.status == 404
    finally:
        await client.close()
    impl.assert_not_awaited()


async def test_without_the_session_cog_it_is_503(repo) -> None:
    client = await _client(repo, _bot(impl=None, thread=_thread()))
    try:
        resp = await client.post(f"/api/threads/{THREAD}/sleep")
        assert resp.status == 503
    finally:
        await client.close()


async def test_a_non_numeric_id_is_400(repo) -> None:
    client = await _client(repo, _bot(impl=AsyncMock(return_value=True), thread=_thread()))
    try:
        resp = await client.post("/api/threads/abc/sleep")
        assert resp.status == 400
    finally:
        await client.close()


async def test_a_sleep_that_raises_is_500_not_a_crash(repo) -> None:
    impl = AsyncMock(side_effect=RuntimeError("tmux gone"))
    client = await _client(repo, _bot(impl=impl, thread=_thread()))
    try:
        resp = await client.post(f"/api/threads/{THREAD}/sleep")
        assert resp.status == 500
    finally:
        await client.close()
