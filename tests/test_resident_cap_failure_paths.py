"""常駐上限 (#576 / #742) の「壊れたとき」と「入口・出口」を固定する。

``tests/test_resident_cap.py`` は正常系（LRU 順・走行中の保護・緊急ブレーキ）を
押さえている。ここは残りの経路:

* 入力が読めないとき（tmux が答えない・台帳が読めない・メモリが読めない）に
  **何も眠らせない側へ倒れる**こと
* 眠らせる先のスレッドが引けない / スリープ本体が失敗したときに、ほかの
  victim を道連れにしないこと
* ループの起動・停止・例外からの生還
* cgroup / ``/proc/meminfo`` の読み取りの端

出典はそれぞれの docstring に書く。実機 tmux には一切触れない（``c_lord.tmux._run``
か ``resident_fn`` 等の注入で差し替える）。
"""

from __future__ import annotations

import asyncio
import datetime
import logging
from unittest.mock import AsyncMock, MagicMock, patch

import discord
import pytest

from c_lord import resident_cap
from c_lord.database.repository import SessionRecord
from c_lord.resident_cap import (
    EMERGENCY_LOW_FRACTION,
    ResidentCapLoop,
    select_lru_victims,
)
from c_lord.tmux import PaneTurnStates

GIB = 1024**3
NOW = datetime.datetime(2026, 8, 31, 18, 0, 0)
LOW = (31 * GIB, int(31 * GIB * 0.05))
#: ペインを読んで「走っているものも、読めなかったものも無い」(#1034)。
_NO_PANE_EVIDENCE = PaneTurnStates(frozenset(), frozenset())

OK = (31 * GIB, int(31 * GIB * 0.50))


def _rec(tid: int, *, hours_ago: float) -> SessionRecord:
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
        closed_at=None,
    )


def _bot(*, cog: object | None = None, channels: dict[int, object] | None = None) -> MagicMock:
    bot = MagicMock()
    bot.get_cog = MagicMock(return_value=cog)
    if channels is None:
        bot.get_channel = MagicMock(side_effect=lambda t: MagicMock(id=t))
    else:
        bot.get_channel = MagicMock(side_effect=lambda t: channels.get(t))
    return bot


def _cog(sleep_impl: AsyncMock | None = None) -> MagicMock:
    cog = MagicMock()
    cog._sleep_workspace_impl = sleep_impl or AsyncMock(return_value=True)
    return cog


def _repo(records: list[SessionRecord]) -> MagicMock:
    repo = MagicMock()
    repo.list_all = AsyncMock(return_value=records)
    return repo


def _mem_seq(samples: list[tuple[int, int]]):
    seq = list(samples)

    def _mem():
        return seq[0] if len(seq) == 1 else seq.pop(0)

    return _mem


def _slept(cog: MagicMock) -> list[int]:
    return [c.kwargs["channel"].id for c in cog._sleep_workspace_impl.await_args_list]


# ── tmux が答えないときは、何も眠らせない ─────────────────────────────────────


