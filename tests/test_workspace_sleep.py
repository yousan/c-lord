"""スリープの操作そのもの — Issue #572。

スリープは3操作のいちばん内側（スリープ ⊂ 停止 ⊂ 削除）。**claude を止めるだけ**
で、そこから先には一切触らない:

* docker は止めない — 走っているビルドや DB が飛ぶ。しかも回収したい 400MB は
  docker を止めなくても取れるので、止める理由が無い
* 作業ディレクトリ・会話履歴・volume・``sessions`` の行はそのまま
* スレッド名も変えない。``closed_at`` も立てない（それは「停止」の印）

**気づかれないのがゴール**なので、通常は1文字も投稿しない。例外は1つだけ:
docker が動いていたときは、ポートを掴んだままになることを1件だけ知らせる。
利用者が自力で気づけず、次に環境を立てるとき衝突として跳ね返ってくるため。

あるべき動きは docs/specs/workspace-sleep.md。
"""

from __future__ import annotations

from types import SimpleNamespace
from unittest.mock import AsyncMock, MagicMock

import discord
import pytest

from c_lord.database.repository import SessionRecord
from c_lord.devenv import DevContainer
from c_lord.workspace_notice import WorkspaceReason


def _record(thread_id: int = 555) -> SessionRecord:
    return SessionRecord(
        thread_id=thread_id,
        session_id="sess-abc",
        working_dir="/tmp/x",
        model=None,
        origin="discord",
        summary=None,
        created_at="2026-08-31 06:00:00",
        last_used_at="2026-08-31 07:00:00",
        topic="認証リファクタ",
    )


def _thread(thread_id: int = 555) -> MagicMock:
    thread = MagicMock(spec=discord.Thread)
    thread.id = thread_id
    thread.parent_id = 999
    thread.name = "W3 │ #404 認証リファクタ"
    thread.edit = AsyncMock()
    thread.send = AsyncMock()
    return thread


def _container(name: str, port: int, *, running: bool = True) -> DevContainer:
    return DevContainer(
        container_id="c" * 12,
        name=name,
        status="running" if running else "exited",
        ports=(port,),
        project="supabase",
        source="mount",
    )


def _cog(*, window_exists: bool = True, killed: bool = True) -> SimpleNamespace:
    """A SessionManageCog wired to mocks, plus handles on the mocks it resolves."""
    from c_lord.cogs.session_manage import SessionManageCog

    bot = MagicMock()
    bot.channel_id = 999
    bot.get_cog = MagicMock(return_value=None)

    repo = MagicMock()
    repo.get = AsyncMock(return_value=_record())
    repo.set_closed = AsyncMock()
    repo.set_slept = AsyncMock()
    repo.delete = AsyncMock()

    cog = SessionManageCog(bot=bot, repo=repo)

    tmux_mgr = MagicMock()
    tmux_mgr.session_exists = MagicMock(return_value=window_exists)
    tmux_mgr.kill_session = MagicMock(return_value=killed)
    cog._resolve_tmux_manager = AsyncMock(return_value=tmux_mgr)

    sdm = MagicMock()
    sdm.base_dir = "/home/yousan/c-lord-sessions/999"
    sdm.cleanup_for_thread = MagicMock()
    cog._resolve_session_dir_manager = AsyncMock(return_value=sdm)

    return SimpleNamespace(cog=cog, repo=repo, tmux=tmux_mgr, sdm=sdm)


@pytest.fixture
def no_docker(monkeypatch):
    """Default: the host has no dev environment for this workspace."""
    import c_lord.cogs.session_manage as sm

    monkeypatch.setattr(sm, "containers_for_session_dir", AsyncMock(return_value=[]))
    stop = AsyncMock(return_value=[])
    monkeypatch.setattr(sm, "stop_containers", stop)
    return stop


