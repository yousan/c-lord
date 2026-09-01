"""4時間アイドルでスリープする — Issue #572。

**スリープは claude プロセスだけを止める。利用者は気づかない。** docker は止め
ない（走っているビルドや DB が飛ぶ。回収したい 400MB は docker を止めなくても
取れる）。作業ディレクトリ・会話履歴・volume はそのまま。スレッド名も変えない。
手動コマンドも作らない（「停止とどう違う?」の混乱を招くだけ）。

閾値4時間の根拠は #540 の実測。人間が同じスレッドに戻ってくるまでの間隔
(n=2650) は p95 = 2.42h なので、**4時間なら影響を受ける復帰は 3.89%**（26回に
1回）。そのときも次の投稿で無言復元するので、利用者には見えない。

アイドルの測り方の落とし穴は #574 と同じ — tmux の ``window_activity`` は
Claude の TUI がスピナーを描き続けるせいで、誰も触っていなくても更新される。
判定は人間の発言 (``last_used_at``) を見る。
"""

from __future__ import annotations

import datetime
from dataclasses import replace
from unittest.mock import AsyncMock, MagicMock

import pytest

from c_lord.database.repository import SessionRecord
from c_lord.idle_sleep import (
    IDLE_SLEEP_HOURS_DEFAULT,
    IdleSleepLoop,
    idle_label_for_hours,
    idle_sleep_hours,
)
from c_lord.idle_sweep import select_idle_workspaces

NOW = datetime.datetime(2026, 8, 31, 12, 0, 0)
FOUR_HOURS = datetime.timedelta(hours=4)


def _rec(tid: int, *, hours_ago: float, closed: bool = False) -> SessionRecord:
    ts = (NOW - datetime.timedelta(hours=hours_ago)).strftime("%Y-%m-%d %H:%M:%S")
    return SessionRecord(
        thread_id=tid,
        session_id="a" * 32,
        working_dir="/w",
        model="opus",
        origin="discord",
        summary=None,
        created_at=ts,
        last_used_at=ts,
        closed_at=ts if closed else None,
    )


class TestZeroConfigThreshold:
    """#540: 既定値はホスト規模に依存しない定数。2GiB の VPS でも同じ4時間。"""

    def test_default_is_four_hours(self, monkeypatch) -> None:
        monkeypatch.delenv("CLORD_IDLE_SLEEP_HOURS", raising=False)
        assert idle_sleep_hours() == IDLE_SLEEP_HOURS_DEFAULT == 4

    def test_env_overrides(self, monkeypatch) -> None:
        monkeypatch.setenv("CLORD_IDLE_SLEEP_HOURS", "8")
        assert idle_sleep_hours() == 8

    def test_fractional_hours_are_allowed(self, monkeypatch) -> None:
        """検証と運用調整のため。整数しか受けないと staging で試せない。"""
        monkeypatch.setenv("CLORD_IDLE_SLEEP_HOURS", "0.5")
        assert idle_sleep_hours() == 0.5

    def test_zero_disables(self, monkeypatch) -> None:
        monkeypatch.setenv("CLORD_IDLE_SLEEP_HOURS", "0")
        assert idle_sleep_hours() == 0

    def test_garbage_falls_back_to_the_default(self, monkeypatch) -> None:
        """.env の typo で省メモリが黙って止まってはいけない（落ちるのはもっと駄目）。"""
        monkeypatch.setenv("CLORD_IDLE_SLEEP_HOURS", "four")
        assert idle_sleep_hours() == IDLE_SLEEP_HOURS_DEFAULT

    def test_negative_falls_back_to_the_default(self, monkeypatch) -> None:
        monkeypatch.setenv("CLORD_IDLE_SLEEP_HOURS", "-2")
        assert idle_sleep_hours() == IDLE_SLEEP_HOURS_DEFAULT


class TestIdleLabel:
    def test_label_reads_as_a_span_of_hours(self) -> None:
        assert idle_label_for_hours(4) == "4時間"

    def test_a_whole_number_never_shows_a_decimal_point(self) -> None:
        assert idle_label_for_hours(4.0) == "4時間"

    def test_a_fraction_is_shown_as_written(self) -> None:
        assert idle_label_for_hours(0.5) == "0.5時間"


