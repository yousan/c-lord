"""#687 — a ``[Scheduled]`` thread must be repliable.

A scheduled run creates a real Discord thread, works in a fixed checkout and
reports into that thread.  Answering it did nothing: ``SchedulerCog._run_task``
passed ``repo=None``, so no ``sessions`` row was ever written, and
:func:`c_lord.session_resume.classify` — which decides on the row alone —
判定 the thread UNTRACKED and dropped every message with 「復元できるワーク
スペースがありません」.  The workspace was alive in tmux the whole time.

Two halves, and both are needed:

* the scheduler must persist the row (and the checkout it ran in), and
* the reply path must *use* that checkout instead of cloning a session dir over
  it — otherwise the row exists but points somewhere Claude never runs, and the
  transcript mirror follows it into an empty directory.
"""

from __future__ import annotations

import contextlib
from pathlib import Path
from unittest.mock import AsyncMock, MagicMock, patch

import discord
import pytest

from c_lord.cogs.claude_chat import ClaudeChatCog
from c_lord.cogs.scheduler import SchedulerCog
from c_lord.database.repository import SessionRecord
from c_lord.database.task_repo import TaskRepository
from c_lord.session_resume import ThreadResume, accepts_message, classify
from c_lord.workspace_dir import external_workspace

AUDIT_DIR = "/home/yousan/c-lord-audit"


# ---------------------------------------------------------------------------
# Fixtures shared with tests/test_scheduled_run_window.py's shape
# ---------------------------------------------------------------------------


def _make_bot() -> MagicMock:
    bot = MagicMock()
    bot.loop = MagicMock()
    bot.settings_repo = None
    bot.session_repo = MagicMock()
    return bot


def _make_runner_config() -> MagicMock:
    cfg = MagicMock()
    cfg.model = "sonnet"
    cfg.working_dir = None
    cfg.timeout_seconds = 300
    cfg.effort = None
    return cfg


@pytest.fixture
async def task_repo(tmp_path) -> TaskRepository:
    r = TaskRepository(str(tmp_path / "tasks.db"))
    await r.init_db()
    return r


@pytest.fixture
def cog(task_repo: TaskRepository) -> SchedulerCog:
    return SchedulerCog(_make_bot(), _make_runner_config(), repo=task_repo)


def _wire_channel(cog: SchedulerCog) -> tuple[MagicMock, MagicMock]:
    thread = AsyncMock(spec=discord.Thread)
    thread.id = 777
    starter = AsyncMock()
    starter.create_thread = AsyncMock(return_value=thread)
    channel = AsyncMock(spec=discord.TextChannel)
    channel.id = 99
    channel.send = AsyncMock(return_value=starter)
    cog.bot.get_channel = MagicMock(return_value=channel)

    tmux = MagicMock()
    tmux.create_session = MagicMock(return_value="w7")
    tmux.session_exists = MagicMock(return_value=True)
    cog._resolve_tmux_manager = AsyncMock(return_value=tmux)  # type: ignore[method-assign]
    return thread, tmux


async def _make_task(repo: TaskRepository, *, working_dir: str | None) -> dict:
    task_id = await repo.create(
        name="audit",
        prompt="do the weekly audit",
        interval_seconds=604800,
        channel_id=99,
        working_dir=working_dir,
    )
    task = await repo.get(task_id)
    assert task is not None
    return task


# ---------------------------------------------------------------------------
# AC1/AC2 — the scheduler persists the session row
# ---------------------------------------------------------------------------


class TestSchedulerPersistsTheSessionRow:
    async def test_run_task_passes_the_session_repository(
        self, cog: SchedulerCog, task_repo: TaskRepository
    ) -> None:
        """The regression: ``repo=None`` meant no row, and no row meant no replies."""
        _wire_channel(cog)
        task = await _make_task(task_repo, working_dir=AUDIT_DIR)

        with patch("c_lord.cogs.scheduler.run_claude_with_config", new_callable=AsyncMock) as run:
            await cog._run_task(task)

        assert run.call_args[0][0].repo is cog.bot.session_repo

    async def test_run_task_records_the_checkout_it_ran_in(
        self, cog: SchedulerCog, task_repo: TaskRepository
    ) -> None:
        """``working_dir`` is what the transcript mirror is restored from (#71)."""
        _wire_channel(cog)
        task = await _make_task(task_repo, working_dir=AUDIT_DIR)

        with patch("c_lord.cogs.scheduler.run_claude_with_config", new_callable=AsyncMock) as run:
            await cog._run_task(task)

        assert run.call_args[0][0].working_dir == AUDIT_DIR

    async def test_working_dir_falls_back_to_the_runner_default(
        self, cog: SchedulerCog, task_repo: TaskRepository
    ) -> None:
        """A task with no working_dir runs in the runner's dir — record that one."""
        _wire_channel(cog)
        cog.runner.working_dir = "/srv/default"
        task = await _make_task(task_repo, working_dir=None)

        with patch("c_lord.cogs.scheduler.run_claude_with_config", new_callable=AsyncMock) as run:
            await cog._run_task(task)

        assert run.call_args[0][0].working_dir == "/srv/default"

    async def test_a_row_makes_the_thread_accept_messages(self) -> None:
        """Why the row matters at all: it is the whole of the resume verdict (#538)."""
        record = SessionRecord(
            thread_id=777,
            session_id="tmux-777",
            working_dir=AUDIT_DIR,
            model=None,
            origin="scheduled",
            summary=None,
            created_at="2026-09-04 11:00:00",
            last_used_at="2026-09-04 11:00:00",
        )
        assert classify(None) is ThreadResume.UNTRACKED
        assert not accepts_message(classify(None))
        assert accepts_message(classify(record))


