"""#742 — 「このペインは、いまターンの途中か」。

これは**再起動を生き延びる唯一の根拠**なので、判定はハンドメイドの文字列では
なく**実機から採ったペイン**に対して固定する。fixture は 2026-09-15 に本番ホスト
(Claude Code v2.1.271) の tmux ペインから ``capture-pane -p -J`` で採取した。
"""

from __future__ import annotations

from pathlib import Path

import pytest

from c_lord.pane_running import pane_shows_running

FIXTURES = Path(__file__).parent / "fixtures" / "panes"


def _pane(name: str) -> str:
    return (FIXTURES / name).read_text(encoding="utf-8")


class TestRealPanes:
    def test_a_pane_mid_turn_is_running(self) -> None:
        """``✶ Transfiguring… (4m 6s · ↓ 48.0k tokens …)`` — 実行中の spinner。"""
        assert pane_shows_running(_pane("i742_running_mid_turn_v2_1_271.txt")) is True

    def test_a_pane_that_just_finished_is_not_running(self) -> None:
        """``✻ Sautéed for 14s · done 9:08 AM`` — **終わった**ペインの通常の姿。

        ここを「走っている」と読むと、上限も緊急ブレーキも**1本も眠らせられなく
        なる**（待機中のペインはほぼ全部この形で止まっている）。glyph ではなく
        括弧のタイマーを見る理由がこれ (#190)。
        """
        assert pane_shows_running(_pane("i742_idle_after_turn_v2_1_271.txt")) is False

    def test_the_spinner_is_found_above_the_input_box_and_footer(self) -> None:
        """spinner は入力ボックス + フッタに 10〜20 行押し上げられる (#190)。"""
        assert pane_shows_running(_pane("running_spinner_above_footer.txt")) is True

    def test_a_bare_input_box_is_not_running(self) -> None:
        assert pane_shows_running(_pane("input_box_empty.txt")) is False


class TestEdges:
    @pytest.mark.parametrize("text", ["", "   \n\n"])
    def test_an_unreadable_pane_is_not_evidence_of_work(self, text: str) -> None:
        assert pane_shows_running(text) is False

    @pytest.mark.parametrize(
        "line",
        [
            "✻ Running… (12s · esc to interrupt)",
            "✢ Swirling… (2m 29s · ↓ 9.3k tokens)",
            "✶ Creating PR… (11m 57s · ↑ 36.5k tokens)",
            "· Transfiguring… (1h 2m 3s · ↓ 6.5k tokens)",
        ],
    )
    def test_every_spinner_shape_carries_the_timer(self, line: str) -> None:
        """glyph は Claude Code が回すたびに増える。見るのはタイマーのほう。"""
        assert pane_shows_running(line) is True

    @pytest.mark.parametrize(
        "line",
        [
            "✻ Baked for 3m 9s",
            "✻ Sautéed for 14s · done 9:08 AM",
            "  ⎿  Running… (3s)",
        ],
    )
    def test_a_finished_turn_is_not_running(self, line: str) -> None:
        assert pane_shows_running(line) is False

    def test_a_spinner_scrolled_far_above_the_probe_window_is_not_claimed(self) -> None:
        """30 行より上のものは**現在の**状態の証拠ではない。"""
        pane = "✻ Running… (12s · esc to interrupt)\n" + "\n".join(str(i) for i in range(40))
        assert pane_shows_running(pane) is False
