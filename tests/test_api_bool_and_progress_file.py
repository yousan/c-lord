"""#1038 / #1039 — REST API の真偽値と progress_file の置き場所。

#1038: ``bool("false")`` は True なので、``"enabled": "false"`` を送るとタスクが
有効になり、200 が返っていた（``run_immediately`` も同じ）。おぷー決定
（2026-10-29）: 案A — JSON の true/false 以外は 400 で理由を返す。

#1039: ``/api/reply`` の ``progress_file`` は bot のホスト上の任意のファイルを
添付できた。``CLORD_API_SECRET`` で他人・他ホストを入れる運用では ``.env`` を
Discord に出させる経路になる。おぷー決定（2026-10-29）: 案B — c-lord の session
dir の下だけに制限し、外は添付せず WARNING。欄は消さない。
"""

from __future__ import annotations

import logging
from pathlib import Path
from unittest.mock import AsyncMock, MagicMock

import pytest
from aiohttp.test_utils import TestClient, TestServer

from c_lord.database.notification_repo import NotificationRepository
from c_lord.database.task_repo import TaskRepository
from c_lord.ext.api_server import ApiServer


@pytest.fixture
async def repos(tmp_path: Path) -> tuple[NotificationRepository, TaskRepository]:
    db = str(tmp_path / "t.db")
    repo, task_repo = NotificationRepository(db), TaskRepository(db)
    await repo.init_db()
    await task_repo.init_db()
    return repo, task_repo


@pytest.fixture
async def task_client(repos) -> TestClient:
    repo, task_repo = repos
    api = ApiServer(repo=repo, bot=MagicMock(), default_channel_id=1, task_repo=task_repo)
    client = TestClient(TestServer(api.app))
    await client.start_server()
    yield client
    await client.close()


async def _create(client: TestClient, **extra: object):
    body = {"name": "t1", "prompt": "p", "interval_seconds": 60, "channel_id": 1, **extra}
    return await client.post("/api/tasks", json=body)


class TestEnabledMustBeABoolean:
    @pytest.mark.asyncio
    @pytest.mark.parametrize("value", ["false", "0", "no", "", 0, 1, None, [], {}])
    async def test_patch_with_a_non_boolean_is_400_and_changes_nothing(
        self, task_client: TestClient, repos, value: object
    ) -> None:
        _, task_repo = repos
        created = await _create(task_client)
        task_id = (await created.json())["id"]
        await task_client.patch(f"/api/tasks/{task_id}", json={"enabled": False})

        resp = await task_client.patch(f"/api/tasks/{task_id}", json={"enabled": value})

        assert resp.status == 400
        assert (await resp.json()) == {"error": "enabled must be true or false"}
        task = next(t for t in await task_repo.get_all() if t["id"] == task_id)
        assert not task["enabled"], "a rejected PATCH re-enabled the task"

    @pytest.mark.asyncio
    async def test_the_issue_repro_string_false_does_not_enable(
        self, task_client: TestClient, repos
    ) -> None:
        """Issue 本文の再現: 止めたタスクに ``"enabled": "false"`` → 有効にならない。"""
        _, task_repo = repos
        task_id = (await (await _create(task_client)).json())["id"]
        await task_client.patch(f"/api/tasks/{task_id}", json={"enabled": False})

        await task_client.patch(f"/api/tasks/{task_id}", json={"enabled": "false"})

        task = next(t for t in await task_repo.get_all() if t["id"] == task_id)
        assert not task["enabled"]

    @pytest.mark.asyncio
    @pytest.mark.parametrize("value", [True, False])
    async def test_real_booleans_still_work(
        self, task_client: TestClient, repos, value: bool
    ) -> None:
        _, task_repo = repos
        task_id = (await (await _create(task_client)).json())["id"]

        resp = await task_client.patch(f"/api/tasks/{task_id}", json={"enabled": value})

        assert resp.status == 200
        task = next(t for t in await task_repo.get_all() if t["id"] == task_id)
        assert bool(task["enabled"]) is value

    @pytest.mark.asyncio
    async def test_other_fields_are_not_applied_when_enabled_is_rejected(
        self, task_client: TestClient, repos
    ) -> None:
        """400 を返したなら、同じ本文の他の欄も書き込まない（半分だけ適用しない）。"""
        _, task_repo = repos
        task_id = (await (await _create(task_client)).json())["id"]

        resp = await task_client.patch(
            f"/api/tasks/{task_id}", json={"enabled": "false", "prompt": "changed"}
        )

        assert resp.status == 400
        task = next(t for t in await task_repo.get_all() if t["id"] == task_id)
        assert task["prompt"] == "p"

    @pytest.mark.asyncio
    async def test_a_bad_interval_does_not_half_apply_enabled(
        self, task_client: TestClient, repos
    ) -> None:
        """#1037 の interval_seconds の 400 でも、同じ本文の enabled を先に書き込まない。"""
        _, task_repo = repos
        task_id = (await (await _create(task_client)).json())["id"]
        await task_client.patch(f"/api/tasks/{task_id}", json={"enabled": False})

        resp = await task_client.patch(
            f"/api/tasks/{task_id}", json={"enabled": True, "interval_seconds": "soon"}
        )

        assert resp.status == 400
        task = next(t for t in await task_repo.get_all() if t["id"] == task_id)
        assert not task["enabled"]