# ---------------------------------------------------------------------------
# AC3/AC4 — the reply path honours the recorded checkout
# ---------------------------------------------------------------------------


class TestExternalWorkspaceRule:
    def test_a_fixed_checkout_is_reused(self, tmp_path: Path) -> None:
        checkout = tmp_path / "c-lord-audit"
        checkout.mkdir()
        assert external_workspace(
            str(checkout), base_dir=str(tmp_path / "sessions"), thread_id=777
        ) == str(checkout)

    def test_an_ordinary_session_dir_is_not_external(self, tmp_path: Path) -> None:
        """Normal threads must keep going through create_session_dir (#518 hook)."""
        base = tmp_path / "sessions"
        (base / "777").mkdir(parents=True)
        assert external_workspace(str(base / "777"), base_dir=str(base), thread_id=777) is None

    def test_no_record_yet_is_not_external(self, tmp_path: Path) -> None:
        assert external_workspace(None, base_dir=str(tmp_path), thread_id=777) is None
        assert external_workspace("", base_dir=str(tmp_path), thread_id=777) is None

    def test_a_vanished_checkout_falls_back(self, tmp_path: Path) -> None:
        """A path that is not there would start Claude nowhere."""
        assert (
            external_workspace(
                str(tmp_path / "gone"), base_dir=str(tmp_path / "sessions"), thread_id=777
            )
            is None
        )

    def test_without_a_session_dir_base_any_existing_dir_wins(self, tmp_path: Path) -> None:
        assert external_workspace(str(tmp_path), base_dir=None, thread_id=777) == str(tmp_path)


class _Stop(BaseException):
    """Sentinel raised to halt _run_claude right after the decision under test."""


def _chat_cog(
    record: SessionRecord | None, base_dir: str
) -> tuple[ClaudeChatCog, MagicMock, MagicMock]:
    bot = MagicMock()
    bot.channel_id = 999
    bot.settings_repo = None
    bot.get_cog = MagicMock(return_value=None)
    bot.transcript_mirror_cog = None
    repo = MagicMock()
    repo.get = AsyncMock(return_value=record)
    repo.save = AsyncMock()
    cog = ClaudeChatCog(bot=bot, repo=repo, runner=MagicMock())

    sdm = MagicMock()
    sdm.base_dir = base_dir
    sdm.create_session_dir = MagicMock(return_value=str(Path(base_dir) / "501"))
    tmux = MagicMock()
    # Halt the turn as soon as the working dir has been decided and handed on.
    tmux.create_session = MagicMock(side_effect=_Stop)
    cog._resolve_session_dir_manager = AsyncMock(return_value=sdm)  # type: ignore[method-assign]
    cog._resolve_tmux_manager = AsyncMock(return_value=tmux)  # type: ignore[method-assign]
    cog._get_dashboard = MagicMock(return_value=None)  # type: ignore[method-assign]
    cog._get_coordination = MagicMock(return_value=None)  # type: ignore[method-assign]
    cog._get_current_model = AsyncMock(return_value=None)  # type: ignore[method-assign]
    return cog, sdm, tmux


def _thread_and_message() -> tuple[MagicMock, MagicMock]:
    thread = MagicMock(spec=discord.Thread)
    thread.id = 501
    thread.parent_id = 500
    thread.send = AsyncMock(return_value=MagicMock())

    message = MagicMock(spec=discord.Message)
    message.id = 77
    author = MagicMock()
    author.id = 4242
    author.display_name = "yousan"
    author.bot = False
    message.author = author
    message.add_reaction = AsyncMock()
    message.remove_reaction = AsyncMock()
    message.clear_reaction = AsyncMock()
    return thread, message


def _record(working_dir: str) -> SessionRecord:
    return SessionRecord(
        thread_id=501,
        session_id="tmux-501",
        working_dir=working_dir,
        model=None,
        origin="scheduled",
        summary=None,
        created_at="2026-09-04 11:00:00",
        last_used_at="2026-09-04 11:00:00",
    )


class TestReplyUsesTheRecordedCheckout:
    async def test_scheduled_thread_runs_in_its_own_checkout(self, tmp_path: Path) -> None:
        """The bug: the reply cloned a session dir and ran Claude's mirror at it."""
        checkout = tmp_path / "c-lord-audit"
        checkout.mkdir()
        base = str(tmp_path / "sessions")
        cog, sdm, tmux = _chat_cog(_record(str(checkout)), base)
        thread, message = _thread_and_message()

        with contextlib.suppress(BaseException):
            await cog._run_claude(message, thread, "続きをお願い", None)

        sdm.create_session_dir.assert_not_called()
        tmux.create_session.assert_called_once_with(thread.id, str(checkout))

    async def test_ordinary_thread_is_unchanged(self, tmp_path: Path) -> None:
        """No-op for every thread whose workspace *is* its session dir."""
        base = tmp_path / "sessions"
        (base / "501").mkdir(parents=True)
        cog, sdm, tmux = _chat_cog(_record(str(base / "501")), str(base))
        thread, message = _thread_and_message()

        with contextlib.suppress(BaseException):
            await cog._run_claude(message, thread, "続きをお願い", None)

        sdm.create_session_dir.assert_called_once()
        tmux.create_session.assert_called_once_with(thread.id, str(base / "501"))

    async def test_first_turn_still_creates_the_session_dir(self, tmp_path: Path) -> None:
        """No row yet — the ordinary birth path must not be disturbed."""
        base = str(tmp_path / "sessions")
        cog, sdm, _tmux = _chat_cog(None, base)
        thread, message = _thread_and_message()

        with contextlib.suppress(BaseException):
            await cog._run_claude(message, thread, "はじめまして", None)

        sdm.create_session_dir.assert_called_once()
