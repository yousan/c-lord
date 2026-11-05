"""``/skill`` and a message turn racing for the trigger on record (#1262).

A ``/skill`` run in an existing thread has no trigger message of its own, so it
makes the earlier turns' stop counting (#1234). Claude's pane answers the input
it got last, so which turn is "the latest" is decided by the order in which the
prompts *reached the pane* — not by when a trigger was recorded, and not by when
a cog called ``run_claude_with_config`` (the runner sends much later: after
SYSTEM, the session save, the menu check). Ways the wrong person was pinged
after a restart, or ``/skill``'s answer was posted as a reply to someone else:

* A's message turn sets the trigger in memory, and its DB write is still
  running when ``/skill`` looks. ``/skill`` forgot only the old value on the
  row, and A's write landed afterwards: after a restart A was pinged.
* A's message arrives while ``/skill`` prepares, and A's prompt reaches the pane
  *before* ``/skill``'s. ``/skill`` is the last turn, but A's trigger stayed.
* A's prompt reaches the pane *after* ``/skill``'s: A's is the last turn and must
  keep its reply — a guess of "sent" made before the real send took it away.
* The DB is locked when ``/skill`` clears the record: the #1234 symptom came back.

Real SQLite, real ``SkillCommandCog._run_skill_impl``, real
``ClaudeChatCog._run_claude``, real ``TranscriptMirrorCog``. The fake runs call
``RunConfig.on_prompt_sent`` where the real runner does: after the SYSTEM save,
when the prompt is in the pane.
"""

from __future__ import annotations

import asyncio
import contextlib
import sqlite3
from collections.abc import Awaitable, Callable
from unittest.mock import AsyncMock, MagicMock, patch

import discord

from c_lord import turn_events
from c_lord.cogs import skill_command
from c_lord.cogs.transcript_mirror import TranscriptMirrorCog
from c_lord.database.models import init_db
from c_lord.database.repository import SessionRepository
from tests.test_trigger_on_record import OWNER, _api_error, _bot, _msg, _skill_turn
from tests.test_workspace_failure import _make_cog as _make_chat_cog
from tests.test_workspace_failure import _message as _chat_message

A = 4242  # author of tests.test_workspace_failure._message()
P = 777  # an earlier requester (message 555)
S = 888  # runs /skill


def _locked(db: str) -> sqlite3.Connection:
    """Hold an exclusive lock on *db* — another writer (a slow migration, a
    second process, a long transaction) makes c-lord's write fail as 'locked'."""
    conn = sqlite3.connect(db, timeout=0, isolation_level=None)
    conn.execute("BEGIN EXCLUSIVE")
    return conn


async def _chat_turn(
    repo: SessionRepository,
    mirror: TranscriptMirrorCog,
    mid: int,
    *,
    before_send: Callable[[], Awaitable[None]] | None = None,
    before_run: Callable[[], Awaitable[None]] | None = None,
    delivered: bool = True,
    sending: bool = False,
) -> None:
    """A's message turn through the real ClaudeChatCog._run_claude, to its end.

    ``sending=True``: the runner has begun the send (``on_send_started``) before
    *before_send* — the keys may land even if the turn is cancelled there.

    *before_run* runs when the trigger is recorded and the Stop notice is being
    posted — before ``run_claude_with_config``. The run does what the runner
    does, in its order: begin the turn, save at SYSTEM, then (after
    *before_send*) the prompt reaches the pane."""
    cog = _make_chat_cog()
    cog.repo = repo
    cog.bot.transcript_mirror_cog = mirror
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
    notice = MagicMock()

    async def send(*args, **kwargs) -> MagicMock:  # noqa: ANN002, ANN003
        if before_run is not None:
            await before_run()
        return notice

    thread.send = AsyncMock(side_effect=send)
    message = _chat_message()
    message.id = mid
    message.webhook_id = None

    async def run(config) -> None:  # noqa: ANN001
        turn_events.begin_turn(config.thread, config.started_by or turn_events.StartedBy("user"))
        await repo.save(42, "sess-1")
        if sending and (started := getattr(config, "on_send_started", None)) is not None:
            started()
        if before_send is not None:
            await before_send()
        # RunConfig.on_prompt_sent (#1262); absent before it existed.
        if delivered and (sent := getattr(config, "on_prompt_sent", None)) is not None:
            await sent()

    with patch("c_lord.cogs.claude_chat.run_claude_with_config", side_effect=run):
        await cog._run_claude(message, thread, "hi", None)


async def _setup(tmp_path):  # noqa: ANN001, ANN202
    db = str(tmp_path / "s.db")
    await init_db(db)
    repo = SessionRepository(db)
    await repo.update_trigger_message(42, 555)  # P's earlier message
    await repo.save(42, "sess-1")
    bot, channels = _bot({555: _msg(555, P), 556: _msg(556, A)})
    mirror = TranscriptMirrorCog(bot, session_repo=repo)
    mirror.set_trigger_message(42, 555)
    return repo, bot, channels, mirror


