"""Who the ❌ pings after a restart: the trigger message on record (#1234 / #1235).

#1212 (PR #1230) made the API error ❌ for a turn Claude started on its own,
after a bot restart, ping the author of the recorded trigger message
(``sessions.trigger_message_id``). Two ways the record named the wrong person:

* #1234 — A asks by message, then B runs ``/skill`` in the same thread.
  ``/skill`` recorded no trigger, so A's message stayed on record and after a
  restart A — who did not ask for the last turn — was pinged instead of B.
  ``/skill`` now clears the record; with nothing on record the owner is pinged.
* #1235 — a thread's first turn records its trigger before the session row
  exists (held until ``save`` makes the row). When that turn failed before its
  row existed, the held trigger was written onto the row the *next* turn made
  (another person's ``/skill``). It is now dropped when its own turn ends.
"""

from __future__ import annotations

import asyncio
from types import SimpleNamespace
from unittest.mock import AsyncMock, MagicMock, patch

import discord

from c_lord import turn_events
from c_lord.cogs.skill_command import SkillCommandCog
from c_lord.cogs.transcript_mirror import TranscriptMirrorCog
from c_lord.database.models import init_db
from c_lord.database.repository import SessionRepository
from c_lord.discord_ui.authorization import Authorizer
from tests.test_workspace_failure import _make_cog as _make_chat_cog
from tests.test_workspace_failure import _message as _chat_message

OWNER = 1
BOT_SELF = 99
A = 777
B = 888


def _msg(mid: int, author: int) -> MagicMock:
    m = MagicMock()
    m.id = mid
    m.webhook_id = None
    m.author = SimpleNamespace(id=author, bot=False, name="x")
    return m


def _bot(messages: dict[int, MagicMock]) -> tuple[MagicMock, dict[int, MagicMock]]:
    bot = MagicMock()
    bot.owner_id = OWNER
    bot.user = SimpleNamespace(id=BOT_SELF)
    channels: dict[int, MagicMock] = {}

    def get_channel(cid: int) -> MagicMock:
        if cid not in channels:
            ch = MagicMock()
            ch.send = AsyncMock()

            async def fetch(mid: int) -> MagicMock:
                if mid in messages:
                    return messages[mid]
                raise discord.NotFound(MagicMock(status=404), "gone")

            ch.fetch_message = AsyncMock(side_effect=fetch)
            ch.parent = None
            channels[cid] = ch
        return channels[cid]

    bot.get_channel.side_effect = get_channel
    return bot, channels


async def _api_error(bot, repo, thread_id: int, channels) -> str:  # noqa: ANN001
    cog = TranscriptMirrorCog(bot, session_repo=repo)
    await cog._make_api_error_sink(thread_id)("API Error: The response stopped arriving.")
    return channels[thread_id].send.call_args.args[0]


async def _skill_turn(
    repo: SessionRepository, thread_id: int, user_id: int, mirror: object | None = None
) -> None:
    """Run ``/skill`` in an existing thread; the run itself does what a real one
    does to the record: begin the turn for the invoker, save at SYSTEM."""
    bot = MagicMock()
    bot.settings_repo = None
    bot.transcript_mirror_cog = mirror if mirror is not None else MagicMock()
    default_runner = MagicMock(
        model="sonnet", working_dir="/srv/default", timeout_seconds=300, effort=None
    )
    cog = SkillCommandCog(
        bot=bot,
        repo=repo,
        runner=default_runner,
        claude_channel_id=999,
        skills_dir="/nonexistent/skills",
        authorizer=Authorizer(allow_anyone=True),
    )
    cog._skills = [{"name": "todoist", "description": "Tasks"}]
    tmux = MagicMock()
    tmux.create_session = MagicMock(return_value="w7")
    tmux.session_exists = MagicMock(return_value=True)
    sdm = MagicMock()
    sdm.create_session_dir = MagicMock(return_value="/sessions/999/42")
    cog._resolve_tmux_manager = AsyncMock(return_value=tmux)  # type: ignore[method-assign]
    cog._resolve_session_dir_manager = AsyncMock(return_value=sdm)  # type: ignore[method-assign]
    thread = MagicMock(spec=discord.Thread)
    thread.id = thread_id
    thread.parent_id = 999
    thread.send = AsyncMock()

    async def run(config) -> None:  # noqa: ANN001
        turn_events.begin_turn(
            SimpleNamespace(id=thread_id, parent_id=1, guild=None),
            turn_events.StartedBy("user", str(user_id)),
        )
        await repo.save(thread_id, "sess-1")

    with (
        patch("c_lord.cogs.skill_command.run_claude_with_config", side_effect=run),
        patch(
            "c_lord.cogs.skill_command.foreign_owner_notice_for",
            new_callable=AsyncMock,
            return_value=None,
        ),
    ):
        await cog._run_skill_impl(
            channel=thread,
            user=MagicMock(id=user_id),
            name="todoist",
            args=None,
            respond=AsyncMock(),
            ack=AsyncMock(),
        )


