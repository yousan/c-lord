"""The trigger on record after the #1262 fix: the remaining wrong pings.

The trigger on record (``sessions.trigger_message_id``, #115) is who the ❌
pings after a restart (#1212) and which message the answer replies to. Ways it
still named the wrong person:

* #1254: B's message turn could not write its trigger (the database was locked).
  The failure was swallowed without a log, the row kept A's earlier message, and
  after a restart the ❌ pinged A, who had not asked.
* #1270: A1's prompt reached the pane and Claude is answering it; A2 is recorded
  and its send fails. A2's trigger was dropped as "never delivered" (#1262) and
  nothing was left: A1's answer was not a reply to A1, and after a restart the
  ❌ pinged the owner instead of A1's author.
* #1271: A's send was refused (``send_input`` returned False — nothing reached
  the pane) and the turn was then cancelled (a shutdown) while the runner asked
  the pane why. The send had begun, so the trigger was kept as "may have
  landed": after a restart the ❌ pinged A for a prompt the pane never got.

Real SQLite throughout (``SessionRepository``), real ``ClaudeChatCog._run_claude``.
"""

from __future__ import annotations

import asyncio
import contextlib
import logging
import sqlite3
import threading
from unittest.mock import AsyncMock, MagicMock, patch

import discord

from c_lord import turn_events
from c_lord.claude.tmux_runner import TmuxClaudeRunner
from c_lord.database.models import init_db
from c_lord.database.repository import SessionRepository
from tests.test_trigger_on_record import _api_error, _bot, _msg
from tests.test_trigger_skill_races_1262 import (
    _after_restart,
    _chat_turn,
    _locked,
    _row_trigger,
    _setup,
)
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


async def test_an_older_refused_write_does_not_override_a_newer_one(tmp_path, monkeypatch) -> None:  # noqa: ANN001
    """A's write (556) waits on the lock while B's later write (557) lands; A's
    write is then refused. 557 is the newer trigger: A's refusal must not keep
    556 for the next save to write over it."""
    from c_lord.database import repository as repository_mod

    repo = await _p_on_record(tmp_path)
    real_connect = repository_mod.aiosqlite.connect
    gate = asyncio.Event()
    waiting = asyncio.Event()
    calls = 0

    class _Refused:
        async def __aenter__(self):  # noqa: ANN204
            waiting.set()
            await gate.wait()
            raise sqlite3.OperationalError("database is locked")

        async def __aexit__(self, *exc) -> None:  # noqa: ANN002
            return None

    def connect(*args, **kwargs):  # noqa: ANN002, ANN003, ANN202
        nonlocal calls
        calls += 1
        return _Refused() if calls == 1 else real_connect(*args, **kwargs)

    monkeypatch.setattr(repository_mod.aiosqlite, "connect", connect)
    a_write = asyncio.create_task(repo.update_trigger_message(42, 556))
    await waiting.wait()
    await repo.update_trigger_message(42, 557)  # B's write lands
    gate.set()
    with contextlib.suppress(sqlite3.OperationalError):
        await a_write
    record = await repo.get(42)
    assert record is not None and record.trigger_message_id == 557, record.trigger_message_id
    await repo.save(42, "sess-1")
    assert _row_trigger(repo.db_path) == 557


async def test_a_deleted_thread_forgets_its_kept_trigger_state(tmp_path) -> None:  # noqa: ANN001
    """A refused write kept for thread 42 must not land on the row a later
    session makes for that thread after ``delete()``."""
    repo = await _p_on_record(tmp_path)
    await _refused_update(repo, 556)
    repo.note_trigger_sent(42, 555)
    repo.note_trigger_in_flight(42, 557)
    await repo.delete(42)
    assert repo.sent_trigger(42) is None
    assert not repo.trigger_in_flight(42, 557)
    assert repo.pending_trigger(42) is None
    await repo.save(42, "sess-2")
    assert _row_trigger(repo.db_path) is None
    record = await repo.get(42)
    assert record is not None and record.trigger_message_id is None


# -- #1270: an undelivered A2 dropped while A1 is being answered --------------------


async def test_a2_undelivered_while_a1_answering_keeps_a1(tmp_path, monkeypatch) -> None:  # noqa: ANN001
    """A1 (556) is in the pane and Claude answers it. A2 (557) is recorded and
    its send fails. A1's answer still replies to A1, and a restart's ❌ pings
    A1's author — not the owner."""
    _quiet(monkeypatch)
    repo, _b, _c, mirror = await _setup(tmp_path)
    gate = asyncio.Event()
    parked = asyncio.Event()

    async def a1_answering() -> None:  # A1's prompt is in the pane; Claude answers
        repo.note_trigger_sent(42, 556)  # what on_prompt_sent does
        parked.set()
        await gate.wait()

    t1 = asyncio.create_task(_chat_turn(repo, mirror, 556, before_send=a1_answering))
    try:
        await parked.wait()
        await _chat_turn(repo, mirror, 557, delivered=False)  # A2 never reaches the pane
        ref = await mirror._build_trigger_reference(42)  # A1's answer arrives now
        mem, row = mirror.trigger_message(42), _row_trigger(repo.db_path)
        after = await _after_restart(repo.db_path)  # bot restarts mid-answer
    finally:
        gate.set()
        await t1
        turn_events.reset()
    assert ref is not None and ref.message_id == 556, ("A1's answer replies to", ref, mem, row)
    assert after.endswith(f"<@{B}>"), ("restart ❌ pings", after, mem, row)


