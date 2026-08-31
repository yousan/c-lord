"""行を失った作業ディレクトリを回収する — Issue #613 (#577 の1件目).

#575 の 30 日スイープは ``sessions`` の行を消すときに、その行が指す作業
ディレクトリも一緒に消す。**行が最初から無いディレクトリには、その仕組みは
永久に届かない。** 行は Discord スレッドと作業コピーを結ぶ唯一の handle なので、
行を失った時点でディレクトリは誰からも参照されなくなり、しかし誰も消さない。

本番実測 (tachikoma / 2026-08-31): 作業ディレクトリ 625 件のうち **313 件・
17.3 GB が行を持たない**。内訳は worktree がクリーンなもの 107 件、未コミットの
変更を抱えたもの 53 件、git ですらないもの 153 件。

**危険な方向はひとつしかない — 消しすぎ。** 失われた作業は戻らないが、消し
そびれた 17 GB は次の掃除で回収できる。だからこのモジュールのあらゆる判定は
迷ったら「残す」に倒れる:

* ``<root>/<数字>/<数字>`` の形でないものは見ない。root の下に誰かが置いた
  チェックアウトやバックアップは、このループの管轄ではない
* シンボリックリンクは辿らない。リンク先を消して本体を失うのが最悪の事故
* git でないディレクトリは消さない。クリーンかどうかを確かめる手段が無く、
  「残骸」と「誰かの生成物」を区別できない
* 未コミットの変更が1つでもあれば残す

期間を Claude Code の ``cleanupPeriodDays`` から読まないのは意図的。#575 の行
スイープは「Claude が会話を忘れたらチェックアウトも役目を終える」という対応
関係で期間を揃えていたが、**孤児には対応する行も transcript も無い**。揃える
相手がいないものを揃えたことにするのは嘘なので、ここは c-lord 自身の定数を持つ。
"""

from __future__ import annotations

import logging
import os
import time
from dataclasses import dataclass
from pathlib import Path

from .session_cleanup import _MIN_PATH_DEPTH, DirOutcome
from .session_dir import _is_clean

logger = logging.getLogger(__name__)

__all__ = [
    "ORPHAN_SWEEP_DAYS_DEFAULT",
    "OrphanCandidate",
    "OrphanSweepResult",
    "find_orphan_dirs",
    "orphan_sweep_days",
    "remove_orphan_dir",
    "sweep_orphan_dirs",
]

#: 行を失ったディレクトリを消すまでの日数。
#:
#: 30 は #575 の行スイープと同じ長さだが、由来は別（上の docstring 参照）。
#: ホスト規模に依存しない定数なので、ゼロコンフィグで配ってよい (#540)。
ORPHAN_SWEEP_DAYS_DEFAULT = 30

_ENV_DAYS = "CLORD_ORPHAN_SWEEP_DAYS"

#: 一度に消す上限。ここで律速するのは I/O であって Discord API ではないので、
#: #593 で問題になった「小さすぎる cap が積み残しを作る」形にはならない。
#: それでも上限を置くのは、初回の一掃で数百 GB 分の ``rmtree`` を同時に走らせて
#: ホストの I/O を飽和させないため。
MAX_REMOVALS_PER_PASS = 50

#: 6 時間ごと。孤児は「増えるのが遅く、放置しても数時間では困らない」性質の
#: 問題なので、起動時ではなく定期実行にする。起動のたびに 600 以上の
#: ディレクトリを ``git status`` で走査すると、起動そのものが遅くなる。
SWEEP_INTERVAL_SECONDS = 6 * 3600.0

_SECONDS_PER_DAY = 86400.0


@dataclass(frozen=True)
class OrphanCandidate:
    """行を持たず、十分に古い作業ディレクトリ。"""

    path: Path
    idle_days: float


@dataclass(frozen=True)
class OrphanSweepResult:
    """ひと掃きの結果。"""

    removed: int
    kept: int
    reclaimed_bytes: int = 0
    #: 持ち主のいない docker コンテナの名前 (#612)。**止めはしない** — 中身は
    #: 誰かのデータで、勝手に落とす判断はディレクトリの削除より重い。まず
    #: 「在ることが分かる」状態にする。
    orphan_containers: tuple[str, ...] = ()


def orphan_sweep_days() -> int:
    """孤児を消すまでの日数。``0`` は無効化。"""
    raw = os.getenv(_ENV_DAYS, "").strip()
    if not raw:
        return ORPHAN_SWEEP_DAYS_DEFAULT
    try:
        value = int(raw)
    except ValueError:
        logger.warning(
            "%s=%r is not a number — falling back to %d days",
            _ENV_DAYS,
            raw,
            ORPHAN_SWEEP_DAYS_DEFAULT,
        )
        return ORPHAN_SWEEP_DAYS_DEFAULT
    return max(0, value)