async def _chat_turn_that_dies_before_system(repo: SessionRepository) -> int:
    """A thread's first message turn that ends before its SYSTEM event (no row)."""
    cog = _make_chat_cog()
    cog.repo = repo
    sdm = MagicMock()
    sdm.create_session_dir = MagicMock(return_value="/tmp/work")
    tmux = MagicMock()
    tmux.create_session = MagicMock(return_value="w1")
    cog._resolve_session_dir_manager = AsyncMock(return_value=sdm)  # type: ignore[method-assign]
    cog._resolve_tmux_manager = AsyncMock(return_value=tmux)  # type: ignore[method-assign]
    cog._get_dashboard = MagicMock(return_value=None)  # type: ignore[method-assign]
    cog._get_coordination = MagicMock(return_value=None)  # type: ignore[method-assign]
    cog._get_current_model = AsyncMock(return_value=None)  # type: ignore[method-assign]
    cog._apply_thread_naming = AsyncMock()  # type: ignore[method-assign]
    thread = MagicMock(spec=discord.Thread)
    thread.id = 42
    thread.parent_id = 500
    thread.send = AsyncMock(return_value=MagicMock())
    message = _chat_message()
    with patch("c_lord.cogs.claude_chat.run_claude_with_config", AsyncMock(return_value=None)):
        await cog._run_claude(message, thread, "hi", None)
    return message.id


async def test_skill_turn_by_b_after_restart_pings_b_not_a(tmp_path, monkeypatch) -> None:  # noqa: ANN001
    """#1234: A asks by message, then B runs /skill in the same thread (records no
    trigger). Before a restart the ❌ pings B (memory). After a restart it should
    not switch to A, who did not ask for the last turn."""
    monkeypatch.delenv(turn_events.ENV_LOG, raising=False)
    monkeypatch.delenv(turn_events.ENV_URL, raising=False)
    db = str(tmp_path / "s.db")
    await init_db(db)
    repo = SessionRepository(db)
    thread = 42
    await repo.update_trigger_message(thread, 555)  # A's message
    await repo.save(thread, "sess-1")
    bot, channels = _bot({555: _msg(555, A)})
    turn_events.reset()
    try:
        # B's /skill turn: begin_turn with StartedBy("user", B), save via SYSTEM event.
        await _skill_turn(repo, thread, B)
        before = await _api_error(bot, repo, thread, channels)
        turn_events.reset()  # restart
        after = await _api_error(bot, repo, thread, channels)
    finally:
        turn_events.reset()
    assert before.endswith(f"<@{B}>"), before
    assert after.endswith(f"<@{B}>") or after.endswith(f"<@{OWNER}>"), after


async def test_skill_turn_answer_does_not_quote_the_previous_message(tmp_path) -> None:  # noqa: ANN001
    """#1234 (review): the mirror keeps its own copy of the trigger in memory and
    reads it before the record. B's /skill answer must not reply to A's message
    in the same process either — not only after a restart."""
    db = str(tmp_path / "s.db")
    await init_db(db)
    repo = SessionRepository(db)
    thread = 42
    await repo.update_trigger_message(thread, 555)  # A's message
    await repo.save(thread, "sess-1")
    bot, _channels = _bot({555: _msg(555, A)})
    mirror = TranscriptMirrorCog(bot, session_repo=repo)
    mirror.set_trigger_message(thread, 555)
    turn_events.reset()
    try:
        await _skill_turn(repo, thread, B, mirror=mirror)
    finally:
        turn_events.reset()
    assert await mirror._build_trigger_reference(thread) is None


async def test_two_threads_first_turns_do_not_mix(tmp_path, monkeypatch) -> None:  # noqa: ANN001
    monkeypatch.delenv(turn_events.ENV_LOG, raising=False)
    monkeypatch.delenv(turn_events.ENV_URL, raising=False)
    db = str(tmp_path / "s.db")
    await init_db(db)
    repo = SessionRepository(db)
    await repo.update_trigger_message(10, 1010)
    await repo.update_trigger_message(20, 2020)
    await repo.save(20, "s20")
    await repo.save(10, "s10")
    bot, channels = _bot({1010: _msg(1010, A), 2020: _msg(2020, B)})
    turn_events.reset()
    try:
        assert (await _api_error(bot, repo, 10, channels)).endswith(f"<@{A}>")
        assert (await _api_error(bot, repo, 20, channels)).endswith(f"<@{B}>")
    finally:
        turn_events.reset()