async def test_a2_undelivered_does_not_replace_a_newer_trigger(tmp_path, monkeypatch) -> None:  # noqa: ANN001
    """A1 (556) was delivered; A2 (557) is recorded; A3 (558) is recorded after
    it; then A2's send fails. A3's trigger is the newer one and stays."""
    _quiet(monkeypatch)
    repo, _b, _c, mirror = await _setup(tmp_path)
    repo.note_trigger_sent(42, 556)

    async def a3_arrives() -> None:  # A2's send is failing; A3 is recorded meanwhile
        mirror.set_trigger_message(42, 558)
        await repo.update_trigger_message(42, 558)

    try:
        await _chat_turn(repo, mirror, 557, before_send=a3_arrives, delivered=False)
    finally:
        turn_events.reset()
    assert mirror.trigger_message(42) == 558
    assert _row_trigger(repo.db_path) == 558


async def test_a2_undelivered_after_a_skill_restores_nothing(tmp_path, monkeypatch) -> None:  # noqa: ANN001
    """A /skill reached the pane after A1: the pane answers the /skill, so an
    undelivered A2 is dropped without bringing A1 back."""
    _quiet(monkeypatch)
    repo, _b, _c, mirror = await _setup(tmp_path)
    repo.note_trigger_sent(42, 556)
    repo.note_trigger_sent(42, None)  # a /skill's prompt reached the pane
    try:
        await _chat_turn(repo, mirror, 557, delivered=False)
    finally:
        turn_events.reset()
    assert mirror.trigger_message(42) is None
    assert _row_trigger(repo.db_path) is None


# -- #1271: a refused send, then cancelled before the turn ended --------------------


async def test_refused_send_then_cancelled_drops_trigger(tmp_path, monkeypatch) -> None:  # noqa: ANN001
    """The real TmuxClaudeRunner on a fake tmux: send_input is refused, and the
    turn is cancelled while the runner asks the pane why (``input_box_holds``).
    Nothing reached the pane, so A's trigger is dropped like any undelivered
    turn's, and a restart's ❌ does not ping A."""
    _quiet(monkeypatch)
    repo, _b, _c, mirror = await _setup(tmp_path)
    asking, release = threading.Event(), threading.Event()

    def holds(*a, **k):  # noqa: ANN002, ANN003, ANN202
        asking.set()
        release.wait(10)
        return False

    mgr = MagicMock()
    mgr.capture_pane.return_value = ""
    mgr.is_claude_running.return_value = True
    mgr.send_input.return_value = False  # tmux refused: nothing reached the pane
    mgr.input_box_holds.side_effect = holds
    mgr.duplicate_window_names.return_value = []
    mgr.session_name = "clord"
    mgr.create_session.return_value = "w1"

    cog = _make_chat_cog()
    cog.repo = repo
    cog.bot.transcript_mirror_cog = mirror
    cog._resolve_session_dir_manager = AsyncMock(  # type: ignore[method-assign]
        return_value=MagicMock(create_session_dir=MagicMock(return_value="/tmp/work"))
    )
    cog._resolve_tmux_manager = AsyncMock(return_value=mgr)  # type: ignore[method-assign]
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

    async def run(config) -> None:  # noqa: ANN001 — the hook wiring of _run_helper._run_turn
        runner = config.runner
        runner.on_prompt_sent = config.on_prompt_sent
        runner.on_send_started = config.on_send_started
        turn_events.begin_turn(config.thread, config.started_by or turn_events.StartedBy("user"))
        async for ev in runner.run(config.prompt, session_id=config.session_id):
            if ev.session_id:
                await repo.save(42, "sess-1")
            if ev.is_complete:
                return

    try:
        with (
            patch("c_lord.cogs.claude_chat.run_claude_with_config", side_effect=run),
            patch.object(TmuxClaudeRunner, "peek_pending_ask", AsyncMock(return_value=None)),
        ):
            task = asyncio.create_task(cog._run_claude(message, thread, "hi", None))
            for _ in range(500):
                if task.done() or asking.is_set():
                    break
                await asyncio.sleep(0.01)
            assert asking.is_set(), "never reached the refusal diagnosis"
            task.cancel()  # bot shutdown
            with contextlib.suppress(asyncio.CancelledError):
                await task
        mem, row = mirror.trigger_message(42), _row_trigger(repo.db_path)
        after = await _after_restart(repo.db_path)
    finally:
        release.set()
        turn_events.reset()
    assert mem != 556 and row != 556, ("A's trigger kept after a refused send", mem, row)
    assert not after.endswith(f"<@{B}>"), ("restart ❌ pings A", after)
