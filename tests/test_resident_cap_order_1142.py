"""#1142 / #1094 — ブレーキと常駐数の上限が「誰を・何本」眠らせるか。

* #1142: 「アイドルの長い順」を最後の依頼 (``last_used_at``) だけで決めると、依頼の
  あと何時間も自分で作業を続けているスレッド (待ち・起床予定・サブエージェント) から
  先に眠る。2026-10-29 23:31 のブレーキは 17 秒前まで書いていた W13 を眠らせた。
  idle-sleep (#922/#964) と同じ時計 — 最後の依頼と transcript の最後のイベントの
  遅いほう — で並べる
* #1094: 眠らせる途中で窓が自分で消えた候補の分まで補って、上限を下回っていた
"""

from __future__ import annotations

import datetime
from unittest.mock import AsyncMock, patch

import pytest

from c_lord.resident_cap import ResidentCapLoop, select_lru_victims
from tests.test_resident_cap_failure_paths import (
    _NO_PANE_EVIDENCE,
    LOW,
    NOW,
    OK,
    _bot,
    _cog,
    _rec,
    _repo,
    _slept,
)


def _ago(**kw: float) -> datetime.datetime:
    return NOW - datetime.timedelta(**kw)


# ── #1142 ────────────────────────────────────────────────────────────────────


def test_a_thread_still_writing_its_transcript_is_chosen_after_a_quiet_one_1142() -> None:
    """A: 依頼は3時間前だが10秒前まで書いていた / B: 1時間前から静か → B が先。"""
    a, b = _rec(1, hours_ago=3), _rec(2, hours_ago=1)
    activity = {1: _ago(seconds=10), 2: _ago(hours=1)}
    victims = select_lru_victims(
        [a, b], resident={1, 2}, target=1, in_flight=set(), activity=activity
    )
    assert victims == [2]


def test_without_a_transcript_the_request_time_still_orders_1142() -> None:
    """transcript が読めない／無いワークスペースは従来どおり ``last_used_at`` で並ぶ。"""
    recs = [_rec(1, hours_ago=1), _rec(2, hours_ago=3), _rec(3, hours_ago=2)]
    assert select_lru_victims(recs, resident={1, 2, 3}, target=0, in_flight=set()) == [2, 3, 1]
    assert select_lru_victims(recs, resident={1, 2, 3}, target=0, in_flight=set(), activity={}) == [
        2,
        3,
        1,
    ]
    # 古い transcript は依頼の時刻を古くしない (遅いほうを取る)
    assert select_lru_victims(
        recs, resident={1, 2, 3}, target=0, in_flight=set(), activity={1: _ago(hours=9)}
    ) == [2, 3, 1]


def test_running_turns_are_still_never_chosen_1142() -> None:
    recs = [_rec(1, hours_ago=5), _rec(2, hours_ago=1)]
    victims = select_lru_victims(
        recs, resident={1, 2}, target=0, in_flight={1}, activity={2: _ago(seconds=5)}
    )
    assert victims == [2]


@pytest.mark.asyncio
async def test_the_brake_sleeps_the_quiet_workspace_first_1142() -> None:
    """ブレーキの1 tick: transcript を直前まで書いていたスレッドは後回しになる。"""
    cog = _cog()
    written = {1: _ago(seconds=17), 2: _ago(hours=1, minutes=36)}
    loop = ResidentCapLoop(
        _bot(cog=cog),
        _repo([_rec(1, hours_ago=5), _rec(2, hours_ago=2)]),
        limit=20,
        resident_fn=lambda: {1, 2},
        in_flight_fn=set,
        mem_fn=lambda: LOW,
        activity_fn=lambda rec: written.get(rec.thread_id),
    )
    with patch("c_lord.tmux.pane_turn_states", return_value=_NO_PANE_EVIDENCE):
        for _ in range(3):
            await loop.tick()
    assert _slept(cog)[:1] == [2]