class TestAnUnansweringTmuxEvictsNothing:
    """常駐数は ``tmux list-panes -a`` 1回で数える（resident-cap.md「常駐数の数え方」）。

    tmux が答えないとき「常駐 0 本」と読むのは正しい倒れ方: 上限は**取り除く
    だけ**のループなので (ResidentCapLoop docstring)、読めない = 何もしない、で
    なければならない。逆に倒れると、読めなかった瞬間に全員が追い出される。
    """

    @pytest.mark.asyncio
    async def test_a_raising_resident_fn_counts_as_nobody_resident(self) -> None:
        cog = _cog()
        records = [_rec(i, hours_ago=i) for i in range(1, 30)]

        def _boom() -> set[int]:
            raise RuntimeError("tmux: no server running")

        loop = ResidentCapLoop(
            _bot(cog=cog), _repo(records), limit=2, resident_fn=_boom, mem_fn=lambda: OK
        )
        assert await loop.tick() == 0
        cog._sleep_workspace_impl.assert_not_awaited()

    @pytest.mark.asyncio
    async def test_the_default_reader_survives_a_tmux_that_is_not_installed(self) -> None:
        """``resident_fn`` を注入しない本番経路。``tmux -V`` が失敗 = tmux 無し。"""
        cog = _cog()
        records = [_rec(i, hours_ago=i) for i in range(1, 30)]
        loop = ResidentCapLoop(_bot(cog=cog), _repo(records), limit=2, mem_fn=lambda: OK)

        fail = MagicMock(returncode=1, stdout="", stderr="")
        with patch("c_lord.tmux._run", return_value=fail):
            assert await loop.tick() == 0
        cog._sleep_workspace_impl.assert_not_awaited()

    @pytest.mark.asyncio
    async def test_the_default_reader_survives_resident_thread_ids_raising(self) -> None:
        cog = _cog()
        records = [_rec(i, hours_ago=i) for i in range(1, 30)]
        loop = ResidentCapLoop(_bot(cog=cog), _repo(records), limit=2, mem_fn=lambda: OK)

        with patch("c_lord.tmux.resident_thread_ids", side_effect=OSError("tmux hung")):
            assert await loop.tick() == 0
        cog._sleep_workspace_impl.assert_not_awaited()

    @pytest.mark.asyncio
    async def test_the_brake_with_an_unanswering_tmux_sleeps_nothing(self, caplog) -> None:
        """逼迫中でも、誰が常駐しているか分からなければ眠らせない — 「眠らせるものが
        無い」と1回言って 0 を返す (#742 AC5: 無限ループにも洪水にもしない)。"""
        cog = _cog()
        records = [_rec(i, hours_ago=i) for i in range(1, 4)]

        def _boom() -> set[int]:
            raise RuntimeError("tmux hung")

        loop = ResidentCapLoop(
            _bot(cog=cog),
            _repo(records),
            limit=20,
            resident_fn=_boom,
            mem_fn=lambda: LOW,
        )
        with caplog.at_level(logging.WARNING):
            results = [await loop.tick() for _ in range(5)]
        assert results == [0] * 5
        cog._sleep_workspace_impl.assert_not_awaited()
        nothing = [r for r in caplog.records if "nothing it may sleep" in r.getMessage()]
        assert len(nothing) == 1


# ── 「走っている」の根拠が読めないとき ─────────────────────────────────────


