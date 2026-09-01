"""常駐ワークスペース数の上限 — Issue #576。

TTL（#572 スリープ / #574 停止）が主たる制御で、これは**暴走時のバックストップ**。

`MAX_CONCURRENT_SESSIONS` はこの役目を果たしていない。あれは
``async with self._semaphore:`` が**ターンの実行だけ**を包む同時実行数で、ターンが
終われば解放される — tmux の claude は生き続ける。「上限」という名前と実態が
食い違っている典型で、本 Issue はその実態のほうを作る。

**上限だけは ホスト規模に依存する**ので、TTL と違って固定既定値を配れない
(#540 合意)。既定は ``MemTotal`` から算出する::

    N = max(2, floor(MemTotal_GiB × 0.4 / 0.45 GiB))

0.45 GiB は実測の限界コスト（PSS private 255–398MB ＋ 残骸ウィンドウ分）、
0.4 は「c-lord の常駐分がホスト RAM の4割を超えたら c-lord 専用機でない限り
事故」という保守側の線。

超過時は **LRU でスリープ**。**新規は絶対に待たせない** — 実測で yousan は同時に
11本の並行タスクを走らせており、待ちキューを入れると普通にできている並行作業が
塞がれる。上限を主制御にすると「他人の作業を殺す」か「自分の作業が始まらない」の
どちらかが必ず起きる。

緊急ブレーキは**片方向のみ**。余裕があるから上限を上げる、は絶対にやらない。
"""

from __future__ import annotations

import datetime
import logging
from dataclasses import replace
from unittest.mock import AsyncMock, MagicMock

import pytest

from c_lord.database.repository import SessionRecord
from c_lord.resident_cap import (
    MIN_RESIDENT_LIMIT,
    ResidentCapLoop,
    compute_limit,
    max_resident_workspaces,
    select_lru_victims,
)

GIB = 1024**3
NOW = datetime.datetime(2026, 8, 31, 18, 0, 0)


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


class TestZeroConfigLimit:
    """#540: 上限だけはホスト規模に依存するので、固定既定値を配ってはいけない。"""

    @pytest.mark.parametrize(
        ("gib", "expected"),
        [
            (2, 2),  # floor(2×0.4/0.45)=1 → 下限 2。小さいホストでは上限が主制御になる
            (8, 7),
            (16, 14),
            (31.34, 27),  # tachikoma 現在
            (47, 41),  # tachikoma 再起動前
        ],
    )
    def test_the_limit_follows_the_hosts_memory(self, gib: float, expected: int) -> None:
        assert compute_limit(int(gib * GIB)) == expected

    def test_a_tiny_host_still_gets_a_usable_floor(self) -> None:
        """0本にすると c-lord が何もできなくなる。ブレーキであって停止装置ではない。"""
        assert compute_limit(256 * 1024 * 1024) == MIN_RESIDENT_LIMIT == 2

    def test_env_overrides(self, monkeypatch) -> None:
        monkeypatch.setenv("CLORD_MAX_RESIDENT_WORKSPACES", "30")
        assert max_resident_workspaces(total_bytes=31 * GIB) == 30

    def test_unset_falls_back_to_the_computed_value(self, monkeypatch) -> None:
        """**未設定時の既定は自動算出のまま。** 他人の 2GiB VPS を壊さないため。"""
        monkeypatch.delenv("CLORD_MAX_RESIDENT_WORKSPACES", raising=False)
        assert max_resident_workspaces(total_bytes=31 * GIB) == 27

    def test_garbage_falls_back_to_the_computed_value(self, monkeypatch) -> None:
        monkeypatch.setenv("CLORD_MAX_RESIDENT_WORKSPACES", "thirty")
        assert max_resident_workspaces(total_bytes=31 * GIB) == 27

    def test_negative_falls_back_to_the_computed_value(self, monkeypatch) -> None:
        monkeypatch.setenv("CLORD_MAX_RESIDENT_WORKSPACES", "-5")
        assert max_resident_workspaces(total_bytes=31 * GIB) == 27

    def test_zero_disables_the_cap(self, monkeypatch) -> None:
        monkeypatch.setenv("CLORD_MAX_RESIDENT_WORKSPACES", "0")
        assert max_resident_workspaces(total_bytes=31 * GIB) == 0


