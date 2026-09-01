"""#633: the menu watchdog must bridge each menu once, with its own 経緯.

Production evidence (bot host, ``journalctl --user -u c-lord.service``):

* one thread logged ``menu watchdog: bridging unwatched TUI menu`` **188** times,
* the same ``❓ 切り口`` menu embed reached Discord **6 times over 3 days**, every
  copy within ~10 seconds of a ``Started menu watchdog loop`` line — i.e. after a
  bot restart, because the whole re-bridge ledger lived in memory,
* and on 2026-08-28 a menu was posted with 2751 characters of "経緯" that were
  byte-identical to a message already delivered on 2026-08-26 (message ids
  ``1542012051361239073`` and ``1542810482526658591``, same 1900-char body).

The three tests below pin the three halves of that: the ledger must survive a
restart, a menu that was already bridged must not be posted again, and the
prose carried as 経緯 must be the block that is actually contiguous with the
menu — not one the transcript mirror already delivered.
"""

from __future__ import annotations

import asyncio
from pathlib import Path
from unittest.mock import AsyncMock, MagicMock, patch

import pytest

from c_lord import thread_state_sync
from c_lord.database.models import init_db


def _fixture(name: str) -> str:
    return (Path(__file__).parent / "fixtures" / "panes" / name).read_text()


# ── AC3: 経緯 must belong to this menu ────────────────────────────────────────


class TestContextStopsAtToolBoundary:
    """The 経緯 must not reach across a tool block into already-sent prose."""

    def test_folded_tool_summary_ends_the_context_block(self) -> None:
        """RED (#633): a real pane capture with a folded tool summary.

        ``tests/fixtures/panes/i633_stale_prose_above_folded_tool.txt`` is a
        240-row ``capture-pane`` of a live Claude Code v2.1.252 session — the
        same tall capture the watchdog takes — in which one turn wrote prose,
        ran a Bash tool, then opened an AskUserQuestion.  On redraw the finished
        tool block folds to the single indented line ``Ran 1 shell command``,
        which the upward walk used to step straight over: the menu then carried
        431 characters of prose the transcript mirror had *already* posted.
        """
        from c_lord.claude.tmux_runner import _normalize_capture, _parse_ask_from_pane

        pane = _fixture("i633_stale_prose_above_folded_tool.txt")
        question = _parse_ask_from_pane(_normalize_capture(pane))

        assert question is not None
        assert question.header == "ロールバック"
        assert question.context == "", (
            "the prose above this menu is separated from it by a completed tool "
            "block, so the mirror already delivered it — carrying it again is "
            "how a 2-day-old answer was re-posted as 'new' (#633)"
        )

    def test_contiguous_prose_is_still_carried(self) -> None:
        """#399/#549 must not regress: prose that IS the menu's own still posts."""
        from c_lord.claude.tmux_runner import _normalize_capture, _parse_ask_from_pane

        pane = _fixture("ask_context_prose_above_menu.txt")
        question = _parse_ask_from_pane(_normalize_capture(pane))

        assert question is not None
        assert "私の推しは (A) です" in question.context


# ── AC1 / AC2: one bridge per menu, across restarts ──────────────────────────


def _make_loop(ledger=None, is_processing=lambda _tid: False):
    bot = MagicMock()
    bot.get_cog.return_value = None
    bot.tmux_manager = MagicMock()
    bot.tmux_manager.capture_pane_tall = MagicMock(return_value="")
    bot.ask_repo = None
    thread = MagicMock(spec=thread_state_sync.discord.Thread)
    bot.get_channel.return_value = thread
    loop = thread_state_sync.MenuWatchdogLoop(
        bot, interval_seconds=60, is_processing=is_processing, rebridge_ledger=ledger
    )
    return loop, bot, thread


async def _sweep(loop, thread_id: int, pane: str) -> None:
    """Run one watchdog pass over *pane* and wait for the bridge task."""
    with patch.object(thread_state_sync, "_capture_pane_text", return_value=pane):
        await loop._maybe_bridge_open_menu(thread_id, "sess", "w1", pane)
        await asyncio.sleep(0)
        task = loop._ask_bridges.get(thread_id)
        if task is not None:
            await task


@pytest.fixture
async def ledger_db(tmp_path):
    db_path = str(tmp_path / "sessions.db")
    await init_db(db_path)
    return db_path


