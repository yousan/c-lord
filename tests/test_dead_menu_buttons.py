"""#752: a button that looks pressable must work — or stop looking pressable.

Measured 2026-09-18 over 6,721 bot messages: **97** buttons that looked live and
did nothing when pressed (43 unanswered ❓ menus, 29 ⏹ Stop, 25 others), the
oldest 99 days old, while ``pending_asks`` — the only thing restart recovery
reads — held **0** rows. Three holes, each pinned here:

1. **A new question overwrote the previous one's ledger row** (``pending_asks``
   is keyed by thread). The previous menu's message kept its buttons with no row
   left to re-arm *or* retire it — so on the next restart it went dead.
2. **Startup only looked at ledger rows.** A menu with no row was invisible to
   it however long it sat there.
3. **The ⏹ Stop sweep saw 100 messages of 200 threads.** Production had 339
   threads, and a Stop from 2026-08-07 had 164 messages on top of it.
"""

from __future__ import annotations

import asyncio
import datetime as dt
import logging
from unittest.mock import AsyncMock, MagicMock

import discord
import pytest

from c_lord.claude.types import AskOption, AskQuestion, ask_question_to_dict
from c_lord.database.ask_repo import PendingAskRepository
from c_lord.database.models import init_db
from c_lord.discord_ui import ask_handler
from c_lord.discord_ui.ask_bus import ask_bus
from c_lord.discord_ui.ask_handler import bridge_pane_ask
from c_lord.discord_ui.views import STOP_MESSAGE_PREFIX

BOT_ID = 42
HUMAN_ID = 7
THREAD_ID = 752_000_001


def _snowflake(minutes_ago: float) -> int:
    """A message id from *minutes_ago* — before this process started when > 0."""
    when = dt.datetime.now(dt.timezone.utc) - dt.timedelta(minutes=minutes_ago)  # noqa: UP017
    return discord.utils.time_snowflake(when)


def _question(header: str = "方針") -> AskQuestion:
    return AskQuestion(
        question=f"{header}はどうしますか?",
        header=header,
        options=[AskOption("A", ""), AskOption("B", "")],
    )


# ── fakes ────────────────────────────────────────────────────────────────────


class _Button:
    def __init__(self, custom_id: str | None, *, url: str | None = None, disabled=False) -> None:
        self.custom_id = custom_id
        self.url = url
        self.disabled = disabled


class _Row:
    def __init__(self, *children: _Button) -> None:
        self.children = list(children)


class _Msg:
    def __init__(
        self,
        msg_id: int,
        *,
        author_id: int = BOT_ID,
        content: str = "",
        components: list | None = None,
        fail: bool = False,
    ) -> None:
        self.id = msg_id
        self.author = MagicMock()
        self.author.id = author_id
        self.content = content
        self.components = components or []
        self.embeds = [MagicMock()]
        self.deleted = False
        self.edits: list[dict] = []
        self._fail = fail

    async def delete(self) -> None:
        if self._fail:
            raise discord.HTTPException(MagicMock(status=403), "Missing Access")
        self.deleted = True

    async def edit(self, **kwargs) -> None:
        if self._fail:
            raise discord.HTTPException(MagicMock(status=403), "Missing Access")
        self.edits.append(kwargs)


def _ask_menu(msg_id: int, thread_id: int = THREAD_ID, **kw) -> _Msg:
    return _Msg(
        msg_id,
        components=[_Row(_Button(f"ask_{thread_id}_0_0"), _Button(f"ask_{thread_id}_0_1"))],
        **kw,
    )


def _stop(msg_id: int, **kw) -> _Msg:
    return _Msg(
        msg_id, content=f"{STOP_MESSAGE_PREFIX} (`w1`)", components=[_Row(_Button("x"))], **kw
    )


class _Thread:
    """Enough of ``discord.Thread`` for the sweep, honouring history()'s paging."""

    def __init__(self, thread_id: int, messages: list[_Msg]) -> None:
        self.id = thread_id
        self.messages = sorted(messages, key=lambda m: m.id)
        self.last_message_id = self.messages[-1].id if self.messages else None
        self.history_calls: list[dict] = []

    def history(self, *, limit=100, before=None, after=None, oldest_first=None):
        self.history_calls.append({"limit": limit, "before": before, "after": after})
        msgs = self.messages
        if before is not None:
            msgs = [m for m in msgs if m.id < before.id]
        if after is not None:
            msgs = [m for m in msgs if m.id > after.id]
        if oldest_first is None:
            oldest_first = after is not None
        ordered = msgs if oldest_first else list(reversed(msgs))
        picked = ordered if limit is None else ordered[:limit]

        async def _gen():
            for m in picked:
                yield m

        return _gen()


