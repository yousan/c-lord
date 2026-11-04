"""同じリポジトリを何度もダウンロードして持つのをやめる — Issue #643 (#577 の2件目).

``session_dir.py`` はスレッドを立てるたびに ``git clone --depth=1`` を打つ。
240スレッドなら240クローンで、**中身は240個とも同じ**。本番実測
(tachikoma / 2026-08-31): ``c-lord-sessions`` 121 GB のうち **117 GB が1リポジトリ
(``project_30_ehon-ya``) の240クローン**で、その ``.git`` だけで **39.2 GB**。

一方で **全履歴の bare ミラー1個は 256 MB** しかない。depth=1 のクローン1個
(233 MB) とほぼ同じ。GitHub 側がまとめて repack した pack を送ってくるので、
240個が別々に持つと共通部分の圧縮が効かず、合計が 39 GB に膨らんでいた。
**まとめると 256 MB、バラすと 39 GB** — これが回収量の正体。

やることは1つだけ。origin ごとに bare ミラーを ``<sessions root>/.mirrors/`` に
1個持ち、クローンに ``--reference-if-able`` で参照させる。git は
``.git/objects/info/alternates`` という1行のファイルを書き、以後そのクローンは
手元に無いオブジェクトをミラーに見に行く。実測で ``.git`` は 233 MB → **1.2 MB**、
クローンは **3.8 秒** (ネットワーク転送がほぼ消えるので今より速い)。

**ミラーは derived data である。** 中身は origin にあるものの写しでしかなく、
失われる情報が無い。``sessions.db`` のような「消えたら戻らないもの」ではない。
消えても作業ファイルと未コミットの変更は無傷で、履歴が読めなくなるだけ
(実測: ``fatal: Failed to traverse parents of commit ...``)。**作り直せば28秒で
完全復旧する** (log / status / fsck すべて green を確認済み)。だからここでは
「ミラーが無ければ作る」を毎回やる — 事故が事故で終わる。

守る不変条件は2つしかない:

1. **ミラーが無くても・作れなくても、クローンは今までどおり成功する。**
   ミラーは高速化と節約のためのもので、動作の前提ではない。この
   モジュールのあらゆる失敗は ``None`` を返して終わる
2. **ミラーのパスが置き場の外に出ない。** origin URL は ``/clord repo:`` で
   利用者入力から届く (#514)

**更新 (fetch) が時間切れになるミラー** (#1133/#1134 — 本番の ``yousan/openclaw``
は 10/27〜10/29 の6回すべてで 300 秒の時間切れだった):

- 時間切れのときは git をプロセスグループごと止め、受信途中の
  ``objects/pack/tmp_*`` (1回約3GB) をその場で消す。殺された fetch の書きかけは
  git 自身には消せない。**別の git がいま書いているものは消さない** (#1173)
- bot が終わるとき (停止シグナル・``atexit``) は走っている git もグループごと止める
  (#1174)。別グループなので、止めないと Ctrl-C / SIGTERM の後も受信を続ける
- ミラーに印 (``clord-fetch-timed-out``) を置き、**次からはクローンを待たせない**。
  印が1時間以上古ければ、長い上限 (1時間) で**裏で**取り直し、成功したら印を
  消して元の「更新してから使う」に戻る。直後は裏でも取り直さない (1回3GB を
  スレッドごとに繰り返さない)
- 誰かが同じミラーを更新中 (``clord-fetch.lock``) なら、その終わりも待たない
- 以前に殺された fetch の書きかけ (2時間以上更新の無い ``tmp_*``) は、更新中で
  ないときに消す。書きかけが残っていた = 前に fetch が殺された、なので印も置く

ミラーが古いままでも正しさは変わらない。クローンは ``--reference-if-able`` で
足りないオブジェクトを origin から取る。
"""

from __future__ import annotations

import atexit
import hashlib
import logging
import os
import re
import shutil
import signal
import subprocess
import threading
import time
import uuid
from pathlib import Path
from typing import Any

logger = logging.getLogger(__name__)

