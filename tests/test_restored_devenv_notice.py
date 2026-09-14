"""復元したとき「開発環境は止まったまま」と言う1行 — Issue #730.

#540 decided, deliberately, that **restoring a workspace must not start docker
back up**: ``docker compose up`` takes tens of seconds to minutes, and someone
coming back only to re-read the conversation should not trigger that. The other
half of that decision — *say so instead* — was written as an AC on #574 and then
deferred three times (PR #591 → PR #592 → "#575 で") until it fell out entirely.

So the stop notice says 「開発環境 (docker) ⏹ 停止（:55322 を解放）」 and the
restore notice said nothing at all. Stopping was announced; staying stopped was
not.

The line is built by one function that both restore paths call, for the reason
:mod:`c_lord.workspace_notice` already exists: two functions producing "the
same" message drift, and #538 was exactly that.
"""

from __future__ import annotations

from unittest.mock import AsyncMock, MagicMock

import discord
import pytest

from c_lord.database.repository import SessionRecord
from c_lord.devenv import DevContainer


def _c(name: str, ports: tuple[int, ...] = (), status: str = "exited") -> DevContainer:
    return DevContainer(
        container_id=f"id-{name}",
        name=name,
        status=status,
        ports=ports,
        project=None,
        source="mount",
    )


def _record(
    thread_id: int = 555, *, closed_at: str | None = "2026-09-01 10:00:00"
) -> SessionRecord:
    return SessionRecord(
        thread_id=thread_id,
        session_id="sess-abc",
        working_dir="/tmp/x",
        model=None,
        origin="discord",
        summary=None,
        created_at="2026-09-01 09:00:00",
        last_used_at="2026-09-01 09:30:00",
        topic="メモリ設計",
        issue_ref="730",
        closed_at=closed_at,
    )


def _thread(thread_id: int = 555) -> MagicMock:
    thread = MagicMock(spec=discord.Thread)
    thread.id = thread_id
    thread.parent_id = 999
    thread.name = "W3 │ #730 メモリ設計"
    thread.edit = AsyncMock()
    thread.send = AsyncMock()
    return thread


# ── the wording itself ───────────────────────────────────────────────────────


class TestTheLine:
    def test_says_the_dev_environment_is_still_stopped(self) -> None:
        """AC1's actual content: the reader is told the state they are in."""
        from c_lord.workspace_notice import restored_devenv_line

        line = restored_devenv_line([_c("supabase_db_555", (55322,))])
        assert line is not None
        assert "開発環境" in line
        assert "停止したまま" in line

    def test_says_it_will_not_be_started_automatically(self) -> None:
        """The line exists *because* c-lord deliberately does not start docker.

        Without this half the reader reasonably concludes c-lord is broken —
        it knows the environment is down and says nothing about why it is
        leaving it that way (#540).
        """
        from c_lord.workspace_notice import restored_devenv_line

        line = restored_devenv_line([_c("supabase_db_555", (55322,))])
        assert line is not None
        assert "自動" in line

    def test_names_the_containers_so_the_reader_can_recognise_them(self) -> None:
        from c_lord.workspace_notice import restored_devenv_line

        line = restored_devenv_line([_c("supabase_db_555", (55322,))])
        assert line is not None
        assert "supabase_db_555" in line

    def test_is_one_line(self) -> None:
        """#540: 「1行だけ出して、必要なら Claude に頼めば起こせる」形にする。"""
        from c_lord.workspace_notice import restored_devenv_line

        line = restored_devenv_line([_c("a"), _c("b"), _c("c"), _c("d")])
        assert line is not None
        assert "\n" not in line

    def test_nothing_to_say_when_there_is_no_dev_environment(self) -> None:
        """AC5. Most threads never start docker — they must not gain a notice."""
        from c_lord.workspace_notice import restored_devenv_line

        assert restored_devenv_line([]) is None

    def test_nothing_to_say_when_everything_is_already_running(self) -> None:
        """AC6. A *sleep* leaves docker running on purpose, so the restore from
        one has nothing to report — and sleep's whole goal is going unnoticed."""
        from c_lord.workspace_notice import restored_devenv_line

        assert restored_devenv_line([_c("db", (5432,), status="running")]) is None

    def test_a_partly_running_environment_still_reports_the_stopped_half(self) -> None:
        """Half-up is the case the reader can least work out for themselves."""
        from c_lord.workspace_notice import restored_devenv_line

        line = restored_devenv_line(
            [_c("db", (5432,), status="running"), _c("studio", (55323,), status="exited")]
        )
        assert line is not None
        assert "studio" in line
        assert "db" not in line.replace("studio", "")


# ── discovery wrapper: never a dependency, never an exception ────────────────