class TestCgroupWins:
    """#576 AC2: コンテナで動かすと ``MemTotal`` はホスト全体を指してしまう。

    2 GiB を割り当てたコンテナの中で 47 GiB のホストの数字を読んだら、上限は
    41 になり、**制限のあるほうの環境で確実に OOM する**。
    """

    def test_a_cgroup_limit_is_preferred_over_meminfo(self, tmp_path, monkeypatch) -> None:
        from c_lord import resident_cap

        (tmp_path / "memory.max").write_text("2147483648\n")  # 2 GiB
        monkeypatch.setattr(resident_cap, "_CGROUP_V2_MAX", tmp_path / "memory.max")
        monkeypatch.setattr(resident_cap, "_CGROUP_V1_MAX", tmp_path / "nonexistent")
        monkeypatch.setattr(resident_cap, "_read_meminfo_total", lambda: 47 * GIB)

        assert resident_cap.memory_total() == 2 * GIB

    def test_an_unlimited_cgroup_falls_back_to_meminfo(self, tmp_path, monkeypatch) -> None:
        from c_lord import resident_cap

        (tmp_path / "memory.max").write_text("max\n")
        monkeypatch.setattr(resident_cap, "_CGROUP_V2_MAX", tmp_path / "memory.max")
        monkeypatch.setattr(resident_cap, "_CGROUP_V1_MAX", tmp_path / "nonexistent")
        monkeypatch.setattr(resident_cap, "_read_meminfo_total", lambda: 47 * GIB)

        assert resident_cap.memory_total() == 47 * GIB

    def test_the_v1_sentinel_is_not_mistaken_for_a_real_limit(self, tmp_path, monkeypatch) -> None:
        """cgroup v1 は「無制限」を巨大な数で表す。素直に読むと上限が天文学的になる。"""
        from c_lord import resident_cap

        (tmp_path / "limit").write_text("9223372036854771712\n")
        monkeypatch.setattr(resident_cap, "_CGROUP_V2_MAX", tmp_path / "nonexistent")
        monkeypatch.setattr(resident_cap, "_CGROUP_V1_MAX", tmp_path / "limit")
        monkeypatch.setattr(resident_cap, "_read_meminfo_total", lambda: 47 * GIB)

        assert resident_cap.memory_total() == 47 * GIB

    def test_an_unreadable_cgroup_does_not_crash(self, tmp_path, monkeypatch) -> None:
        from c_lord import resident_cap

        (tmp_path / "memory.max").write_text("not-a-number\n")
        monkeypatch.setattr(resident_cap, "_CGROUP_V2_MAX", tmp_path / "memory.max")
        monkeypatch.setattr(resident_cap, "_CGROUP_V1_MAX", tmp_path / "nonexistent")
        monkeypatch.setattr(resident_cap, "_read_meminfo_total", lambda: 47 * GIB)

        assert resident_cap.memory_total() == 47 * GIB