class TestInFlightEvidenceSources:
    """#742 — 「走っている」は in-memory 台帳とペインの spinner の**和集合**。

    ``_turns_in_flight`` の docstring: 台帳だけが立ち上がり直後（spinner 未描画）を
    知っており、ペインだけが再起動後を知っている。どちらか片方でも「走っている」
    と言えば選ばれない。
    """

    @pytest.mark.asyncio
    async def test_the_ledger_alone_protects_a_turn_whose_pane_has_no_spinner_yet(
        self,
    ) -> None:
        cog = _cog()
        cog._active_tasks = {1: object()}  # spinner より先に台帳に載っている
        records = [_rec(1, hours_ago=99), _rec(2, hours_ago=1), _rec(3, hours_ago=2)]
        loop = ResidentCapLoop(
            _bot(cog=cog),
            _repo(records),
            limit=2,
            resident_fn=lambda: {1, 2, 3},
            mem_fn=lambda: OK,
        )
        with patch("c_lord.tmux.pane_turn_states", return_value=_NO_PANE_EVIDENCE):
            assert await loop.tick() == 1
        assert _slept(cog) == [3]

    @pytest.mark.asyncio
    async def test_the_pane_is_asked_only_about_residents_the_ledger_does_not_know(
        self,
    ) -> None:
        """ペインは1本ごとに capture-pane 1回かかる。台帳で既に分かっているものは
        聞かない（``in_flight | running_on_host(candidates - in_flight)``）。"""
        cog = _cog()
        cog._active_tasks = {1: object()}
        records = [_rec(i, hours_ago=10 - i) for i in range(1, 4)]
        loop = ResidentCapLoop(
            _bot(cog=cog),
            _repo(records),
            limit=2,
            resident_fn=lambda: {1, 2, 3},
            mem_fn=lambda: OK,
        )
        with patch("c_lord.tmux.pane_turn_states", return_value=_NO_PANE_EVIDENCE) as running:
            await loop.tick()
        (asked,), _ = running.call_args
        assert asked == {2, 3}

    @pytest.mark.asyncio
    async def test_the_pane_is_never_asked_under_the_limit(self) -> None:
        """resident-cap.md: 平常時（上限内・逼迫なし）は capture-pane を1回も呼ばない。"""
        cog = _cog()
        records = [_rec(i, hours_ago=i) for i in range(1, 4)]
        loop = ResidentCapLoop(
            _bot(cog=cog),
            _repo(records),
            limit=5,
            resident_fn=lambda: {1, 2, 3},
            mem_fn=lambda: OK,
        )
        with patch("c_lord.tmux.pane_turn_states") as running:
            assert await loop.tick() == 0
        running.assert_not_called()
        loop._repo.list_all.assert_not_awaited()  # 上限内ならDBも読まない

    @pytest.mark.asyncio
    async def test_no_candidates_means_no_pane_reads(self) -> None:
        loop = ResidentCapLoop(_bot(cog=_cog()), _repo([]), limit=2, mem_fn=lambda: OK)
        with patch("c_lord.tmux.pane_turn_states") as running:
            assert await loop._running_on_host(set()) == set()
        running.assert_not_called()

    def test_a_missing_chat_cog_reads_as_an_empty_ledger(self) -> None:
        loop = ResidentCapLoop(_bot(cog=None), _repo([]), limit=2, mem_fn=lambda: OK)
        assert loop._in_flight() == set()

    def test_a_ledger_keyed_by_strings_is_normalised_to_ints(self) -> None:
        cog = _cog()
        cog._active_tasks = {"11": object(), 12: object()}
        loop = ResidentCapLoop(_bot(cog=cog), _repo([]), limit=2, mem_fn=lambda: OK)
        assert loop._in_flight() == {11, 12}

    def test_a_ledger_that_cannot_be_read_does_not_raise(self) -> None:
        cog = _cog()
        cog._active_tasks = {"not-a-number": object()}
        loop = ResidentCapLoop(_bot(cog=cog), _repo([]), limit=2, mem_fn=lambda: OK)
        assert loop._in_flight() == set()

    def test_a_raising_injected_ledger_does_not_raise(self) -> None:
        def _boom() -> set[int]:
            raise RuntimeError

        loop = ResidentCapLoop(
            _bot(cog=_cog()), _repo([]), limit=2, in_flight_fn=_boom, mem_fn=lambda: OK
        )
        assert loop._in_flight() == set()


# ── 眠らせる先が引けない / 眠らせられない ─────────────────────────────────