__all__ = [
    "FETCH_TIMED_OUT_MARK",
    "MIRRORS_DIR_NAME",
    "ensure_mirror",
    "mirror_name",
    "mirrors_enabled",
    "mirrors_root_for",
    "normalise_origin",
    "stop_running_git",
    "wait_for_background_fetches",
]

#: 置き場の名前。ワークスペースは ``<root>/<channel>/<thread>`` という
#: **全部数字**の形なので、数字でない名前は #613 の掃除の候補に構造的に
#: 上がらない (``orphan_dirs._workspace_dirs`` が入口で ``isdigit()`` を見る)。
#: ドット始まりにしているのは ``rm -rf <root>/*`` が当たらないため。
MIRRORS_DIR_NAME = ".mirrors"

_ENV_ENABLED = "CLORD_GIT_MIRRORS"
_FALSEY = {"0", "false", "no", "off"}

#: ミラーを作るまで待つ上限。超えたらミラー無しで進む — クローンを
#: 止めるほどの価値はこの最適化に無い。ehon-ya (256 MB) の実測は 28 秒。
_CLONE_TIMEOUT = 900.0

#: 既存ミラーの更新を待つ上限。失敗しても困らない — クローン側が
#: 足りないオブジェクトを origin から取るだけで、正しさは変わらない。
_FETCH_TIMEOUT = 300.0

#: 更新が時間切れになったミラーに置く印 (#1133)。あれば次の更新を待たない。
FETCH_TIMED_OUT_MARK = "clord-fetch-timed-out"

#: 印が付いたミラーを裏で取り直すまでの間隔。印の mtime = 最後に失敗した時刻。
_BACKGROUND_RETRY_AFTER = 3600.0

#: 裏で取り直すときの上限。誰も待っていないので長くてよい。
_BACKGROUND_FETCH_TIMEOUT = 3600.0

#: 同じミラーの更新を1本にする flock のファイル (ミラーの中に置く)。
_FETCH_LOCK = "clord-fetch.lock"

#: これより長く更新の無い ``objects/pack/tmp_*`` は、殺された fetch の書きかけ
#: とみなして消す (#1134)。走っている fetch は書きかけを書き続けるので mtime が
#: 新しい。c-lord の fetch は最長 ``_BACKGROUND_FETCH_TIMEOUT`` で止まる。
_STALE_GARBAGE_AGE = 2 * 3600.0

_background: dict[str, threading.Thread] = {}
_background_lock = threading.Lock()

_README = """# c-lord git object mirrors

このディレクトリには、c-lord が作るセッション用クローンが**共有している git
オブジェクト**が入っています (Issue #643)。

## これは何か

同じリポジトリを何十回もクローンすると、同じ中身を何十個も持つことになります。
本番では1リポジトリの240クローンだけで `.git` が 39 GB ありました。ここに
bare ミラーを1個だけ置き、各クローンには `--reference-if-able` で参照させる
ことで、1クローンあたりの `.git` が 233 MB から 1.2 MB になります。

## 消してしまったら

**作業ファイルと未コミットの変更は無事です。** git が管理していないので影響を
受けません。壊れるのは「履歴が読めなくなる」ところまでで、データは失われません。

**c-lord が次にクローンするとき自動で作り直します。** 待てない場合は手動でも
直せます:

    git clone --mirror <リポジトリのURL> <このディレクトリ>/<元と同じ名前>.git

既存のクローンをミラーから独立させたい場合は、そのクローンで:

    git repack -a -d && rm -f .git/objects/info/alternates

## 消さないほうがよい理由

消えても直りますが、直るまでの間このミラーを参照しているクローンは履歴を
読めません。ここは「キャッシュ」ではなく「共有されている実体」です。
"""


def mirrors_enabled() -> bool:
    """``CLORD_GIT_MIRRORS=0`` で無効化。既定は有効 (ゼロコンフィグ)。"""
    return os.getenv(_ENV_ENABLED, "").strip().lower() not in _FALSEY


