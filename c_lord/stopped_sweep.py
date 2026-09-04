"""停止済みなのに開いたままのスレッドを畳む — Issue #685.

**なぜ要るのか。** アーカイブは :func:`c_lord.session_close.apply_closed_name`
が「停止したその瞬間」に1回だけ投げる。#609 でその1回は直ったが、**直る前に
停止したスレッドを拾い直す経路はどこにも無い**。自動停止のスイープ
(:mod:`c_lord.idle_stop` / :mod:`c_lord.idle_sweep`) は「まだ動いている
ワークスペース」を探すので、``closed_at`` の入った行は最初から候補に入らない。

結果、本番の実測 (2026-09-04) はこうなっていた::

    bot から見えるアクティブ（未アーカイブ）スレッド: 322
      うち名前が [停止] / [終了] で始まるもの:        186  ← 58%

いちばん古いものは 2026-06-29 に停止して以来、``auto_archive_duration`` が
3日なのに 2ヶ月開いたままだった。Discord の自動アーカイブは当てにできない。
**サイドバーの過半が終わったスレッド**という状態は、#271 / #609 が片付けよう
とした当のものなので、取り残しを拾う経路をここに作る。

**なぜ名前で拾い、行で止めるのか。** 拾う条件は Discord のスレッド名の先頭
マーカー (``[停止]`` / 旧 ``[終了]``) ただ1つ。本番の 186 本を DB と突き合わせる
と **179 本は行があり全て ``closed_at`` 入り、7 本は行が無い** — 行を拾う条件に
すると、行を失った 7 本が永久に取り残される。マーカーは c-lord 自身しか書かない
ので、名前を見れば停止済みだと分かる。

ただし名前は飾りで、状態の正本は行のほう。再開時の PATCH は改名とアーカイブ解除
を同時に投げるが、改名枠（10 分に約2回）が切れていると
:func:`c_lord.session_close._build_and_apply` のリトライがアーカイブ解除だけを
再適用する — つまり **生きているのに ``[停止]`` を名乗るスレッド**があり得る。
名前だけで畳むと作業中のスレッドを畳んでしまうので、行が「停止していない」と
言うものは除く。行が読めないティックは何もしない（全部が「行が無い」に見える
状態で name-only に落ちるのは、いちばん巻き込む方向）。

**なぜ投稿しないのか。** Discord はアーカイブ済みスレッドに投稿されると自動で
開く (#379, #609)。畳むことだけが目的のこのスイープが一言でも喋れば、自分で
閉じたものを自分で開けることになる。改名もしない — 名前はすでに ``[停止]``
だし、Discord の改名枠（1スレッド 10 分に約2回）を畳むためだけに使うのは無駄。
投げる PATCH は ``archived=True`` ただ1つ。
"""

from __future__ import annotations

import asyncio
import contextlib
import datetime
import logging
from collections.abc import Callable, Iterable, Sequence
from typing import Any

from .thread_name import CLOSED_MARK, LEGACY_CLOSED_MARKS
from .utils.logger import log_ctx

logger = logging.getLogger(__name__)

__all__ = [
    "MAX_ARCHIVES_PER_TICK",
    "QUIET_PERIOD",
    "SWEEP_INTERVAL_SECONDS",
    "StoppedThreadSweepLoop",
    "is_stopped_name",
    "select_unarchived_stopped",
]

#: 停止を表すスレッド名の先頭マーカー。旧 ``[終了]`` も対象 — 本番に残っている
#: 取り残しの一部はその時代のもので、拾えなければ永久に開いたままになる。
_STOPPED_MARKS: tuple[str, ...] = (CLOSED_MARK, *LEGACY_CLOSED_MARKS)

#: 直近の投稿からこれだけ経つまでは畳まない。
#:
#: 停止済みスレッドに投稿すると Discord がスレッドを開き、bot が「▶️ 再開する」
#: の案内を出す (#512)。その真上でアーカイブし返すと、利用者は目の前の再開
#: ボタンを取り上げられる。同じ猶予が #379 の形も防ぐ: ミラーがまだ喋っている
#: スレッドを畳むと「畳む→ミラーが開く→畳む」を延々と撃ち合い、レート制限を
#: 掃除ではなく喧嘩に使ってしまう。静かになるまで待てば、どちらも起きない。
#:
#: 1時間は「案内を読んでボタンを押す」には十分に長く、「もう見ていない」を
#: 待たせるには十分に短い。押されなければ次のティックで畳む — 停止済みで
#: あることは変わらないので、それが正しい落ち着き先。
QUIET_PERIOD = datetime.timedelta(hours=1)

