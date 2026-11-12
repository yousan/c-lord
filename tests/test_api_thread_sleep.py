"""POST /api/threads/{thread_id}/sleep — staging の release が検証窓を寝かせる入口 (#955).

検証が終わっても、検証用スレッドの Claude は 4 時間スリープ (#572) まで居座る。
``staging.sh release`` はこの API で、bot 自身のスリープ
(``SessionManageCog._sleep_workspace_impl``) を呼ぶ。kill を自前でやらないのは、
ミラーを先に止める (#379) ことと、``set_slept`` で次の発言を無言で復元すること
を、二つ目の実装に写さないため。

#1214: 眠らせ損ねた（kill が失敗して窓が残っているかもしれない、``False``）ときも
``not_resident`` と返していた。``release`` はそれを「bot はその窓を持っていない」と読むので、
残った常駐に誰も気づかない。眠らせ損ねは ``not_resident`` と区別して返す。
"""

from __future__ import annotations

import os
import tempfile
from unittest.mock import AsyncMock, MagicMock, patch

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
    # ``None`` is the sleep's "there was no window" (#1192); ``False`` is a failure (#1214).
    client = await _client(repo, _bot(impl=AsyncMock(return_value=None), thread=_thread()))
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


async def test_a_failed_kill_is_not_reported_not_resident(repo) -> None:
    """#1214: ``False`` (the window may still be there) must not read as "no window"."""
    client = await _client(repo, _bot(impl=AsyncMock(return_value=False), thread=_thread()))
    try:
        resp = await client.post(f"/api/threads/{THREAD}/sleep")
        body = await resp.json()
        assert body.get("status") != "not_resident", "a failed kill was reported as not_resident"
        assert resp.status == 503
        assert body == {"status": "failed", "thread_id": str(THREAD)}
    finally:
        await client.close()


# --- independent review of #1214: only a failed kill is ``failed`` ---
#
# ``staging.sh release`` closes a pane by id when the bot answers
# ``not_resident``. A window in another tmux session of ours (binding changed,
# not moved yet), a channel that is not a thread, or no tmux manager say nothing
# about a window *this* sleep could kill: answered ``not_resident`` as before
# #1214, so release still closes the pane. The resident cap keeps making up for
# them (``False``) — that is the impl's default, unchanged.


def _real_cog(mgr: object) -> object:
    from c_lord.cogs.session_manage import SessionManageCog

    cog = SessionManageCog.__new__(SessionManageCog)
    cog._resolve_tmux_manager = AsyncMock(return_value=mgr)  # type: ignore[method-assign]
    cog._stop_transcript_mirror = AsyncMock()  # type: ignore[method-assign]
    cog.repo = MagicMock()
    cog.repo.set_slept = AsyncMock()
    return cog


def _real_bot(cog: object, channel: object) -> MagicMock:
    bot = MagicMock()
    bot.cogs = {"SessionManageCog": cog}
    bot.get_channel.return_value = channel
    return bot


def _mgr(*, present: list[bool | None], killed: bool = False) -> MagicMock:
    mgr = MagicMock()
    mgr.window_present = MagicMock(side_effect=present)
    mgr.kill_session = MagicMock(return_value=killed)
    return mgr


def _real_thread() -> MagicMock:
    thread = _thread()
    thread.parent_id = 10
    return thread


async def _post(repo: NotificationRepository, bot: MagicMock) -> tuple[int, dict]:
    client = await _client(repo, bot)
    try:
        resp = await client.post(f"/api/threads/{THREAD}/sleep")
        return resp.status, await resp.json()
    finally:
        await client.close()


async def test_window_in_another_session_of_ours_is_not_resident(repo) -> None:
    cog = _real_cog(_mgr(present=[False]))
    with patch("c_lord.tmux.is_resident_anywhere", return_value=True):
        status, body = await _post(repo, _real_bot(cog, _real_thread()))
    assert (status, body.get("status")) == (200, "not_resident"), body


async def test_a_channel_that_is_not_a_thread_is_not_resident(repo) -> None:
    status, body = await _post(repo, _real_bot(_real_cog(_mgr(present=[True])), MagicMock()))
    assert (status, body.get("status")) == (200, "not_resident"), body


async def test_no_tmux_manager_is_not_resident(repo) -> None:
    status, body = await _post(repo, _real_bot(_real_cog(None), _real_thread()))
    assert (status, body.get("status")) == (200, "not_resident"), body


async def test_a_real_failed_kill_is_still_failed(repo) -> None:
    cog = _real_cog(_mgr(present=[True, True]))
    with patch("c_lord.tmux.is_resident_anywhere", return_value=True):
        status, body = await _post(repo, _real_bot(cog, _real_thread()))
    assert (status, body.get("status")) == (503, "failed"), body


async def test_tmux_that_does_not_answer_is_still_failed(repo) -> None:
    cog = _real_cog(_mgr(present=[None]))
    status, body = await _post(repo, _real_bot(cog, _real_thread()))
    assert (status, body.get("status")) == (503, "failed"), body
    cog = _real_cog(_mgr(present=[False]))
    with patch("c_lord.tmux.is_resident_anywhere", return_value=None):
        status, body = await _post(repo, _real_bot(cog, _real_thread()))
    assert (status, body.get("status")) == (503, "failed"), body