def _bot(*threads: _Thread) -> MagicMock:
    by_id = {t.id: t for t in threads}
    bot = MagicMock()
    bot.user = MagicMock()
    bot.user.id = BOT_ID
    bot.get_channel = MagicMock(side_effect=lambda tid: by_id.get(tid))
    bot.fetch_channel = AsyncMock(side_effect=lambda tid: by_id[tid])
    return bot


def _session_repo(*thread_ids: int) -> MagicMock:
    repo = MagicMock()
    repo.list_alive = AsyncMock(return_value=[MagicMock(thread_id=t) for t in thread_ids])
    return repo


def _retired(msg: _Msg) -> bool:
    return any(e.get("view", "unset") is None for e in msg.edits)


# ── ① a new question retires the previous one's buttons (AC1 / AC5) ──────────


@pytest.fixture(autouse=True)
def _isolated_shared_ledger():
    """Keep the process-wide menu ledger (#717) from leaking between tests."""
    from c_lord.menu_ledger import use_shared_ledger

    use_shared_ledger(None)
    yield
    use_shared_ledger(None)


@pytest.fixture
async def ask_repo(tmp_path) -> PendingAskRepository:
    db = str(tmp_path / "sessions.db")
    await init_db(db)
    return PendingAskRepository(db)


def _bridge_thread(new_msg_id: int, previous: _Msg | None) -> tuple[MagicMock, MagicMock]:
    thread = MagicMock()
    thread.id = THREAD_ID
    new_msg = MagicMock()
    new_msg.id = new_msg_id
    new_msg.edit = AsyncMock()
    thread.send = AsyncMock(return_value=new_msg)

    async def _fetch(message_id: int):
        if previous is not None and message_id == previous.id:
            return previous
        raise discord.NotFound(MagicMock(status=404), "Unknown Message")

    thread.fetch_message = AsyncMock(side_effect=_fetch)
    return thread, new_msg


def _pane_showing(question: AskQuestion) -> MagicMock:
    """A pane that keeps the new menu open — the bridge is still waiting on it."""
    runner = MagicMock()
    runner.peek_pending_ask = AsyncMock(return_value=question)
    runner.peek_menu_state = AsyncMock(return_value=(question, True))
    runner.cancel_menu = AsyncMock()
    return runner


async def _bridge_until_posted(thread, question, runner, ask_repo) -> None:
    """Run the bridge until its menu is up, then pre-empt it (the #315 path)."""
    task = asyncio.create_task(bridge_pane_ask(thread, question, runner, ask_repo=ask_repo))
    for _ in range(200):
        if thread.send.await_count:
            break
        await asyncio.sleep(0.01)
    await asyncio.sleep(0.05)
    ask_bus.post_answer(THREAD_ID, [])
    await asyncio.wait_for(task, timeout=3.0)


@pytest.mark.asyncio
async def test_a_new_question_retires_the_previous_menu(ask_repo, monkeypatch) -> None:
    """AC1/AC5 — production 2026-09-08, W14/W15: the first menu's row was
    overwritten by the next question and its buttons stayed up for 9 days.

    RED before #752: the previous message was never touched.
    """
    monkeypatch.setattr(ask_handler, "_PANE_RESOLVE_POLL", 0.01)
    previous = _ask_menu(_snowflake(30))
    # The previous menu's row, as the previous process left it.
    await ask_repo.save(
        thread_id=THREAD_ID,
        session_id="",
        questions=[ask_question_to_dict(_question("スコープ"))],
        message_id=previous.id,
    )
    thread, _new = _bridge_thread(_snowflake(0), previous)
    question = _question("方針")

    await _bridge_until_posted(thread, question, _pane_showing(question), ask_repo)

    assert _retired(previous), f"the superseded menu kept its buttons: {previous.edits!r}"
    edit = next(e for e in previous.edits if e.get("view", "unset") is None)
    assert "無効" in (edit.get("content") or ""), f"say why it stopped working: {edit!r}"
    assert "embed" not in edit, "keep the question readable — only the buttons go"


@pytest.mark.asyncio
async def test_no_previous_menu_means_nothing_is_touched(ask_repo, monkeypatch) -> None:
    monkeypatch.setattr(ask_handler, "_PANE_RESOLVE_POLL", 0.01)
    thread, _new = _bridge_thread(_snowflake(0), None)
    question = _question()

    await _bridge_until_posted(thread, question, _pane_showing(question), ask_repo)

    thread.fetch_message.assert_not_awaited()