class TestOneBridgePerMenu:
    """AC1/AC2 — identity is the menu's own text, and it survives a restart."""

    @pytest.mark.asyncio
    async def test_same_menu_is_not_rebridged_after_a_restart(self, ledger_db) -> None:
        """RED (#633): every restart re-posted the same stranded menu.

        Two ``MenuWatchdogLoop`` instances over one database stand in for a bot
        restart — production restarted 5+ times a day, and each fresh process
        started the stuck menu's budget over at ``attempt=1/3``.
        """
        from c_lord.database.menu_bridge_repo import MenuBridgeRepository
        from c_lord.thread_state_sync import MenuRebridgeLedger

        pane = _fixture("ask_rich_descriptions.txt")

        before, _, _ = _make_loop(ledger=MenuRebridgeLedger(MenuBridgeRepository(ledger_db)))
        with patch("c_lord.discord_ui.ask_handler.bridge_pane_ask", new=AsyncMock()) as bridge:
            await _sweep(before, 633_001, pane)
        assert bridge.await_count == 1, "the first sighting must reach Discord"

        after, _, _ = _make_loop(ledger=MenuRebridgeLedger(MenuBridgeRepository(ledger_db)))
        with patch("c_lord.discord_ui.ask_handler.bridge_pane_ask", new=AsyncMock()) as bridge:
            await _sweep(after, 633_001, pane)
        assert bridge.await_count == 0, (
            "a restart must not re-post a menu this bot already bridged — that is "
            "the 188-times loop (#633)"
        )

    @pytest.mark.asyncio
    async def test_resolved_menu_left_on_screen_is_not_rebridged(self, ledger_db) -> None:
        """AC2: an answered menu whose frame is still in the pane stays quiet."""
        from c_lord.database.menu_bridge_repo import MenuBridgeRepository
        from c_lord.thread_state_sync import MenuRebridgeLedger

        pane = _fixture("ask_rich_descriptions.txt")
        loop, _, _ = _make_loop(ledger=MenuRebridgeLedger(MenuBridgeRepository(ledger_db)))

        with patch("c_lord.discord_ui.ask_handler.bridge_pane_ask", new=AsyncMock()) as bridge:
            await _sweep(loop, 633_002, pane)  # bridged, then resolved normally
            await _sweep(loop, 633_002, pane)  # same frame still on screen
            await _sweep(loop, 633_002, pane)
        assert bridge.await_count == 1

    @pytest.mark.asyncio
    async def test_a_new_question_after_the_menu_closes_is_bridged(self, ledger_db) -> None:
        """The dedup must not silence a menu the user has never seen.

        The budget is released by *observation*: once a sweep sees the pane with
        no menu at all, the episode is over and the next menu — even a textually
        identical re-ask — is a new decision.
        """
        from c_lord.database.menu_bridge_repo import MenuBridgeRepository
        from c_lord.thread_state_sync import MenuRebridgeLedger

        pane = _fixture("ask_rich_descriptions.txt")
        idle = "❯ \n  ⏵⏵ bypass permissions on\n"
        loop, _, _ = _make_loop(ledger=MenuRebridgeLedger(MenuBridgeRepository(ledger_db)))

        with patch("c_lord.discord_ui.ask_handler.bridge_pane_ask", new=AsyncMock()) as bridge:
            await _sweep(loop, 633_003, pane)
            await _sweep(loop, 633_003, idle)  # menu closed — episode over
            await _sweep(loop, 633_003, pane)  # asked again → a new decision
        assert bridge.await_count == 2

    @pytest.mark.asyncio
    async def test_an_empty_capture_does_not_release_the_budget(self, ledger_db) -> None:
        """#485: an empty capture is 'unknown', never 'the menu closed'."""
        from c_lord.database.menu_bridge_repo import MenuBridgeRepository
        from c_lord.thread_state_sync import MenuRebridgeLedger

        pane = _fixture("ask_rich_descriptions.txt")
        loop, _, _ = _make_loop(ledger=MenuRebridgeLedger(MenuBridgeRepository(ledger_db)))

        with patch("c_lord.discord_ui.ask_handler.bridge_pane_ask", new=AsyncMock()) as bridge:
            await _sweep(loop, 633_004, pane)
            await _sweep(loop, 633_004, "")
            await _sweep(loop, 633_004, pane)
        assert bridge.await_count == 1


    @pytest.mark.asyncio
    async def test_a_post_that_failed_is_still_retried(self, ledger_db) -> None:
        """#579 must survive #633: a menu nobody could SEE keeps its budget.

        Discord rejects the whole message when one button label is empty, so a
        menu can fail to post; the watchdog retries it (capped at
        ``_ASK_BRIDGE_MAX_FAILURES``). If the one-post budget were spent by the
        attempt rather than by a delivered menu, that retry would be dead and
        the user would never see the question at all.
        """
        from c_lord.database.menu_bridge_repo import MenuBridgeRepository
        from c_lord.thread_state_sync import MenuRebridgeLedger

        pane = _fixture("ask_rich_descriptions.txt")
        loop, _, _ = _make_loop(ledger=MenuRebridgeLedger(MenuBridgeRepository(ledger_db)))
        boom = AsyncMock(side_effect=RuntimeError("400 Bad Request: label required"))

        with patch("c_lord.discord_ui.ask_handler.bridge_pane_ask", new=boom):
            await _sweep(loop, 633_005, pane)
            await _sweep(loop, 633_005, pane)
        assert boom.await_count == 2, "a failed post must be retried, not deduped away"


class TestMenuFingerprint:
    """AC1 asks for the identity rule to be stated; this is it, executable."""

    def test_same_text_same_fingerprint_different_text_different(self) -> None:
        from c_lord.claude.types import AskOption, AskQuestion
        from c_lord.thread_state_sync import menu_fingerprint

        def q(header: str, question: str, labels: list[str]) -> AskQuestion:
            return AskQuestion(
                question=question,
                header=header,
                options=[AskOption(label=x) for x in labels],
            )

        base = q("切り口", "どの切り口で書きますか", ["具体から", "抽象から"])
        assert menu_fingerprint(base) == menu_fingerprint(
            q("切り口", "どの切り口で書きますか", ["具体から", "抽象から"])
        )
        # A different header, question or option set is a different decision.
        assert menu_fingerprint(base) != menu_fingerprint(
            q("連携の扱い", "どの切り口で書きますか", ["具体から", "抽象から"])
        )
        assert menu_fingerprint(base) != menu_fingerprint(
            q("切り口", "どの切り口で書きますか", ["具体から", "抽象から", "両方"])
        )
        assert menu_fingerprint(base) != menu_fingerprint(
            q("切り口", "別の問い", ["具体から", "抽象から"])
        )
