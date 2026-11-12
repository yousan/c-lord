"""ランプの説明が実際に出るものと食い違わないこと — Issue #753.

利用者ガイドの「Status Indicators」は、利用者が Discord で見る絵文字の意味を教える表。
そこに **一度も出たことがない 🗜️** が載り、**実際に出る ⏳** が抜けていた。さらに
ガイド・ARCHITECTURE・CLAUDE.md・README が揃って「スレッド名にも 🟢/🟡 が出る」と
書いていたが、スレッド名ランプは #329 から既定オフ（``CLORD_THREAD_LAMP=1`` で有効）。

#723（「tool-use embed が出る」と書いてあったが 0 件）と同じ形の drift なので、
文章ではなく **コードを根拠に** 検査する:

- 表に載せてよい絵文字 = :mod:`c_lord.discord_ui.status` が持つランプだけ。🗜️ を塗る
  ``set_compact()`` は ``StreamEvent.is_compact`` をセットするコードが無く一度も
  呼ばれなかったので、#1316 で定数ごと消した。ランプを増やした人は ``EMOJI_*`` を
  足すことになり、このテストが利用者ガイドの表の更新を求める。
- スレッド名のランプに触れる段落は、既定オフ／有効化の方法まで書くこと。
"""

from __future__ import annotations

import re
from pathlib import Path

import pytest

from c_lord.discord_ui import status
from c_lord.discord_ui.status import (
    EMOJI_ERROR,
    EMOJI_RUNNING,
    EMOJI_STALL_HARD,
    EMOJI_STALL_SOFT,
    EMOJI_WAITING,
)

ROOT = Path(__file__).resolve().parent.parent

#: 文脈圧縮の目印。ランプ（リアクション）としては存在しない — スレッドに出る 1 行 (#628) の絵文字。
COMPACT_EMOJI = "\U0001f5dc\ufe0f"

USER_GUIDES = {
    "docs/USER_GUIDE.md": r"^### Status Indicators",
    "docs/ja/USER_GUIDE.md": r"^### ステータス表示",
}

#: 「スレッド名のランプ」を説明している文書（#753 の該当箇所 + README）。
LAMP_DOCS = (
    "docs/USER_GUIDE.md",
    "docs/ja/USER_GUIDE.md",
    "docs/ARCHITECTURE.md",
    "CLAUDE.md",
    "README.md",
)

#: 「スレッド名のランプ」への言及。"thread renames" / 「スレッド名変更」（レート制限の話）は
#: ランプの説明ではないので拾わない。
_THREAD_NAME_LAMP = re.compile(
    r"(?:thread[- ]name|スレッド名)[\s*の]{0,6}(?:lamp|ランプ|🟢)"
    r"|🟢/🟡[^.。]{0,20}(?:thread[- ]name|スレッド名)",
    re.IGNORECASE,
)


def _bare(emoji: str) -> str:
    """Drop the emoji variation selector so ``⚠`` and ``⚠️`` compare equal."""
    return emoji.replace("\ufe0f", "")


def _reachable_lamps() -> set[str]:
    """Every lamp the status module defines — nothing else can be painted."""
    return {_bare(v) for k, v in vars(status).items() if k.startswith("EMOJI_")}


def _section(doc: str, heading: str) -> str:
    start = re.search(heading, doc, re.MULTILINE)
    assert start is not None, f"見出し {heading!r} が無い"
    rest = doc[start.end() :]
    end = re.search(r"^#{2,3} ", rest, re.MULTILINE)
    return rest[: end.start()] if end else rest


def _table_emojis(section: str) -> set[str]:
    """First cell of every body row of the first markdown table in ``section``."""
    cells = []
    for line in section.splitlines():
        m = re.match(r"^\|\s*([^|]+?)\s*\|", line)
        if m and not set(m.group(1)) <= set("-: "):
            cells.append(m.group(1))
    return {_bare(c) for c in cells[1:]}  # cells[0] is the header ("Emoji" / "絵文字")


def _paragraphs(text: str) -> list[str]:
    """Prose paragraphs, list items and table rows — each checked on its own.

    Splitting on blank lines alone would make CLAUDE.md's whole Key Design
    Decisions list one "paragraph", so a single compliant item would excuse
    every other item in it.
    """
    return [p for p in re.split(r"\n\s*\n|\n(?=\s*(?:[-*]|\d+\.)\s|\|)", text) if p.strip()]


def test_the_lamps_are_the_five_the_guide_explains() -> None:
    """前提の確認: 塗れるランプは 🟢🟡❌⏳⚠️ の 5 つで、🗜️ は無い (#753/#1316)。

    これが落ちたらランプが増減している — 利用者ガイドの表（下のテスト）も合わせること。
    """
    expected = {EMOJI_RUNNING, EMOJI_WAITING, EMOJI_ERROR, EMOJI_STALL_SOFT, EMOJI_STALL_HARD}
    assert _reachable_lamps() == {_bare(e) for e in expected}
    assert _bare(COMPACT_EMOJI) not in _reachable_lamps()
    assert not hasattr(status.StatusManager, "set_compact")


@pytest.mark.parametrize("path", sorted(USER_GUIDES))
def test_user_guide_lists_exactly_the_lamps_that_can_appear(path: str) -> None:
    doc = (ROOT / path).read_text(encoding="utf-8")
    listed = _table_emojis(_section(doc, USER_GUIDES[path]))
    reachable = _reachable_lamps()
    assert listed == reachable, (
        f"{path} (#753): 実際には付かない絵文字が表にある: {sorted(listed - reachable)} / "
        f"実際に付く絵文字が表に無い: {sorted(reachable - listed)}"
    )


@pytest.mark.parametrize("path", LAMP_DOCS)
def test_thread_name_lamp_is_described_as_opt_in(path: str) -> None:
    """スレッド名のランプに触れる段落は、既定オフと有効化の方法を書く (#329/#753)。"""
    doc = (ROOT / path).read_text(encoding="utf-8")
    offenders = [
        p.strip()[:120]
        for p in _paragraphs(doc)
        if _THREAD_NAME_LAMP.search(p) and "CLORD_THREAD_LAMP" not in p
    ]
    assert not offenders, (
        f"{path}: スレッド名のランプを説明しているのに、既定オフ（#329）と "
        f"`CLORD_THREAD_LAMP=1` での有効化が書かれていない段落: {offenders}"
    )


@pytest.mark.parametrize("path", LAMP_DOCS)
def test_compact_emoji_is_not_described_as_a_live_lamp(path: str) -> None:
    """🗜️ に触れる段落は「到達しない」か、生きているミラーの1行 (#628) を指す (#753)。"""
    doc = (ROOT / path).read_text(encoding="utf-8")
    offenders = [
        p.strip()[:120]
        for p in _paragraphs(doc)
        if _bare(COMPACT_EMOJI) in _bare(p) and not re.search(r"unreachable|到達しない|#628", p)
    ]
    assert not offenders, f"{path}: 🗜️ を出る目印として書いている段落: {offenders}"