class TestLruSelection:
    def test_the_longest_idle_go_first(self) -> None:
        records = [_rec(1, hours_ago=1), _rec(2, hours_ago=9), _rec(3, hours_ago=5)]
        victims = select_lru_victims(records, resident={1, 2, 3}, target=1, in_flight=set())
        assert victims == [2, 3]

    def test_exactly_enough_are_chosen(self) -> None:
        """上限まで下げたら止める。余分に眠らせない。"""
        records = [_rec(i, hours_ago=i) for i in range(1, 6)]
        victims = select_lru_victims(records, resident=set(range(1, 6)), target=4, in_flight=set())
        assert victims == [5]

    def test_nothing_is_chosen_under_the_limit(self) -> None:
        records = [_rec(1, hours_ago=9), _rec(2, hours_ago=8)]
        assert select_lru_victims(records, resident={1, 2}, target=5, in_flight=set()) == []

    def test_a_workspace_with_a_running_turn_is_never_chosen(self) -> None:
        """#576 AC5. 上限のために動いている作業を殺すのは、上限の意味を裏切る。"""
        records = [_rec(1, hours_ago=99), _rec(2, hours_ago=3)]
        victims = select_lru_victims(records, resident={1, 2}, target=1, in_flight={1})
        assert victims == [2]

    def test_only_resident_workspaces_are_chosen(self) -> None:
        """既に眠っているものを「眠らせた」ことにしない（数が合わなくなる）。"""
        records = [_rec(1, hours_ago=9), _rec(2, hours_ago=8)]
        assert select_lru_victims(records, resident={2}, target=0, in_flight=set()) == [2]

    def test_a_resident_without_a_row_is_never_chosen(self) -> None:
        """行の無いウィンドウは選べない（アイドル時間が測れない）。

        残骸は #613 の掃除と startup の tmux リーパーの担当。
        """
        records = [_rec(1, hours_ago=9)]
        victims = select_lru_victims(records, resident={1, 999}, target=1, in_flight=set())
        assert victims == [1]

    def test_an_unparsable_timestamp_is_skipped(self) -> None:
        records = [replace(_rec(1, hours_ago=9), last_used_at="nope"), _rec(2, hours_ago=8)]
        assert select_lru_victims(records, resident={1, 2}, target=1, in_flight=set()) == [2]


class TestOnlyOurOwnWorkspacesAreCounted:
    """1台の tmux サーバを、そのホストの c-lord 全部と人間の手作業が共有している。

    ホスト全体で数えると、**このボットが決して眠らせられない他人のワークスペース**
    （victim は自分の ``sessions`` 行からしか選ばれない）が、自分の利用者を
    押し出すことになる。ホスト全体の逼迫は緊急ブレーキ（``MemAvailable`` を見る）の
    担当で、上限は自分が責任を持てる範囲だけを見る。
    """

    @pytest.mark.asyncio
    async def test_another_bots_residents_do_not_push_ours_out(self) -> None:
        ours = [_rec(i, hours_ago=i) for i in range(1, 4)]
        # 自分は3本。同じ tmux サーバに他所の 20 本が居る。
        foreign = set(range(100, 120))
        loop = _loop(ours, resident={1, 2, 3} | foreign, limit=5)

        assert await loop.tick() == 0
        loop._cog_for_test._sleep_workspace_impl.assert_not_awaited()

    @pytest.mark.asyncio
    async def test_our_own_excess_is_still_enforced(self) -> None:
        ours = [_rec(i, hours_ago=i) for i in range(1, 8)]
        loop = _loop(ours, resident=set(range(1, 8)) | {100, 101}, limit=5)

        assert await loop.tick() == 2


class TestNewWorkspacesAreNeverBlocked:
    """#576 AC4 — **待たせるのは却下**（Issue 本文）。

    実測で yousan は同時に11本の並行タスクを走らせている。待ちキューを入れると、
    いま普通にできている並行作業が塞がれる。上限を主制御にすると「他人の作業を
    殺す」か「自分の作業が始まらない」のどちらかが必ず起きる。
    """

    def test_the_turn_path_does_not_even_know_about_the_cap(self) -> None:
        """構造で担保する — ターンの経路が上限を参照していなければ、待つコードは
        書きようがない。「待たせない」を実装の善意ではなく形で固定する。"""
        import ast
        from pathlib import Path

        pkg = Path(__file__).resolve().parent.parent / "c_lord"
        forbidden = {"ResidentCapLoop", "max_resident_workspaces", "select_lru_victims"}
        for rel in ("cogs/claude_chat.py", "cogs/_run_helper.py", "tmux.py", "session_dir.py"):
            tree = ast.parse((pkg / rel).read_text(encoding="utf-8"))
            names = {n.id for n in ast.walk(tree) if isinstance(n, ast.Name)}
            names |= {n.attr for n in ast.walk(tree) if isinstance(n, ast.Attribute)}
            names |= {a.name.rsplit(".", 1)[-1] for a in ast.walk(tree) if isinstance(a, ast.alias)}
            assert not (names & forbidden), (
                f"c_lord/{rel} references the resident cap. 新規ワークスペースの作成"
                f"経路が上限を見た時点で「待たせない」は破れる (#576)。"
            )

    @pytest.mark.asyncio
    async def test_a_wildly_over_capacity_host_still_only_sleeps_idle_ones(self) -> None:
        records = [_rec(i, hours_ago=i) for i in range(1, 31)]
        # 全部が走行中 = 眠らせられるものが1つも無い状況でも、tick は素通りする。
        loop = _loop(records, resident=set(range(1, 31)), limit=5, in_flight=set(range(1, 31)))
        assert await loop.tick() == 0