def normalise_origin(origin: str) -> str:
    """同じリポジトリを指す表記を1つに畳む。

    本番実測では ``yousan/c-lord`` が ``git@github.com:...git`` 125件 /
    ``https://github.com/...git`` 10件 / ``https://github.com/...`` 6件に
    分かれていた。**ここが分かれるとミラーが3つでき、節約が丸ごと消える。**
    """
    text = origin.strip()
    text = re.sub(r"^[a-zA-Z][a-zA-Z0-9+.-]*://", "", text)
    # scp 形式 (``git@github.com:owner/repo``) を ``host/owner/repo`` に均す。
    scp = re.match(r"^[^/@]+@([^:/]+):(.*)$", text)
    text = f"{scp.group(1)}/{scp.group(2)}" if scp else re.sub(r"^[^/@]+@", "", text)
    text = text.rstrip("/")
    if text.endswith(".git"):
        text = text[:-4]
    return text.lower()


def mirror_name(origin: str) -> str:
    """ミラーのディレクトリ名。**区切りも ``..`` も残らない。**

    読める slug は人間のためだけのもので、同一性は正規化後の URL の
    ダイジェストが担保する。slug から ``.`` を落としてあるので ``..`` は
    構造的に作れず、パス・トラバーサルの経路が閉じる (#514: origin は
    ``/clord repo:`` から利用者入力として届く)。
    """
    normalised = normalise_origin(origin)
    digest = hashlib.sha256(normalised.encode("utf-8", "replace")).hexdigest()[:12]
    slug = re.sub(r"[^A-Za-z0-9_-]+", "-", normalised).strip("-")[:60].strip("-")
    return f"{slug or 'repo'}-{digest}.git"


def mirrors_root_for(base_dir: str | Path) -> Path:
    """``base_dir`` (= ``<sessions root>/<channel_id>``) からミラー置き場を導く。

    利用者に設定させないのは意図的 (#540 ゼロコンフィグ)。そして
    **セッションと同じディスクに必ず乗る**のがこの導き方の要点 — セッションを
    大きいディスクに移した利用者がいても、節約する対象と同じ場所に
    ミラーがついてくる。``~/.cache`` を使わないのは、XDG が「いつ消しても
    よい」と規定していて意味が真逆になるため。
    """
    return Path(base_dir).parent / MIRRORS_DIR_NAME


#: いま走っている git のプロセスグループ (#1174)。bot が終わるときにまとめて止める。
_running_groups: set[int] = set()
_running_lock = threading.Lock()
#: ``stop_running_git`` ran: a git killed from here on did not time out.
_stopping = threading.Event()


def stop_running_git() -> int:
    """走っているミラー用の git をグループごと止める。止めた数を返す。Never raises.

    git は自分のプロセスグループで走るので (#1134)、端末の Ctrl-C や bot への
    SIGTERM が届かない。何もしないと bot が終わったあとも git が受信を続け、
    flock も外れるので次の bot が同じミラーにもう1本 fetch を出す (#1174)。
    プロセスの終了時 (``atexit``) と、停止シグナルを受けたとき (``main``) に呼ぶ。
    """
    _stopping.set()
    with _running_lock:
        groups = list(_running_groups)
    stopped = 0
    for pgid in groups:
        try:
            os.killpg(pgid, signal.SIGKILL)
            stopped += 1
        except OSError:
            continue
    if stopped:
        logger.info("git mirror: stopped %d running git process group(s) on shutdown", stopped)
    return stopped


atexit.register(stop_running_git)


def _run(args: list[str], *, timeout: float) -> subprocess.CompletedProcess[str] | None:
    """Never raises. 失敗は非ゼロ、OS エラーは ``None``、時間切れは ``returncode=-9``.

    git は自分の新しいプロセスグループで走らせ、時間切れのときは**グループごと**
    止める (#1134)。``subprocess.run(timeout=)`` は親の git しか殺さず、受信を
    担う子 (index-pack / ssh) が書きかけの pack を育て続けていた。
    別グループなので bot の停止は届かない — ``stop_running_git`` が代わりに止める (#1174)。
    """
    try:
        proc = subprocess.Popen(
            args,
            stdout=subprocess.PIPE,
            stderr=subprocess.PIPE,
            text=True,
            start_new_session=True,
        )
    except OSError as exc:
        logger.warning("git mirror: %s failed: %s", " ".join(args[:3]), exc)
        return None
    with _running_lock:
        _running_groups.add(proc.pid)
    try:
        return _wait(proc, args, timeout=timeout)
    finally:
        with _running_lock:
            _running_groups.discard(proc.pid)


