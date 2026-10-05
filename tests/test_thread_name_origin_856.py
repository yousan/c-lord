"""Issue #856: a thread opened as ``#812 と #815 を直して…`` must keep ``#812``.

Dispatch threads start with the Issue number the work is for. The first naming
pass runs at the start of the first turn — **before** the session row exists —
so the topic and number it worked out were only stashed in memory and drained
on the *next* naming pass. A dispatch thread is usually one long turn followed
by ``/workspace-stop``; that next pass never came (or a restart wiped the
stash), and stopping rebuilt the name by parsing ``W13 │ #812 と…``, which
strips the leading ``#812`` as decoration — ``[停止] と #815 を直してくださ…``.
The stop rename was then recorded as a manual rename and locked.

These tests pin the fix:

* the stash is persisted the moment the row is saved (AC1),
* a leading ``#NNN`` in the name is the thread's origin, never just dropped
  (AC1/AC2/AC4),
* a rename c-lord itself wrote is never taken for a manual one (AC3), while a
  real manual rename still locks (AC5).
"""

from __future__ import annotations

from unittest.mock import AsyncMock, MagicMock

import discord
import pytest

from c_lord import thread_name as tn
from c_lord.database.models import init_db
from c_lord.database.repository import SessionRepository

FIRST = "**#812 と #815 を直してください。**\nhttps://github.com/yousan/c-lord/issues/812"
OPENED_AS = "#812 と #815 を直してくださ…"


@pytest.fixture(autouse=True)
def _clean_registry():
    registry = getattr(tn, "_OWN_RENAMES", {})
    registry.clear()
    yield
    registry.clear()


async def _repo(tmp_path) -> SessionRepository:
    db = str(tmp_path / "s.db")
    await init_db(db)
    return SessionRepository(db)


def _thread(name: str, thread_id: int = 1553957602214412458) -> MagicMock:
    thread = MagicMock(spec=discord.Thread)
    thread.id = thread_id
    thread.name = name
    thread.archived = False

    async def edit(**kwargs):
        if "name" in kwargs:
            thread.name = kwargs["name"]

    thread.edit = AsyncMock(side_effect=edit)
    return thread


def _cog(repo: SessionRepository, branch: str | None = "main"):
    from tests.test_claude_chat import _make_cog

    cog = _make_cog()
    cog.repo = repo
    cog._git_current_branch = MagicMock(return_value=branch)  # type: ignore[method-assign]
    return cog


def _tmux(number: int = 13) -> MagicMock:
    tmux = MagicMock()
    tmux.get_window_info = MagicMock(return_value=(f"@{number}", number))
    return tmux


# ── pure helpers ──────────────────────────────────────────────────────────


@pytest.mark.parametrize(
    ("name", "expected"),
    [
        ("W13 │ #812 と #815 を直してくださ…", "812"),
        ("#812 と #815 を直してくださ…", "812"),
        ("🟢 W5 │ #769 → #718 の順に直してく…", "769"),
        ("[停止] #404 認証リファクタ", "404"),
        ("qiita-article:W1 │ #598 Qiita記事", "598"),
        ("W3 │ 認証リファクタ", None),
        ("と #815 を直してくださ…", None),
        ("", None),
    ],
)
def test_parse_origin_ref_from_name(name: str, expected: str | None) -> None:
    assert tn.parse_origin_ref_from_name(name) == expected


def test_own_rename_is_consumed_once() -> None:
    tn.note_own_rename(1, "W1 │ a")
    assert tn.consume_own_rename(1, "W1 │ a") is True
    assert tn.consume_own_rename(1, "W1 │ a") is False
    assert tn.consume_own_rename(2, "W1 │ a") is False


# ── AC1: the first naming pass records origin 812 ─────────────────────────


async def test_ac1_origin_survives_when_row_is_saved_after_naming(tmp_path) -> None:
    """The naming pass ran before the row existed; saving the row persists it."""
    repo = await _repo(tmp_path)
    cog = _cog(repo)
    thread = _thread(OPENED_AS)

    await cog._apply_thread_naming(
        thread=thread, tmux_manager=_tmux(), first_message=FIRST, working_dir="/x"
    )
    assert thread.name == "W13 │ #812 と #815 を直してくださ…"

    # event_processor saves the row on Claude's first event …
    await repo.save(thread.id, "11111111-2222-3333-4444-555555555555")
    # … and tells the cog, which persists what it worked out.
    await cog.on_session_saved(thread.id)

    record = await repo.get(thread.id)
    assert record is not None
    assert record.origin_issue_ref == "812"
    assert record.topic == "と #815 を直してくださ…"


async def test_ac1_origin_from_name_when_row_exists_without_topic(tmp_path) -> None:
    repo = await _repo(tmp_path)
    await repo.save(1, "11111111-2222-3333-4444-555555555555")
    cog = _cog(repo)
    thread = _thread(OPENED_AS, thread_id=1)

    await cog._apply_thread_naming(thread=thread, tmux_manager=_tmux(), first_message=FIRST)

    record = await repo.get(1)
    assert record is not None
    assert record.origin_issue_ref == "812"


# ── AC2: stopping keeps #812 ──────────────────────────────────────────────