class TestDiscovery:
    @pytest.mark.asyncio
    async def test_reports_the_stopped_containers_of_that_session_dir(self, monkeypatch) -> None:
        from c_lord import workspace_notice

        async def fake(session_dir: str) -> list[DevContainer]:
            assert session_dir == "/work/555"
            return [_c("supabase_db_555", (55322,))]

        monkeypatch.setattr(workspace_notice, "containers_for_session_dir", fake)
        line = await workspace_notice.restored_devenv_notice("/work/555")
        assert line is not None
        assert "supabase_db_555" in line

    @pytest.mark.asyncio
    async def test_a_host_without_docker_says_nothing(self, monkeypatch) -> None:
        """AC7. docker is not a dependency of c-lord — a host without it is a
        normal host, so the restore must be exactly as it was before (#573)."""
        from c_lord import workspace_notice

        async def fake(session_dir: str) -> list[DevContainer]:
            return []

        monkeypatch.setattr(workspace_notice, "containers_for_session_dir", fake)
        assert await workspace_notice.restored_devenv_notice("/work/555") is None

    @pytest.mark.asyncio
    async def test_a_broken_docker_never_breaks_the_restore(self, monkeypatch) -> None:
        """AC7. The restore is the user's turn starting. A wedged daemon must
        cost them a missing sentence, never the turn."""
        from c_lord import workspace_notice

        async def boom(session_dir: str) -> list[DevContainer]:
            raise RuntimeError("docker daemon is not responding")

        monkeypatch.setattr(workspace_notice, "containers_for_session_dir", boom)
        assert await workspace_notice.restored_devenv_notice("/work/555") is None

    @pytest.mark.asyncio
    async def test_no_session_dir_means_no_docker_call_at_all(self, monkeypatch) -> None:
        from c_lord import workspace_notice

        called = False

        async def fake(session_dir: str) -> list[DevContainer]:
            nonlocal called
            called = True
            return []

        monkeypatch.setattr(workspace_notice, "containers_for_session_dir", fake)
        assert await workspace_notice.restored_devenv_notice(None) is None
        assert called is False


# ── AC4: the restore must not start docker ───────────────────────────────────


class TestNeverStartsDocker:
    def test_there_is_no_start_verb_to_call_by_accident(self) -> None:
        """AC4, structurally.

        #540 decided restoring must **not** run compose. The durable guard is not
        a test of today's message but the absence of the capability: if nothing
        in :mod:`c_lord.devenv` can start a container, no future edit to a
        restore path can quietly make a restore take three minutes.
        """
        from c_lord import devenv

        verbs = [
            name
            for name in dir(devenv)
            if not name.startswith("_") and callable(getattr(devenv, name))
        ]
        assert not [v for v in verbs if "start" in v or "up" in v.split("_")], verbs

    @pytest.mark.asyncio
    async def test_discovery_is_the_only_docker_verb_used(self, monkeypatch) -> None:
        """``restored_devenv_notice`` may inspect. It may not act."""
        from c_lord import devenv, workspace_notice

        argvs: list[list[str]] = []

        async def fake_docker(argv: list[str]) -> tuple[int, str]:
            argvs.append(argv)
            return 1, ""

        monkeypatch.setattr(devenv, "_docker", fake_docker)
        await workspace_notice.restored_devenv_notice("/work/555")

        for argv in argvs:
            assert "start" not in argv
            assert "up" not in argv


# ── AC1/AC2/AC3: both restore paths, one function ────────────────────────────


class TestWorkspaceStartPosts:
    """``/workspace-start`` — :meth:`SessionManageCog._reopen_workspace_impl`."""

    def _cog(self, record: SessionRecord | None):
        from c_lord.cogs.session_manage import SessionManageCog

        bot = MagicMock()
        bot.channel_id = 999
        bot.get_cog = MagicMock(return_value=None)
        repo = MagicMock()
        repo.get = AsyncMock(return_value=record)
        repo.set_closed = AsyncMock()
        cog = SessionManageCog(bot=bot, repo=repo)
        sdm = MagicMock()
        sdm.base_dir = "/work"
        cog._resolve_session_dir_manager = AsyncMock(return_value=sdm)
        return cog

    def _ctx(self, thread: MagicMock) -> MagicMock:
        ctx = MagicMock()
        ctx.send = AsyncMock()
        ctx.channel = thread
        return ctx

    @staticmethod
    def _said(ctx: MagicMock, thread: MagicMock) -> str:
        parts: list[str] = []
        for call in list(ctx.send.call_args_list) + list(thread.send.call_args_list):
            embed = call.kwargs.get("embed")
            if embed is not None:
                parts += [str(embed.title or ""), str(embed.description or "")]
            parts += [str(a) for a in call.args]
        return "\n".join(parts)

    @pytest.mark.asyncio
    async def test_reopening_says_the_environment_is_still_stopped(self, monkeypatch) -> None:
        """AC1."""
        from c_lord import workspace_notice

        async def fake(session_dir: str) -> list[DevContainer]:
            return [_c("supabase_db_555", (55322,))]

        monkeypatch.setattr(workspace_notice, "containers_for_session_dir", fake)

        cog = self._cog(_record())
        thread = _thread()
        ctx = self._ctx(thread)
        await cog.reopen_workspace_text.callback(cog, ctx)

        assert "停止したまま" in self._said(ctx, thread)

    @pytest.mark.asyncio
    async def test_reopening_without_docker_is_unchanged(self, monkeypatch) -> None:
        """AC5: no dev environment, no extra sentence."""
        from c_lord import workspace_notice

        async def fake(session_dir: str) -> list[DevContainer]:
            return []

        monkeypatch.setattr(workspace_notice, "containers_for_session_dir", fake)

        cog = self._cog(_record())
        thread = _thread()
        ctx = self._ctx(thread)
        await cog.reopen_workspace_text.callback(cog, ctx)

        assert "停止したまま" not in self._said(ctx, thread)