#: 1ティックで畳む上限。
#:
#: :data:`c_lord.idle_stop.MAX_STOPS_PER_TICK` が実質無制限なのとは逆に、ここは
#: **本当に上限として効かせる**。あちらが一度に捌くのは「これから停止する数本」
#: だが、こちらの初回は 186 本の PATCH を一斉に投げることになる。畳むのは急ぎの
#: 用事ではないので、レート制限に突っ込む理由が無い。25 本 × 5 分間隔なら本番の
#: 積み残しは 40 分ほどで片付く。
MAX_ARCHIVES_PER_TICK = 25

#: ティックの間隔。取り残しは自然には増えない（#609 以降の停止はその場で畳まれ
#: る）ので、頻度ではなく「いつか必ず拾う」ことだけが要る。
SWEEP_INTERVAL_SECONDS = 300.0


def is_stopped_name(name: str | None) -> bool:
    """スレッド名が ``[停止]`` / ``[終了]`` で始まるか。

    **先頭でなければ対象外**。「停止の設計を相談する」のようなトピックや、本文
    中にマーカーを含む名前を巻き込まないため (AC4)。文字列でない値は False —
    Discord から欠けた値が来てもスイープごと落とさない。
    """
    if not isinstance(name, str):
        return False
    return name.startswith(_STOPPED_MARKS)


def _last_activity(thread: Any) -> datetime.datetime | None:
    """スレッドの最終投稿時刻。読めなければ ``None``（＝静かとみなす）。"""
    import discord

    last_message_id = getattr(thread, "last_message_id", None)
    if not last_message_id:
        return None
    try:
        return discord.utils.snowflake_time(int(last_message_id))
    except (TypeError, ValueError, OverflowError):
        return None


def select_unarchived_stopped(
    threads: Iterable[Any],
    *,
    now: datetime.datetime,
    quiet_period: datetime.timedelta = QUIET_PERIOD,
    limit: int | None = None,
) -> list[Any]:
    """畳むべきスレッドを、最終投稿の古い順に返す。

    選ばれるのは **すべて** 満たすものだけ:

    * 名前が停止マーカーで始まる（:func:`is_stopped_name`）— 現役スレッドを
      巻き込まないための唯一の条件 (AC4)
    * まだアーカイブされていない。``archived`` が **明示的に True** のときだけ
      除く。読めない値を「アーカイブ済み」と決めつけると取り残しが残るし、
      再アーカイブは冪等なので間違えても害が無い
    * 最終投稿から *quiet_period* 以上経っている（:data:`QUIET_PERIOD` の理由）

    古い順なのは :mod:`c_lord.idle_sweep` と同じ理由 — 実行ごとの証跡を比べら
    れるようにするため。投稿の無いスレッドは最も古いものとして先頭に来る。
    """
    cutoff = now - quiet_period
    aged: list[tuple[datetime.datetime, int, Any]] = []
    for thread in threads:
        if not is_stopped_name(getattr(thread, "name", None)):
            continue
        if getattr(thread, "archived", False) is True:
            continue
        last = _last_activity(thread)
        if last is not None:
            if last.tzinfo is not None and now.tzinfo is None:
                last = last.replace(tzinfo=None)
            if last > cutoff:
                continue
        # 投稿の無いスレッドは「いちばん静か」— 先頭に置く。
        sort_key = last if last is not None else datetime.datetime.min.replace(tzinfo=now.tzinfo)
        aged.append((sort_key, int(getattr(thread, "id", 0)), thread))

    aged.sort(key=lambda row: (row[0], row[1]))
    picked = [thread for _, _, thread in aged]
    return picked[:limit] if limit is not None else picked


