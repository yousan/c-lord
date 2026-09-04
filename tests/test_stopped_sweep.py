"""停止済みなのに開いたままのスレッドを畳むスイープ — Issue #685.

RED を先に置く: 本番で 186 本が「名前は ``[停止]`` なのに ``archived=False``」の
まま残っていた。ここで守るのは3つ — **拾えること**(AC1/AC2)、**一度に投げすぎ
ないこと**(AC3)、そして何より **現役スレッドを巻き込まないこと**(AC4)。
"""

from __future__ import annotations

import datetime

import pytest

from c_lord.stopped_sweep import (
    MAX_ARCHIVES_PER_TICK,
    StoppedThreadSweepLoop,
    is_stopped_name,
    select_unarchived_stopped,
)

UTC = datetime.timezone.utc  # noqa: UP017 — datetime.UTC is 3.11+, we support 3.10
NOW = datetime.datetime(2026, 9, 4, 12, 0, tzinfo=UTC)

# Discord のエポック (2015-01-01) — snowflake を作るために使う。
_DISCORD_EPOCH_MS = 1420070400000


def _snowflake(at: datetime.datetime) -> int:
    """*at* に作られたメッセージ ID。last_message_id を組み立てるため。"""
    return (int(at.timestamp() * 1000) - _DISCORD_EPOCH_MS) << 22


class FakeThread:
    """``discord.Thread`` のうち、このスイープが触る面だけを持つ替え玉。"""

    def __init__(
        self,
        thread_id: int,
        name: str,
        *,
        archived: bool = False,
        last_message_at: datetime.datetime | None = None,
    ) -> None:
        self.id = thread_id
        self.name = name
        self.archived = archived
        self.last_message_id = _snowflake(last_message_at) if last_message_at else None
        self.edits: list[dict] = []
        self.sent: list[object] = []

    async def edit(self, **kwargs):
        self.edits.append(kwargs)
        if "archived" in kwargs:
            self.archived = bool(kwargs["archived"])
        return self

    async def send(self, *args, **kwargs):  # pragma: no cover - must never be called
        self.sent.append((args, kwargs))


class FakeGuild:
    def __init__(self, threads: list[FakeThread]) -> None:
        self._threads = threads
        self.id = 1
        self.name = "guild"

    async def active_threads(self) -> list[FakeThread]:
        return list(self._threads)


class FakeBot:
    def __init__(self, guilds: list[FakeGuild]) -> None:
        self.guilds = guilds


def _old(hours: float = 240.0) -> datetime.datetime:
    return NOW - datetime.timedelta(hours=hours)


# ── 名前で判定する (AC4 の土台) ────────────────────────────────────────


@pytest.mark.parametrize(
    "name,expected",
    [
        ("[停止] #587 特商法4項目をコマンド実装", True),
        ("[停止] W28 │ #587 特商法4項目", True),
        ("[終了] 最近のアクセス", True),  # 旧マーカーも拾う
        ("🟢 W3 │ #404 認証リファクタ", False),
        ("W131 │ #540 メモリ →#588", False),
        ("停止の設計を相談する", False),  # マーカーではなく本文
        ("これは [停止] ではない", False),  # 先頭でなければ対象外
        ("", False),
    ],
)
def test_is_stopped_name(name: str, expected: bool) -> None:
    assert is_stopped_name(name) is expected


def test_is_stopped_name_survives_a_non_string() -> None:
    """Discord から欠けた値が来ても落ちない（落ちたらスイープごと止まる）。"""
    assert is_stopped_name(None) is False  # type: ignore[arg-type]


# ── 選び方 ────────────────────────────────────────────────────────────


def test_select_picks_only_unarchived_stopped_threads() -> None:
    stale = FakeThread(1, "[停止] 古い作業", last_message_at=_old())
    already = FakeThread(2, "[停止] もう畳んだ", archived=True, last_message_at=_old())
    live = FakeThread(3, "🟢 W3 │ 現役", last_message_at=_old())

    picked = select_unarchived_stopped([stale, already, live], now=NOW)

    assert [t.id for t in picked] == [1]