class TestMessageRestorePosts:
    """A message arriving at a stopped workspace — ``ClaudeChatCog._reopen_thread``.

    The chokepoint for **both** remaining restores: the 7-day auto-stop undone
    by a message (#700) and the 「▶️ 再開する」 button (#512).
    """

    @staticmethod
    def _cog():
        from c_lord.cogs.claude_chat import ClaudeChatCog

        bot = MagicMock()
        bot.settings_repo = None
        bot.get_cog = MagicMock(return_value=None)
        repo = MagicMock()
        repo.get = AsyncMock(return_value=_record())
        repo.set_closed = AsyncMock()
        runner = MagicMock()
        runner.model = "sonnet"
        runner.effort = None
        runner.working_dir = "/fallback"
        runner.timeout_seconds = 300
        cog = ClaudeChatCog(bot=bot, repo=repo, runner=runner)
        sdm = MagicMock()
        sdm.base_dir = "/work"
        cog._resolve_session_dir_manager = AsyncMock(return_value=sdm)  # type: ignore[method-assign]
        return cog

    @pytest.mark.asyncio
    async def test_auto_reopen_says_the_environment_is_still_stopped(self, monkeypatch) -> None:
        """AC2, automatic half (#700)."""
        from c_lord import workspace_notice
        from c_lord.cogs import claude_chat

        monkeypatch.setattr(claude_chat, "apply_open_name", AsyncMock(return_value="W3 │ x"))

        async def fake(session_dir: str) -> list[DevContainer]:
            return [_c("supabase_db_555", (55322,))]

        monkeypatch.setattr(workspace_notice, "containers_for_session_dir", fake)

        cog = self._cog()
        thread = _thread()
        await cog._reopen_thread(thread, auto=True)

        blob = "\n".join(str(c) for c in thread.send.call_args_list)
        assert "停止したまま" in blob

    @pytest.mark.asyncio
    async def test_button_reopen_says_it_too(self, monkeypatch) -> None:
        """AC2, manual half (#512). Same chokepoint, so it cannot diverge."""
        from c_lord import workspace_notice
        from c_lord.cogs import claude_chat

        monkeypatch.setattr(claude_chat, "apply_open_name", AsyncMock(return_value="W3 │ x"))

        async def fake(session_dir: str) -> list[DevContainer]:
            return [_c("supabase_db_555", (55322,))]

        monkeypatch.setattr(workspace_notice, "containers_for_session_dir", fake)

        cog = self._cog()
        thread = _thread()
        await cog._reopen_thread(thread, auto=False)

        blob = "\n".join(str(c) for c in thread.send.call_args_list)
        assert "停止したまま" in blob

    @pytest.mark.asyncio
    async def test_a_failed_notice_never_blocks_the_reopen(self, monkeypatch) -> None:
        """The reopen is the user's turn starting; the sentence is a courtesy."""
        from c_lord import workspace_notice
        from c_lord.cogs import claude_chat

        monkeypatch.setattr(claude_chat, "apply_open_name", AsyncMock(return_value="W3 │ x"))

        async def boom(session_dir: str) -> list[DevContainer]:
            raise RuntimeError("docker is wedged")

        monkeypatch.setattr(workspace_notice, "containers_for_session_dir", boom)

        cog = self._cog()
        thread = _thread()
        await cog._reopen_thread(thread, auto=True)

        cog.repo.set_closed.assert_awaited_once_with(555, False)


class TestOneFunctionForBothPaths:
    """AC3 — the structural half.

    #538's lesson: the side that *announces* a behaviour and the side that
    *implements* it drifted because they were two functions. Both restore paths
    must reach the wording through the same one.
    """

    def test_both_cogs_call_the_shared_builder(self) -> None:
        from pathlib import Path

        import c_lord

        root = Path(c_lord.__file__).parent
        for name in ("cogs/session_manage.py", "cogs/claude_chat.py"):
            body = (root / name).read_text(encoding="utf-8")
            assert "restored_devenv_notice" in body, f"{name} builds its own wording"

    def test_the_wording_exists_in_exactly_one_place(self) -> None:
        from pathlib import Path

        import c_lord

        root = Path(c_lord.__file__).parent
        hits = [
            p.relative_to(root).as_posix()
            for p in root.rglob("*.py")
            if "停止したまま" in p.read_text(encoding="utf-8")
        ]
        assert hits == ["workspace_notice.py"], f"wording duplicated in {hits}"