def _loop(
    records,
    *,
    resident: set[int],
    limit: int,
    in_flight: set[int] | None = None,
    mem: list[tuple[int, int]] | None = None,
    sleep_impl: AsyncMock | None = None,
) -> ResidentCapLoop:
    cog = MagicMock()
    cog._sleep_workspace_impl = sleep_impl or AsyncMock(return_value=True)

    bot = MagicMock()
    bot.get_cog = MagicMock(return_value=cog)
    bot.get_channel = MagicMock(side_effect=lambda t: MagicMock(id=t))

    repo = MagicMock()
    repo.list_all = AsyncMock(return_value=records)

    samples = list(mem or [(31 * GIB, 20 * GIB)])

    def _mem():
        return samples[0] if len(samples) == 1 else samples.pop(0)

    loop = ResidentCapLoop(
        bot,
        repo,
        limit=limit,
        resident_fn=lambda: set(resident),
        in_flight_fn=lambda: set(in_flight or set()),
        mem_fn=_mem,
    )
    loop._cog_for_test = cog  # type: ignore[attr-defined]
    return loop


class TestCapEnforcement:
    @pytest.mark.asyncio
    async def test_over_the_limit_sleeps_down_to_it(self) -> None:
        records = [_rec(i, hours_ago=i) for i in range(1, 8)]
        loop = _loop(records, resident=set(range(1, 8)), limit=5)

        assert await loop.tick() == 2
        ids = [c.kwargs["channel"].id for c in loop._cog_for_test._sleep_workspace_impl.await_args_list]
        assert ids == [7, 6]  # 最も長くアイドルな順

    @pytest.mark.asyncio
    async def test_at_the_limit_nothing_happens(self) -> None:
        records = [_rec(i, hours_ago=i) for i in range(1, 6)]
        loop = _loop(records, resident=set(range(1, 6)), limit=5)

        assert await loop.tick() == 0
        loop._cog_for_test._sleep_workspace_impl.assert_not_awaited()

    @pytest.mark.asyncio
    async def test_a_disabled_cap_never_sleeps_anything(self) -> None:
        records = [_rec(i, hours_ago=i) for i in range(1, 40)]
        loop = _loop(records, resident=set(range(1, 40)), limit=0)

        assert await loop.tick() == 0
        loop._cog_for_test._sleep_workspace_impl.assert_not_awaited()

    @pytest.mark.asyncio
    async def test_the_reason_is_the_cap_not_idleness(self) -> None:
        """上限で眠らせるものは4時間経っているとは限らない。

        10分前に使ったワークスペースを「4時間 操作が無かったため」と説明したら
        嘘になる。通知は理由ごとに1行だけ変わる仕組みなので、そこを正しく使う。
        """
        from c_lord.workspace_notice import WorkspaceReason

        records = [_rec(i, hours_ago=0.1 * i) for i in range(1, 8)]
        loop = _loop(records, resident=set(range(1, 8)), limit=5)
        await loop.tick()

        kwargs = loop._cog_for_test._sleep_workspace_impl.await_args.kwargs
        assert kwargs["reason"] is WorkspaceReason.CAP
        assert kwargs["idle_label"] == "5本"