class TestOneBadVictimDoesNotTakeOthersDown:
    """スリープは #572 の ``_sleep_workspace_impl`` 1本だけを使う（``_sleep_one`` docstring,
    #538「同じ結果の実装が2つあると必ずズレる」）。その手前でスレッドが引けない・
    本体が例外を投げる、は1本の失敗であって tick 全体の失敗ではない。

    数えるのは**実際に眠った本数**（session_manage ``_sleep_workspace_impl`` docstring,
    #604「試行回数を数えると、していない進捗を報告する」）。
    """

    @pytest.mark.asyncio
    async def test_a_cache_miss_is_fetched_from_the_api(self) -> None:
        cog = _cog()
        bot = _bot(cog=cog, channels={})
        bot.fetch_channel = AsyncMock(side_effect=lambda t: MagicMock(id=t))
        records = [_rec(i, hours_ago=i) for i in range(1, 4)]
        loop = ResidentCapLoop(
            bot,
            _repo(records),
            limit=2,
            resident_fn=lambda: {1, 2, 3},
            in_flight_fn=set,
            mem_fn=lambda: OK,
        )
        assert await loop.tick() == 1
        bot.fetch_channel.assert_awaited_once_with(3)
        assert _slept(cog) == [3]

    @pytest.mark.parametrize(
        "exc",
        [
            discord.NotFound(MagicMock(status=404), "Unknown Channel"),
            discord.Forbidden(MagicMock(status=403), "Missing Access"),
            RuntimeError("gateway hiccup"),
        ],
        ids=["deleted", "forbidden", "other"],
    )
    @pytest.mark.asyncio
    async def test_an_unreachable_thread_is_skipped_and_the_next_still_sleeps(
        self, exc: Exception, caplog
    ) -> None:
        cog = _cog()
        bot = _bot(cog=cog, channels={2: MagicMock(id=2)})
        bot.fetch_channel = AsyncMock(side_effect=exc)
        records = [_rec(i, hours_ago=i) for i in range(1, 5)]  # 4 が最古
        loop = ResidentCapLoop(
            bot,
            _repo(records),
            limit=2,
            resident_fn=lambda: {1, 2, 3, 4},
            in_flight_fn=set,
            mem_fn=lambda: OK,
        )
        deleted = isinstance(exc, discord.NotFound)
        with (
            caplog.at_level(logging.INFO, logger="c_lord.resident_cap"),
            patch("c_lord.tmux.close_thread_windows", return_value=1),
        ):
            # 4 と 3 はキャッシュに無く API でも引けない。#1033: 飛ばして次の 2 で補う
            # （1 も引けないので、眠るのは 2 だけ）。#1086: NotFound（削除済み）は
            # 補わずに窓を閉じ、数にも入れない — 生きているのは 1 と 2 で上限ちょうど
            assert await loop.tick() == (0 if deleted else 1)
        assert _slept(cog) == ([] if deleted else [2])
        msgs = [r.getMessage() for r in caplog.records]
        why = ("not reachable", "could not", "no longer exists")
        assert any("thread=4" in m and any(w in m for w in why) for m in msgs)
        assert any("thread=3" in m and any(w in m for w in why) for m in msgs)
        if not deleted:
            # 眠らせたのは実際に眠った1本だけ
            assert any("slept 1 workspace(s) to stay under the limit" in m for m in msgs)

    @pytest.mark.asyncio
    async def test_a_sleep_that_raises_is_logged_and_the_rest_continue(self, caplog) -> None:
        async def _impl(*, channel, reason, idle_label):
            if channel.id == 4:
                raise RuntimeError("docker died")
            return True

        cog = _cog(AsyncMock(side_effect=_impl))
        records = [_rec(i, hours_ago=i) for i in range(1, 5)]
        loop = ResidentCapLoop(
            _bot(cog=cog),
            _repo(records),
            limit=2,
            resident_fn=lambda: {1, 2, 3, 4},
            in_flight_fn=set,
            mem_fn=lambda: OK,
        )
        with caplog.at_level(logging.INFO, logger="c_lord.resident_cap"):
            # #1033: 4 の失敗は次の 2 で補い、1回の tick で上限まで戻す
            assert await loop.tick() == 2
        assert _slept(cog) == [4, 3, 2]
        assert any(
            r.levelno == logging.ERROR and "failed to sleep thread=4" in r.getMessage()
            for r in caplog.records
        )
        assert any("slept 2 workspace(s)" in r.getMessage() for r in caplog.records)

    @pytest.mark.asyncio
    async def test_a_sleep_that_kills_nothing_is_not_counted(self, caplog) -> None:
        """``_sleep_workspace_impl`` が False（窓がもう無かった）なら数えない (#604)。"""
        cog = _cog(AsyncMock(return_value=False))
        records = [_rec(i, hours_ago=i) for i in range(1, 5)]
        loop = ResidentCapLoop(
            _bot(cog=cog),
            _repo(records),
            limit=2,
            resident_fn=lambda: {1, 2, 3, 4},
            in_flight_fn=set,
            mem_fn=lambda: OK,
        )
        with caplog.at_level(logging.INFO, logger="c_lord.resident_cap"):
            assert await loop.tick() == 0
        msgs = [r.getMessage() for r in caplog.records]
        # 着手の1行は出る（何本選んだか）が、「眠らせた」の1行は出ない
        assert any("4 resident > limit 2 — sleeping 2 longest-idle" in m for m in msgs)
        assert not any("to stay under the limit" in m for m in msgs)

    @pytest.mark.asyncio
    async def test_without_the_session_manage_cog_nothing_is_slept(self, caplog) -> None:
        """2つ目の「眠らせる」実装は持たない (#538) — 本体が居なければ眠らせられない、
        と WARNING で言う。"""
        records = [_rec(i, hours_ago=i) for i in range(1, 5)]
        bot = _bot(cog=None)
        loop = ResidentCapLoop(
            bot,
            _repo(records),
            limit=2,
            resident_fn=lambda: {1, 2, 3, 4},
            in_flight_fn=set,
            mem_fn=lambda: OK,
        )
        with caplog.at_level(logging.WARNING, logger="c_lord.resident_cap"):
            assert await loop.tick() == 0
        assert any("SessionManageCog unavailable" in r.getMessage() for r in caplog.records)
        bot.get_channel.assert_not_called()  # スレッドを引きに行く前に止まる