class TestRunImmediatelyMustBeABoolean:
    @pytest.mark.asyncio
    @pytest.mark.parametrize("value", ["false", "0", "true", 0, 1, None])
    async def test_create_with_a_non_boolean_is_400(
        self, task_client: TestClient, repos, value: object
    ) -> None:
        _, task_repo = repos
        resp = await _create(task_client, run_immediately=value)

        assert resp.status == 400
        assert (await resp.json()) == {"error": "run_immediately must be true or false"}
        assert await task_repo.get_all() == []

    @pytest.mark.asyncio
    @pytest.mark.parametrize("extra", [{}, {"run_immediately": True}, {"run_immediately": False}])
    async def test_omitted_or_boolean_still_creates(
        self, task_client: TestClient, extra: dict
    ) -> None:
        resp = await _create(task_client, **extra)
        assert resp.status == 201


@pytest.fixture
def thread_mock() -> MagicMock:
    import discord

    thread = MagicMock(spec=discord.Thread)
    thread.id = 555666777
    thread.send = AsyncMock(return_value=MagicMock(id=1))
    return thread


async def _reply_client(repos, thread_mock: MagicMock, session_dir_base: Path | None):
    repo, _ = repos
    bot = MagicMock()
    bot.get_channel.return_value = thread_mock
    api = ApiServer(repo=repo, bot=bot, default_channel_id=1)
    if session_dir_base is not None:
        api.session_dir_base = str(session_dir_base)
    client = TestClient(TestServer(api.app))
    await client.start_server()
    return client


def _sent_files(thread_mock: MagicMock) -> list:
    files: list = []
    for call in thread_mock.send.call_args_list:
        if call.kwargs.get("file") is not None:
            files.append(call.kwargs["file"])
        files.extend(call.kwargs.get("files") or [])
    return files