def _normalise(path: str) -> str:
    """行が持つパスと、ディスク上のパスを突き合わせられる形にする。

    行の ``working_dir`` は必ずしも正規化されていない。``realpath`` を通さずに
    文字列比較すると、生きているワークスペースを孤児と誤認しうる — それは
    このモジュールで唯一許されない間違いなので、両側を通す。
    """
    try:
        return os.path.realpath(path)
    except (OSError, ValueError):
        return path


def _last_touched(path: Path) -> float:
    """このワークスペースが最後に動いた時刻（epoch 秒）。

    **``.git`` の mtime を見てはいけない。** ``git status`` が ``.git/index`` を
    書き直すので、``.git`` は「最後に誰かが作業した時刻」ではなく「最後に誰かが
    状態を*問い合わせた*時刻」になる。そしてこの掃除は残す判定のたびに
    ``git status`` を走らせる — つまり ``.git`` を見る実装は、**掃除が自分の見て
    いる時計を毎回リセットする**。

    本番データで実際に踏んだ (2026-08-31): 孤児 160 件すべてが「0.0 日前」を
    返し、閾値 30 日の候補が 0 件になった。原因は数分前に自分で流した dry-run
    の ``git status`` だった。ディレクトリ自身の mtime では 2026-05-07 で、
    3ヶ月以上放置されていた。

    深いところのファイルだけが編集された場合はここに現れないが、それは
    :func:`remove_orphan_dir` の worktree クリーン判定が捕まえる — 変更が
    あれば dirty になり、消さない。時計と安全弁を別の信号にしてあるので、
    片方が鈍っても失われる作業は無い。

    読めなければ **いま** を返す = 消さない側に倒れる。
    """
    try:
        return path.stat().st_mtime
    except OSError:
        return time.time()


def _workspace_dirs(root: Path):
    """``<root>/<channel_id>/<thread_id>`` だけを列挙する。

    形が合わないものは黙って飛ばす。root の下には c-lord が作っていない
    ディレクトリが在りうるし、在っても不思議ではない。
    """
    try:
        channels = sorted(root.iterdir())
    except OSError:
        return
    for channel in channels:
        if not channel.name.isdigit() or channel.is_symlink() or not channel.is_dir():
            continue
        try:
            threads = sorted(channel.iterdir())
        except OSError:
            continue
        for thread in threads:
            if not thread.name.isdigit() or thread.is_symlink() or not thread.is_dir():
                continue
            yield thread


def find_orphan_dirs(
    root: Path | str,
    known_dirs: set[str],
    *,
    min_idle_days: int,
    now: float | None = None,
) -> list[OrphanCandidate]:
    """行を持たず ``min_idle_days`` 以上動いていないディレクトリを返す。

    Never raises: 走査中の ``OSError`` は「そのディレクトリを候補にしない」に
    なるだけで、掃除全体を落とさない。
    """
    root = Path(root)
    known = {_normalise(d) for d in known_dirs if d}
    at = time.time() if now is None else now

    found: list[OrphanCandidate] = []
    for path in _workspace_dirs(root):
        if _normalise(str(path)) in known:
            continue
        idle_days = (at - _last_touched(path)) / _SECONDS_PER_DAY
        if idle_days < min_idle_days:
            continue
        found.append(OrphanCandidate(path=path, idle_days=idle_days))
    return found


def remove_orphan_dir(path: Path | str) -> DirOutcome:
    """安全なときだけ *path* を消す。Never raises.

    :func:`c_lord.session_cleanup.remove_clean_session_dir` と同じ判定を、行を
    持たないディレクトリに対して行う。判定を書き直さないのは意図的 — 「消して
    よいか」の定義が2つに分かれた瞬間に、片方だけが緩む。
    """
    import shutil

    try:
        target = Path(path)
        if len([p for p in target.parts if p not in ("/", "")]) < _MIN_PATH_DEPTH:
            logger.warning("orphan sweep: refusing suspiciously shallow path %r", str(target))
            return DirOutcome.KEPT_UNSAFE
        if target.is_symlink() or not target.is_dir():
            return DirOutcome.ABSENT
    except (OSError, ValueError):
        return DirOutcome.ABSENT

    if not _is_clean(str(target)):
        logger.debug("orphan sweep: keeping %s — uncommitted work or not a git repo", target)
        return DirOutcome.KEPT_DIRTY

    try:
        shutil.rmtree(target)
    except OSError as exc:
        logger.warning("orphan sweep: failed to remove %s: %s", target, exc)
        return DirOutcome.KEPT_DIRTY
    logger.info("orphan sweep: removed %s (no sessions row)", target)
    return DirOutcome.REMOVED


def _size_of(path: Path) -> int:
    total = 0
    for dirpath, _dirnames, filenames in os.walk(path, onerror=lambda _e: None):
        for name in filenames:
            try:
                total += os.lstat(os.path.join(dirpath, name)).st_size
            except OSError:
                continue
    return total