@pytest.mark.asyncio
async def test_a_previous_menu_that_cannot_be_retired_is_logged(
    ask_repo, monkeypatch, caplog
) -> None:
    """AC4: not silently — the thread and the reason land in the INFO log."""
    monkeypatch.setattr(ask_handler, "_PANE_RESOLVE_POLL", 0.01)
    previous = _ask_menu(_snowflake(30), fail=True)
    await ask_repo.save(
        thread_id=THREAD_ID,
        session_id="",
        questions=[ask_question_to_dict(_question("スコープ"))],
        message_id=previous.id,
    )
    thread, _new = _bridge_thread(_snowflake(0), previous)
    question = _question()

    with caplog.at_level(logging.INFO, logger="c_lord.discord_ui.ask_handler"):
        await _bridge_until_posted(thread, question, _pane_showing(question), ask_repo)

    lines = [r for r in caplog.records if "#752" in r.getMessage()]
    assert lines, caplog.text
    assert all(r.levelno >= logging.INFO for r in lines)
    assert any(f"thread={THREAD_ID}" in r.getMessage() for r in lines), caplog.text


# ── ② startup retires ❓ menus nothing can serve any more (AC2) ──────────────


@pytest.mark.asyncio
async def test_startup_retires_a_menu_that_has_no_ledger_row() -> None:
    """AC2 — RED before #752: startup only read ``pending_asks``, which was 0
    rows in production while 43 menus stood live."""
    from c_lord.stale_stop_buttons import sweep_dead_buttons

    orphan = _ask_menu(_snowflake(60 * 24 * 9))
    rearmed = _ask_menu(_snowflake(30))
    prose = _Msg(_snowflake(20), content="実装しました。")
    human_menu = _ask_menu(_snowflake(10), author_id=HUMAN_ID)
    thread = _Thread(THREAD_ID, [orphan, rearmed, prose, human_menu])

    await sweep_dead_buttons(
        _bot(thread),
        _session_repo(THREAD_ID),
        keep_menu=lambda _t, message_id: message_id == rearmed.id,
    )

    assert _retired(orphan), "a menu with no handler anywhere must stop looking live"
    assert "無効" in (orphan.edits[-1].get("content") or "")
    assert "embed" not in orphan.edits[-1], "keep the question — only the buttons go"
    assert not orphan.deleted, "a menu is a record of what was asked — never delete it"
    assert not rearmed.edits, "restart recovery re-armed this one — it works"
    assert not prose.edits and not human_menu.edits


@pytest.mark.asyncio
async def test_a_menu_posted_by_this_process_is_never_retired() -> None:
    """Only a previous process's messages can be residue: anything newer than
    this process's start was drawn by live code (a turn that began while the
    sweep was still walking threads)."""
    from c_lord.stale_stop_buttons import sweep_dead_buttons

    fresh_menu = _ask_menu(_snowflake(-1))  # created after this process started
    fresh_stop = _stop(_snowflake(-1))
    thread = _Thread(THREAD_ID, [fresh_menu, fresh_stop])

    await sweep_dead_buttons(_bot(thread), _session_repo(THREAD_ID))

    assert not fresh_menu.edits and not fresh_stop.deleted


# ── ③ the ⏹ Stop sweep reaches past 100 messages and 200 threads (AC3) ──────


@pytest.mark.asyncio
async def test_a_stop_buried_under_164_messages_is_still_removed() -> None:
    """AC3 — production: ``W1 │ おぷー管理`` 2026-08-07, 164 messages above it.

    RED before #752: the sweep read the newest 100 messages and stopped.
    """
    from c_lord.stale_stop_buttons import sweep_dead_buttons

    buried = _stop(_snowflake(60 * 24 * 47))
    chatter = [_Msg(_snowflake(60 * 24 * 40 - i), content=f"msg {i}") for i in range(164)]
    thread = _Thread(THREAD_ID, [buried, *chatter])

    await sweep_dead_buttons(_bot(thread), _session_repo(THREAD_ID))

    assert buried.deleted


@pytest.mark.asyncio
async def test_threads_past_the_first_200_are_visited() -> None:
    """AC3 — production had 339 threads; the oldest 139 were never visited."""
    from c_lord.stale_stop_buttons import sweep_dead_buttons

    threads = [_Thread(900_000 + i, [_stop(_snowflake(60 + i))]) for i in range(260)]

    await sweep_dead_buttons(_bot(*threads), _session_repo(*(t.id for t in threads)))

    assert threads[-1].messages[0].deleted, "the 260th thread was never visited"


# ── ④ the next startup reads only what is new (cursor) ──────────────────────