def test_select_never_touches_a_live_thread() -> None:
    """AC4: `[停止]` の付いていないスレッドは、どれだけ放置されていても対象外。"""
    live = [
        FakeThread(10, "🟢 W3 │ 認証リファクタ", last_message_at=_old(24 * 90)),
        FakeThread(11, "W131 │ #540 メモリ →#588", last_message_at=_old(24 * 90)),
        FakeThread(12, "⚪ 終わったプロジェクト", last_message_at=_old(24 * 90)),
    ]
    assert select_unarchived_stopped(live, now=NOW) == []


def test_select_leaves_a_thread_someone_just_posted_in() -> None:
    """直前に投稿があったスレッドは次のティックに回す。

    停止済みスレッドに投稿すると Discord が勝手に開き、bot が「▶️ 再開する」を
    出す。その真上でアーカイブし返すと、利用者は再開ボタンを取り上げられる。
    ミラーがまだ喋っていた場合(#379)に開閉を撃ち合うのも同じ理由で防げる。
    """
    just_now = NOW - datetime.timedelta(minutes=5)
    fresh = FakeThread(1, "[停止] さっき触った", last_message_at=just_now)
    settled = FakeThread(2, "[停止] 放置", last_message_at=_old())

    picked = select_unarchived_stopped([fresh, settled], now=NOW)

    assert [t.id for t in picked] == [2]


def test_select_handles_a_thread_with_no_messages() -> None:
    """``last_message_id`` が無いスレッドは「静か」とみなして拾う。"""
    empty = FakeThread(1, "[停止] 発言なし", last_message_at=None)
    assert [t.id for t in select_unarchived_stopped([empty], now=NOW)] == [1]


def test_select_orders_oldest_first() -> None:
    newer = FakeThread(1, "[停止] 新しめ", last_message_at=_old(50))
    older = FakeThread(2, "[停止] 古い", last_message_at=_old(500))
    assert [t.id for t in select_unarchived_stopped([newer, older], now=NOW)] == [2, 1]


# ── 1ティックの上限 (AC3) ─────────────────────────────────────────────


@pytest.mark.asyncio
async def test_tick_caps_how_many_it_archives_per_pass() -> None:
    """AC3: 186 本を一度に PATCH せず、残りは次のティックに回す。"""
    threads = [FakeThread(100 + i, f"[停止] 作業{i}", last_message_at=_old()) for i in range(60)]
    loop = StoppedThreadSweepLoop(
        FakeBot([FakeGuild(threads)]), now_fn=lambda: NOW, max_per_tick=25
    )

    archived = await loop.tick()

    assert archived == 25
    assert sum(1 for t in threads if t.edits) == 25


@pytest.mark.asyncio
async def test_the_backlog_drains_over_successive_ticks() -> None:
    threads = [FakeThread(100 + i, f"[停止] 作業{i}", last_message_at=_old()) for i in range(60)]
    loop = StoppedThreadSweepLoop(
        FakeBot([FakeGuild(threads)]), now_fn=lambda: NOW, max_per_tick=25
    )

    total = 0
    for _ in range(3):
        total += await loop.tick()

    assert total == 60
    assert all(t.archived for t in threads)
    assert await loop.tick() == 0  # 片付いたら空振りする


@pytest.mark.asyncio
async def test_default_cap_is_bounded() -> None:
    """既定の上限が「実質無制限」になっていないこと (AC3)。"""
    assert 0 < MAX_ARCHIVES_PER_TICK <= 50


# ── 何をするか / しないか ─────────────────────────────────────────────


@pytest.mark.asyncio
async def test_tick_archives_without_renaming_or_posting() -> None:
    """AC5: 何も投稿しない。名前はすでに `[停止]` なので触らない。

    投稿すれば Discord がスレッドを開き直す（#379 と同じ形）。改名は Discord の
    「10 分に約2回」の枠を、畳むためだけに使い潰す。
    """
    stale = FakeThread(1, "[停止] 古い作業", last_message_at=_old())
    loop = StoppedThreadSweepLoop(FakeBot([FakeGuild([stale])]), now_fn=lambda: NOW)

    assert await loop.tick() == 1
    assert stale.edits == [{"archived": True}]
    assert stale.sent == []


@pytest.mark.asyncio
async def test_tick_leaves_live_threads_alone() -> None:
    """AC4 をループの高さでも留める。"""
    live = FakeThread(1, "🟢 W3 │ 認証リファクタ", last_message_at=_old())
    stale = FakeThread(2, "[停止] 古い作業", last_message_at=_old())
    loop = StoppedThreadSweepLoop(FakeBot([FakeGuild([live, stale])]), now_fn=lambda: NOW)

    await loop.tick()

    assert live.edits == []
    assert live.archived is False
    assert stale.archived is True


