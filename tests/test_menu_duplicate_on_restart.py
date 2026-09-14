"""#717: a restart must not post a second copy of a question already on screen.

Production, 2026-09-08 23:27. The bot was restarted while two threads had an
open question. **Both** of them (2/2) received, seconds after the new process
came up, a byte-identical copy of the pre-menu 経緯 message *and* of the question
card — so the thread showed two live sets of buttons for one decision, with
nothing to say which one to press:

* ``W3 │ #540`` — posted 22:50:28, posted again 23:27:41
* ``W5 │ #713`` — posted 23:00:35, posted again 23:27:42

The re-poster was the #359 menu watchdog. Everything it uses to tell "already
bridged" from "nobody has seen this" was in memory — ``_is_processing``,
``_ask_bridges``, ``ask_bus`` — and the one persistent check, the #633
``menu_bridges`` ledger, was only ever written *by the watchdog itself*. The
first post had come from the turn-side bridge, which wrote nothing: to the new
process the menu looked unbridged. (Production's ``menu_bridges`` table held two
rows, both written at 23:27:41 — i.e. by the duplicate posts.)

So the fix is that ``bridge_pane_ask`` — the funnel every menu route goes
through — writes the menu down as it posts it, in the same ledger the watchdog
reads. These tests pin that, and the two things it must not break: a question
the user has *not* seen is still bridged, and a post that never reached Discord
is still retried (#579).
"""

from __future__ import annotations

import asyncio
from pathlib import Path
from unittest.mock import AsyncMock, MagicMock, patch

import pytest

from c_lord import thread_state_sync
from c_lord.database.menu_bridge_repo import MenuBridgeRepository
from c_lord.database.models import init_db
from c_lord.discord_ui import ask_handler
from c_lord.discord_ui.ask_bus import ask_bus
from c_lord.discord_ui.ask_handler import bridge_pane_ask
from c_lord.menu_ledger import (
    MenuRebridgeLedger,
    menu_fingerprint,
    use_shared_ledger,
)


def _fixture(name: str) -> str:
    return (Path(__file__).parent / "fixtures" / "panes" / name).read_text()


def _pane_question(pane: str):
    """The question the watchdog would parse out of *pane* — the same object the
    turn-side bridge gets, so both sides fingerprint the identical menu."""
    from c_lord.claude.tmux_runner import _normalize_capture, _parse_ask_from_pane

    question = _parse_ask_from_pane(_normalize_capture(pane))
    assert question is not None, "fixture no longer parses as a menu"
    return question


@pytest.fixture
async def ledger_db(tmp_path):
    db_path = str(tmp_path / "sessions.db")
    await init_db(db_path)
    return db_path


@pytest.fixture(autouse=True)
def _isolated_shared_ledger():
    """Keep the process-wide ledger from leaking between tests."""
    use_shared_ledger(None)
    yield
    use_shared_ledger(None)


def _watchdog(ledger):
    """A menu watchdog with no live turn, no bridge task — i.e. a fresh process."""
    bot = MagicMock()
    bot.get_cog.return_value = None
    bot.tmux_manager = MagicMock()
    bot.tmux_manager.capture_pane_tall = MagicMock(return_value="")
    bot.ask_repo = None
    bot.get_channel.return_value = MagicMock(spec=thread_state_sync.discord.Thread)
    return thread_state_sync.MenuWatchdogLoop(
        bot, interval_seconds=60, is_processing=lambda _tid: False, rebridge_ledger=ledger
    )


async def _sweep(loop, thread_id: int, pane: str) -> None:
    """Run one watchdog pass over *pane* and wait for the bridge task."""
    with patch.object(thread_state_sync, "_capture_pane_text", return_value=pane):
        await loop._maybe_bridge_open_menu(thread_id, "sess", "w1", pane)
        await asyncio.sleep(0)
        task = loop._ask_bridges.get(thread_id)
        if task is not None:
            await task


def _open_pane_runner(question) -> MagicMock:
    """A runner whose pane keeps showing *question* — the menu stays open."""
    runner = MagicMock()
    runner.peek_pending_ask = AsyncMock(return_value=question)
    runner.peek_menu_state = AsyncMock(return_value=(question, True))
    runner.cancel_menu = AsyncMock(return_value=True)
    return runner


async def _post_from_a_turn(thread_id: int, question, *, send=None) -> MagicMock:
    """Post *question* the way a live turn does, then walk away from the wait.

    Cancelling stands in for the process dying: the menu message is on screen
    and the TUI menu is still open, which is exactly the state a restart finds.
    """
    thread = MagicMock()
    thread.id = thread_id
    msg = MagicMock()
    msg.id = thread_id + 1
    msg.edit = AsyncMock()
    thread.send = send or AsyncMock(return_value=msg)
    repo = MagicMock()
    repo.save = AsyncMock()
    repo.delete = AsyncMock()

    task = asyncio.create_task(
        bridge_pane_ask(thread, question, _open_pane_runner(question), ask_repo=repo)
    )
    await asyncio.sleep(0.05)  # let it post, then stop waiting for a click
    task.cancel()
    with pytest.raises(asyncio.CancelledError):
        await task
    ask_bus.unregister(thread_id)
    return thread