async def test_db_write_landing_after_the_skill_started_does_not_ping_a(
    tmp_path, monkeypatch
) -> None:  # noqa: ANN001
    """556 is in memory when /skill starts and its DB write lands during the
    prepare, but no message turn is on its way to the pane with it (not in
    flight, never sent) — it is an earlier turn's. /skill is the last turn: 556
    is forgotten from memory and from the row together (they used to disagree),
    and a restart does not ping A."""
    monkeypatch.delenv(turn_events.ENV_LOG, raising=False)
    monkeypatch.delenv(turn_events.ENV_URL, raising=False)
    repo, bot, channels, mirror = await _setup(tmp_path)
    mirror.set_trigger_message(42, 556)  # claude_chat: set_trigger_message (sync)

    async def prepare(self, **kw):  # noqa: ANN001, ANN003, ANN202
        await repo.update_trigger_message(42, 556)  # claude_chat's DB write lands now
        return True, "/sessions/999/42"

    turn_events.reset()
    try:
        with patch.object(skill_command.SkillCommandCog, "_prepare_workspace", prepare):
            await _skill_turn(repo, 42, S, mirror=mirror)
        mem = mirror.trigger_message(42)
        turn_events.reset()  # restart
        after = await _api_error(bot, repo, 42, channels)
    finally:
        turn_events.reset()
    record = await repo.get(42)
    assert mem is None, mem
    assert record is not None and record.trigger_message_id is None, record.trigger_message_id
    assert after.endswith(f"<@{OWNER}>"), after


async def test_a_prompt_in_the_pane_before_skill_prepare_ends_does_not_own_the_answer(
    tmp_path, monkeypatch
) -> None:  # noqa: ANN001
    """S runs /skill; during its prepare A's message turn runs and ends (A's
    prompt reached the pane before /skill's). The last turn is S's /skill — its
    answer must not be a reply to A's message, and after a restart its ❌ must
    not ping A."""
    monkeypatch.delenv(turn_events.ENV_LOG, raising=False)
    monkeypatch.delenv(turn_events.ENV_URL, raising=False)
    repo, bot, channels, mirror = await _setup(tmp_path)

    async def prepare(self, **kw):  # noqa: ANN001, ANN003, ANN202
        await _chat_turn(repo, mirror, 556)
        return True, "/sessions/999/42"

    turn_events.reset()
    try:
        with patch.object(skill_command.SkillCommandCog, "_prepare_workspace", prepare):
            await _skill_turn(repo, 42, S, mirror=mirror)
        ref = await mirror._build_trigger_reference(42)
        before = await _api_error(bot, repo, 42, channels)
        turn_events.reset()  # restart
        after = await _api_error(bot, repo, 42, channels)
    finally:
        turn_events.reset()
    record = await repo.get(42)
    assert before.endswith(f"<@{S}>"), before
    assert ref is None or ref.message_id != 556, f"S's answer replies to A's {ref.message_id}"
    assert not after.endswith(f"<@{A}>"), (after, record.trigger_message_id if record else None)


async def test_a_prompt_in_the_pane_after_skill_run_began_but_before_its_send(
    tmp_path,
) -> None:  # noqa: ANN001
    """A's message is recorded during /skill's prepare; /skill's run starts and
    saves at SYSTEM; A's turn then runs and its prompt reaches the pane; only
    then /skill's does. The pane answers /skill last — its answer is not a reply
    to A, and A is not on the row."""
    repo, _bot_, _ch, mirror = await _setup(tmp_path)
    a_noted = asyncio.Event()
    a_may_send = asyncio.Event()
    a_sent = asyncio.Event()
    tasks: list[asyncio.Task[None]] = []

    async def a_before_run() -> None:
        a_noted.set()
        await a_may_send.wait()

    async def a_turn() -> None:
        await _chat_turn(repo, mirror, 556, before_run=a_before_run)
        a_sent.set()

    async def prepare(self, **kw):  # noqa: ANN001, ANN003, ANN202
        tasks.append(asyncio.create_task(a_turn()))
        await a_noted.wait()  # A's trigger is recorded, its prompt not sent yet
        return True, "/sessions/999/42"

    async def skill_before_send() -> None:
        a_may_send.set()
        await a_sent.wait()  # A's prompt reaches the pane first

    turn_events.reset()
    try:
        with patch.object(skill_command.SkillCommandCog, "_prepare_workspace", prepare):
            await _skill_turn(repo, 42, S, mirror=mirror, before_send=skill_before_send)
        await asyncio.gather(*tasks)
    finally:
        turn_events.reset()
    ref = await mirror._build_trigger_reference(42)
    record = await repo.get(42)
    assert ref is None or ref.message_id != 556, f"S's answer replies to A's {ref.message_id}"
    assert record is not None and record.trigger_message_id is None, record.trigger_message_id