class TestSelection:
    """選び方は #574 と同じ関数。時間か日かの違いしかないので共有する。"""

    def test_a_workspace_idle_past_the_threshold_is_selected(self) -> None:
        assert select_idle_workspaces([_rec(1, hours_ago=5)], now=NOW, threshold=FOUR_HOURS) == [1]

    def test_a_workspace_inside_the_threshold_is_left_alone(self) -> None:
        assert select_idle_workspaces([_rec(1, hours_ago=3.9)], now=NOW, threshold=FOUR_HOURS) == []

    def test_exactly_at_the_threshold_is_not_yet_selected(self) -> None:
        assert select_idle_workspaces([_rec(1, hours_ago=4)], now=NOW, threshold=FOUR_HOURS) == []

    def test_an_already_stopped_workspace_is_not_selected(self) -> None:
        """停止済みには止める claude がもう居ない。毎tick触ると通知が湧く。"""
        rec = _rec(1, hours_ago=99, closed=True)
        assert select_idle_workspaces([rec], now=NOW, threshold=FOUR_HOURS) == []

    def test_a_workspace_with_a_running_turn_is_never_selected(self) -> None:
        """#572 AC: 走っているターンの最中には絶対にスリープしない。

        ``last_used_at`` はターンの**開始**時刻なので、5時間走り続けている
        ターンはタイムスタンプ上アイドルに見える。殺せば実作業が飛ぶ。
        """
        picked = select_idle_workspaces(
            [_rec(1, hours_ago=5)], now=NOW, threshold=FOUR_HOURS, in_flight={1}
        )
        assert picked == []

    def test_an_unparsable_timestamp_is_skipped_not_guessed(self) -> None:
        rec = replace(_rec(1, hours_ago=5), last_used_at="not-a-date")
        assert select_idle_workspaces([rec], now=NOW, threshold=FOUR_HOURS) == []

    def test_oldest_first(self) -> None:
        picked = select_idle_workspaces(
            [_rec(1, hours_ago=5), _rec(2, hours_ago=40), _rec(3, hours_ago=9)],
            now=NOW,
            threshold=FOUR_HOURS,
        )
        assert picked == [2, 3, 1]


class TestLoopUsesTheOneSleepPath:
    """自動と手動を2実装にしない（#538 の失敗形）。

    スリープに手動コマンドは無いが、#576（常駐上限）が同じ操作を LRU で呼ぶ。
    どちらも ``SessionManageCog._sleep_workspace_impl`` を呼ぶ一本道にしておく。
    """

    def _cog(self) -> MagicMock:
        cog = MagicMock()
        cog._sleep_workspace_impl = AsyncMock(return_value=True)
        return cog

    def _bot(self, cog: MagicMock) -> MagicMock:
        bot = MagicMock()
        bot.get_cog = MagicMock(return_value=cog)
        bot.get_channel = MagicMock(side_effect=lambda t: MagicMock(id=t))
        return bot

    @pytest.mark.asyncio
    async def test_loop_calls_the_sleep_impl_with_the_idle_reason(self) -> None:
        from c_lord.workspace_notice import WorkspaceReason

        cog = self._cog()
        repo = MagicMock()
        repo.list_all = AsyncMock(return_value=[_rec(1, hours_ago=9)])

        await IdleSleepLoop(self._bot(cog), repo, threshold_hours=4, now_fn=lambda: NOW).tick()

        cog._sleep_workspace_impl.assert_awaited_once()
        kwargs = cog._sleep_workspace_impl.await_args.kwargs
        assert kwargs["reason"] is WorkspaceReason.IDLE
        assert kwargs["idle_label"] == "4時間"

    def test_the_cog_really_has_the_method_the_loop_looks_up(self) -> None:
        """``get_cog`` + ``getattr(name)`` は AST 上ただの文字列なので、
        tests/test_wiring.py の参照チェックには映らない。実物で確かめる。

        これが無いと、メソッド名を変えた瞬間にスリープは**モックのテストを全部
        緑にしたまま**本番で何もしなくなる（#570 / #612 と同じ形）。
        """
        from c_lord.cogs.session_manage import SessionManageCog

        assert callable(getattr(SessionManageCog, "_sleep_workspace_impl", None))

    @pytest.mark.asyncio
    async def test_threshold_zero_disables_the_loop(self) -> None:
        repo = MagicMock()
        repo.list_all = AsyncMock(return_value=[_rec(1, hours_ago=999)])

        await IdleSleepLoop(MagicMock(), repo, threshold_hours=0, now_fn=lambda: NOW).tick()

        repo.list_all.assert_not_awaited()

    @pytest.mark.asyncio
    async def test_one_failure_does_not_abort_the_rest(self) -> None:
        cog = self._cog()
        cog._sleep_workspace_impl = AsyncMock(side_effect=[RuntimeError("boom"), True])
        repo = MagicMock()
        repo.list_all = AsyncMock(return_value=[_rec(1, hours_ago=9), _rec(2, hours_ago=8)])

        await IdleSleepLoop(self._bot(cog), repo, threshold_hours=4, now_fn=lambda: NOW).tick()

        assert cog._sleep_workspace_impl.await_count == 2