class TestTheTurnSideBridgeWritesTheMenuDown:
    """AC1 — the post that actually happens in production must reach the ledger."""

    @pytest.mark.asyncio
    async def test_bridge_pane_ask_records_the_menu_it_posted(self, ledger_db, monkeypatch) -> None:
        """RED (#717): production's ``menu_bridges`` had no row for these menus."""
        monkeypatch.setattr(ask_handler, "_PANE_RESOLVE_POLL", 0.01)
        repo = MenuBridgeRepository(ledger_db)
        use_shared_ledger(MenuRebridgeLedger(repo))
        question = _pane_question(_fixture("ask_rich_descriptions.txt"))

        thread = await _post_from_a_turn(717_001, question)

        assert thread.send.await_count >= 1, "the menu never reached Discord"
        assert await repo.posts(717_001, menu_fingerprint(question)) == 1, (
            "the turn-side bridge posted a menu without writing it down — the "
            "next process then has no way to know the question is already on "
            "screen, which is #717"
        )


class TestARestartDoesNotRepostIt:
    """AC2/AC4 — the sweep of the NEXT process must stay quiet."""

    @pytest.mark.asyncio
    async def test_watchdog_does_not_post_a_second_copy_after_a_restart(
        self, ledger_db, monkeypatch
    ) -> None:
        """RED (#717): 2/2 threads with an open question were duplicated.

        Two ledgers over one database stand in for the restart: the turn posts
        the menu in the first process, and the fresh process's first sweep sees
        the same menu still open in the pane.
        """
        monkeypatch.setattr(ask_handler, "_PANE_RESOLVE_POLL", 0.01)
        pane = _fixture("ask_rich_descriptions.txt")
        question = _pane_question(pane)

        use_shared_ledger(MenuRebridgeLedger(MenuBridgeRepository(ledger_db)))
        await _post_from_a_turn(717_002, question)

        # --- new process: empty in-memory state, same DB ---------------------
        use_shared_ledger(MenuRebridgeLedger(MenuBridgeRepository(ledger_db)))
        loop = _watchdog(MenuRebridgeLedger(MenuBridgeRepository(ledger_db)))
        with patch("c_lord.discord_ui.ask_handler.bridge_pane_ask", new=AsyncMock()) as bridge:
            await _sweep(loop, 717_002, pane)

        assert bridge.await_count == 0, (
            "the restart posted the question a second time — two identical "
            "cards, two live button sets, and the 経緯 message again (#717)"
        )

    @pytest.mark.asyncio
    async def test_a_question_the_user_has_not_seen_is_still_bridged(
        self, ledger_db, monkeypatch
    ) -> None:
        """The ledger must silence *that* menu, not the thread.

        A menu posted by a turn is written down under its own fingerprint; the
        next question in the same thread is a different decision, and the
        watchdog is the only thing that can put it on screen once the turn has
        finalized.
        """
        monkeypatch.setattr(ask_handler, "_PANE_RESOLVE_POLL", 0.01)
        answered_pane = _fixture("ask_rich_descriptions.txt")
        next_pane = _fixture("ask_user_question_3options.txt")
        assert menu_fingerprint(_pane_question(answered_pane)) != menu_fingerprint(
            _pane_question(next_pane)
        ), "the two fixtures must be different questions for this test to mean anything"

        use_shared_ledger(MenuRebridgeLedger(MenuBridgeRepository(ledger_db)))
        await _post_from_a_turn(717_003, _pane_question(answered_pane))

        loop = _watchdog(MenuRebridgeLedger(MenuBridgeRepository(ledger_db)))
        with patch("c_lord.discord_ui.ask_handler.bridge_pane_ask", new=AsyncMock()) as bridge:
            await _sweep(loop, 717_003, next_pane)

        assert bridge.await_count == 1, (
            "a question nobody has seen must still be bridged — deduping the "
            "whole thread would hide it (#633 keys on the menu, not the thread)"
        )

    @pytest.mark.asyncio
    async def test_a_menu_that_never_reached_discord_is_still_bridged(
        self, ledger_db, monkeypatch
    ) -> None:
        """#579 must survive #717: the budget is spent by a menu the user can SEE.

        Discord rejects the whole message when a button label is unusable, so the
        turn-side bridge can fail to post. Writing the menu down before knowing
        it landed would leave the user with no question at all.
        """
        monkeypatch.setattr(ask_handler, "_PANE_RESOLVE_POLL", 0.01)
        pane = _fixture("ask_rich_descriptions.txt")
        question = _pane_question(pane)

        use_shared_ledger(MenuRebridgeLedger(MenuBridgeRepository(ledger_db)))
        thread = MagicMock()
        thread.id = 717_004
        thread.send = AsyncMock(side_effect=RuntimeError("400 Bad Request: label required"))
        with pytest.raises(RuntimeError):
            await bridge_pane_ask(thread, question, _open_pane_runner(question))
        ask_bus.unregister(717_004)

        loop = _watchdog(MenuRebridgeLedger(MenuBridgeRepository(ledger_db)))
        with patch("c_lord.discord_ui.ask_handler.bridge_pane_ask", new=AsyncMock()) as bridge:
            await _sweep(loop, 717_004, pane)

        assert bridge.await_count == 1, (
            "a menu that never reached the thread must still be retried (#579)"
        )