async def test_a_prompt_in_the_pane_after_skills_keeps_its_reply(tmp_path) -> None:  # noqa: ANN001
    """Control, through the real "sent" path: A's message is recorded during
    /skill's prepare, and A's prompt reaches the pane only after /skill's. A's
    is the last turn: its answer replies to A, and A stays on the row."""
    repo, _bot_, _ch, mirror = await _setup(tmp_path)
    a_noted = asyncio.Event()
    a_may_send = asyncio.Event()
    tasks: list[asyncio.Task[None]] = []

    async def a_before_send() -> None:
        a_noted.set()
        await a_may_send.wait()

    async def prepare(self, **kw):  # noqa: ANN001, ANN003, ANN202
        tasks.append(asyncio.create_task(_chat_turn(repo, mirror, 556, before_send=a_before_send)))
        await a_noted.wait()
        return True, "/sessions/999/42"

    async def skill_after_send() -> None:
        a_may_send.set()  # A's prompt goes in after /skill's
        await asyncio.gather(*tasks)

    turn_events.reset()
    try:
        with patch.object(skill_command.SkillCommandCog, "_prepare_workspace", prepare):
            await _skill_turn(repo, 42, S, mirror=mirror, after_send=skill_after_send)
    finally:
        turn_events.reset()
    assert mirror.trigger_message(42) == 556
    ref = await mirror._build_trigger_reference(42)
    assert ref is not None and ref.message_id == 556
    record = await repo.get(42)
    assert record is not None and record.trigger_message_id == 556


async def test_skill_turn_with_locked_db_does_not_ping_a_after_restart(
    tmp_path, monkeypatch
) -> None:  # noqa: ANN001
    """#1234 with the clear failing (database is locked): S's /skill turn, then a
    restart. The ❌ must not ping P, who asked for the turn before."""
    monkeypatch.delenv(turn_events.ENV_LOG, raising=False)
    monkeypatch.delenv(turn_events.ENV_URL, raising=False)
    db = str(tmp_path / "s.db")
    await init_db(db)
    repo = SessionRepository(db)
    await repo.update_trigger_message(42, 555)  # P
    await repo.save(42, "sess-1")
    real_clear = repo.clear_trigger_message

    async def clear(thread_id: int, **kw) -> None:  # noqa: ANN003
        conn = _locked(db)
        try:
            await real_clear(thread_id, **kw)
        finally:
            conn.rollback()
            conn.close()

    repo.clear_trigger_message = clear  # type: ignore[method-assign]
    turn_events.reset()
    try:
        await _skill_turn(repo, 42, S)
        bot, channels = _bot({555: _msg(555, P)})
        turn_events.reset()  # restart
        after = await _api_error(bot, repo, 42, channels)
    finally:
        turn_events.reset()
    assert not after.endswith(f"<@{P}>"), after


def _row_trigger(db: str) -> int | None:
    conn = sqlite3.connect(db)
    try:
        return conn.execute(
            "SELECT trigger_message_id FROM sessions WHERE thread_id = 42"
        ).fetchone()[0]
    finally:
        conn.close()


async def _refused_clear(repo: SessionRepository, ids: set[int]) -> None:
    conn = _locked(repo.db_path)
    try:
        try:
            await repo.clear_trigger_message(42, only=ids)
        except sqlite3.OperationalError:
            return
        raise AssertionError("the clear should have been refused")
    finally:
        conn.rollback()
        conn.close()


async def test_a_clear_refused_by_a_locked_db_lands_with_the_next_save(tmp_path) -> None:  # noqa: ANN001
    """The clear fails (locked): the ids are kept — hidden from reads in this
    process, and written by the thread's next session save."""
    db = str(tmp_path / "s.db")
    await init_db(db)
    repo = SessionRepository(db)
    await repo.update_trigger_message(42, 555)
    await repo.save(42, "sess-1")
    await _refused_clear(repo, {555})
    assert _row_trigger(db) == 555
    record = await repo.get(42)
    assert record is not None and record.trigger_message_id is None
    await repo.save(42, "sess-1")
    assert _row_trigger(db) is None