async def sweep_orphan_dirs(
    root: Path | str,
    known_dirs: set[str],
    *,
    min_idle_days: int,
    max_per_pass: int = MAX_REMOVALS_PER_PASS,
) -> OrphanSweepResult:
    """ひと掃き。消せたものだけを上限に数える。

    試行回数ではなく **実際に消せた数** を上限に数えるのは #593 の教訓。先頭に
    消せないもの（未コミット・git でない）が並ぶと、試行回数で数える実装は
    毎回そこで打ち止めになり、後ろのディレクトリに永久に到達しない。本番の
    孤児 313 件のうち 206 件が「消せないもの」なので、これは実際に起きる。
    """
    import asyncio

    candidates = await asyncio.to_thread(
        find_orphan_dirs, root, known_dirs, min_idle_days=min_idle_days
    )
    removed = kept = 0
    reclaimed = 0
    for candidate in candidates:
        if removed >= max_per_pass:
            break
        size = await asyncio.to_thread(_size_of, candidate.path)
        outcome = await asyncio.to_thread(remove_orphan_dir, candidate.path)
        if outcome is DirOutcome.REMOVED:
            removed += 1
            reclaimed += size
        elif outcome in (DirOutcome.KEPT_DIRTY, DirOutcome.KEPT_UNSAFE):
            kept += 1

    if removed or kept:
        logger.info(
            "orphan sweep: removed %d directory(ies) (%.1f GB), kept %d with work in them",
            removed,
            reclaimed / 1024**3,
            kept,
        )
    return OrphanSweepResult(removed=removed, kept=kept, reclaimed_bytes=reclaimed)


class OrphanSweepLoop:
    """定期的に孤児ディレクトリを回収するバックグラウンドタスク。

    起動時ではなく定期実行にしているのは、走査が ``git status`` を数百回叩く
    ためで、起動を待たせるほどの緊急性がこの問題には無いから。最初の掃きは
    起動から少し置いてから走る。
    """

    def __init__(
        self,
        session_repo: object,
        sessions_root: Path | str,
        *,
        min_idle_days: int | None = None,
        interval_seconds: float = SWEEP_INTERVAL_SECONDS,
        max_per_pass: int = MAX_REMOVALS_PER_PASS,
        container_scan: object | None = None,
    ) -> None:
        self._repo = session_repo
        self._root = Path(sessions_root)
        self._days = min_idle_days if min_idle_days is not None else orphan_sweep_days()
        self._interval = interval_seconds
        self._max_per_pass = max_per_pass
        self._container_scan = container_scan
        self._task: object | None = None

    def start(self) -> None:
        """Spawn the loop task. Idempotent, and a no-op when disabled."""
        import asyncio

        if self._days <= 0:
            logger.info("orphan sweep: disabled (%s=0)", _ENV_DAYS)
            return
        if self._task is not None and not getattr(self._task, "done", lambda: True)():
            return
        self._task = asyncio.create_task(self._run(), name="orphan_sweep")
        logger.info(
            "Started orphan-dir sweep (root=%s, threshold=%dd, interval=%.0fs)",
            self._root,
            self._days,
            self._interval,
        )

    async def stop(self) -> None:
        """Cancel the loop. Safe to call multiple times."""
        import asyncio
        import contextlib

        if self._task is None:
            return
        self._task.cancel()  # type: ignore[attr-defined]
        with contextlib.suppress(asyncio.CancelledError, Exception):
            await self._task  # type: ignore[misc]
        self._task = None

    async def _run(self) -> None:
        import asyncio

        await asyncio.sleep(min(300.0, self._interval))
        while True:
            try:
                await self.tick()
            except asyncio.CancelledError:
                raise
            except Exception:
                logger.exception("orphan sweep tick raised")
            await asyncio.sleep(self._interval)

    async def tick(self) -> OrphanSweepResult:
        """ひと掃き。DB を読めなければ **何も消さない**。

        行の一覧が取れないまま走ると、生きているワークスペースが軒並み
        「行が無い」に見える。ここが空振りしたら掃除ごと諦めるのが正しい。
        """
        known = await self._repo.all_working_dirs()  # type: ignore[attr-defined]
        result = await sweep_orphan_dirs(
            self._root,
            known,
            min_idle_days=self._days,
            max_per_pass=self._max_per_pass,
        )
        names = await self._scan_containers(known)
        if names:
            logger.info(
                "orphan sweep: %d docker container(s) have no workspace: %s "
                "(left running - stopping them is a data decision, see #612)",
                len(names),
                ", ".join(names),
            )
        return OrphanSweepResult(
            removed=result.removed,
            kept=result.kept,
            reclaimed_bytes=result.reclaimed_bytes,
            orphan_containers=names,
        )

    async def _scan_containers(self, known: set[str]) -> tuple[str, ...]:
        """持ち主のいないコンテナの名前。docker が無い環境では常に空。

        #573 で作りながら **どこからも呼ばれていなかった** ``orphaned_containers``
        の呼び出し元がここ (#612)。一覧できるだけで誰も見ない状態を残さない。
        """
        import contextlib

        scan = self._container_scan
        if scan is None:
            from .devenv import orphaned_containers

            scan = orphaned_containers
        with contextlib.suppress(Exception):
            return tuple(c.name for c in await scan(known))  # type: ignore[operator]
        return ()