# ── 緊急ブレーキの端 ─────────────────────────────────────────────────────


class TestBrakeEdges:
    """resident-cap.md「緊急ブレーキ」: 10% を3回連続で発火、回復したら止める。
    ``_emergency_brake`` docstring: 1本ずつ、毎回メモリを読み直す。"""

    @pytest.mark.asyncio
    async def test_the_brake_reads_memory_after_each_victim_even_when_sleep_fails(
        self,
    ) -> None:
        """眠らせ損ねても次へ進み、回復を見たらそこで止まる。"""
        sleep = AsyncMock(side_effect=[False, True, True, True])
        cog = _cog(sleep)
        records = [_rec(i, hours_ago=i) for i in range(1, 6)]
        # tick 1..3 の観測 + ENGAGED 時の再読 + 各 victim 後の再読
        mem = _mem_seq([LOW, LOW, LOW, LOW, LOW, OK, OK])
        loop = ResidentCapLoop(
            _bot(cog=cog),
            _repo(records),
            limit=20,
            resident_fn=lambda: {1, 2, 3, 4, 5},
            in_flight_fn=set,
            mem_fn=mem,
        )
        await loop.tick()
        await loop.tick()
        assert await loop.tick() == 1
        assert _slept(cog) == [5, 4]  # 5 は失敗、4 で回復
        assert loop._braking is False
        assert loop._low_streak == 0

    @pytest.mark.asyncio
    async def test_recovery_on_the_next_tick_logs_the_release(self, caplog) -> None:
        """全員走っていて眠らせられないまま、メモリが自然に戻ったとき — 解除は
        次の観測で WARNING として1行出る（踏んだ／戻ったを両方残す）。"""
        cog = _cog()
        records = [_rec(i, hours_ago=i) for i in range(1, 3)]
        loop = ResidentCapLoop(
            _bot(cog=cog),
            _repo(records),
            limit=20,
            resident_fn=lambda: {1, 2},
            in_flight_fn=lambda: {1, 2},
            mem_fn=_mem_seq([LOW, LOW, LOW, LOW, OK]),
        )
        with caplog.at_level(logging.WARNING, logger="c_lord.resident_cap"):
            for _ in range(3):
                await loop.tick()
            assert loop._braking is True
            await loop.tick()
        assert loop._braking is False
        released = [r for r in caplog.records if "EMERGENCY brake released" in r.getMessage()]
        assert len(released) == 1
        assert released[0].levelno == logging.WARNING

    @pytest.mark.asyncio
    async def test_low_samples_while_braking_drop_to_debug(self, caplog) -> None:
        """#742 AC5: 踏んでいる間の「memory low n/3」は DEBUG に落ちる。"""
        loop = ResidentCapLoop(
            _bot(cog=_cog()),
            _repo([_rec(1, hours_ago=1)]),
            limit=20,
            resident_fn=lambda: {1},
            in_flight_fn=lambda: {1},
            mem_fn=lambda: LOW,
        )
        with caplog.at_level(logging.DEBUG, logger="c_lord.resident_cap"):
            for _ in range(6):
                await loop.tick()
        low = [r for r in caplog.records if "memory low" in r.getMessage()]
        assert [r.levelno for r in low[:3]] == [logging.WARNING] * 3
        assert all(r.levelno == logging.DEBUG for r in low[3:])
        assert len(low) == 6

    @pytest.mark.asyncio
    async def test_exactly_ten_percent_is_not_low(self) -> None:
        """「下回ったら」— ちょうど10%は low サンプルに数えない。"""
        total = 100 * GIB
        loop = ResidentCapLoop(
            _bot(cog=_cog()),
            _repo([]),
            limit=0,
            resident_fn=set,
            mem_fn=lambda: (total, int(total * EMERGENCY_LOW_FRACTION)),
        )
        for _ in range(5):
            await loop.tick()
        assert loop._low_streak == 0

    @pytest.mark.asyncio
    async def test_unreadable_memory_keeps_the_brake_off(self) -> None:
        """``memory_snapshot`` docstring: total 0 は「判定できない」でブレーキは止まる。
        ``mem_fn`` 自体が投げても同じ (0, 0) に倒れる。"""

        def _boom():
            raise OSError("no /proc")

        cog = _cog()
        loop = ResidentCapLoop(
            _bot(cog=cog),
            _repo([_rec(1, hours_ago=1)]),
            limit=0,
            resident_fn=lambda: {1},
            in_flight_fn=set,
            mem_fn=_boom,
        )
        for _ in range(5):
            assert await loop.tick() == 0
        assert loop._low_streak == 0
        cog._sleep_workspace_impl.assert_not_awaited()

    @pytest.mark.asyncio
    async def test_the_brake_runs_even_with_the_cap_disabled(self) -> None:
        """``CLORD_MAX_RESIDENT_WORKSPACES=0`` は上限を切るだけ。ホスト全体の逼迫は
        ブレーキの担当（resident-cap.md「数えるのは自分のものだけ」）なので止まらない。"""
        cog = _cog()
        loop = ResidentCapLoop(
            _bot(cog=cog),
            _repo([_rec(1, hours_ago=5), _rec(2, hours_ago=1)]),
            limit=0,
            resident_fn=lambda: {1, 2},
            in_flight_fn=set,
            mem_fn=_mem_seq([LOW, LOW, LOW, LOW, OK]),
        )
        await loop.tick()
        await loop.tick()
        assert await loop.tick() == 1
        assert _slept(cog) == [1]

    @pytest.mark.asyncio
    async def test_the_brake_only_touches_our_own_rows(self) -> None:
        """victim は自分の ``sessions`` 行からしか選ばれない（resident-cap.md
        「数えるのは自分のものだけ」）。他所の窓は逼迫中でも触らない。"""
        cog = _cog()
        loop = ResidentCapLoop(
            _bot(cog=cog),
            _repo([_rec(1, hours_ago=1)]),
            limit=20,
            resident_fn=lambda: {1, 500, 501},
            in_flight_fn=set,
            mem_fn=_mem_seq([LOW, LOW, LOW, LOW, LOW, LOW]),
        )
        await loop.tick()
        await loop.tick()
        assert await loop.tick() == 1
        assert _slept(cog) == [1]