async def test_next_human_turn_overwrites_the_record(tmp_path, monkeypatch) -> None:  # noqa: ANN001
    monkeypatch.delenv(turn_events.ENV_LOG, raising=False)
    monkeypatch.delenv(turn_events.ENV_URL, raising=False)
    db = str(tmp_path / "s.db")
    await init_db(db)
    repo = SessionRepository(db)
    await repo.update_trigger_message(42, 555)
    await repo.save(42, "s")
    await repo.update_trigger_message(42, 556)
    await repo.save(42, "s")
    bot, channels = _bot({555: _msg(555, A), 556: _msg(556, B)})
    turn_events.reset()
    try:
        assert (await _api_error(bot, repo, 42, channels)).endswith(f"<@{B}>")
    finally:
        turn_events.reset()


async def test_stale_pending_is_not_written_by_a_later_save(tmp_path, monkeypatch) -> None:  # noqa: ANN001
    """#1235: A's first message is recorded (no row), the turn dies before SYSTEM.
    A later save (B's /skill, a scheduled run) then creates the row. The row should
    not carry A's trigger."""
    db = str(tmp_path / "s.db")
    await init_db(db)
    repo = SessionRepository(db)
    await _chat_turn_that_dies_before_system(repo)  # A, pending; never reached SYSTEM
    await repo.save(42, "sess-from-skill")  # another turn creates the row
    record = await repo.get(42)
    assert record is not None and record.trigger_message_id is None, record.trigger_message_id


async def test_a_first_turn_that_reaches_system_keeps_its_trigger(tmp_path) -> None:  # noqa: ANN001
    """#1235 guard: dropping the held trigger at turn end must not undo #1212 —
    a first turn whose SYSTEM event made the row keeps its trigger on record."""
    db = str(tmp_path / "s.db")
    await init_db(db)
    repo = SessionRepository(db)
    real_save = repo.save
    calls: list[str] = []

    async def run(config) -> None:  # noqa: ANN001
        calls.append("run")
        await real_save(42, "sess-1")

    cog = _make_chat_cog()
    cog.repo = repo
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
    with patch("c_lord.cogs.claude_chat.run_claude_with_config", side_effect=run):
        await cog._run_claude(message, thread, "hi", None)
    assert calls == ["run"]
    record = await repo.get(42)
    assert record is not None and record.trigger_message_id == message.id


async def test_a_newer_pending_trigger_survives_the_older_turns_end(tmp_path) -> None:  # noqa: ANN001
    """#1235 guard: the next message's turn (which replaced the held trigger)
    keeps its own when the turn it interrupted ends afterwards."""
    db = str(tmp_path / "s.db")
    await init_db(db)
    repo = SessionRepository(db)
    await repo.update_trigger_message(42, 555)  # A's first turn, no row
    await repo.update_trigger_message(42, 556)  # B's message preempts it
    repo.discard_pending_trigger(42, 555)  # A's turn ends afterwards
    await repo.save(42, "s")  # B's turn reaches SYSTEM
    record = await repo.get(42)
    assert record is not None and record.trigger_message_id == 556


async def test_slow_fetch_is_bounded(tmp_path, monkeypatch) -> None:  # noqa: ANN001
    monkeypatch.delenv(turn_events.ENV_LOG, raising=False)
    monkeypatch.delenv(turn_events.ENV_URL, raising=False)
    db = str(tmp_path / "s.db")
    await init_db(db)
    repo = SessionRepository(db)
    await repo.update_trigger_message(42, 555)
    await repo.save(42, "s")
    bot, channels = _bot({})
    ch = bot.get_channel(42)

    async def hang(mid: int) -> None:
        await asyncio.sleep(3600)

    ch.fetch_message = AsyncMock(side_effect=hang)
    real_wait_for = asyncio.wait_for

    async def fast_wait_for(aw, timeout):  # noqa: ANN001
        return await real_wait_for(aw, 0.05)

    monkeypatch.setattr("c_lord.cogs.transcript_mirror.asyncio.wait_for", fast_wait_for)
    turn_events.reset()
    try:
        assert (await _api_error(bot, repo, 42, channels)).endswith(f"<@{OWNER}>")
    finally:
        turn_events.reset()
