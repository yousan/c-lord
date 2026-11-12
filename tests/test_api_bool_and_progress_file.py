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
# <<REDACTED 5 lines>>
from c_lord.database.notification_repo import NotificationRepository
from c_lord.database.task_repo import TaskRepository
from c_lord.ext.api_server import ApiServer
# <<REDACTED 3 lines>>
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


class TestProgressFileOpenedOnceInsideTheBoundary:
    """#1105 / #1106 / #1107 — #1058 の事後レビュー第4弾で見つかった穴。

    #1106: 範囲を ``realpath`` の文字列で確かめた後に名前でもう一度開いていたため、
    その間に途中のディレクトリを外向きの symlink に替えると外のファイルが添付された
    (TOCTOU)。開いた後に 404/400 で返すとファイルも開きっぱなしだった。
    #1105: NUL 入りの値で ``realpath`` が ``ValueError`` を出し 500 になっていた。
    #1107: 捨てたときの WARNING が値を ``%s`` で書き、改行で偽のログ行を作れた。
    """

    @pytest.mark.asyncio
    async def test_swapping_a_dir_for_an_outward_symlink_after_the_check_leaks_nothing(
        self, repos, thread_mock: MagicMock, tmp_path: Path, monkeypatch
    ) -> None:
        """確かめた直後（realpath の直後）に途中のディレクトリを外向き symlink に替える。

        決定的に再現するため、``realpath`` が対象のパスを解決した直後に差し替える。
        """
        import os

        base = tmp_path / "sessions"
        sub = base / "1" / "2" / "sub"
        sub.mkdir(parents=True)
        (sub / ".env").write_text("harmless\n")
        outside = tmp_path / "outside"
        outside.mkdir()
        (outside / ".env").write_text("SECRET\n")
        real_realpath = os.path.realpath

        def racing_realpath(p, *a, **k):
            out = real_realpath(p, *a, **k)
            if str(p).endswith(os.path.join("sub", ".env")) and not sub.is_symlink():
                (sub / ".env").unlink()
                sub.rmdir()
                sub.symlink_to(outside)
            return out

        sent: list[bytes] = []

        async def send(*_a, **k):
            for f in [k.get("file"), *(k.get("files") or [])]:
                if f is not None:
                    sent.append(f.fp.read())
            return MagicMock(id=1)

        thread_mock.send = AsyncMock(side_effect=send)
        client = await _reply_client(repos, thread_mock, base)
        try:
            monkeypatch.setattr(os.path, "realpath", racing_realpath)
            resp = await client.post(
                "/api/reply",
                json={"thread_id": 555666777, "content": "x", "progress_file": str(sub / ".env")},
            )
        finally:
            monkeypatch.undo()
            await client.close()

        assert b"SECRET\n" not in sent, "確かめた後に差し替えた外のファイルが添付された"
        assert resp.status in (200, 400)

    @pytest.mark.asyncio
    async def test_a_symlinked_dir_inside_the_session_still_attaches(
        self, repos, thread_mock: MagicMock, tmp_path: Path
    ) -> None:
        """中を指す symlink（例: 置き場所自体が symlink 経由）は従来どおり添付する。"""
        base = tmp_path / "sessions"
        (base / "1" / "2").mkdir(parents=True)
        (base / "1" / "2" / "progress.txt").write_text("ok\n")
        link_base = tmp_path / "linkbase"
        link_base.symlink_to(base)
        client = await _reply_client(repos, thread_mock, link_base)
        try:
            resp = await client.post(
                "/api/reply",
                json={
                    "thread_id": 555666777,
                    "content": "x",
                    "progress_file": str(link_base / "1" / "2" / "progress.txt"),
                },
            )
        finally:
            await client.close()

        assert resp.status == 200
        assert len(_sent_files(thread_mock)) == 1

    @pytest.mark.asyncio
    async def test_a_directory_inside_is_400(
        self, repos, thread_mock: MagicMock, tmp_path: Path
    ) -> None:
        base = tmp_path / "sessions"
        (base / "1" / "2").mkdir(parents=True)
        client = await _reply_client(repos, thread_mock, base)
        try:
            resp = await client.post(
                "/api/reply",
                json={"thread_id": 555666777, "content": "x", "progress_file": str(base / "1")},
            )
        finally:
            await client.close()

        assert resp.status == 400
        assert _sent_files(thread_mock) == []

    @pytest.mark.skipif(
        not Path("/proc/self/fd").is_dir(), reason="needs /proc to list open descriptors"
    )
    @pytest.mark.asyncio
    async def test_a_404_does_not_leave_the_file_open(self, repos, tmp_path: Path) -> None:
        import gc
        import os

        base = tmp_path / "sessions"
        f = base / "1" / "2" / "progress.txt"
        f.parent.mkdir(parents=True)
        f.write_text("x")
        repo, _ = repos
        bot = MagicMock()
        bot.get_channel.return_value = None
        bot.fetch_channel = AsyncMock(side_effect=Exception("gone"))
        api = ApiServer(repo=repo, bot=bot, default_channel_id=1)
        api.session_dir_base = str(base)
        client = TestClient(TestServer(api.app))
        await client.start_server()
        try:
            for _ in range(5):
                resp = await client.post(
                    "/api/reply",
                    json={"thread_id": 7, "content": "x", "progress_file": str(f)},
                )
                assert resp.status == 404
        finally:
            await client.close()
        gc.collect()

        def _open_to(path: str) -> int:
            n = 0
            for fd in os.listdir("/proc/self/fd"):
                try:
                    if os.readlink(f"/proc/self/fd/{fd}") == path:
                        n += 1
                except OSError:
                    pass
            return n

        assert _open_to(os.path.realpath(f)) == 0, "404 で返したのに添付のファイルが開いたまま"

    @pytest.mark.asyncio
    @pytest.mark.parametrize("where", ["inside", "outside"])
    async def test_a_nul_in_the_path_is_not_a_500(
        self, repos, thread_mock: MagicMock, tmp_path: Path, where: str
    ) -> None:
        """#1105: 外の場所と同じく添付を捨てて本文は届ける（200 + ignored）。"""
        base = tmp_path / "sessions"
        (base / "1" / "2").mkdir(parents=True)
        value = str(base / "1" / "2" / "a\x00b") if where == "inside" else "/etc/pass\x00wd"
        client = await _reply_client(repos, thread_mock, base)
        try:
            resp = await client.post(
                "/api/reply",
                json={"thread_id": 555666777, "content": "x", "progress_file": value},
            )
            body = await resp.json()
        finally:
            await client.close()

        assert resp.status == 200
        assert body.get("progress_file") == "ignored"
        assert _sent_files(thread_mock) == []

    @pytest.mark.asyncio
    async def test_a_dir_one_may_pass_but_not_list_still_attaches(
        self, repos, thread_mock: MagicMock, tmp_path: Path
    ) -> None:
        """#1185: 途中に「読めないが通れる」ディレクトリ（0311）があっても添付する。

        #1160 で途中のディレクトリを ``O_RDONLY`` で開くようになり、読み取り権限の
        無いディレクトリの下の progress が ``progress_file not found`` の 400 になった
        （以前は 200 で添付）。通るのに要るのは実行権限だけ。
        """
        import os

        if os.geteuid() == 0:
            pytest.skip("root ignores directory permissions")
        if not (getattr(os, "O_PATH", 0) or getattr(os, "O_SEARCH", 0)):
            pytest.skip("no O_PATH / O_SEARCH: a directory can only be opened to read it")
        base = tmp_path / "sessions"
        xonly = base / "1" / "2" / "xonly"
        xonly.mkdir(parents=True)
        (xonly / "p.txt").write_text("ok\n")
        xonly.chmod(0o311)
        client = await _reply_client(repos, thread_mock, base)
        try:
            resp = await client.post(
                "/api/reply",
                json={
                    "thread_id": 555666777,
                    "content": "x",
                    "progress_file": str(xonly / "p.txt"),
                },
            )
            body = await resp.text()
        finally:
            xonly.chmod(0o755)
            await client.close()

        assert resp.status == 200, body
        assert len(_sent_files(thread_mock)) == 1

    @pytest.mark.asyncio
    async def test_the_warning_stays_on_one_line(
        self, repos, thread_mock: MagicMock, tmp_path: Path, caplog
    ) -> None:
        """#1107: 呼び出し元の改行でログに偽の行を作らせない。"""
        base = tmp_path / "sessions"
        base.mkdir()
        client = await _reply_client(repos, thread_mock, base)
        try:
            with caplog.at_level(logging.WARNING):
                await client.post(
                    "/api/reply",
                    json={
                        "thread_id": 555666777,
                        "content": "x",
                        "progress_file": "/x\n2026-10-29 00:00:00 [INFO] fake: thread=999 exit",
                    },
                )
        finally:
            await client.close()

        msgs = [r.getMessage() for r in caplog.records if "progress_file" in r.getMessage()]
        assert msgs, "WARNING が出ていない"
        assert not any("\n" in m for m in msgs), "呼び出し元の改行がそのままログに書かれた"