class TestEmergencyBrake:
    """スパイクだけ潰す。常時フィードバックは却下（非決定的でテストできない）。"""

    def _low(self) -> tuple[int, int]:
        return (31 * GIB, int(31 * GIB * 0.05))  # 5% — 閾値 10% を下回る

    def _ok(self) -> tuple[int, int]:
        return (31 * GIB, int(31 * GIB * 0.50))

    @pytest.mark.asyncio
    async def test_one_low_sample_does_not_fire(self) -> None:
        records = [_rec(i, hours_ago=i) for i in range(1, 4)]
        loop = _loop(records, resident={1, 2, 3}, limit=10, mem=[self._low(), self._ok()])

        assert await loop.tick() == 0
        loop._cog_for_test._sleep_workspace_impl.assert_not_awaited()

    @pytest.mark.asyncio
    async def test_three_consecutive_low_samples_fire(self) -> None:
        records = [_rec(i, hours_ago=i) for i in range(1, 4)]
        loop = _loop(
            records,
            resident={1, 2, 3},
            limit=10,
            mem=[self._low(), self._low(), self._low(), self._ok()],
        )

        assert await loop.tick() == 0
        assert await loop.tick() == 0
        assert await loop.tick() > 0

    @pytest.mark.asyncio
    async def test_a_recovery_resets_the_streak(self) -> None:
        """間欠的なスパイクでブレーキを踏まない（PSI avg10 は 0.00 = 常時逼迫ではない）。"""
        records = [_rec(i, hours_ago=i) for i in range(1, 4)]
        loop = _loop(
            records,
            resident={1, 2, 3},
            limit=10,
            mem=[self._low(), self._low(), self._ok(), self._low(), self._ok()],
        )

        await loop.tick()
        await loop.tick()
        await loop.tick()  # 回復 → streak リセット
        assert await loop.tick() == 0  # 低いのは1回目に戻っている
        loop._cog_for_test._sleep_workspace_impl.assert_not_awaited()

    @pytest.mark.asyncio
    async def test_it_stops_as_soon_as_memory_recovers(self) -> None:
        """回復したら止める。全部眠らせない。"""
        records = [_rec(i, hours_ago=i) for i in range(1, 11)]
        mem = [self._low(), self._low(), self._low(), self._ok()]
        loop = _loop(records, resident=set(range(1, 11)), limit=20, mem=mem)

        await loop.tick()
        await loop.tick()
        slept = await loop.tick()

        assert slept == 1, "1本眠らせて回復したのに、まだ眠らせ続けている"

    @pytest.mark.asyncio
    async def test_a_running_turn_survives_the_brake(self) -> None:
        records = [_rec(1, hours_ago=99), _rec(2, hours_ago=1)]
        loop = _loop(
            records,
            resident={1, 2},
            limit=20,
            in_flight={1},
            mem=[self._low(), self._low(), self._low(), self._ok()],
        )
        await loop.tick()
        await loop.tick()
        await loop.tick()

        ids = [c.kwargs["channel"].id for c in loop._cog_for_test._sleep_workspace_impl.await_args_list]
        assert 1 not in ids

    @pytest.mark.asyncio
    async def test_the_brake_says_it_was_memory_pressure(self) -> None:
        """「操作が無かったため」でも「上限のため」でもない。起きたことを書く。"""
        from c_lord.workspace_notice import WorkspaceReason

        records = [_rec(i, hours_ago=i) for i in range(1, 4)]
        loop = _loop(
            records,
            resident={1, 2, 3},
            limit=10,
            mem=[self._low(), self._low(), self._low(), self._ok()],
        )
        await loop.tick()
        await loop.tick()
        await loop.tick()

        kwargs = loop._cog_for_test._sleep_workspace_impl.await_args.kwargs
        assert kwargs["reason"] is WorkspaceReason.PRESSURE

    @pytest.mark.asyncio
    async def test_the_brake_is_logged_loudly_with_context(self, caplog) -> None:
        records = [_rec(i, hours_ago=i) for i in range(1, 4)]
        loop = _loop(
            records,
            resident={1, 2, 3},
            limit=10,
            mem=[self._low(), self._low(), self._low(), self._ok()],
        )
        with caplog.at_level(logging.WARNING):
            await loop.tick()
            await loop.tick()
            await loop.tick()

        fired = [r for r in caplog.records if "emergency" in r.getMessage().lower()]
        assert fired, "緊急ブレーキが WARNING 以上で記録されていない"
        assert any("thread=" in r.getMessage() for r in caplog.records)

    @pytest.mark.asyncio
    async def test_the_brake_never_raises_the_limit(self) -> None:
        """#576 AC7 — **片方向のみ**。上げる方向は事故を増やすだけで、
        利用者から見た挙動が予測不能になる。"""
        records = [_rec(i, hours_ago=i) for i in range(1, 4)]
        loop = _loop(
            records,
            resident={1, 2, 3},
            limit=10,
            mem=[self._low(), self._low(), self._low()] + [self._ok()] * 20,
        )
        for _ in range(10):
            await loop.tick()

        assert loop.limit == 10

    def test_the_limit_has_no_setter_at_all(self) -> None:
        """「上げない」を実装の善意ではなく形で固定する。

        書ける属性にした瞬間、いつか誰かが「余裕があるから広げよう」と書ける。
        読み取り専用なら、上げる方向のコードは**書けない**。
        """
        loop = _loop([], resident=set(), limit=7)
        assert loop.limit == 7
        with pytest.raises(AttributeError):
            loop.limit = 99  # type: ignore[misc]