class StoppedThreadSweepLoop:
    """``[停止]`` なのに開いたままのスレッドを、少しずつ畳んでいくループ。

    候補は DB ではなく Discord に聞く: ``GET /guilds/{id}/threads/active`` が
    「まだ開いているスレッド」をギルドごとに1回で返すので、行を失った 7 本も
    含めて取りこぼさず、しかも1本ずつ ``fetch_channel`` する必要が無い。
    """

    name = "stopped-sweep"

    def __init__(
        self,
        bot: Any,
        session_repo: Any | None = None,
        *,
        interval_seconds: float = SWEEP_INTERVAL_SECONDS,
        max_per_tick: int = MAX_ARCHIVES_PER_TICK,
        quiet_period: datetime.timedelta = QUIET_PERIOD,
        now_fn: Callable[[], datetime.datetime] | None = None,
    ) -> None:
        self._bot = bot
        self._repo = session_repo
        self._interval = interval_seconds
        self._max_per_tick = max_per_tick
        self._quiet_period = quiet_period
        # noqa on the tz: datetime.UTC is 3.11+, and c-lord supports 3.10.
        self._now = now_fn or (
            lambda: datetime.datetime.now(datetime.timezone.utc)  # noqa: UP017
        )
        # 権限が無い / 消えたスレッドを覚えておく。5 分ごとに同じ 403 を投げ
        # 続けると、1本の権限漏れが毎ティックの枠を食い潰す (#593 と同じ形)。
        self._skip: set[int] = set()
        self._task: asyncio.Task[None] | None = None

    # ── lifecycle ────────────────────────────────────────────────────────

    def start(self) -> None:
        """ループを起こす。二重起動しない。"""
        if self._max_per_tick <= 0:
            logger.info("%s: disabled", self.name)
            return
        if self._task is not None and not self._task.done():
            return
        self._task = asyncio.create_task(self._run(), name="stopped_sweep")
        logger.info(
            "Started %s loop (interval=%.0fs, max_per_tick=%d)",
            self.name,
            self._interval,
            self._max_per_tick,
        )

    async def stop(self) -> None:
        """ループを止める。何度呼んでもよい。"""
        if self._task is None:
            return
        self._task.cancel()
        with contextlib.suppress(asyncio.CancelledError, Exception):
            await self._task
        self._task = None

    async def _run(self) -> None:
        # 接続とギルドのキャッシュが揃うまで待ってから最初の掃きに入る。
        await asyncio.sleep(min(60.0, self._interval))
        while True:
            try:
                await self.tick()
            except asyncio.CancelledError:
                raise
            except Exception:
                logger.exception("%s tick raised", self.name)
            await asyncio.sleep(self._interval)

    # ── one pass ─────────────────────────────────────────────────────────

    async def _active_threads(self) -> list[Any]:
        """全ギルドの未アーカイブスレッド。引けなかったギルドは飛ばす。

        1ギルドが落ちても残りは掃く: 掃除は全か無かではない。
        """
        threads: list[Any] = []
        for guild in list(getattr(self._bot, "guilds", []) or []):
            try:
                threads.extend(await guild.active_threads())
            except Exception as exc:
                logger.info(
                    "%s: could not list active threads in guild=%s (%s) — skipping",
                    self.name,
                    getattr(guild, "id", "?"),
                    exc,
                )
        return threads

    async def _live_thread_ids(self) -> set[int] | None:
        """行が「停止していない」と言うスレッド。読めなければ ``None``。

        ``None`` は「分からない」であって「1本も無い」ではない。呼び手はこの
        違いでティックごと諦める — docstring の最後の段落を参照。
        """
        if self._repo is None:
            return set()
        try:
            return set(await self._repo.open_thread_ids())
        except Exception:
            logger.warning(
                "%s: could not read the session table — skipping this tick",
                self.name,
                exc_info=True,
            )
            return None

    async def _archive(self, thread: Any) -> bool:
        """1本畳む。畳めたときだけ True。

        ``archived=True`` 以外は何も送らない — 改名も投稿もしない理由はモジュール
        の docstring を参照。
        """
        import discord

        try:
            await thread.edit(archived=True)
        except (discord.Forbidden, discord.NotFound) as exc:
            # 権限が無い / もう存在しない。次のティックで直る類ではないので覚える。
            logger.info(
                "%s %s: cannot archive (%s) — not retrying",
                log_ctx(thread_id=getattr(thread, "id", 0)),
                self.name,
                exc,
            )
            self._skip.add(int(getattr(thread, "id", 0)))
            return False
        except Exception as exc:
            # レート制限や一時的な失敗。次のティックでまた拾われる。
            logger.info(
                "%s %s: archive failed (%s) — will retry",
                log_ctx(thread_id=getattr(thread, "id", 0)),
                self.name,
                exc,
            )
            return False
        logger.info(
            "%s %s: archived a stopped thread %r (#685)",
            log_ctx(thread_id=getattr(thread, "id", 0)),
            self.name,
            getattr(thread, "name", ""),
        )
        return True

    async def tick(self) -> int:
        """ひと掃き。実際に畳めた本数を返す。

        テストが実時計なしで回せるように public。
        """
        live = await self._live_thread_ids()
        if live is None:
            return 0

        threads = await self._active_threads()
        due: Sequence[Any] = select_unarchived_stopped(
            threads,
            now=self._now(),
            quiet_period=self._quiet_period,
        )
        due = [
            t
            for t in due
            if int(getattr(t, "id", 0)) not in self._skip and int(getattr(t, "id", 0)) not in live
        ]
        if not due:
            return 0

        if len(due) > self._max_per_tick:
            # 黙って切り捨てない: 済んだ数しか出さないログは「全部片付いた」と
            # 読めてしまう (#593)。
            logger.info(
                "%s: %d stopped thread(s) still open; archiving up to %d this tick, "
                "the rest follow on later ticks",
                self.name,
                len(due),
                self._max_per_tick,
            )

        archived = 0
        for thread in due:
            if archived >= self._max_per_tick:
                break
            # 上限に数えるのは「実際に畳めた」ものだけ。試行を数えると、届かな
            # かった数本が毎ティック先頭を塞いだまま進捗を報告してしまう (#604)。
            if await self._archive(thread):
                archived += 1

        if archived:
            logger.info("%s: archived %d stopped thread(s) (#685)", self.name, archived)
        return archived