async def test_a_refused_clear_never_wipes_a_newer_message(tmp_path) -> None:  # noqa: ANN001
    """A refused clear names exact ids: a newer message written to the row in the
    meantime stays, through reads and through the next save (so no separate "a
    newer write cancels the waiting clear" rule is needed)."""
    db = str(tmp_path / "s.db")
    await init_db(db)
    repo = SessionRepository(db)
    await repo.update_trigger_message(42, 555)
    await repo.save(42, "sess-1")
    await _refused_clear(repo, {555})
    await repo.update_trigger_message(42, 556)
    record = await repo.get(42)
    assert record is not None and record.trigger_message_id == 556
    await repo.save(42, "sess-1")
    assert _row_trigger(db) == 556


async def test_a_turn_in_flight_when_skill_starts_and_sent_after_it_keeps_its_reply(
    tmp_path,
) -> None:  # noqa: ANN001
    """A's trigger is recorded (memory and row) and then /skill starts — so it is
    in /skill's snapshot. A is still posting its Stop notice and saving at SYSTEM,
    so /skill's prompt reaches the pane first and A's goes last. A owns the
    answer: its reply and its row stay."""
    repo, _bot_, _ch, mirror = await _setup(tmp_path)
    a_noted = asyncio.Event()
    a_may_go = asyncio.Event()

    async def a_before_run() -> None:
        a_noted.set()
        await a_may_go.wait()

    a_task = asyncio.create_task(_chat_turn(repo, mirror, 556, before_run=a_before_run))
    await a_noted.wait()  # A's trigger is on record, its prompt not sent

    async def skill_after_send() -> None:
        a_may_go.set()  # A's prompt reaches the pane after /skill's
        await a_task

    turn_events.reset()
    try:
        await _skill_turn(repo, 42, S, mirror=mirror, after_send=skill_after_send)
    finally:
        turn_events.reset()
    assert repo.sent_trigger(42) == 556
    assert mirror.trigger_message(42) == 556
    ref = await mirror._build_trigger_reference(42)
    assert ref is not None and ref.message_id == 556
    record = await repo.get(42)
    assert record is not None and record.trigger_message_id == 556


async def test_a_turn_that_ended_without_reaching_the_pane_is_no_longer_in_flight(
    tmp_path,
) -> None:  # noqa: ANN001
    """A's message turn recorded 556 and ended without its prompt reaching the
    pane (the send failed). It is not on its way any more, so a later /skill —
    the last turn the pane got — forgets it from memory and row."""
    repo, _bot_, _ch, mirror = await _setup(tmp_path)
    turn_events.reset()
    try:
        await _chat_turn(repo, mirror, 556, delivered=False)
        await _skill_turn(repo, 42, S, mirror=mirror)
    finally:
        turn_events.reset()
    assert not repo.trigger_in_flight(42, 556)
    assert mirror.trigger_message(42) is None
    assert _row_trigger(repo.db_path) is None


async def test_a_skill_whose_send_failed_forgets_nothing(tmp_path) -> None:  # noqa: ANN001
    """/skill saves at SYSTEM but its prompt never reaches the pane. P's turn is
    still the last one the pane got: memory and row both keep P's 555."""
    repo, _bot_, _ch, mirror = await _setup(tmp_path)
    turn_events.reset()
    try:
        await _skill_turn(repo, 42, S, mirror=mirror, delivered=False)
    finally:
        turn_events.reset()
    assert mirror.trigger_message(42) == 555
    assert _row_trigger(repo.db_path) == 555


async def test_two_skills_back_to_back_leave_nothing_of_the_earlier_requester(
    tmp_path, monkeypatch
) -> None:  # noqa: ANN001
    """Guard: /skill twice in a row (S, then 999) — P's trigger is gone from
    memory and record; a restart pings the owner."""
    monkeypatch.delenv(turn_events.ENV_LOG, raising=False)
    monkeypatch.delenv(turn_events.ENV_URL, raising=False)
    repo, bot, channels, mirror = await _setup(tmp_path)
    turn_events.reset()
    try:
        await _skill_turn(repo, 42, S, mirror=mirror)
        await _skill_turn(repo, 42, 999, mirror=mirror)
        assert await mirror._build_trigger_reference(42) is None
        turn_events.reset()
        after = await _api_error(bot, repo, 42, channels)
    finally:
        turn_events.reset()
    assert after.endswith(f"<@{OWNER}>"), after


async def test_pending_trigger_and_a_newer_row_during_prepare(tmp_path) -> None:  # noqa: ANN001
    """Guard: P's first-turn 555 is pending (no row) when /skill starts. During
    prepare P's turn reaches SYSTEM (row=555) and A's message 556 is recorded but
    not sent to the pane yet. /skill forgets 555 only; A's 556 stays."""
    db = str(tmp_path / "s.db")
    await init_db(db)
    repo = SessionRepository(db)
    await repo.update_trigger_message(42, 555)
    bot, _ = _bot({556: _msg(556, A)})
    mirror = TranscriptMirrorCog(bot, session_repo=repo)

    async def prepare(self, **kw):  # noqa: ANN001, ANN003, ANN202
        await repo.save(42, "sess-1")
        mirror.set_trigger_message(42, 556)
        await repo.update_trigger_message(42, 556)
        return True, "/sessions/999/42"

    turn_events.reset()
    try:
        with patch.object(skill_command.SkillCommandCog, "_prepare_workspace", prepare):
            await _skill_turn(repo, 42, S, mirror=mirror)
    finally:
        turn_events.reset()
    record = await repo.get(42)
    assert record is not None and record.trigger_message_id == 556
    assert repo.pending_trigger(42) is None