class TestTheNoticeTellsTheTruthAboutWhy:
    """通知は「棚卸し」で、``reason`` が変えてよいのは説明の1行だけ（#571）。

    上限で眠らせたものを「4時間 操作が無かったため」と説明したら嘘になる。
    嘘をつく通知は、通知が無いより悪い。
    """

    def _embed(self, reason, label=None):
        from c_lord.workspace_notice import WorkspaceAction, workspace_notice_embed

        return workspace_notice_embed(WorkspaceAction.SLEEP, reason=reason, idle_label=label)

    def test_a_capped_eviction_says_it_hit_the_limit(self) -> None:
        from c_lord.workspace_notice import WorkspaceReason

        e = self._embed(WorkspaceReason.CAP, "30本")
        assert "上限" in e.description
        assert "30本" in e.description
        assert "操作が無かった" not in e.description

    def test_the_emergency_brake_says_it_was_memory(self) -> None:
        from c_lord.workspace_notice import WorkspaceReason

        e = self._embed(WorkspaceReason.PRESSURE)
        assert "メモリ" in e.description
        assert "操作が無かった" not in e.description

    def test_every_reason_keeps_the_same_inventory(self) -> None:
        """説明の1行以外は完全に同一。ズレる余地を作らない。"""
        from c_lord.workspace_notice import WorkspaceReason

        fields = [
            tuple((f.name, f.value) for f in self._embed(r, "30本").fields)
            for r in WorkspaceReason
        ]
        assert len(set(fields)) == 1


class TestAnOlderBotSurvivesANewerDatabase:
    """マイグレーションは前にしか進まないので、**DB はコードと同じか、先を行く**。

    ``SessionRecord(**dict(row))`` は知らない列で ``TypeError`` になるため、列を
    足したブランチを検証した staging clone を ``main`` に戻した瞬間、**すべての
    読み取りが落ちる**（2026-08-31 に ``slept_at`` で実際に踏んだ）。本番の
    ロールバックでも同じことが起きる。

    知らない列は落として読む = 「その列が存在しなかった頃」と同じ挙動に戻る。
    """

    @pytest.mark.asyncio
    async def test_a_column_the_dataclass_does_not_know_is_ignored(self, tmp_path) -> None:
        import aiosqlite

        from c_lord.database.models import init_db
        from c_lord.database.repository import SessionRepository

        db_path = str(tmp_path / "sessions.db")
        await init_db(db_path)
        repo = SessionRepository(db_path)
        await repo.save(555, "sess-abc", working_dir="/w")

        # 未来のリリースが足した列。いまのコードは知らない。
        async with aiosqlite.connect(db_path) as db:
            await db.execute("ALTER TABLE sessions ADD COLUMN a_column_from_the_future TEXT")
            await db.execute("UPDATE sessions SET a_column_from_the_future = 'x'")
            await db.commit()

        record = await repo.get(555)
        assert record is not None and record.thread_id == 555
        assert (await repo.list_all())[0].thread_id == 555