class TestArchivedThreadsAreReachable:
    """#593 と同じ罠。``get_channel`` はキャッシュしか見ない。

    4時間放置されたスレッドは、auto-archive を1時間に設定したチャンネルでは
    もうアーカイブ済み。キャッシュミスを「消えた」と読むと1件も眠らない。
    """

    @pytest.mark.asyncio
    async def test_an_archived_thread_is_fetched_and_slept(self) -> None:
        cog = MagicMock()
        cog._sleep_workspace_impl = AsyncMock(return_value=True)
        repo = MagicMock()
        repo.list_all = AsyncMock(return_value=[_rec(1, hours_ago=9)])

        bot = MagicMock()
        bot.get_cog = MagicMock(return_value=cog)
        bot.get_channel = MagicMock(return_value=None)
        bot.fetch_channel = AsyncMock(side_effect=lambda t: MagicMock(id=t))

        await IdleSleepLoop(bot, repo, threshold_hours=4, now_fn=lambda: NOW).tick()

        cog._sleep_workspace_impl.assert_awaited_once()

    @pytest.mark.asyncio
    async def test_a_deleted_thread_does_not_block_the_ones_behind_it(self) -> None:
        import discord

        cog = MagicMock()
        cog._sleep_workspace_impl = AsyncMock(return_value=True)
        repo = MagicMock()
        repo.list_all = AsyncMock(
            return_value=[_rec(1, hours_ago=40), _rec(2, hours_ago=30), _rec(3, hours_ago=20)]
        )

        async def _fetch(tid):
            if tid == 1:
                raise discord.NotFound(MagicMock(status=404), "Unknown Channel")
            return MagicMock(id=tid)

        bot = MagicMock()
        bot.get_cog = MagicMock(return_value=cog)
        bot.get_channel = MagicMock(return_value=None)
        bot.fetch_channel = AsyncMock(side_effect=_fetch)

        await IdleSleepLoop(bot, repo, threshold_hours=4, now_fn=lambda: NOW).tick()

        ids = [c.kwargs["channel"].id for c in cog._sleep_workspace_impl.await_args_list]
        assert ids == [2, 3]


class TestObservability:
    """#604 の教訓: 入力側の件数だけ出すと、1件も眠っていなくても正常に見える。"""

    @pytest.mark.asyncio
    async def test_the_number_actually_slept_is_logged(self, caplog) -> None:
        import logging

        cog = MagicMock()
        # 3件選ばれるが、実際に眠るのは1件だけ（残りは既にスリープ済み）。
        cog._sleep_workspace_impl = AsyncMock(side_effect=[True, False, False])
        repo = MagicMock()
        repo.list_all = AsyncMock(
            return_value=[_rec(1, hours_ago=9), _rec(2, hours_ago=8), _rec(3, hours_ago=7)]
        )
        bot = MagicMock()
        bot.get_cog = MagicMock(return_value=cog)
        bot.get_channel = MagicMock(side_effect=lambda t: MagicMock(id=t))

        with caplog.at_level(logging.INFO):
            await IdleSleepLoop(bot, repo, threshold_hours=4, now_fn=lambda: NOW).tick()

        slept_lines = [r for r in caplog.records if "idle-sleep" in r.getMessage()]
        assert slept_lines, "スリープしたことがログに残らない"
        assert any("1" in r.getMessage() for r in slept_lines)

    @pytest.mark.asyncio
    async def test_nothing_is_logged_when_nothing_was_slept(self, caplog) -> None:
        """毎tick「0件眠らせました」を出すとログが読めなくなる。"""
        import logging

        cog = MagicMock()
        cog._sleep_workspace_impl = AsyncMock(return_value=False)
        repo = MagicMock()
        repo.list_all = AsyncMock(return_value=[_rec(1, hours_ago=9)])
        bot = MagicMock()
        bot.get_cog = MagicMock(return_value=cog)
        bot.get_channel = MagicMock(side_effect=lambda t: MagicMock(id=t))

        with caplog.at_level(logging.INFO):
            await IdleSleepLoop(bot, repo, threshold_hours=4, now_fn=lambda: NOW).tick()

        assert "idle-sleep: slept" not in caplog.text