# -- Where "sent" is decided: the runner, after the send succeeds -------------


def _tmux(*, running: bool, send_ok: bool = True, calls: list[str] | None = None) -> MagicMock:
    log = calls if calls is not None else []
    mgr = MagicMock()
    mgr.capture_pane.return_value = ""
    mgr.is_claude_running.return_value = running
    mgr.send_input.side_effect = lambda *a, **k: log.append("send_input") or send_ok
    mgr.start_claude.side_effect = lambda *a, **k: log.append("start_claude") or True
    mgr.input_box_holds.return_value = False
    mgr.duplicate_window_names.return_value = []
    mgr.session_name = "clord"
    mgr._find_window_for_thread.return_value = "work1"
    return mgr


async def _until_hook_or_end(runner, calls: list[str]) -> list:  # noqa: ANN001
    """Drive *runner* until its hook ran or it yielded a final event."""
    from c_lord.claude.tmux_runner import TmuxClaudeRunner

    assert isinstance(runner, TmuxClaudeRunner)
    events: list = []
    agen = runner.run("hi")

    async def go() -> None:
        async for event in agen:
            events.append(event)
            if event.is_complete:
                return

    task = asyncio.create_task(go())
    for _ in range(200):
        if "hook" in calls or task.done():
            break
        await asyncio.sleep(0.01)
    task.cancel()
    with contextlib.suppress(asyncio.CancelledError, Exception):
        await task
    await agen.aclose()
    return events


async def test_the_runner_reports_the_prompt_only_after_send_input_succeeded() -> None:
    from c_lord.claude.tmux_runner import TmuxClaudeRunner

    calls: list[str] = []
    runner = TmuxClaudeRunner(_tmux(running=True, calls=calls), thread_id=42)

    async def hook() -> None:
        calls.append("hook")

    runner.on_prompt_sent = hook
    runner.on_send_started = lambda: calls.append("started")
    with patch.object(TmuxClaudeRunner, "peek_pending_ask", AsyncMock(return_value=None)):
        await _until_hook_or_end(runner, calls)
    assert calls[:3] == ["started", "send_input", "hook"], calls


async def test_the_runner_does_not_report_a_prompt_that_was_not_delivered() -> None:
    from c_lord.claude.tmux_runner import TmuxClaudeRunner

    calls: list[str] = []
    runner = TmuxClaudeRunner(_tmux(running=True, send_ok=False, calls=calls), thread_id=42)

    async def hook() -> None:
        calls.append("hook")

    runner.on_prompt_sent = hook
    with patch.object(TmuxClaudeRunner, "peek_pending_ask", AsyncMock(return_value=None)):
        events = await _until_hook_or_end(runner, calls)
    assert "hook" not in calls, calls
    assert events and events[-1].is_complete and events[-1].error


async def test_the_runner_reports_the_prompt_of_a_cold_start() -> None:
    from c_lord.claude.tmux_runner import TmuxClaudeRunner

    calls: list[str] = []
    runner = TmuxClaudeRunner(_tmux(running=False, calls=calls), thread_id=42)

    async def hook() -> None:
        calls.append("hook")

    runner.on_prompt_sent = hook
    runner.on_send_started = lambda: calls.append("started")
    with patch.object(TmuxClaudeRunner, "_handle_startup_prompts", AsyncMock()):
        await _until_hook_or_end(runner, calls)
    assert calls[:3] == ["started", "start_claude", "hook"], calls


async def test_run_claude_with_config_hands_on_prompt_sent_to_the_runner(monkeypatch) -> None:  # noqa: ANN001
    from c_lord.claude.types import MessageType, StreamEvent
    from c_lord.cogs import _run_helper
    from tests.test_run_helper_paths import _config

    for var in (turn_events.ENV_LOG, turn_events.ENV_URL, turn_events.ENV_SECRET):
        monkeypatch.delenv(var, raising=False)
    monkeypatch.setattr(_run_helper, "_post_context_usage", AsyncMock())
    _run_helper._IN_FLIGHT.clear()
    calls: list[str] = []

    async def hook() -> None:
        calls.append("hook")

    config = _config(on_prompt_sent=hook, on_send_started=lambda: calls.append("started"))
    runner = config.runner

    async def run(prompt: str, session_id: str | None = None):  # noqa: ANN202
        yield StreamEvent(message_type=MessageType.SYSTEM, session_id="s1")
        runner.on_send_started()
        await runner.on_prompt_sent()
        yield StreamEvent(message_type=MessageType.RESULT, is_complete=True)

    runner.run = run
    turn_events.reset()
    try:
        await _run_helper.run_claude_with_config(config)
    finally:
        turn_events.reset()
        _run_helper._IN_FLIGHT.clear()
    assert calls == ["started", "hook"]