@pytest.mark.asyncio
async def test_a_thread_the_bot_may_not_edit_is_not_retried_forever() -> None:
    """403 を毎ティック投げ続けない（1本の権限漏れで枠を食い潰さない）。"""
    import discord

    class Forbidden(FakeThread):
        def __init__(self, *args, **kwargs):
            super().__init__(*args, **kwargs)
            self.attempts = 0

        async def edit(self, **kwargs):
            self.attempts += 1
            raise discord.Forbidden(_FakeResponse(403), "missing Manage Threads")

    blocked = Forbidden(1, "[停止] 権限が無い", last_message_at=_old())
    loop = StoppedThreadSweepLoop(FakeBot([FakeGuild([blocked])]), now_fn=lambda: NOW)

    assert await loop.tick() == 0
    assert await loop.tick() == 0
    assert blocked.attempts == 1


@pytest.mark.asyncio
async def test_one_unreachable_guild_does_not_stop_the_others() -> None:
    class BrokenGuild(FakeGuild):
        async def active_threads(self):
            raise RuntimeError("boom")

    stale = FakeThread(1, "[停止] 古い作業", last_message_at=_old())
    loop = StoppedThreadSweepLoop(
        FakeBot([BrokenGuild([]), FakeGuild([stale])]), now_fn=lambda: NOW
    )

    assert await loop.tick() == 1
    assert stale.archived is True


class _FakeResponse:
    """``discord.HTTPException`` が読む最小限のレスポンス。"""

    def __init__(self, status: int) -> None:
        self.status = status
        self.reason = "Forbidden"


# ── 行が「まだ生きている」と言うスレッドは触らない ───────────────────


class FakeRepo:
    def __init__(self, open_ids: set[int]) -> None:
        self._open = open_ids

    async def open_thread_ids(self) -> set[int]:
        return set(self._open)


@pytest.mark.asyncio
async def test_a_live_workspace_wearing_a_stale_stopped_name_is_left_alone() -> None:
    """再開時の改名が枠切れで落ちると、生きたまま `[停止]` を名乗るスレッドが残る。

    ``session_close._build_and_apply`` の改名リトライはアーカイブだけを再適用
    するので、名前だけを見て畳むと**作業中のスレッドを畳んでしまう** (AC4)。
    状態の正本は行なので、行が「停止していない」と言うなら名前より行を採る。
    """
    live = FakeThread(1, "[停止] 改名が落ちた現役", last_message_at=_old())
    really_stopped = FakeThread(2, "[停止] 本当に停止", last_message_at=_old())
    loop = StoppedThreadSweepLoop(
        FakeBot([FakeGuild([live, really_stopped])]),
        session_repo=FakeRepo({1}),
        now_fn=lambda: NOW,
    )

    assert await loop.tick() == 1
    assert live.edits == []
    assert really_stopped.archived is True


@pytest.mark.asyncio
async def test_a_thread_with_no_row_is_still_archived_by_name() -> None:
    """行を失った 7 本を取り残さない — DB 主導だと永久に届かない。"""
    rowless = FakeThread(42, "[停止] 行が無い", last_message_at=_old())
    loop = StoppedThreadSweepLoop(
        FakeBot([FakeGuild([rowless])]), session_repo=FakeRepo(set()), now_fn=lambda: NOW
    )

    assert await loop.tick() == 1
    assert rowless.archived is True


@pytest.mark.asyncio
async def test_an_unreadable_db_does_not_archive_anything() -> None:
    """行が読めないときは畳まない。

    全スレッドが「行が無い」に見える状態で name-only に落ちると、現役を巻き込む
    方向に倒れる。掃除は急がないので、読めないなら次のティックに回す。
    """
    stale = FakeThread(1, "[停止] 古い作業", last_message_at=_old())

    class BrokenRepo:
        async def open_thread_ids(self):
            raise RuntimeError("db is locked")

    loop = StoppedThreadSweepLoop(
        FakeBot([FakeGuild([stale])]), session_repo=BrokenRepo(), now_fn=lambda: NOW
    )

    assert await loop.tick() == 0
    assert stale.edits == []