@pytest.mark.asyncio
async def test_an_unreadable_transcript_does_not_stop_the_brake_1142() -> None:
    cog = _cog()

    def boom(_rec: object) -> datetime.datetime | None:
        raise OSError("disk")

    loop = ResidentCapLoop(
        _bot(cog=cog),
        _repo([_rec(1, hours_ago=5), _rec(2, hours_ago=2)]),
        limit=20,
        resident_fn=lambda: {1, 2},
        in_flight_fn=set,
        mem_fn=lambda: LOW,
        activity_fn=boom,
    )
    with patch("c_lord.tmux.pane_turn_states", return_value=_NO_PANE_EVIDENCE):
        for _ in range(3):
            await loop.tick()
    assert _slept(cog)[:1] == [1]


# ── #1094 ────────────────────────────────────────────────────────────────────


@pytest.mark.asyncio
async def test_a_window_that_vanished_on_its_own_is_not_made_up_for_1094() -> None:
    """常駐 {1,2,3,4}・上限2。4 は数えた直後に自分で終わっていた → {1,2} が残る。

    眠らせる直前に数え直した超過分だけ眠らせる。
    """
    live = {1, 2, 3, 4}
    listed = {"stale": True}

    async def impl(*, channel: object, reason: object, idle_label: object) -> bool:
        tid = channel.id  # type: ignore[attr-defined]
        if tid == 4:  # claude exited between list-panes and kill
            live.discard(4)
            listed["stale"] = False
            return False
        live.discard(tid)
        return True

    cog = _cog(AsyncMock(side_effect=impl))
    loop = ResidentCapLoop(
        _bot(cog=cog),
        _repo([_rec(i, hours_ago=i) for i in range(1, 5)]),
        limit=2,
        resident_fn=lambda: {1, 2, 3, 4} if listed["stale"] else set(live),
        in_flight_fn=set,
        mem_fn=lambda: OK,
    )
    with patch("c_lord.tmux.pane_turn_states", return_value=_NO_PANE_EVIDENCE):
        await loop.tick()
    assert live == {1, 2}, f"slept below the limit: {sorted(live)} left (limit 2)"


@pytest.mark.asyncio
async def test_a_sleep_that_really_failed_is_still_made_up_for_1094() -> None:
    """窓が残ったまま失敗した (Discord エラー等) 候補は、従来どおり次の候補で補う。"""
    live = {1, 2, 3, 4}

    async def impl(*, channel: object, reason: object, idle_label: object) -> bool:
        tid = channel.id  # type: ignore[attr-defined]
        if tid == 4:
            raise RuntimeError("discord 500")
        live.discard(tid)
        return True

    cog = _cog(AsyncMock(side_effect=impl))
    loop = ResidentCapLoop(
        _bot(cog=cog),
        _repo([_rec(i, hours_ago=i) for i in range(1, 5)]),
        limit=2,
        resident_fn=lambda: set(live),
        in_flight_fn=set,
        mem_fn=lambda: OK,
    )
    with patch("c_lord.tmux.pane_turn_states", return_value=_NO_PANE_EVIDENCE):
        await loop.tick()
    assert live == {1, 4}


@pytest.mark.asyncio
async def test_window_already_gone_is_made_up_and_goes_below_limit() -> None:
    """Issue #1094 の再現試験そのまま: 常駐一覧は古いまま {1,2,3,4} を返し続ける。

    眠らせる処理が「殺す窓が無かった」と答えたら、その分は補わない。
    """
    resident = {1, 2, 3, 4}

    async def impl(*, channel: object, reason: object, idle_label: object) -> bool:
        if channel.id == 4:  # type: ignore[attr-defined]  # claude exited
            return False
        resident.discard(channel.id)  # type: ignore[attr-defined]
        return True

    resident.discard(4)  # it really is gone now
    cog = _cog(AsyncMock(side_effect=impl))
    loop = ResidentCapLoop(
        _bot(cog=cog),
        _repo([_rec(i, hours_ago=i) for i in range(1, 5)]),
        limit=2,
        resident_fn=lambda: {1, 2, 3, 4},
        in_flight_fn=set,
        mem_fn=lambda: OK,
    )
    with patch("c_lord.tmux.pane_turn_states", return_value=_NO_PANE_EVIDENCE):
        await loop.tick()
    assert len(resident) >= 2, f"slept below the limit: {sorted(resident)} left (limit 2)"