@pytest.mark.asyncio
async def test_the_next_startup_reads_only_what_was_posted_since(tmp_path, monkeypatch) -> None:
    """Reaching deep once must not mean reaching deep on every restart: the
    first visit remembers how far it read, the next one starts there."""
    import c_lord.stale_stop_buttons as sweep_mod
    from c_lord.database.sweep_cursor_repo import SweepCursorRepository
    from c_lord.stale_stop_buttons import sweep_dead_buttons

    db = str(tmp_path / "sessions.db")
    await init_db(db)
    cursors = SweepCursorRepository(db)
    old = _Msg(_snowflake(120), content="old")
    thread = _Thread(THREAD_ID, [old])

    # Process 1 started 100 minutes ago and swept.
    started_1 = dt.datetime.now(dt.timezone.utc) - dt.timedelta(minutes=100)  # noqa: UP017
    monkeypatch.setattr(sweep_mod, "_PROCESS_STARTED_AT", started_1)
    await sweep_dead_buttons(_bot(thread), _session_repo(THREAD_ID), cursors=cursors)
    cursor_1 = (await cursors.get_all()).get(THREAD_ID)
    assert cursor_1 is not None and old.id <= cursor_1 < discord.utils.time_snowflake(started_1)

    # Process 1 then left a Stop behind; process 2 starts now.
    leftover = _stop(_snowflake(60))
    thread.messages.append(leftover)
    thread.last_message_id = leftover.id
    thread.history_calls.clear()
    monkeypatch.setattr(sweep_mod, "_PROCESS_STARTED_AT", dt.datetime.now(dt.timezone.utc))  # noqa: UP017

    await sweep_dead_buttons(_bot(thread), _session_repo(THREAD_ID), cursors=cursors)

    assert leftover.deleted
    assert thread.history_calls[0]["after"].id == cursor_1, "re-read history it had already seen"


@pytest.mark.asyncio
async def test_a_thread_with_nothing_new_costs_no_history_request(tmp_path) -> None:
    from c_lord.database.sweep_cursor_repo import SweepCursorRepository
    from c_lord.stale_stop_buttons import sweep_dead_buttons

    db = str(tmp_path / "sessions.db")
    await init_db(db)
    cursors = SweepCursorRepository(db)
    msg = _Msg(_snowflake(120))
    thread = _Thread(THREAD_ID, [msg])
    await cursors.set(THREAD_ID, msg.id)

    await sweep_dead_buttons(_bot(thread), _session_repo(THREAD_ID), cursors=cursors)

    assert thread.history_calls == []


# ── ⑤ what could not be removed is said out loud (AC4) ──────────────────────


@pytest.mark.asyncio
async def test_residue_that_cannot_be_removed_is_logged_at_info(tmp_path, caplog) -> None:
    """AC4 (#678): never give up silently — count and thread, at INFO. And
    the cursor must not move past it, so the next startup tries again."""
    from c_lord.database.sweep_cursor_repo import SweepCursorRepository
    from c_lord.stale_stop_buttons import sweep_dead_buttons

    db = str(tmp_path / "sessions.db")
    await init_db(db)
    cursors = SweepCursorRepository(db)
    stuck_stop = _stop(_snowflake(90), fail=True)
    stuck_menu = _ask_menu(_snowflake(80), fail=True)
    later = _Msg(_snowflake(70))
    thread = _Thread(THREAD_ID, [stuck_stop, stuck_menu, later])

    with caplog.at_level(logging.INFO, logger="c_lord.stale_stop_buttons"):
        await sweep_dead_buttons(_bot(thread), _session_repo(THREAD_ID), cursors=cursors)

    failures = [
        r
        for r in caplog.records
        if r.levelno == logging.INFO and "could not" in r.getMessage() and "#752" in r.getMessage()
    ]
    assert failures, caplog.text
    text = failures[0].getMessage()
    assert f"thread={THREAD_ID}" in text and "2" in text, text
    cursor = (await cursors.get_all()).get(THREAD_ID)
    assert cursor is None or cursor < stuck_stop.id, "the cursor skipped past residue"


# ── ⑥ startup order: re-arm first, then sweep with what was re-armed ────────


@pytest.mark.asyncio
async def test_startup_rearms_before_it_sweeps(monkeypatch) -> None:
    """The sweep must know which menus restart recovery brought back to life,
    or it would strip the very buttons #671 just re-armed."""
    import c_lord.startup_recovery as sr

    order: list[str] = []
    seen_keep: list = []

    async def _recover(*_a, **_k):
        order.append("recover")
        return 1

    async def _sweep(_bot, _repo, *, keep_menu, cursors=None, **_k):
        order.append("sweep")
        seen_keep.append(keep_menu)
        return 0

    monkeypatch.setattr(sr, "recover_ask_menus", _recover)
    monkeypatch.setattr(sr, "sweep_dead_buttons", _sweep)
    ask_repo = MagicMock()
    ask_repo.list_all = AsyncMock(return_value=[MagicMock(thread_id=THREAD_ID, message_id=12345)])

    await sr.run_startup_recovery(MagicMock(), MagicMock(), ask_repo)

    assert order == ["recover", "sweep"]
    keep = seen_keep[0]
    assert keep(THREAD_ID, 12345) is True
    assert keep(THREAD_ID, 99999) is False