# ── LRU の端 ─────────────────────────────────────────────────────────────


class TestLruEdges:
    def test_a_negative_target_is_treated_as_zero(self) -> None:
        recs = [_rec(1, hours_ago=1), _rec(2, hours_ago=2)]
        assert select_lru_victims(recs, resident={1, 2}, target=-5, in_flight=set()) == [2, 1]

    def test_ties_keep_the_repository_order(self) -> None:
        """同時刻は安定ソートで行の並びのまま（``list.sort`` は安定）。"""
        recs = [_rec(3, hours_ago=1), _rec(1, hours_ago=1), _rec(2, hours_ago=1)]
        assert select_lru_victims(recs, resident={1, 2, 3}, target=1, in_flight=set()) == [3, 1]

    def test_protected_rows_still_count_toward_the_excess(self) -> None:
        """走っているものは選ばれないが、常駐数には入る。超過分は残りから取る。"""
        recs = [_rec(i, hours_ago=i) for i in range(1, 5)]
        got = select_lru_victims(recs, resident={1, 2, 3, 4}, target=2, in_flight={4, 3})
        assert got == [2, 1]


# ── ループの入口と出口 ───────────────────────────────────────────────────


class TestLifecycle:
    """setup.py が ``ResidentCapLoop(bot, session_repo, limit=_cap)`` を作り start する。"""

    @pytest.mark.asyncio
    async def test_start_logs_the_limit_and_is_idempotent(self, caplog) -> None:
        loop = ResidentCapLoop(_bot(cog=_cog()), _repo([]), limit=27, mem_fn=lambda: OK)
        with caplog.at_level(logging.INFO, logger="c_lord.resident_cap"):
            loop.start()
            first = loop._task
            loop.start()
            assert loop._task is first
        try:
            started = [r for r in caplog.records if "Started resident-cap loop" in r.getMessage()]
            assert len(started) == 1
            assert "limit=27, interval=30s, emergency<10% of total" in started[0].getMessage()
        finally:
            await loop.stop()
        assert loop._task is None

    @pytest.mark.asyncio
    async def test_a_disabled_cap_says_disabled(self, caplog) -> None:
        loop = ResidentCapLoop(_bot(cog=_cog()), _repo([]), limit=0, mem_fn=lambda: OK)
        with caplog.at_level(logging.INFO, logger="c_lord.resident_cap"):
            loop.start()
        await loop.stop()
        assert any("limit=disabled" in r.getMessage() for r in caplog.records)

    @pytest.mark.asyncio
    async def test_stop_without_start_is_a_noop(self) -> None:
        loop = ResidentCapLoop(_bot(cog=_cog()), _repo([]), limit=2, mem_fn=lambda: OK)
        await loop.stop()
        assert loop._task is None

    @pytest.mark.asyncio
    async def test_a_raising_tick_does_not_kill_the_loop(self, caplog) -> None:
        loop = ResidentCapLoop(
            _bot(cog=_cog()), _repo([]), limit=2, interval_seconds=0.0, mem_fn=lambda: OK
        )
        calls = 0

        async def _tick() -> int:
            nonlocal calls
            calls += 1
            if calls >= 3:
                raise asyncio.CancelledError
            raise RuntimeError("db locked")

        loop.tick = _tick  # type: ignore[method-assign]
        with (
            caplog.at_level(logging.ERROR, logger="c_lord.resident_cap"),
            pytest.raises(asyncio.CancelledError),
        ):
            await loop._run()
        assert calls == 3
        raised = [r for r in caplog.records if "resident-cap tick raised" in r.getMessage()]
        assert len(raised) == 2

    @pytest.mark.asyncio
    async def test_the_first_tick_waits_for_startup(self) -> None:
        """最初の tick は ``min(90s, interval×3)`` 待つ — 起動直後には走らない。"""
        loop = ResidentCapLoop(
            _bot(cog=_cog()), _repo([]), limit=2, interval_seconds=30.0, mem_fn=lambda: OK
        )
        sleeps: list[float] = []

        async def _sleep(seconds: float) -> None:
            sleeps.append(seconds)
            raise asyncio.CancelledError

        with (
            patch("c_lord.resident_cap.asyncio.sleep", _sleep),
            pytest.raises(asyncio.CancelledError),
        ):
            await loop._run()
        assert sleeps == [90.0]

    def test_the_limit_defaults_to_the_computed_value(self, monkeypatch) -> None:
        monkeypatch.delenv("CLORD_MAX_RESIDENT_WORKSPACES", raising=False)
        monkeypatch.setattr(resident_cap, "memory_total", lambda: 8 * GIB)
        loop = ResidentCapLoop(_bot(cog=_cog()), _repo([]))
        assert loop.limit == 7  # resident-cap.md の表: 8 GiB → 7


