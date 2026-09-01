"""7日アイドルで自動停止する — Issue #574 の後半。

閾値の根拠は #540 の実測。人間が同じスレッドに戻ってくるまでの間隔 (n=2650) は
p95 = 2.42h で、**7日を超えて戻ってくる復帰は 0.49%** — 200回に1回しかない。
しかも停止は何も失わない (docker を起こし直すだけ) ので、外したときの損害が
ほぼゼロ。30日まで待つとポートを1ヶ月掴み続けるぶんだけ損をする。

アイドルの測り方には落とし穴がある。tmux の ``window_activity`` は使えない —
Claude の TUI はスピナーとステータス行を描き続けるので、**誰も触っていなくても
更新され続ける**。実測では 51 本の claude 全部が「直近1〜3時間以内に activity
あり」に見えた。判定は人間の発言 (``last_used_at``) を見る。
"""

from __future__ import annotations

import datetime
from dataclasses import replace

from c_lord.database.repository import SessionRecord
from c_lord.idle_stop import IDLE_STOP_DAYS_DEFAULT, idle_stop_days, select_idle_workspaces

NOW = datetime.datetime(2026, 8, 26, 12, 0, 0)


def _rec(tid: int, *, days_ago: float, closed: bool = False) -> SessionRecord:
    ts = (NOW - datetime.timedelta(days=days_ago)).strftime("%Y-%m-%d %H:%M:%S")
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


class TestSelection:
    def test_a_workspace_idle_past_the_threshold_is_selected(self) -> None:
        picked = select_idle_workspaces([_rec(1, days_ago=8)], now=NOW, threshold_days=7)
        assert picked == [1]

    def test_a_workspace_inside_the_threshold_is_left_alone(self) -> None:
        assert select_idle_workspaces([_rec(1, days_ago=6.9)], now=NOW, threshold_days=7) == []

    def test_exactly_at_the_threshold_is_not_yet_selected(self) -> None:
        """Strictly greater — a boundary tick must not fire a day early."""
        assert select_idle_workspaces([_rec(1, days_ago=7)], now=NOW, threshold_days=7) == []

    def test_an_already_stopped_workspace_is_not_stopped_again(self) -> None:
        """Re-stopping would re-post the notice every tick, forever."""
        assert (
            select_idle_workspaces([_rec(1, days_ago=30, closed=True)], now=NOW, threshold_days=7)
            == []
        )

    def test_a_workspace_with_a_running_turn_is_never_selected(self) -> None:
        """``last_used_at`` is stamped when the turn *starts*. A turn that has been
        running for over a week is still a turn — killing it would destroy work."""
        picked = select_idle_workspaces(
            [_rec(1, days_ago=8)], now=NOW, threshold_days=7, in_flight={1}
        )
        assert picked == []

    def test_an_unparsable_timestamp_is_skipped_not_guessed(self) -> None:
        """Never destroy something because a date failed to parse."""
        rec = _rec(1, days_ago=8)
        rec = replace(rec, last_used_at="not-a-date")
        assert select_idle_workspaces([rec], now=NOW, threshold_days=7) == []

    def test_oldest_first(self) -> None:
        """Deterministic order keeps logs and evidence comparable across runs."""
        picked = select_idle_workspaces(
            [_rec(1, days_ago=8), _rec(2, days_ago=30), _rec(3, days_ago=15)],
            now=NOW,
            threshold_days=7,
        )
        assert picked == [2, 3, 1]

    def test_empty_input(self) -> None:
        assert select_idle_workspaces([], now=NOW, threshold_days=7) == []


class TestZeroConfigThreshold:
    def test_default_is_seven_days(self, monkeypatch) -> None:
        """#540: the threshold is a property of how people use Discord, not of
        the host, so it ships as one constant for everybody."""
        monkeypatch.delenv("CLORD_IDLE_STOP_DAYS", raising=False)
        assert idle_stop_days() == IDLE_STOP_DAYS_DEFAULT == 7

    def test_env_overrides(self, monkeypatch) -> None:
        monkeypatch.setenv("CLORD_IDLE_STOP_DAYS", "14")
        assert idle_stop_days() == 14

    def test_zero_disables(self, monkeypatch) -> None:
        """An operator must be able to turn automatic stopping off entirely."""
        monkeypatch.setenv("CLORD_IDLE_STOP_DAYS", "0")
        assert idle_stop_days() == 0

    def test_garbage_falls_back_to_the_default(self, monkeypatch) -> None:
        """A typo in .env must not silently disable the feature — nor crash."""
        monkeypatch.setenv("CLORD_IDLE_STOP_DAYS", "seven")
        assert idle_stop_days() == IDLE_STOP_DAYS_DEFAULT

    def test_negative_falls_back_to_the_default(self, monkeypatch) -> None:
        monkeypatch.setenv("CLORD_IDLE_STOP_DAYS", "-3")
        assert idle_stop_days() == IDLE_STOP_DAYS_DEFAULT