async def test_ac2_stop_keeps_the_origin_number(tmp_path) -> None:
    from c_lord.session_close import apply_closed_name

    repo = await _repo(tmp_path)
    cog = _cog(repo)
    thread = _thread(OPENED_AS)
    await cog._apply_thread_naming(
        thread=thread, tmux_manager=_tmux(), first_message=FIRST, working_dir="/x"
    )
    await repo.save(thread.id, "11111111-2222-3333-4444-555555555555")
    await cog.on_session_saved(thread.id)

    name = await apply_closed_name(repo, thread)
    assert name.startswith("[停止] #812 ")


async def test_ac2_stop_keeps_the_number_even_if_nothing_was_persisted(tmp_path) -> None:
    """Belt and braces: a row with no topic/origin still keeps the name's number."""
    from c_lord.session_close import apply_closed_name

    repo = await _repo(tmp_path)
    await repo.save(7, "11111111-2222-3333-4444-555555555555")
    thread = _thread("W13 │ #812 と #815 を直してくださ…", thread_id=7)

    name = await apply_closed_name(repo, thread)
    assert name.startswith("[停止] #812 と #815")


# ── AC4: #769 → #718 keeps #769 after the branch moves to fix/718 ─────────


async def test_ac4_branch_number_does_not_replace_the_opened_for_number(tmp_path) -> None:
    repo = await _repo(tmp_path)
    cog = _cog(repo, branch="main")
    thread = _thread("#769 → #718 の順に直してく…")
    first = "**#769 → #718 の順に直してください。**"

    await cog._apply_thread_naming(
        thread=thread, tmux_manager=_tmux(5), first_message=first, working_dir="/x"
    )
    await repo.save(thread.id, "11111111-2222-3333-4444-555555555555")
    await cog.on_session_saved(thread.id)

    cog._git_current_branch = MagicMock(return_value="fix/718-foo")  # type: ignore[method-assign]
    await cog._apply_thread_naming(
        thread=thread, tmux_manager=_tmux(5), first_message="続き", working_dir="/x"
    )
    assert thread.name.startswith("W5 │ #769 ")
    assert "#718 → #718" not in thread.name


async def test_ac4_lost_stash_still_recovers_origin_from_the_name(tmp_path) -> None:
    """A restart wiped the stash; the next pass reads #769 back off the name."""
    repo = await _repo(tmp_path)
    await repo.save(9, "11111111-2222-3333-4444-555555555555")
    cog = _cog(repo, branch="fix/718-foo")
    thread = _thread("W5 │ #769 → #718 の順に直してく…", thread_id=9)

    await cog._apply_thread_naming(
        thread=thread, tmux_manager=_tmux(5), first_message="続き", working_dir="/x"
    )
    record = await repo.get(9)
    assert record is not None
    assert record.origin_issue_ref == "769"
    assert record.issue_ref == "718"
    assert thread.name.startswith("W5 │ #769 ")
    assert "#718 → #718" not in thread.name


# ── AC3 / AC5: on_thread_update ───────────────────────────────────────────


def _bot(repo: SessionRepository):
    from c_lord.bot import ClaudeDiscordBot

    bot = MagicMock()
    bot.session_repo = repo
    return bot, ClaudeDiscordBot.on_thread_update


async def test_ac3_clords_own_rename_is_not_a_manual_rename(tmp_path) -> None:
    repo = await _repo(tmp_path)
    cog = _cog(repo)
    thread = _thread(OPENED_AS)
    before = MagicMock(spec=discord.Thread)
    before.name = OPENED_AS

    await cog._apply_thread_naming(
        thread=thread, tmux_manager=_tmux(), first_message=FIRST, working_dir="/x"
    )
    await repo.save(thread.id, "11111111-2222-3333-4444-555555555555")
    await cog.on_session_saved(thread.id)

    bot, handler = _bot(repo)
    await handler(bot, before, thread)  # Discord echoes our own W13 rename

    record = await repo.get(thread.id)
    assert record is not None
    assert record.topic_source != "manual"
    assert not record.auto_topic_locked


async def test_ac3_stop_rename_is_not_a_manual_rename(tmp_path) -> None:
    from c_lord.session_close import apply_closed_name

    repo = await _repo(tmp_path)
    await repo.save(7, "11111111-2222-3333-4444-555555555555")
    thread = _thread("W13 │ #812 と #815 を直してくださ…", thread_id=7)
    before = MagicMock(spec=discord.Thread)
    before.name = thread.name

    await apply_closed_name(repo, thread)
    bot, handler = _bot(repo)
    await handler(bot, before, thread)

    record = await repo.get(7)
    assert record is not None
    assert record.topic_source != "manual"
    assert not record.auto_topic_locked


async def test_ac5_a_real_manual_rename_still_locks(tmp_path) -> None:
    repo = await _repo(tmp_path)
    await repo.save(7, "11111111-2222-3333-4444-555555555555")
    await repo.set_topic(7, "と #815 を直してくださ…", source="thread_name")
    before = MagicMock(spec=discord.Thread)
    before.name = "W13 │ #812 と #815 を直してくださ…"
    after = _thread("W13 │ #812 自分で付けた名前", thread_id=7)

    bot, handler = _bot(repo)
    await handler(bot, before, after)

    record = await repo.get(7)
    assert record is not None
    assert record.topic == "自分で付けた名前"
    assert record.topic_source == "manual"
    assert record.auto_topic_locked
    # … and the number the user kept in front is not thrown away either.
    assert record.origin_issue_ref == "812"
