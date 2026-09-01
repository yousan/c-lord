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
"""

from __future__ import annotations

import hashlib
import logging
import os
import re
import shutil
import subprocess
import uuid
from pathlib import Path

logger = logging.getLogger(__name__)

__all__ = [
    "MIRRORS_DIR_NAME",
    "ensure_mirror",
    "mirror_name",
    "mirrors_enabled",
    "mirrors_root_for",
    "normalise_origin",
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


def _run(args: list[str], *, timeout: float) -> subprocess.CompletedProcess[str] | None:
    """Never raises. タイムアウトも失敗も ``None`` / 非ゼロで返る。"""
    try:
        return subprocess.run(args, capture_output=True, text=True, timeout=timeout)
    except (subprocess.TimeoutExpired, OSError) as exc:
        logger.warning("git mirror: %s failed: %s", " ".join(args[:3]), exc)
        return None


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
        _run(["git", "-C", str(target), "fetch", "--quiet"], timeout=_FETCH_TIMEOUT)
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