class TestIdleLabel:
    def test_label_reads_as_a_span_of_days(self) -> None:
        from c_lord.idle_stop import idle_label_for

        assert idle_label_for(7) == "7日間"


class TestLoopUsesTheSameStopPath:
    """#540: automatic and manual must be *one* implementation.

    Two functions that produce "the same" outcome always drift — #538 was
    precisely that (the side announcing a behaviour and the side implementing it
    disagreed about the condition). The loop therefore calls the very function
    ``/workspace-stop`` calls, differing only by the ``reason`` argument.
    """

    def test_loop_calls_close_workspace_impl_with_idle_reason(self) -> None:
        import asyncio
        from unittest.mock import AsyncMock, MagicMock

        from c_lord.idle_stop import IdleStopLoop
        from c_lord.workspace_notice import WorkspaceReason

        cog = MagicMock()
        cog._close_workspace_impl = AsyncMock()

        repo = MagicMock()
        repo.list_all = AsyncMock(return_value=[_rec(1, days_ago=30)])

        bot = MagicMock()
        bot.get_cog = MagicMock(return_value=cog)
        thread = MagicMock()
        bot.get_channel = MagicMock(return_value=thread)

        loop = IdleStopLoop(bot, repo, threshold_days=7, now_fn=lambda: NOW)
        asyncio.run(loop.tick())

        cog._close_workspace_impl.assert_awaited_once()
        kwargs = cog._close_workspace_impl.await_args.kwargs
        assert kwargs["reason"] is WorkspaceReason.IDLE
        assert kwargs["idle_label"] == "7日間"

    def test_threshold_zero_disables_the_loop(self) -> None:
        import asyncio
        from unittest.mock import AsyncMock, MagicMock

        from c_lord.idle_stop import IdleStopLoop

        repo = MagicMock()
        repo.list_all = AsyncMock(return_value=[_rec(1, days_ago=999)])
        bot = MagicMock()

        loop = IdleStopLoop(bot, repo, threshold_days=0, now_fn=lambda: NOW)
        asyncio.run(loop.tick())

        repo.list_all.assert_not_awaited()

    def test_a_thread_discord_no_longer_knows_is_skipped(self) -> None:
        """A deleted thread must not take the whole tick down with it."""
        import asyncio
        from unittest.mock import AsyncMock, MagicMock

        from c_lord.idle_stop import IdleStopLoop

        cog = MagicMock()
        cog._close_workspace_impl = AsyncMock()
        repo = MagicMock()
        repo.list_all = AsyncMock(return_value=[_rec(1, days_ago=30)])
        bot = MagicMock()
        bot.get_cog = MagicMock(return_value=cog)
        bot.get_channel = MagicMock(return_value=None)

        loop = IdleStopLoop(bot, repo, threshold_days=7, now_fn=lambda: NOW)
        asyncio.run(loop.tick())

        cog._close_workspace_impl.assert_not_awaited()

    def test_one_failure_does_not_abort_the_rest(self) -> None:
        import asyncio
        from unittest.mock import AsyncMock, MagicMock

        from c_lord.idle_stop import IdleStopLoop

        cog = MagicMock()
        cog._close_workspace_impl = AsyncMock(side_effect=[RuntimeError("boom"), None])
        repo = MagicMock()
        repo.list_all = AsyncMock(return_value=[_rec(1, days_ago=30), _rec(2, days_ago=20)])
        bot = MagicMock()
        bot.get_cog = MagicMock(return_value=cog)
        bot.get_channel = MagicMock(return_value=MagicMock())

        loop = IdleStopLoop(bot, repo, threshold_days=7, now_fn=lambda: NOW)
        asyncio.run(loop.tick())

        assert cog._close_workspace_impl.await_count == 2


