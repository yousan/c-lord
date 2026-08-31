"""配線されていることをテストで固定する — Issue #612.

**同じバグを2回踏んだ。** #570 は tmux リーパーが「定義されているのに誰からも
呼ばれていない」状態で、空ウィンドウが175本溜まっていた。#573 (PR #587) では
``DevEnvRepository`` と ``orphaned_containers()`` が同じ形で死んでいて、本番 DB に
``dev_environments`` テーブルすら作られていなかった。

どちらも単体テストは緑だった。**単体テストは「関数が正しく動く」ことしか見ない
ので、この class のバグを構造的に取りこぼす。** 呼び出し元の不在は、関数の
振る舞いには一切現れない。

だからここで見るのは振る舞いではなく **参照** — 定義元モジュールの外に、その
名前を使っている場所が1つ以上あるか。粗い判定だが、粗いからこそ「配線を
忘れた」という粗いミスに効く。

新しく「常に動いているべき」ものを足したら、**必ず下の表に足すこと**。
"""

from __future__ import annotations

import ast
from pathlib import Path

import pytest

PACKAGE = Path(__file__).resolve().parent.parent / "c_lord"

#: (シンボル, それを定義しているモジュール) — 定義元の外から参照されていること。
#:
#: 「動いていてほしい」ではなく「配線が無ければ機能が存在しないのと同じ」もの
#: だけを並べる。ヘルパーや型は対象外。
MUST_BE_WIRED = [
    # #570: これを忘れて空ウィンドウが175本溜まった
    ("cleanup_orphaned_all_sessions", "tmux.py"),
    # #574: 7日アイドルの自動停止
    ("IdleStopLoop", "idle_stop.py"),
    # #572: 4時間アイドルの自動スリープ
    #
    # ループが呼ぶ ``_sleep_workspace_impl`` はここでは見られない — ``get_cog`` +
    # ``getattr(cog, "...")`` の名前解決は AST 上ただの文字列で、参照には見えない
    # から。その繋がりは tests/test_idle_sleep.py の
    # ``test_the_cog_really_has_the_method_the_loop_looks_up`` が実物の
    # SessionManageCog に対して確かめる。
    ("IdleSleepLoop", "idle_sleep.py"),
    # #613: 行を失った作業ディレクトリの回収
    ("OrphanSweepLoop", "orphan_dirs.py"),
    # #612: #573 で配線を忘れた2つ
    ("DevEnvRepository", "database/devenv_repo.py"),
    ("orphaned_containers", "devenv.py"),
    # #575: 行を消すときにディレクトリも消す
    ("remove_clean_session_dir", "session_cleanup.py"),
]


def _referenced_names(path: Path) -> set[str]:
    """Every bare name and attribute this module mentions."""
    try:
        tree = ast.parse(path.read_text(encoding="utf-8"))
    except (OSError, SyntaxError):
        return set()
    names: set[str] = set()
    for node in ast.walk(tree):
        if isinstance(node, ast.Name):
            names.add(node.id)
        elif isinstance(node, ast.Attribute):
            names.add(node.attr)
        elif isinstance(node, ast.alias):
            names.add(node.name.rsplit(".", 1)[-1])
    return names


@pytest.mark.parametrize(("symbol", "defined_in"), MUST_BE_WIRED)
def test_symbol_is_referenced_outside_its_own_module(symbol: str, defined_in: str) -> None:
    home = (PACKAGE / defined_in).resolve()
    assert home.is_file(), f"{defined_in} does not exist — update MUST_BE_WIRED"

    callers = [
        path
        for path in PACKAGE.rglob("*.py")
        if path.resolve() != home and symbol in _referenced_names(path)
    ]

    assert callers, (
        f"{symbol} is defined in c_lord/{defined_in} but nothing in c_lord/ "
        f"references it. That is the #570 / #612 bug: the feature exists in the "
        f"source and does not exist in the running bot. Wire it up, or delete it."
    )