def _wait(
    proc: subprocess.Popen[str], args: list[str], *, timeout: float
) -> subprocess.CompletedProcess[str]:
    try:
        out, err = proc.communicate(timeout=timeout)
    except subprocess.TimeoutExpired:
        import contextlib

        with contextlib.suppress(OSError):
            os.killpg(proc.pid, signal.SIGKILL)
        with contextlib.suppress(Exception):
            proc.communicate(timeout=10)
        with _running_lock:
            # Reaped: the number may be reused — never kill it again (#1174).
            _running_groups.discard(proc.pid)
        _wait_for_group_gone(proc.pid)
        logger.warning("git mirror: %s timed out after %.0f seconds", " ".join(args[:4]), timeout)
        return subprocess.CompletedProcess(args, _TIMED_OUT, "", "timed out")
    return subprocess.CompletedProcess(args, proc.returncode, out, err)


_TIMED_OUT = -9


def _wait_for_group_gone(pgid: int, limit: float = 2.0) -> None:
    """SIGKILL したグループの子 (index-pack 等) が本当に居なくなるまで少し待つ。

    ``communicate`` が待つのは親の git だけ。子が最後の1回を書き終える前に
    「書き続けているか」を測ると、自分の書きかけを他人のものと見誤る (#1173)。
    """
    deadline = time.monotonic() + limit
    while time.monotonic() < deadline:
        try:
            os.killpg(pgid, 0)
        except OSError:
            return
        time.sleep(0.05)


#: 時間切れの片付けで「まだ誰かが書いているか」を見る間隔 (#1173)。
_SETTLE_SECONDS = 1.0


def _open_by_someone(paths: list[Path]) -> set[Path]:
    """*paths* のうち、いま生きているプロセスが開いているもの (Linux の ``/proc``)。

    ``/proc`` が無い OS では空を返す — そのときは書き続けているかどうかだけで見る。
    """
    # /proc gives the real, absolute path; a relative or symlinked mirrors root
    # would otherwise never match and the check would be silently off.
    wanted = {os.path.realpath(p): p for p in paths}
    found: set[Path] = set()
    try:
        procs = list(Path("/proc").glob("[0-9]*/fd"))
    except OSError:
        return found
    for fd_dir in procs:
        try:
            fds = list(fd_dir.iterdir())
        except OSError:
            continue
        for fd in fds:
            try:
                link = os.readlink(fd)
            except OSError:
                continue
            if link in wanted:
                found.add(wanted[link])
    return found


def _abandoned(paths: list[Path]) -> list[Path]:
    """止めた git の書きかけだけを選ぶ — **誰かが書いているものは外す** (#1173)。

    自分の git はグループごと止めた後なので、もう書かない・開いていない。
    同じミラーで別の git (手で打った fetch など。c-lord の flock は持たない) が
    受信中の pack は、開かれているか、少し待つあいだに育つ。消すとその fetch が
    失敗するので残す (古くなれば ``_sweep_stale_garbage`` が拾う)。
    """
    if not paths:
        return []

    def _stamp(path: Path) -> tuple[int, int] | None:
        try:
            st = path.stat()
        except OSError:
            return None
        return (st.st_size, st.st_mtime_ns)

    first = {p: _stamp(p) for p in paths}
    time.sleep(_SETTLE_SECONDS)
    still = [p for p in paths if first[p] is not None and _stamp(p) == first[p]]
    busy = _open_by_someone(still)
    return [p for p in still if p not in busy]


def _garbage_files(target: Path) -> list[Path]:
    """殺された git が残しうる受信途中のファイル (``objects/pack/tmp_*``)。"""
    try:
        return [p for p in (target / "objects" / "pack").glob("tmp_*") if p.is_file()]
    except OSError:
        return []


def _remove(paths: list[Path]) -> int:
    import contextlib

    freed = 0
    for path in paths:
        with contextlib.suppress(OSError):
            size = path.stat().st_size
            path.unlink()
            freed += size
    return freed