class TestFirstSweepDoesNotBurst:
    """A backlog must not turn into a wall of notices.

    Measured against the production DB: switching this on would find **20**
    workspaces already past 7 days. Stopping all of them in one tick means 20
    embeds and 20 thread archives back to back — the same burst #277 had to fix
    for the rename loop, and the reader's first experience of the feature would
    be their channel filling up.

    Spreading them over consecutive ticks costs nothing: everything selected has
    already been idle for a week, so another 10 minutes is immaterial.
    """

    def _many(self, n: int) -> list[SessionRecord]:
        return [_rec(i, days_ago=10 + i) for i in range(1, n + 1)]

    def test_a_tick_stops_at_most_the_cap(self) -> None:
        import asyncio
        from unittest.mock import AsyncMock, MagicMock

        from c_lord.idle_stop import IdleStopLoop

        cog = MagicMock()
        cog._close_workspace_impl = AsyncMock()
        repo = MagicMock()
        repo.list_all = AsyncMock(return_value=self._many(20))
        bot = MagicMock()
        bot.get_cog = MagicMock(return_value=cog)
        bot.get_channel = MagicMock(return_value=MagicMock())

        loop = IdleStopLoop(bot, repo, threshold_days=7, now_fn=lambda: NOW, max_per_tick=5)
        asyncio.run(loop.tick())

        assert cog._close_workspace_impl.await_count == 5

    def test_the_backlog_is_reported_so_it_is_not_invisible(self, caplog) -> None:
        """Silent truncation reads as "we handled everything"."""
        import asyncio
        import logging
        from unittest.mock import AsyncMock, MagicMock

        from c_lord.idle_stop import IdleStopLoop

        cog = MagicMock()
        cog._close_workspace_impl = AsyncMock()
        repo = MagicMock()
        repo.list_all = AsyncMock(return_value=self._many(20))
        bot = MagicMock()
        bot.get_cog = MagicMock(return_value=cog)
        bot.get_channel = MagicMock(return_value=MagicMock())

        loop = IdleStopLoop(bot, repo, threshold_days=7, now_fn=lambda: NOW, max_per_tick=5)
        with caplog.at_level(logging.INFO):
            asyncio.run(loop.tick())

        assert "20" in caplog.text

    def test_the_oldest_go_first(self) -> None:
        import asyncio
        from unittest.mock import AsyncMock, MagicMock

        from c_lord.idle_stop import IdleStopLoop

        cog = MagicMock()
        cog._close_workspace_impl = AsyncMock()
        repo = MagicMock()
        repo.list_all = AsyncMock(return_value=self._many(20))
        bot = MagicMock()
        bot.get_cog = MagicMock(return_value=cog)
        channels = {}

        def _get_channel(tid):
            channels.setdefault(tid, MagicMock(id=tid))
            return channels[tid]

        bot.get_channel = _get_channel

        loop = IdleStopLoop(bot, repo, threshold_days=7, now_fn=lambda: NOW, max_per_tick=3)
        asyncio.run(loop.tick())

        picked = [c.await_args.kwargs["channel"].id for c in [cog._close_workspace_impl]] or []
        del picked
        ids = [call.kwargs["channel"].id for call in cog._close_workspace_impl.await_args_list]
        assert ids == [20, 19, 18]


