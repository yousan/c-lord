"""Where c-lord puts what it creates by itself — Issue #837.

c-lord の**生成物**（clone 本体ではなく、c-lord が勝手に作るもの）の既定の置き場を
``~/.c-lord/<instance>/`` に揃える。以前は ``SESSION_DIR_BASE`` の既定が cwd 相対の
``data/sessions`` で、実運用では各 ``.env`` に絶対パスを手書きしていた。その結果、
本番＋staging×4 の1台で ``~`` 直下に c-lord の置き場が 15 個以上並び、どれが本番で
どれが消してよいのかを ls から判断できなかった。

``<instance>`` は ``$CLORD_INSTANCE``、書かなければ clone のディレクトリ名。
ディレクトリ名だけに頼らないのは、clone を改名すると置き場が変わって既存スレッドの
``--resume``（transcript は cwd のパスに紐付く）が切れ、別の場所に同名の clone を
置くと同じ置き場を取り合うため（2026-09-30 合意）。

**既存インスタンスは動かさない**（Zero-Config）: ``SESSION_DIR_BASE`` を書いている
インスタンスはそのまま、書かずに ``./data/sessions`` を使ってきたインスタンスも、
そのディレクトリがある限りそちらを使い続ける。作業ディレクトリを移すと transcript
との紐付けが切れるため、移行はこのモジュールの仕事ではない。

``$CLORD_INSTANCE_ID``（#790、tmux の窓の持ち主）とは別物。あちらは同じ cwd から
2つ起動する特殊構成のための名前で、既定は cwd の実パス。
"""

from __future__ import annotations

import logging
import os
import re
from collections.abc import Mapping
from pathlib import Path

logger = logging.getLogger(__name__)

INSTANCE_ENV = "CLORD_INSTANCE"
# The cwd-relative default every instance used before #837.
LEGACY_SESSION_DIR_BASE = "data/sessions"
# A single, ordinary path component: it is joined under ~/.c-lord and must not
# be able to climb out of it ("..", "a/b") or smuggle in anything odd.
_SAFE_NAME = re.compile(r"^[A-Za-z0-9][A-Za-z0-9._-]*$")
_FALLBACK_NAME = "c-lord"


def clord_home() -> Path:
    """``~/.c-lord`` — the one directory c-lord's own output lives under."""
    return Path.home() / ".c-lord"


def instance_name(env: Mapping[str, str] | None = None, cwd: str | None = None) -> str:
    """This instance's name: ``$CLORD_INSTANCE``, else the clone directory's name."""
    env = os.environ if env is None else env
    explicit = env.get(INSTANCE_ENV, "").strip()
    if explicit:
        if _SAFE_NAME.match(explicit):
            return explicit
        logger.warning(
            "%s=%r is not a plain directory name (letters, digits, '.', '_', '-') "
            "— ignoring it and using the clone directory name (#837)",
            INSTANCE_ENV,
            explicit,
        )
    name = Path(cwd if cwd is not None else os.getcwd()).name
    return name if _SAFE_NAME.match(name) else _FALLBACK_NAME


def default_session_dir_base(
    env: Mapping[str, str] | None = None, cwd: str | None = None
) -> str | None:
    """Session-dir base for an instance whose ``.env`` does not name one.

    ``~/.c-lord/<instance>/sessions`` for a new install. ``None`` when
    ``./data/sessions`` already exists — that instance keeps exactly the
    behaviour it had before #837, so none of its threads lose their workspace.
    """
    here = Path(cwd if cwd is not None else os.getcwd())
    if (here / LEGACY_SESSION_DIR_BASE).is_dir():
        return None
    return str(clord_home() / instance_name(env, str(here)) / "sessions")
