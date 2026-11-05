"""The trigger on record after the #1262 fix: the remaining wrong pings.

The trigger on record (``sessions.trigger_message_id``, #115) is who the ❌
pings after a restart (#1212) and which message the answer replies to. Ways it
still named the wrong person:

* #1254: B's message turn could not write its trigger (the database was locked).
  The failure was swallowed without a log, the row kept A's earlier message, and
  after a restart the ❌ pinged A, who had not asked.

Real SQLite throughout (``SessionRepository``), real ``ClaudeChatCog._run_claude``.
"""

from __future__ import annotations

import logging
import sqlite3
from unittest.mock import AsyncMock, MagicMock, patch

import discord

from c_lord import turn_events
from c_lord.database.models import init_db
from c_lord.database.repository import SessionRepository
from tests.test_trigger_on_record import _api_error, _bot, _msg
from tests.test_trigger_skill_races_1262 import _locked, _row_trigger
from tests.test_workspace_failure import _make_cog as _make_chat_cog
from tests.test_workspace_failure import _message as _chat_message

P = 777  # asked earlier (message 555)
B = 4242  # author of tests.test_workspace_failure._message() (message 556)


def _quiet(monkeypatch) -> None:  # noqa: ANN001
    monkeypatch.delenv(turn_events.ENV_LOG, raising=False)
    monkeypatch.delenv(turn_events.ENV_URL, raising=False)
    turn_events.reset()


# -- #1254: the trigger write of a message turn refused (locked) --------------------


async def _locked_chat_turn(repo: SessionRepository) -> None:
    """B's message turn (556) through the real _run_claude; its trigger write
    meets a locked database."""
    cog = _make_chat_cog()
    cog.repo = repo
    real_update = repo.update_trigger_message

    async def update(thread_id: int, message_id: int) -> None:
        conn = _locked(repo.db_path)
        try:
            await real_update(thread_id, message_id)
        finally:
            conn.rollback()
            conn.close()

    repo.update_trigger_message = update  # type: ignore[method-assign]
    cog._resolve_session_dir_manager = AsyncMock(  # type: ignore[method-assign]
        return_value=MagicMock(create_session_dir=MagicMock(return_value="/tmp/work"))
    )
    cog._resolve_tmux_manager = AsyncMock(  # type: ignore[method-assign]
        return_value=MagicMock(create_session=MagicMock(return_value="w1"))
    )
    cog._get_dashboard = MagicMock(return_value=None)  # type: ignore[method-assign]
    cog._get_coordination = MagicMock(return_value=None)  # type: ignore[method-assign]
    cog._get_current_model = AsyncMock(return_value=None)  # type: ignore[method-assign]
    cog._apply_thread_naming = AsyncMock()  # type: ignore[method-assign]
    thread = MagicMock(spec=discord.Thread)
    thread.id = 42
    thread.parent_id = 500
    thread.send = AsyncMock(return_value=MagicMock())
    message = _chat_message()
    message.id = 556
    message.webhook_id = None

    async def run(config) -> None:  # noqa: ANN001
        # what run_claude_with_config does: begin the turn, save at SYSTEM
        turn_events.begin_turn(config.thread, config.started_by or turn_events.StartedBy("user"))
        await repo.save(42, "sess-1")

    try:
        with patch("c_lord.cogs.claude_chat.run_claude_with_config", side_effect=run):
            await cog._run_claude(message, thread, "hi", None)
    finally:
        repo.update_trigger_message = real_update  # type: ignore[method-assign]


async def _p_on_record(tmp_path) -> SessionRepository:  # noqa: ANN001
    db = str(tmp_path / "s.db")
    await init_db(db)
    repo = SessionRepository(db)
    await repo.update_trigger_message(42, 555)  # P
    await repo.save(42, "sess-1")
    return repo


async def test_b_message_turn_with_locked_db_does_not_ping_a_after_restart(
    tmp_path, monkeypatch
) -> None:  # noqa: ANN001
    """P's turn is on record. B's message turn runs; its trigger write fails
    (database is locked). After a restart the ❌ for that thread must not ping
    P, who did not ask."""
    _quiet(monkeypatch)
    repo = await _p_on_record(tmp_path)
    try:
        await _locked_chat_turn(repo)
        bot, channels = _bot({555: _msg(555, P), 556: _msg(556, B)})
        before = await _api_error(bot, repo, 42, channels)
        turn_events.reset()  # restart
        after = await _api_error(bot, repo, 42, channels)
    finally:
        turn_events.reset()
    assert before.endswith(f"<@{B}>"), before
    assert not after.endswith(f"<@{P}>"), after


async def test_a_refused_trigger_write_is_logged(tmp_path, monkeypatch, caplog) -> None:  # noqa: ANN001
    """#678: the refused write leaves a WARNING naming the thread."""
    _quiet(monkeypatch)
    repo = await _p_on_record(tmp_path)
    with caplog.at_level(logging.WARNING):
        try:
            await _locked_chat_turn(repo)
        finally:
            turn_events.reset()
    assert any(
        r.levelno == logging.WARNING and "thread=42" in r.getMessage() for r in caplog.records
    ), [r.getMessage() for r in caplog.records]


async def _refused_update(repo: SessionRepository, mid: int) -> None:
    conn = _locked(repo.db_path)
    try:
        try:
            await repo.update_trigger_message(42, mid)
        except sqlite3.OperationalError:
            return
        raise AssertionError("the write should have been refused")
    finally:
        conn.rollback()
        conn.close()


async def test_a_refused_trigger_write_is_seen_by_get_and_lands_with_the_next_save(
    tmp_path,
) -> None:  # noqa: ANN001
    repo = await _p_on_record(tmp_path)
    await _refused_update(repo, 556)
    record = await repo.get(42)
    assert record is not None and record.trigger_message_id == 556
    assert _row_trigger(repo.db_path) == 555
    await repo.save(42, "sess-1")
    assert _row_trigger(repo.db_path) == 556


async def test_a_refused_trigger_write_is_written_at_shutdown(tmp_path) -> None:  # noqa: ANN001
    repo = await _p_on_record(tmp_path)
    await _refused_update(repo, 556)
    await repo.flush_unapplied_clears()
    assert _row_trigger(repo.db_path) == 556


async def test_a_newer_write_replaces_a_refused_one(tmp_path) -> None:  # noqa: ANN001
    repo = await _p_on_record(tmp_path)
    await _refused_update(repo, 556)
    await repo.update_trigger_message(42, 557)
    await repo.save(42, "sess-1")
    assert _row_trigger(repo.db_path) == 557


async def test_a_refused_write_that_is_cleared_is_not_written(tmp_path) -> None:  # noqa: ANN001
    """A refused write whose turn is then forgotten (a /skill sent after it, or
    it never reached the pane) ends as if it had landed and been forgotten:
    nothing on record — neither 556 nor P's older 555 (P did not ask)."""
    repo = await _p_on_record(tmp_path)
    await _refused_update(repo, 556)
    await repo.clear_trigger_message(42, only={556})
    record = await repo.get(42)
    assert record is not None and record.trigger_message_id is None
    await repo.save(42, "sess-1")
    await repo.flush_unapplied_clears()
    assert _row_trigger(repo.db_path) is None
