"""A menu answer that never reaches the TUI must say so (#600).

When the button press could not be delivered — the thread's tmux window was not
found, so ``send_keys`` returned False — c-lord dropped it in silence. The menu
therefore stayed open, the watchdog kept finding it "unbridged", and the same ❓
question was re-posted on every restart. Two days passed before anyone noticed,
precisely because nothing said anything.

Two guards, matching the pattern #560 established for the other direction
(never report a delivery as succeeded without checking):

* AC3 — ``answer_menu``/``answer_menu_multi``/``answer_menu_text``/``cancel_menu``
  report whether the keystrokes landed, and the bridge tells the thread when they
  did not.
* AC4 — the same menu is not re-bridged forever (the #579 cap, applied to the
  re-post loop this bug drove).
"""

from __future__ import annotations

from unittest.mock import MagicMock, patch

import pytest

from c_lord.claude.tmux_runner import TmuxClaudeRunner


def _runner(send_keys_ok: bool) -> TmuxClaudeRunner:
    tmux = MagicMock()
    tmux.send_keys.return_value = send_keys_ok
    return TmuxClaudeRunner(tmux_manager=tmux, thread_id=4242)


class TestAnswerReportsDelivery:
    """AC3: the keystroke result must reach the caller, not be discarded."""

    @pytest.mark.asyncio
    async def test_answer_menu_reports_success(self) -> None:
        with patch("c_lord.claude.tmux_runner.asyncio.sleep"):
            assert await _runner(True).answer_menu(1) is True

    @pytest.mark.asyncio
    async def test_answer_menu_reports_failure(self) -> None:
        """``send_keys`` False means no window was found — the answer is lost."""
        with patch("c_lord.claude.tmux_runner.asyncio.sleep"):
            assert await _runner(False).answer_menu(1) is False, (
                "a menu answer that could not be delivered must not read as success "
                "— that silence is what stalled a thread for two days (#600)"
            )

    @pytest.mark.asyncio
    async def test_answer_menu_multi_reports_failure(self) -> None:
        with patch("c_lord.claude.tmux_runner.asyncio.sleep"):
            assert await _runner(False).answer_menu_multi([0, 1], 3) is False

    @pytest.mark.asyncio
    async def test_answer_menu_text_reports_failure(self) -> None:
        with patch("c_lord.claude.tmux_runner.asyncio.sleep"):
            assert await _runner(False).answer_menu_text(2, "自由記述") is False

    @pytest.mark.asyncio
    async def test_cancel_menu_reports_failure(self) -> None:
        with patch("c_lord.claude.tmux_runner.asyncio.sleep"):
            assert await _runner(False).cancel_menu() is False


class TestUserIsToldWhenTheAnswerIsLost:
    """AC3: and the person who pressed the button has to find out."""

    def test_undeliverable_notice_names_the_problem_and_a_next_step(self) -> None:
        from c_lord.discord_ui.ask_handler import _answer_undeliverable_notice

        text = _answer_undeliverable_notice(["選択肢A"])
        assert "届け" in text, "must say the answer did not reach Claude"
        assert "選択肢A" in text, "must echo what the user chose, so it is not lost"

    @pytest.mark.asyncio
    async def test_bridge_posts_the_notice_when_delivery_fails(self) -> None:
        """The end-to-end guard: a failed answer becomes a message in the thread."""
        import c_lord.discord_ui.ask_handler as ah

        thread = MagicMock()
        sent: list[str] = []

        async def _send(content=None, **kw):
            sent.append(content or "")
            return MagicMock()

        thread.send = _send
        thread.id = 4242

        runner = MagicMock()

        async def _answer(*_a, **_k):
            return False  # window not found — the #600 condition

        runner.answer_menu = _answer
        runner.answer_menu_multi = _answer
        runner.answer_menu_text = _answer

        await ah._report_answer_delivery(thread, delivered=False, selected=["選択肢A"])
        assert any("届け" in s for s in sent), (
            "the thread must be told the answer never reached Claude (#600 AC3)"
        )

    @pytest.mark.asyncio
    async def test_successful_delivery_stays_quiet(self) -> None:
        import c_lord.discord_ui.ask_handler as ah

        thread = MagicMock()
        sent: list[str] = []

        async def _send(content=None, **kw):
            sent.append(content or "")
            return MagicMock()

        thread.send = _send
        thread.id = 4242
        await ah._report_answer_delivery(thread, delivered=True, selected=["選択肢A"])
        assert not sent, "a delivered answer must not add noise to the thread"


class TestRebridgeIsCapped:
    """AC4: an unanswerable menu must not be re-posted forever."""

    def test_cap_exists_and_is_small(self) -> None:
        from c_lord.thread_state_sync import _MAX_REBRIDGES_PER_MENU

        assert 1 <= _MAX_REBRIDGES_PER_MENU <= 10, (
            "the same menu was re-posted 6 times over two days; the cap must be "
            "low enough that a stuck menu is obvious rather than endless"
        )

    def test_counts_per_menu_signature_not_per_thread(self) -> None:
        """A *new* question in the same thread must start with a fresh budget."""
        from c_lord.thread_state_sync import MenuRebridgeLedger

        ledger = MenuRebridgeLedger()
        for _ in range(10):
            ledger.record(4242, "切り口")
        assert ledger.exhausted(4242, "切り口") is True
        assert ledger.exhausted(4242, "別の質問") is False, (
            "a different menu in the same thread is a different question — it must "
            "not inherit the stuck one's exhausted budget"
        )

    def test_a_thread_that_moves_on_is_forgiven(self) -> None:
        from c_lord.thread_state_sync import MenuRebridgeLedger

        ledger = MenuRebridgeLedger()
        for _ in range(10):
            ledger.record(4242, "切り口")
        ledger.clear(4242)
        assert ledger.exhausted(4242, "切り口") is False