class _FetchLock:
    """ミラー1個につき更新を1本に絞る flock。取れなければ ``held`` は False (待たない)。"""

    def __init__(self, target: Path) -> None:
        self._path = target / _FETCH_LOCK
        self._fh: Any = None
        self.held = False

    def __enter__(self) -> _FetchLock:
        import fcntl

        try:
            self._fh = open(self._path, "a")  # noqa: SIM115 — closed in __exit__
            fcntl.flock(self._fh, fcntl.LOCK_EX | fcntl.LOCK_NB)
            self.held = True
        except OSError:
            self.held = False
        return self

    def __exit__(self, *_exc: object) -> None:
        import contextlib

        if self._fh is not None:
            with contextlib.suppress(OSError):
                self._fh.close()


def _mark_timed_out(target: Path, when: float | None = None) -> None:
    import contextlib

    with contextlib.suppress(OSError):
        mark = target / FETCH_TIMED_OUT_MARK
        mark.touch()
        if when is not None:
            os.utime(mark, (when, when))


def _sweep_stale_garbage(target: Path) -> None:
    """以前に殺された fetch の書きかけを消す (#1134)。更新中なら触らない。"""
    with _FetchLock(target) as lock:
        if not lock.held:
            return
        now = time.time()
        stale: list[Path] = []
        newest = 0.0
        for path in _garbage_files(target):
            try:
                mtime = path.stat().st_mtime
            except OSError:
                continue
            if now - mtime > _STALE_GARBAGE_AGE:
                stale.append(path)
                newest = max(newest, mtime)
        if not stale:
            return
        freed = _remove(stale)
        logger.info(
            "git mirror: removed %d half-written pack(s) (%.1f GiB) left by killed fetches in %s",
            len(stale),
            freed / 2**30,
            target.name,
        )
        # 書きかけが残っていた = その時刻に fetch が殺された。次からは待たない。
        if not (target / FETCH_TIMED_OUT_MARK).exists():
            _mark_timed_out(target, newest)


def _fetch(target: Path, *, timeout: float) -> None:
    """ミラーを更新する。**誰かが更新中なら待たずに戻る。** Never raises."""
    with _FetchLock(target) as lock:
        if not lock.held:
            logger.info("git mirror: %s is being updated elsewhere — not waiting", target.name)
            return
        before = set(_garbage_files(target))
        result = _run(["git", "-C", str(target), "fetch", "--quiet"], timeout=timeout)
        if result is not None and result.returncode == _TIMED_OUT and _stopping.is_set():
            # bot の停止で止めた (#1174)。時間切れではないので印は付けない。書きかけは
            # 次の起動の古いゴミ掃除 (2時間) が拾う — 停止を1秒でも延ばさない。
            logger.info("git mirror: fetch of %s stopped with the bot", target.name)
        elif result is not None and result.returncode == _TIMED_OUT:
            # 新しくできた書きかけのうち、止めた git のものだけを消す。flock は
            # c-lord 同士の約束でしかなく、手で打った fetch も同じ場所に書く (#1173)。
            ours = _abandoned([p for p in _garbage_files(target) if p not in before])
            freed = _remove(ours)
            _mark_timed_out(target)
            logger.warning(
                "git mirror: fetch of %s timed out; removed %d half-written pack(s) "
                "(%.1f GiB); later clones use it as-is and it is refetched in the background",
                target.name,
                len(ours),
                freed / 2**30,
            )
        elif result is not None and result.returncode == 0:
            import contextlib

            with contextlib.suppress(OSError):
                (target / FETCH_TIMED_OUT_MARK).unlink()


def _start_background_fetch(target: Path) -> None:
    key = str(target)
    with _background_lock:
        running = _background.get(key)
        if running is not None and running.is_alive():
            return
        # 印を新しくしておく — 裏の fetch が失敗しても次の1時間は取り直さない。
        _mark_timed_out(target)
        thread = threading.Thread(
            target=_fetch,
            args=(target,),
            kwargs={"timeout": _BACKGROUND_FETCH_TIMEOUT},
            name=f"git-mirror-fetch-{target.name}",
            daemon=True,
        )
        _background[key] = thread
        thread.start()
    logger.info("git mirror: refetching %s in the background", target.name)