class TestSleepStopsClaudeAndNothingElse:
    @pytest.mark.asyncio
    async def test_the_tmux_window_is_killed(self, no_docker) -> None:
        """#572 AC: claude が止まり、tmux ウィンドウも残骸を残さない。"""
        ws = _cog()
        await ws.cog._sleep_workspace_impl(channel=_thread(), idle_label="4時間")
        ws.tmux.kill_session.assert_called_once_with(555)

    @pytest.mark.asyncio
    async def test_docker_is_never_stopped(self, monkeypatch) -> None:
        """#572 AC: docker コンテナは停止しない。

        走っているビルドや DB がスリープで飛んではいけない。ここが「停止」と
        スリープを分ける唯一の境界線。
        """
        import c_lord.cogs.session_manage as sm

        running = [_container("supabase_db_555", 55322)]
        monkeypatch.setattr(sm, "containers_for_session_dir", AsyncMock(return_value=running))
        stop = AsyncMock(return_value=["supabase_db_555"])
        monkeypatch.setattr(sm, "stop_containers", stop)

        ws = _cog()
        await ws.cog._sleep_workspace_impl(channel=_thread(), idle_label="4時間")

        stop.assert_not_awaited()

    @pytest.mark.asyncio
    async def test_the_session_row_survives(self, no_docker) -> None:
        """会話履歴・作業ディレクトリ・volume を指す唯一の handle を消さない。"""
        ws = _cog()
        await ws.cog._sleep_workspace_impl(channel=_thread(), idle_label="4時間")
        ws.repo.delete.assert_not_awaited()

    @pytest.mark.asyncio
    async def test_the_working_directory_survives(self, no_docker) -> None:
        ws = _cog()
        await ws.cog._sleep_workspace_impl(channel=_thread(), idle_label="4時間")
        ws.sdm.cleanup_for_thread.assert_not_called()

    @pytest.mark.asyncio
    async def test_it_is_not_marked_stopped(self, no_docker) -> None:
        """``closed_at`` は「停止」の印。スリープで立てると、次の投稿が
        「▶️ 再開する」ボタンで止められてしまい、無言復元にならない。"""
        ws = _cog()
        await ws.cog._sleep_workspace_impl(channel=_thread(), idle_label="4時間")
        ws.repo.set_closed.assert_not_awaited()

    @pytest.mark.asyncio
    async def test_the_thread_is_not_renamed(self, no_docker) -> None:
        """マーカーを付けない。左のリストの見た目は1ミリも変わらない。"""
        ws = _cog()
        thread = _thread()
        await ws.cog._sleep_workspace_impl(channel=thread, idle_label="4時間")
        assert not [c for c in thread.edit.call_args_list if "name" in c.kwargs]

    @pytest.mark.asyncio
    async def test_the_thread_is_not_archived(self, no_docker) -> None:
        ws = _cog()
        thread = _thread()
        await ws.cog._sleep_workspace_impl(channel=thread, idle_label="4時間")
        assert not [c for c in thread.edit.call_args_list if "archived" in c.kwargs]


class TestNothingOnTheHostIsMutated:
    """#572 AC: docker コンテナも volume も残る。

    「``stop_containers`` を呼ばない」より強い言い方をする — スリープ中に docker
    へ出る命令が**読み取りだけ**であることを確かめる。volume を消す命令も、
    コンテナを落とす命令も、そもそも発行され得ないことになる。
    """

    @pytest.mark.asyncio
    async def test_docker_is_only_ever_inspected(self, monkeypatch) -> None:
        import c_lord.devenv as devenv

        issued: list[list[str]] = []

        async def _fake_docker(argv: list[str]):
            issued.append(argv)
            return 1, ""  # docker が無いホストと同じ扱い

        monkeypatch.setattr(devenv, "_docker", _fake_docker)

        ws = _cog()
        await ws.cog._sleep_workspace_impl(channel=_thread(), idle_label="4時間")

        assert issued, "docker を一度も見ていない（存在確認すらしていない）"
        for argv in issued:
            assert argv[0] == "docker"
            assert argv[1] in {"ps", "inspect"}, f"読み取り以外の docker 命令: {argv}"


class TestSleepIsSilent:
    @pytest.mark.asyncio
    async def test_nothing_is_posted_when_no_dev_environment_is_running(self, no_docker) -> None:
        """気づかれないのがゴール。「スリープしました」も出さない。"""
        ws = _cog()
        thread = _thread()
        await ws.cog._sleep_workspace_impl(channel=thread, idle_label="4時間")
        thread.send.assert_not_awaited()

    @pytest.mark.asyncio
    async def test_exited_containers_hold_no_port_so_stay_silent(self, monkeypatch) -> None:
        """例外が存在する理由は「ポートを掴んだまま」だけ。掴んでいないなら黙る。"""
        import c_lord.cogs.session_manage as sm

        monkeypatch.setattr(
            sm,
            "containers_for_session_dir",
            AsyncMock(return_value=[_container("supabase_db_555", 55322, running=False)]),
        )
        monkeypatch.setattr(sm, "stop_containers", AsyncMock(return_value=[]))

        ws = _cog()
        thread = _thread()
        await ws.cog._sleep_workspace_impl(channel=thread, idle_label="4時間")

        thread.send.assert_not_awaited()