class TestProgressFileStaysInsideSessionDirs:
    @pytest.mark.asyncio
    async def test_a_file_outside_the_session_dirs_is_not_attached(
        self, repos, thread_mock: MagicMock, tmp_path: Path, caplog
    ) -> None:
        """Issue 本文の再現: ``.env`` のような session dir の外のファイルは添付しない。"""
        base = tmp_path / "sessions"
        base.mkdir()
        secret = tmp_path / ".env"
        secret.write_text("DISCORD_BOT_TOKEN=xxx\n")
        client = await _reply_client(repos, thread_mock, base)
        try:
            with caplog.at_level(logging.WARNING):
                resp = await client.post(
                    "/api/reply",
                    json={"thread_id": 555666777, "content": "x", "progress_file": str(secret)},
                )
                body = await resp.json()
        finally:
            await client.close()

        assert resp.status == 200  # 本文は届ける
        assert _sent_files(thread_mock) == [], ".env が Discord に添付された"
        assert any("progress_file" in r.getMessage() for r in caplog.records)
        assert body.get("progress_file") == "ignored"

    @pytest.mark.asyncio
    async def test_a_symlink_pointing_outside_is_not_attached(
        self, repos, thread_mock: MagicMock, tmp_path: Path
    ) -> None:
        """session dir の中に置いた symlink で外を指しても通さない。"""
        base = tmp_path / "sessions"
        (base / "1" / "2").mkdir(parents=True)
        secret = tmp_path / ".env"
        secret.write_text("TOKEN\n")
        link = base / "1" / "2" / "progress.txt"
        link.symlink_to(secret)
        client = await _reply_client(repos, thread_mock, base)
        try:
            resp = await client.post(
                "/api/reply",
                json={"thread_id": 555666777, "content": "x", "progress_file": str(link)},
            )
        finally:
            await client.close()

        assert resp.status == 200
        assert _sent_files(thread_mock) == []

    @pytest.mark.asyncio
    async def test_dotdot_cannot_climb_out(
        self, repos, thread_mock: MagicMock, tmp_path: Path
    ) -> None:
        base = tmp_path / "sessions"
        base.mkdir()
        secret = tmp_path / ".env"
        secret.write_text("TOKEN\n")
        client = await _reply_client(repos, thread_mock, base)
        try:
            resp = await client.post(
                "/api/reply",
                json={
                    "thread_id": 555666777,
                    "content": "x",
                    "progress_file": str(base / ".." / ".env"),
                },
            )
        finally:
            await client.close()

        assert _sent_files(thread_mock) == []
        assert resp.status == 200

    @pytest.mark.asyncio
    async def test_a_file_inside_a_session_dir_is_still_attached(
        self, repos, thread_mock: MagicMock, tmp_path: Path
    ) -> None:
        base = tmp_path / "sessions"
        (base / "1" / "2").mkdir(parents=True)
        progress = base / "1" / "2" / "progress.txt"
        progress.write_text("step1\n")
        client = await _reply_client(repos, thread_mock, base)
        try:
            resp = await client.post(
                "/api/reply",
                json={"thread_id": 555666777, "content": "x", "progress_file": str(progress)},
            )
        finally:
            await client.close()

        assert resp.status == 200
        assert len(_sent_files(thread_mock)) == 1

    @pytest.mark.asyncio
    async def test_without_a_known_session_dir_nothing_is_attached(
        self, repos, thread_mock: MagicMock, tmp_path: Path
    ) -> None:
        """置き場所が分からない（配線されていない）なら、どこも「中」ではない。"""
        progress = tmp_path / "progress.txt"
        progress.write_text("step1\n")
        client = await _reply_client(repos, thread_mock, None)
        try:
            resp = await client.post(
                "/api/reply",
                json={"thread_id": 555666777, "content": "x", "progress_file": str(progress)},
            )
        finally:
            await client.close()

        assert resp.status == 200
        assert _sent_files(thread_mock) == []

    def test_setup_wires_the_session_dir_base(self) -> None:
        """Zero-config: setup の配線だけで制限が効く（利用者のコード変更は要らない）。"""
        from c_lord.setup import BridgeComponents

        api = MagicMock()
        comps = BridgeComponents(session_repo=MagicMock(), session_dir_base="/x/sessions")
        comps.apply_to_api_server(api)
        assert api.session_dir_base == "/x/sessions"

    @pytest.mark.asyncio
    async def test_a_missing_file_outside_is_ignored_not_400(
        self, repos, thread_mock: MagicMock, tmp_path: Path
    ) -> None:
        """外のパスは中身を見る前に捨てる — 存在するかどうかも答えない。"""
        base = tmp_path / "sessions"
        base.mkdir()
        client = await _reply_client(repos, thread_mock, base)
        try:
            resp = await client.post(
                "/api/reply",
                json={"thread_id": 555666777, "content": "x", "progress_file": "/no/such.txt"},
            )
            body = await resp.json()
        finally:
            await client.close()

        assert resp.status == 200
        assert body.get("progress_file") == "ignored"