# ── メモリの読み取りの端 ─────────────────────────────────────────────────


class TestMemoryReadingEdges:
    """resident-cap.md「cgroup があればそちらが勝つ」: 総量と空きは同じ出所から読む。"""

    def _paths(self, monkeypatch, tmp_path):
        p = {
            "v2max": tmp_path / "v2max",
            "v2cur": tmp_path / "v2cur",
            "v1max": tmp_path / "v1max",
            "v1use": tmp_path / "v1use",
            "meminfo": tmp_path / "meminfo",
        }
        monkeypatch.setattr(resident_cap, "_CGROUP_V2_MAX", p["v2max"])
        monkeypatch.setattr(resident_cap, "_CGROUP_V2_CURRENT", p["v2cur"])
        monkeypatch.setattr(resident_cap, "_CGROUP_V1_MAX", p["v1max"])
        monkeypatch.setattr(resident_cap, "_CGROUP_V1_USAGE", p["v1use"])
        monkeypatch.setattr(resident_cap, "_PROC_MEMINFO", p["meminfo"])
        return p

    def test_v1_usage_is_used_when_v2_current_is_absent(self, monkeypatch, tmp_path) -> None:
        p = self._paths(monkeypatch, tmp_path)
        p["v1max"].write_text(str(2 * GIB))
        p["v1use"].write_text(str(GIB // 2))
        assert resident_cap.memory_available() == 2 * GIB - GIB // 2

    def test_usage_above_the_limit_reads_as_zero_not_negative(self, monkeypatch, tmp_path) -> None:
        p = self._paths(monkeypatch, tmp_path)
        p["v2max"].write_text(str(GIB))
        p["v2cur"].write_text(str(2 * GIB))
        assert resident_cap.memory_available() == 0

    def test_a_cgroup_without_usage_falls_back_to_meminfo(self, monkeypatch, tmp_path) -> None:
        p = self._paths(monkeypatch, tmp_path)
        p["v2max"].write_text(str(2 * GIB))
        p["meminfo"].write_text("MemTotal: 1000 kB\nMemAvailable: 500 kB\n")
        assert resident_cap.memory_available() == 500 * 1024

    def test_snapshot_inside_a_cgroup_pairs_cgroup_numbers(self, monkeypatch, tmp_path) -> None:
        p = self._paths(monkeypatch, tmp_path)  # meminfo 無し（コンテナ内を想定）
        p["v2max"].write_text(str(4 * GIB))
        p["v2cur"].write_text(str(3 * GIB))
        assert resident_cap.memory_snapshot() == (4 * GIB, GIB)

    def test_a_meminfo_without_the_field_reads_zero(self, monkeypatch, tmp_path) -> None:
        p = self._paths(monkeypatch, tmp_path)
        p["meminfo"].write_text("MemFree: 100 kB\n")
        assert resident_cap.memory_available() == 0

    def test_a_garbled_meminfo_reads_zero(self, monkeypatch, tmp_path) -> None:
        p = self._paths(monkeypatch, tmp_path)
        p["meminfo"].write_text("MemAvailable: lots kB\n")
        assert resident_cap.memory_available() == 0

    def test_a_garbled_cgroup_file_is_ignored(self, monkeypatch, tmp_path) -> None:
        p = self._paths(monkeypatch, tmp_path)
        p["v2max"].write_text("12abc\n")
        p["meminfo"].write_text(f"MemTotal: {8 * 1024 * 1024} kB\n")
        assert resident_cap.memory_total() == 8 * GIB

    def test_a_zero_cgroup_limit_is_not_a_limit(self, monkeypatch, tmp_path) -> None:
        p = self._paths(monkeypatch, tmp_path)
        p["v2max"].write_text("0\n")
        p["meminfo"].write_text(f"MemTotal: {8 * 1024 * 1024} kB\n")
        assert resident_cap.memory_total() == 8 * GIB

    def test_compute_limit_on_an_unknown_total_is_the_floor(self) -> None:
        assert resident_cap.compute_limit(0) == resident_cap.MIN_RESIDENT_LIMIT
        assert resident_cap.compute_limit(-1) == resident_cap.MIN_RESIDENT_LIMIT


class TestDarwinGarbage:
    """#899: 空きが読めない Mac ではブレーキを止める（``memory_snapshot`` docstring）。"""

    def test_non_numeric_memorystatus_level_switches_the_brake_off(
        self, monkeypatch, tmp_path
    ) -> None:
        import subprocess

        monkeypatch.setattr(resident_cap, "_PROC_MEMINFO", tmp_path / "none")
        monkeypatch.setattr(resident_cap, "_CGROUP_V2_MAX", tmp_path / "none")
        monkeypatch.setattr(resident_cap, "_CGROUP_V1_MAX", tmp_path / "none")
        monkeypatch.setattr(resident_cap, "_physical_memory", lambda: 16 * GIB)
        monkeypatch.setattr(
            resident_cap.subprocess,
            "run",
            lambda *a, **k: subprocess.CompletedProcess(a, 0, stdout="n/a\n", stderr=""),
        )
        assert resident_cap.memory_snapshot() == (0, 0)