class TestTheOneExceptionIsRunningDocker:
    """唯一の例外 — ポートを掴んだままなので、知らないと次に衝突する。"""

    @pytest.mark.asyncio
    async def test_a_notice_names_the_ports_that_stay_held(self, monkeypatch) -> None:
        import c_lord.cogs.session_manage as sm

        containers = [
            _container("supabase_db_555", 55322),
            _container("supabase_studio_555", 55323),
        ]
        monkeypatch.setattr(sm, "containers_for_session_dir", AsyncMock(return_value=containers))
        monkeypatch.setattr(sm, "stop_containers", AsyncMock(return_value=[]))

        ws = _cog()
        thread = _thread()
        await ws.cog._sleep_workspace_impl(channel=thread, idle_label="4時間")

        thread.send.assert_awaited_once()
        embed = thread.send.await_args.kwargs["embed"]
        text = embed.title + embed.description + "".join(f.value for f in embed.fields)
        assert "55322" in text
        assert "55323" in text

    @pytest.mark.asyncio
    async def test_the_notice_is_the_shared_inventory(self, monkeypatch) -> None:
        """通知は「棚卸し」。手動/自動で別々の文面を書かない（#571）。

        止めたものと**まだ残っているもの**を必ず並べる — 利用者は頼んでいないので
        最初に湧く疑問は「何か失ったのか?」だから。
        """
        import c_lord.cogs.session_manage as sm

        monkeypatch.setattr(
            sm,
            "containers_for_session_dir",
            AsyncMock(return_value=[_container("supabase_db_555", 55322)]),
        )
        monkeypatch.setattr(sm, "stop_containers", AsyncMock(return_value=[]))

        ws = _cog()
        thread = _thread()
        await ws.cog._sleep_workspace_impl(channel=thread, idle_label="4時間")

        embed = thread.send.await_args.kwargs["embed"]
        names = {f.name: f.value for f in embed.fields}
        assert names["Claude"].startswith("⏹")
        assert "動いたまま" in names["開発環境 (docker)"]
        assert "そのまま" in names["作業フォルダ"]
        assert "そのまま" in names["会話履歴"]
        assert "そのまま" in names["DBのデータ (volume)"]

    @pytest.mark.asyncio
    async def test_the_idle_span_is_stated(self, monkeypatch) -> None:
        import c_lord.cogs.session_manage as sm

        monkeypatch.setattr(
            sm,
            "containers_for_session_dir",
            AsyncMock(return_value=[_container("supabase_db_555", 55322)]),
        )
        monkeypatch.setattr(sm, "stop_containers", AsyncMock(return_value=[]))

        ws = _cog()
        thread = _thread()
        await ws.cog._sleep_workspace_impl(
            channel=thread, reason=WorkspaceReason.IDLE, idle_label="4時間"
        )

        embed = thread.send.await_args.kwargs["embed"]
        assert "4時間" in embed.description


class TestAlreadyAsleep:
    """毎tick 同じワークスペースを叩くと、docker の1行が10分おきに湧く。"""

    @pytest.mark.asyncio
    async def test_a_workspace_with_no_window_is_a_noop(self, no_docker) -> None:
        ws = _cog(window_exists=False)
        thread = _thread()
        result = await ws.cog._sleep_workspace_impl(channel=thread, idle_label="4時間")

        assert result is False
        ws.tmux.kill_session.assert_not_called()
        thread.send.assert_not_awaited()

    @pytest.mark.asyncio
    async def test_no_repeat_notice_for_an_already_sleeping_workspace(self, monkeypatch) -> None:
        import c_lord.cogs.session_manage as sm

        monkeypatch.setattr(
            sm,
            "containers_for_session_dir",
            AsyncMock(return_value=[_container("supabase_db_555", 55322)]),
        )
        monkeypatch.setattr(sm, "stop_containers", AsyncMock(return_value=[]))

        ws = _cog(window_exists=False)
        thread = _thread()
        await ws.cog._sleep_workspace_impl(channel=thread, idle_label="4時間")

        thread.send.assert_not_awaited()

    @pytest.mark.asyncio
    async def test_a_channel_without_a_repo_binding_is_a_noop(self, no_docker) -> None:
        ws = _cog()
        ws.cog._resolve_tmux_manager = AsyncMock(return_value=None)
        thread = _thread()

        assert await ws.cog._sleep_workspace_impl(channel=thread, idle_label="4時間") is False
        thread.send.assert_not_awaited()


class TestSleepIsRecordedForASilentResume:
    """次の投稿を**無言で**復元するために、眠らせたことだけ覚えておく。

    覚えるのは文言を選ぶためだけ。復元するかどうかは「ペインが生きているか」
    だけで決める — 2つの情報源が食い違えないようにする（``closed_reason`` と
    同じ規律）。
    """

    @pytest.mark.asyncio
    async def test_the_sleep_is_persisted(self, no_docker) -> None:
        ws = _cog()
        await ws.cog._sleep_workspace_impl(channel=_thread(), idle_label="4時間")
        ws.repo.set_slept.assert_awaited_once_with(555, True)

    @pytest.mark.asyncio
    async def test_nothing_is_persisted_when_nothing_was_slept(self, no_docker) -> None:
        ws = _cog(window_exists=False)
        await ws.cog._sleep_workspace_impl(channel=_thread(), idle_label="4時間")
        ws.repo.set_slept.assert_not_awaited()


class TestTranscriptMirrorIsStoppedFirst:
    """#379: kill が最後の ``<task-notification>`` を書くと、tailing 中の
    ミラーがそれを 👤 としてスレッドに流す。スリープは痕跡を残してはいけない。"""

    @pytest.mark.asyncio
    async def test_the_mirror_is_stopped_before_the_kill(self, no_docker) -> None:
        order: list[str] = []

        ws = _cog()
        ws.cog._stop_transcript_mirror = AsyncMock(side_effect=lambda tid: order.append("mirror"))
        ws.tmux.kill_session = MagicMock(side_effect=lambda tid: order.append("kill") or True)

        await ws.cog._sleep_workspace_impl(channel=_thread(), idle_label="4時間")

        assert order == ["mirror", "kill"]