# -- Shutdown: refused clears are written before the process stops ----------


async def test_a_refused_clear_is_written_at_shutdown(tmp_path) -> None:  # noqa: ANN001
    """A refused clear waits in memory. At shutdown (the DB free again) the chat
    cog's unload writes it, so a real restart does not bring P's trigger back."""
    db = str(tmp_path / "s.db")
    await init_db(db)
    repo = SessionRepository(db)
    await repo.update_trigger_message(42, 555)
    await repo.save(42, "sess-1")
    await _refused_clear(repo, {555})
    assert _row_trigger(db) == 555
    cog = _make_chat_cog()
    cog.repo = repo
    await cog.cog_unload()
    assert _row_trigger(db) is None
    assert not repo.clear_is_waiting(42, 555)


async def test_the_shutdown_lamp_write_back_skips_a_forgotten_trigger(tmp_path) -> None:  # noqa: ANN001
    """#870 writes a running turn's lamp message back at shutdown. One whose clear
    is still waiting (a /skill forgot it) is not written back."""
    db = str(tmp_path / "s.db")
    await init_db(db)
    repo = SessionRepository(db)
    await repo.update_trigger_message(42, 555)
    await repo.save(42, "sess-1")
    await _refused_clear(repo, {555})
    cog = _make_chat_cog()
    cog.repo = repo
    cog._resume_repo = MagicMock(mark=AsyncMock())
    cog._active_runners[42] = MagicMock()
    cog._turn_triggers[42] = 555
    written: list[int] = []
    real_update = repo.update_trigger_message

    async def update(thread_id: int, message_id: int) -> None:
        written.append(message_id)
        await real_update(thread_id, message_id)

    repo.update_trigger_message = update  # type: ignore[method-assign]
    await cog.cog_unload()
    assert written == []
    assert _row_trigger(db) is None
    cog._resume_repo.mark.assert_awaited()


async def test_a_turn_that_fails_before_its_run_is_not_left_in_flight(tmp_path) -> None:  # noqa: ANN001
    """The in-flight mark is set before the trigger is recorded; a turn that
    fails before ``run_claude_with_config`` (here: building its RunConfig) must
    not leave it, or a later /skill would never forget that trigger."""
    import pytest

    repo, _bot_, _ch, mirror = await _setup(tmp_path)
    with (
        patch("c_lord.cogs.claude_chat.RunConfig", side_effect=RuntimeError("boom")),
        pytest.raises(RuntimeError),
    ):
        await _chat_turn(repo, mirror, 556)
    assert not repo.trigger_in_flight(42, 556)


# -- A message turn whose prompt never reached the pane owns no answer --------


async def _after_restart(db: str) -> str:
    """The ❌ a fresh process (new repository on the same file) posts for 42."""
    repo2 = SessionRepository(db)
    bot, channels = _bot({555: _msg(555, P), 556: _msg(556, A)})
    turn_events.reset()
    return await _api_error(bot, repo2, 42, channels)


async def test_a_in_flight_when_skill_sent_then_a_send_fails(tmp_path, monkeypatch) -> None:  # noqa: ANN001
    """W57 round 13: A's message is recorded during /skill's prepare; /skill's
    prompt reaches the pane while A is in flight; then A's send FAILS (A gets its
    own ❌). The pane's last input is S's /skill — its answer must not reply to
    A, and after a real restart (a new repository) its ❌ must not ping A."""
    monkeypatch.delenv(turn_events.ENV_LOG, raising=False)
    monkeypatch.delenv(turn_events.ENV_URL, raising=False)
    repo, _bot_, _ch, mirror = await _setup(tmp_path)
    a_noted = asyncio.Event()
    a_may_send = asyncio.Event()
    tasks: list[asyncio.Task[None]] = []

    async def a_before_send() -> None:
        a_noted.set()
        await a_may_send.wait()

    async def prepare(self, **kw):  # noqa: ANN001, ANN003, ANN202
        tasks.append(
            asyncio.create_task(
                _chat_turn(repo, mirror, 556, before_send=a_before_send, delivered=False)
            )
        )
        await a_noted.wait()
        return True, "/sessions/999/42"

    async def skill_after_send() -> None:
        a_may_send.set()
        await asyncio.gather(*tasks)

    turn_events.reset()
    try:
        with patch.object(skill_command.SkillCommandCog, "_prepare_workspace", prepare):
            await _skill_turn(repo, 42, S, mirror=mirror, after_send=skill_after_send)
        ref = await mirror._build_trigger_reference(42)
        after = await _after_restart(repo.db_path)
    finally:
        turn_events.reset()
    assert (ref is None or ref.message_id != 556) and not after.endswith(f"<@{A}>"), (
        "reply_to",
        ref and ref.message_id,
        "after_restart",
        after,
        "row",
        _row_trigger(repo.db_path),
    )