def wait_for_background_fetches(timeout: float | None = None) -> None:
    """裏の更新が終わるまで待つ (試験用)。"""
    with _background_lock:
        threads = list(_background.values())
    for thread in threads:
        thread.join(timeout)


def _refresh(target: Path) -> None:
    """既存ミラーの更新。**時間切れの前歴があればクローンを待たせない** (#1133)。"""
    _sweep_stale_garbage(target)
    mark = target / FETCH_TIMED_OUT_MARK
    try:
        age = time.time() - mark.stat().st_mtime
    except OSError:
        age = None
    if age is None:
        _fetch(target, timeout=_FETCH_TIMEOUT)
    elif age >= _BACKGROUND_RETRY_AFTER:
        _start_background_fetch(target)
    else:
        logger.info("git mirror: %s timed out %.0f min ago — using it as-is", target.name, age / 60)


def _write_readme(root: Path) -> None:
    import contextlib

    with contextlib.suppress(OSError):
        readme = root / "README.md"
        if not readme.exists():
            readme.write_text(_README, encoding="utf-8")


def ensure_mirror(mirrors_root: Path | str, origin: str) -> Path | None:
    """*origin* のミラーを用意して返す。**用意できなければ ``None``。**

    ``None`` は「ミラー無しで今までどおりクローンする」を意味するだけで、
    エラーではない。呼び出し側はこの戻り値を ``--reference-if-able`` に
    渡すか渡さないかにしか使わない。

    Never raises.
    """
    if not mirrors_enabled():
        return None

    try:
        root = Path(mirrors_root)
        target = root / mirror_name(origin)
        # 名前の時点で区切りを潰しているので届かないはずの経路だが、
        # 「消しすぎ」ではなく「書きすぎ」の側の最後の安全弁として残す。
        if target.resolve().parent != root.resolve():
            logger.warning("git mirror: refusing path outside the mirror dir: %s", target)
            return None
    except (OSError, ValueError):
        return None

    if target.is_dir():
        # 足すだけ。``--prune`` は付けない — upstream で消えたブランチの
        # オブジェクトを参照しているクローンが在りうる。
        _refresh(target)
        return target

    return _create_mirror(root, target, origin)


def _create_mirror(root: Path, target: Path, origin: str) -> Path | None:
    """ミラーを作る。**半端なものを絶対に残さない。**

    一時ディレクトリに clone してから rename するのは、オブジェクトが
    欠けたミラーを誰かが参照する瞬間を作らないため。rename は同一
    ファイルシステム内で atomic なので、``target`` は「存在しない」か
    「完全」かのどちらかしか取りえない。
    """
    try:
        root.mkdir(parents=True, exist_ok=True)
    except OSError as exc:
        logger.warning("git mirror: cannot create %s: %s", root, exc)
        return None
    _write_readme(root)

    staging = root / f".building-{uuid.uuid4().hex}.git"
    # ``--`` で flag 形状の origin を git のオプションとして読ませない (#514)。
    result = _run(
        ["git", "clone", "--mirror", "--quiet", "--", origin, str(staging)],
        timeout=_CLONE_TIMEOUT,
    )
    if result is None or result.returncode != 0:
        detail = (result.stderr.strip() if result else "timed out")[:200]
        logger.info("git mirror: not mirroring %s (%s) — cloning without it", origin, detail)
        _discard(staging)
        return None

    # 自動 gc を止める。ミラーは参照されている側なので、オブジェクトを
    # 刈られると参照しているクローンが壊れる。足すだけの置き場にする。
    _run(["git", "-C", str(staging), "config", "gc.auto", "0"], timeout=30.0)

    try:
        staging.rename(target)
    except OSError:
        # 別のプロセスが先に作り終えていた場合もここに来る。相手のものが
        # 完全なら、こちらは捨てて相手を使えばよい。
        _discard(staging)
        return target if target.is_dir() else None

    logger.info("git mirror: created %s for %s", target.name, origin)
    return target


def _discard(path: Path) -> None:
    import contextlib

    with contextlib.suppress(OSError):
        shutil.rmtree(path)
