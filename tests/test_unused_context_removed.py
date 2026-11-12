"""毎ターン組み立てて捨てていた文字列が、もう組み立てられていないこと (#766)。

#758 AC5 で AI Lounge / concurrency notice は「配線し直さない」と決めた。
組み立て続けると、ログの「Concurrency notice built」とコードが「注入されている」
と読み違えさせる（#758 が4ヶ月見つからなかった理由）。ここでは Issue の再現
コマンド（``grep``）をそのままテストにしておく。
"""

from __future__ import annotations

from pathlib import Path

from c_lord.concurrency import SessionRegistry

PACKAGE = Path(__file__).resolve().parent.parent / "c_lord"


def _grep(needle: str) -> list[str]:
    hits = []
    for path in PACKAGE.rglob("*.py"):
        for no, line in enumerate(path.read_text(encoding="utf-8").splitlines(), 1):
            if needle in line:
                hits.append(f"{path.relative_to(PACKAGE)}:{no}: {line.strip()}")
    return hits


def test_lounge_prompt_builder_is_gone() -> None:
    """AC1: ``grep -rn build_lounge_prompt c_lord/`` が 0 件。"""
    assert _grep("build_lounge_prompt") == []


def test_concurrency_notice_builder_is_gone() -> None:
    """AC1: ``grep -rn build_concurrency_notice c_lord/`` が 0 件。"""
    assert _grep("build_concurrency_notice") == []
    assert not hasattr(SessionRegistry, "build_concurrency_notice")


def test_no_notice_built_log_line() -> None:
    """AC3: ``grep -rn "Concurrency notice built" c_lord/`` が 0 件。"""
    assert _grep("Concurrency notice built") == []