async def test_a_turn_recorded_and_ended_undelivered_during_prepare(tmp_path, monkeypatch) -> None:  # noqa: ANN001
    """W57 round 13: same harm, but A's turn (send failed) is already over
    before /skill's prompt reaches the pane — never in the pane, so it owns no
    answer."""
    monkeypatch.delenv(turn_events.ENV_LOG, raising=False)
    monkeypatch.delenv(turn_events.ENV_URL, raising=False)
    repo, _bot_, _ch, mirror = await _setup(tmp_path)

    async def prepare(self, **kw):  # noqa: ANN001, ANN003, ANN202
        await _chat_turn(repo, mirror, 556, delivered=False)
        return True, "/sessions/999/42"

    turn_events.reset()
    try:
        with patch.object(skill_command.SkillCommandCog, "_prepare_workspace", prepare):
            await _skill_turn(repo, 42, S, mirror=mirror)
        ref = await mirror._build_trigger_reference(42)
        after = await _after_restart(repo.db_path)
    finally:
        turn_events.reset()
    assert not repo.trigger_in_flight(42, 556)
    assert (ref is None or ref.message_id != 556) and not after.endswith(f"<@{A}>"), (
        "reply_to",
        ref and ref.message_id,
        "after_restart",
        after,
        "row",
        _row_trigger(repo.db_path),
    )


# -- PR's "known limit" (ii): the #870 shutdown write-back -------------------------


async def test_a_turn_cancelled_before_its_send_returned_keeps_its_trigger(tmp_path) -> None:  # noqa: ANN001
    """A's turn is cancelled (a shutdown) while its send_input is still running
    in the worker thread. The keystrokes may well have landed — Claude answers
    after the restart — so the trigger is not dropped as "never delivered"."""
    repo, _bot_, _ch, mirror = await _setup(tmp_path)
    sending = asyncio.Event()

    async def before_send() -> None:
        sending.set()
        await asyncio.Event().wait()  # send_input still running

    task = asyncio.create_task(_chat_turn(repo, mirror, 556, before_send=before_send, sending=True))
    await sending.wait()
    task.cancel()
    with contextlib.suppress(asyncio.CancelledError):
        await task
    assert not repo.trigger_in_flight(42, 556)
    assert mirror.trigger_message(42) == 556
    assert _row_trigger(repo.db_path) == 556


def _resume_runner(calls: list[str], *, resume_failed: bool, start_ok: bool = True):  # noqa: ANN202
    from c_lord.claude.tmux_runner import TmuxClaudeRunner

    mgr = _tmux(running=False, calls=calls)
    mgr.start_claude.side_effect = lambda *a, **k: calls.append("start_claude") or start_ok
    runner = TmuxClaudeRunner(mgr, thread_id=42, try_continue=True)

    async def hook() -> None:
        calls.append("hook")

    runner.on_prompt_sent = hook
    runner.on_send_started = lambda: calls.append("started")
    return runner, [
        patch.object(TmuxClaudeRunner, "_handle_startup_prompts", AsyncMock()),
        patch.object(TmuxClaudeRunner, "_note_resume_baseline", AsyncMock()),
        patch.object(TmuxClaudeRunner, "_resume_failed", AsyncMock(return_value=resume_failed)),
    ]


async def test_the_runner_reports_the_prompt_of_a_resume() -> None:
    """Restart-resume (``--continue``): the hook fires once the start succeeded."""
    calls: list[str] = []
    runner, patches = _resume_runner(calls, resume_failed=False)
    with contextlib.ExitStack() as stack:
        for p in patches:
            stack.enter_context(p)
        await _until_hook_or_end(runner, calls)
    assert calls[:3] == ["started", "start_claude", "hook"], calls


async def test_the_runner_reports_the_prompt_once_after_a_resume_falls_back() -> None:
    """``--continue`` found no session, so a fresh start follows: the hook fires
    once, after the fresh start — not after the failed resume."""
    calls: list[str] = []
    runner, patches = _resume_runner(calls, resume_failed=True)
    with contextlib.ExitStack() as stack:
        for p in patches:
            stack.enter_context(p)
        await _until_hook_or_end(runner, calls)
    assert calls[:5] == ["started", "start_claude", "started", "start_claude", "hook"], calls
    assert calls.count("hook") == 1


