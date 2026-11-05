"""``/skill`` and a message turn racing for the trigger on record (#1262).

A ``/skill`` run in an existing thread has no trigger message of its own, so it
makes the previous turn's stop counting (#1234). #1252 did this by remembering
what was on record when ``/skill`` started and forgetting only that. Three ways
the wrong person was still pinged after a restart, or ``/skill``'s answer was
posted as a reply to someone else's message:

* A's message turn sets the trigger in memory, and its DB write is still
  running when ``/skill`` looks. ``/skill`` forgot only the old value on the
  row, and A's write landed afterwards: after a restart A — who did not ask for
  the ``/skill`` turn — was pinged.
* A's message arrives while ``/skill`` prepares its workspace, and A's turn is
  handed to Claude's pane *before* ``/skill``'s. ``/skill`` is the last turn, but
  A's trigger stayed: ``/skill``'s answer replied to A and a restart pinged A.
  (The control: A's turn handed over *after* ``/skill`` keeps A's trigger.)
* The DB is locked when ``/skill`` clears the record: the clear failed and the
  #1234 symptom came back.

Real SQLite, real ``SkillCommandCog._run_skill_impl``, real
``ClaudeChatCog._run_claude``, real ``TranscriptMirrorCog``.
"""

from __future__ import annotations

import sqlite3
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


async def _chat_turn(repo: SessionRepository, mirror: TranscriptMirrorCog, mid: int) -> None:
    """A's message turn through the real ClaudeChatCog._run_claude, to its end."""
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
    thread.send = AsyncMock(return_value=MagicMock())
    message = _chat_message()
    message.id = mid
    message.webhook_id = None

    async def run(config) -> None:  # noqa: ANN001
        turn_events.begin_turn(config.thread, config.started_by or turn_events.StartedBy("user"))
        await repo.save(42, "sess-1")

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
    """A's message comes just before /skill: claude_chat sets the memory (sync)
    then awaits the DB write; /skill looks at the record before that write
    commits. The memory and the record must agree afterwards, and a restart must
    not ping A unless A's trigger is still the one /skill's answer replies to."""
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
    assert record is not None
    assert (mem is None) == (record.trigger_message_id is None), (mem, record.trigger_message_id)
    assert not after.endswith(f"<@{A}>") or mem == 556, after


async def test_trigger_seen_only_in_memory_when_the_skill_started_is_forgotten_everywhere(
    tmp_path,
) -> None:  # noqa: ANN001
    """Same start as above, stricter: what /skill saw in memory when it started
    is on record as far as /skill is concerned, so it is forgotten from the row
    too once its write lands — not only from memory (#1262, W57's fix)."""
    repo, _bot_, _ch, mirror = await _setup(tmp_path)
    mirror.set_trigger_message(42, 556)

    async def prepare(self, **kw):  # noqa: ANN001, ANN003, ANN202
        await repo.update_trigger_message(42, 556)
        return True, "/sessions/999/42"

    turn_events.reset()
    try:
        with patch.object(skill_command.SkillCommandCog, "_prepare_workspace", prepare):
            await _skill_turn(repo, 42, S, mirror=mirror)
    finally:
        turn_events.reset()
    record = await repo.get(42)
    assert mirror.trigger_message(42) is None
    assert record is not None and record.trigger_message_id is None, record.trigger_message_id


async def test_a_turn_handed_to_claude_during_skill_prepare_does_not_own_the_answer(
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


async def test_a_turn_not_yet_handed_to_claude_keeps_its_reply(tmp_path) -> None:  # noqa: ANN001
    """Control: A's message arrives during /skill's prepare and its turn has not
    reached the pane yet, so it goes after /skill's and is the last one. A's
    answer replies to A."""
    repo, _bot_, _ch, mirror = await _setup(tmp_path)

    async def prepare(self, **kw):  # noqa: ANN001, ANN003, ANN202
        mirror.set_trigger_message(42, 556)
        await repo.update_trigger_message(42, 556)
        return True, "/sessions/999/42"

    turn_events.reset()
    try:
        with patch.object(skill_command.SkillCommandCog, "_prepare_workspace", prepare):
            await _skill_turn(repo, 42, S, mirror=mirror)
    finally:
        turn_events.reset()
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
    not handed to Claude yet. /skill forgets 555 only; A's 556 stays."""
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