class TestArchivedThreadsAreReachable:
    """本番で4日間 1件も止まらなかったバグ — #593。

    ログはこう言い続けていた::

        idle-stop: 93 workspace(s) past the threshold; stopping 5 this tick
        （10分おきに、同じ 93 のまま4日間）

    毎tick選ばれる最古5件を Discord に問い合わせると **5件とも archived=True**
    だった。``get_channel`` はキャッシュしか見ないので、Discord が自動アーカイブ
    したスレッドは ``None`` になり、黙って飛ばされていた。

    しかも「対象を古い順に並べて先頭5件だけ処理する」設計だったため、**先頭が
    解決できないと後ろの88件に永久に到達しない**。

    7日放置したスレッドは Discord 側が自動アーカイブしているのが普通なので、
    閾値7日という設計そのものが「ほぼ必ず引けない」条件になっていた。
    """

    def _loop(self, bot, repo, **kw):
        from c_lord.idle_stop import IdleStopLoop

        return IdleStopLoop(bot, repo, threshold_days=7, now_fn=lambda: NOW, **kw)

    def _bot_with_uncached_threads(self, cog, *, fetchable: set[int]):
        import discord
        from unittest.mock import AsyncMock, MagicMock

        bot = MagicMock()
        bot.get_cog = MagicMock(return_value=cog)
        bot.get_channel = MagicMock(return_value=None)  # nothing is cached

        async def _fetch(tid):
            if tid in fetchable:
                return MagicMock(id=tid)
            raise discord.NotFound(MagicMock(status=404), "Unknown Channel")

        bot.fetch_channel = AsyncMock(side_effect=_fetch)
        return bot

    def test_an_archived_thread_is_fetched_and_stopped(self) -> None:
        """The cache miss is not the end of the story — ask the API."""
        import asyncio
        from unittest.mock import AsyncMock, MagicMock

        cog = MagicMock()
        cog._close_workspace_impl = AsyncMock()
        repo = MagicMock()
        repo.list_all = AsyncMock(return_value=[_rec(1, days_ago=30)])
        bot = self._bot_with_uncached_threads(cog, fetchable={1})

        asyncio.run(self._loop(bot, repo).tick())

        cog._close_workspace_impl.assert_awaited_once()

    def test_a_deleted_thread_does_not_block_the_ones_behind_it(self) -> None:
        """The head-of-line bug: 5 unresolvable threads froze the other 88."""
        import asyncio
        from unittest.mock import AsyncMock, MagicMock

        cog = MagicMock()
        cog._close_workspace_impl = AsyncMock()
        repo = MagicMock()
        # 1 is the oldest and is gone; 2 and 3 are reachable and must still run.
        repo.list_all = AsyncMock(
            return_value=[_rec(1, days_ago=40), _rec(2, days_ago=30), _rec(3, days_ago=20)]
        )
        bot = self._bot_with_uncached_threads(cog, fetchable={2, 3})

        asyncio.run(self._loop(bot, repo, max_per_tick=2).tick())

        ids = [c.kwargs["channel"].id for c in cog._close_workspace_impl.await_args_list]
        assert ids == [2, 3]

    def test_a_deleted_thread_is_not_retried_forever(self) -> None:
        """Retrying a thread Discord says does not exist is pure noise, and it
        is what kept the backlog count frozen."""
        import asyncio
        from unittest.mock import AsyncMock, MagicMock

        cog = MagicMock()
        cog._close_workspace_impl = AsyncMock()
        repo = MagicMock()
        repo.list_all = AsyncMock(return_value=[_rec(1, days_ago=40), _rec(2, days_ago=30)])
        bot = self._bot_with_uncached_threads(cog, fetchable={2})

        loop = self._loop(bot, repo, max_per_tick=2)
        asyncio.run(loop.tick())
        bot.fetch_channel.reset_mock()
        asyncio.run(loop.tick())

        assert 1 not in [c.args[0] for c in bot.fetch_channel.await_args_list]

    def test_a_transient_failure_is_retried_next_tick(self) -> None:
        """Only "this does not exist" is permanent. A network blip must not
        write a workspace off for the life of the process."""
        import asyncio
        from unittest.mock import AsyncMock, MagicMock

        cog = MagicMock()
        cog._close_workspace_impl = AsyncMock()
        repo = MagicMock()
        repo.list_all = AsyncMock(return_value=[_rec(1, days_ago=40)])

        bot = MagicMock()
        bot.get_cog = MagicMock(return_value=cog)
        bot.get_channel = MagicMock(return_value=None)
        bot.fetch_channel = AsyncMock(side_effect=OSError("connection reset"))

        loop = self._loop(bot, repo)
        asyncio.run(loop.tick())
        asyncio.run(loop.tick())

        assert bot.fetch_channel.await_count == 2

    def test_skips_are_visible_in_the_log(self, caplog) -> None:
        """The real cost of this bug was four days of not noticing. A skip that
        only logs at DEBUG reads, from outside, as "nothing happened"."""
        import asyncio
        import logging
        from unittest.mock import AsyncMock, MagicMock

        cog = MagicMock()
        cog._close_workspace_impl = AsyncMock()
        repo = MagicMock()
        repo.list_all = AsyncMock(return_value=[_rec(1, days_ago=40)])
        bot = self._bot_with_uncached_threads(cog, fetchable=set())

        with caplog.at_level(logging.INFO):
            asyncio.run(self._loop(bot, repo).tick())

        assert any(r.levelno >= logging.INFO and "1" in r.getMessage() for r in caplog.records)