async def test_the_runner_does_not_report_a_cold_start_that_failed() -> None:
    from c_lord.claude.tmux_runner import TmuxClaudeRunner

    calls: list[str] = []
    mgr = _tmux(running=False, calls=calls)
    mgr.start_claude.side_effect = lambda *a, **k: calls.append("start_claude") or False
    runner = TmuxClaudeRunner(mgr, thread_id=42)

    async def hook() -> None:
        calls.append("hook")

    runner.on_prompt_sent = hook
    with (
        patch.object(TmuxClaudeRunner, "_handle_startup_prompts", AsyncMock()),
        patch.object(TmuxClaudeRunner, "_start_failure_reason", AsyncMock(return_value="x")),
    ):
        events = await _until_hook_or_end(runner, calls)
    assert "hook" not in calls, calls
    assert events and events[-1].is_complete and events[-1].error


# -- A turn cancelled before its send began owns no answer (W57 round 14) -----
#
# "Cancelled before the send" = A's task is cancelled while still posting its
# Stop notice (``before_run``: run_claude_with_config never started) or after
# its run started but before the runner began the send (``before_send``).


async def _cancel_a(where: str, repo, mirror):  # noqa: ANN001, ANN202
    """Start A's turn, wait until it parks at *where*, return (task, cancel())."""
    parked = asyncio.Event()

    async def park() -> None:
        parked.set()
        await asyncio.Event().wait()

    kw = {"before_run": park} if where == "before_run" else {"before_send": park}
    task = asyncio.create_task(_chat_turn(repo, mirror, 556, **kw))
    await parked.wait()

    async def cancel() -> None:
        task.cancel()
        with contextlib.suppress(asyncio.CancelledError):
            await task

    return task, cancel


def _verdict(repo, ref, after):  # noqa: ANN001, ANN202
    """S's answer must not reply to A, and a restart must not ping A."""
    return (
        (ref is None or ref.message_id != 556) and not after.endswith(f"<@{A}>"),
        ("reply_to", ref and ref.message_id, "after", after, "row", _row_trigger(repo.db_path)),
    )


async def _s2(tmp_path, monkeypatch, where: str):  # noqa: ANN001, ANN202
    monkeypatch.delenv(turn_events.ENV_LOG, raising=False)
    monkeypatch.delenv(turn_events.ENV_URL, raising=False)
    repo, _b, _c, mirror = await _setup(tmp_path)

    async def prepare(self, **kw):  # noqa: ANN001, ANN003, ANN202
        _t, cancel = await _cancel_a(where, repo, mirror)
        await cancel()  # A never reached run / never sent
        return True, "/sessions/999/42"

    turn_events.reset()
    try:
        with patch.object(skill_command.SkillCommandCog, "_prepare_workspace", prepare):
            await _skill_turn(repo, 42, S, mirror=mirror)
        ref = await mirror._build_trigger_reference(42)
        after = await _after_restart(repo.db_path)
    finally:
        turn_events.reset()
    return _verdict(repo, ref, after)


async def test_s2_a_cancelled_before_run_during_skill_prepare(tmp_path, monkeypatch) -> None:  # noqa: ANN001
    ok, info = await _s2(tmp_path, monkeypatch, "before_run")
    assert ok, info


async def test_s2_a_cancelled_before_send_during_skill_prepare(tmp_path, monkeypatch) -> None:  # noqa: ANN001
    ok, info = await _s2(tmp_path, monkeypatch, "before_send")
    assert ok, info


async def test_s3_skill_sent_while_a_in_flight_then_a_cancelled_before_send(
    tmp_path, monkeypatch
) -> None:  # noqa: ANN001
    monkeypatch.delenv(turn_events.ENV_LOG, raising=False)
    monkeypatch.delenv(turn_events.ENV_URL, raising=False)
    repo, _b, _c, mirror = await _setup(tmp_path)
    holder: list = []

    async def prepare(self, **kw):  # noqa: ANN001, ANN003, ANN202
        holder.append(await _cancel_a("before_send", repo, mirror))
        return True, "/sessions/999/42"

    async def skill_after_send() -> None:
        await holder[0][1]()  # A cancelled; its prompt was never sent

    turn_events.reset()
    try:
        with patch.object(skill_command.SkillCommandCog, "_prepare_workspace", prepare):
            await _skill_turn(repo, 42, S, mirror=mirror, after_send=skill_after_send)
        ref = await mirror._build_trigger_reference(42)
        after = await _after_restart(repo.db_path)
    finally:
        turn_events.reset()
    ok, info = _verdict(repo, ref, after)
    assert ok, info
