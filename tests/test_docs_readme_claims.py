"""README が、動いていない並行セッションの仕組みを約束していないこと (#758)。

README の1行目と「The Big Idea」は、並行セッションが **git worktree** で分かれ、
**AI Lounge / concurrency notice を毎ターン注入されて** 互いに調整する、と約束していた。
実際には worktree は git clone に置き換わり、注入のほうは #53 (tmux TUI 化) で
``--append-system-prompt`` を渡せなくなってから一度も Claude に届いていない
(``lounge_messages`` 0 行)。``tests/test_lounge.py`` は切れている場所より下の
レイヤーしか見ていなかったので、4ヶ月誰も気づかなかった。

ここでは Issue の再現コマンドと AC を、翻訳版を含めてそのままテストにしておく。
"""

from __future__ import annotations

from pathlib import Path

import pytest

from c_lord.cogs import _run_helper

ROOT = Path(__file__).resolve().parent.parent
LANGS = ("ja", "zh-CN", "ko", "es", "pt-BR", "fr")
READMES = [ROOT / "README.md", *(ROOT / "docs" / lang / "README.md" for lang in LANGS)]


def _ids(paths: list[Path]) -> list[str]:
    return [str(p.relative_to(ROOT)) for p in paths]


@pytest.mark.parametrize("readme", READMES, ids=_ids(READMES))
def test_readme_does_not_claim_append_system_prompt(readme: Path) -> None:
    """AC2 / AC6: 「via ``--append-system-prompt``」で注入している、と書いていない。"""
    assert "--append-system-prompt" not in readme.read_text(encoding="utf-8")


@pytest.mark.parametrize("readme", READMES, ids=_ids(READMES))
def test_readme_does_not_describe_worktrees(readme: Path) -> None:
    """AC1 / AC6: セッションは git worktree ではなく git clone で分かれている。

    ``/worktree-list`` / ``/worktree-cleanup`` / ``WORKTREE_BASE_DIR`` /
    ``worktree.py`` もコードに存在しないので、README に worktree の出番は無い。
    """
    assert "worktree" not in readme.read_text(encoding="utf-8").lower()


@pytest.mark.parametrize("readme", READMES, ids=_ids(READMES))
def test_readme_lounge_mentions_say_it_is_not_delivered(readme: Path) -> None:
    """AC2 / AC6: lounge に触れるなら、いまは Claude に届いていない (#758) と書いてある。"""
    text = readme.read_text(encoding="utf-8")
    if "lounge" in text.lower():
        assert "#758" in text


@pytest.mark.parametrize("readme", READMES, ids=_ids(READMES))
def test_readme_does_not_document_unread_coordination_env(readme: Path) -> None:
    """AC4a: コードのどこも読まない ``CLORD_COORDINATION_CHANNEL_NAME`` を案内しない。"""
    assert "CLORD_COORDINATION_CHANNEL_NAME" not in readme.read_text(encoding="utf-8")


def test_readme_big_idea_does_not_promise_what_nothing_enforces() -> None:
    """AC4a: 根拠が無くなった締めの一文を残さない。"""
    assert "No race conditions" not in (ROOT / "README.md").read_text(encoding="utf-8")


def test_run_helper_docstring_does_not_claim_injection() -> None:
    """AC3: モジュール docstring が「注入していない」というコメントと矛盾しない。"""
    doc = _run_helper.__doc__ or ""
    assert "via --append-system-prompt" not in doc
    assert "not delivered" in doc


def test_commands_lounge_rows_say_nobody_posts() -> None:
    """AC4: ``/api/lounge`` の行に、いま投稿する主体がいないことが分かる注記がある。"""
    lines = (ROOT / "docs" / "COMMANDS.md").read_text(encoding="utf-8").splitlines()
    rows = [line for line in lines if "/api/lounge" in line and line.startswith("|")]
    assert rows
    assert all("#758" in row for row in rows), rows
