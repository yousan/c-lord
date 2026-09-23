"""CONTRIBUTING.md のとおりに PR を出せば dod-gate を通れること — Issue #760.

`dod-gate` は ``main`` の必須チェックで、PR 本文とラベルを読んで落とす
（``.github/scripts/dod_gate.js``）。ところが CONTRIBUTING.md は「CI が通って
レビューされればマージ」と書いたまま 2026-02 から止まっていて、DoD も証跡も
dod-gate も一言も出てこなかった。外から来た人が最初に踏む壁になる。

文章の言い回しではなく **dod-gate 本体を根拠に** 検査する: ゲートが要求する
見出しと免除ラベルは ``dod_gate.js`` から読み出すので、ゲートに条件が増えたのに
CONTRIBUTING が追従していなければ、このテストが落ちる。
"""

from __future__ import annotations

import re
from pathlib import Path

import pytest

ROOT = Path(__file__).resolve().parent.parent
GATE = ROOT / ".github" / "scripts" / "dod_gate.js"
CLAUDE_MD = ROOT / "CLAUDE.md"

#: 英語版が正、日本語版はその翻訳（AC5: 両方を同じ状態にする）。
CONTRIBUTING = ("CONTRIBUTING.md", "docs/ja/CONTRIBUTING.md")

#: 証跡の作り方（AC4）。
EVIDENCE_TOOLS = ("scripts/discord_evidence_shot.sh", "scripts/evidence_upload.py")

#: 「CI が通ればマージ」— DoD と理念が名指しで否定している考え方（AC1）。
STALE_MERGE_RULE = {
    "CONTRIBUTING.md": "Once CI passes and the PR is reviewed",
    "docs/ja/CONTRIBUTING.md": "CI が通過しレビューされたら",
}

_LINK = re.compile(r"\]\(([^)\s]+)\)")


def _read(rel: str) -> str:
    return (ROOT / rel).read_text(encoding="utf-8")


def _gate_sections() -> list[str]:
    """``## <heading>`` sections dod_gate.js looks for in the PR body."""
    return re.findall(r"section\('([^']+)'\)", GATE.read_text(encoding="utf-8"))


def _gate_exempt_labels() -> list[str]:
    return re.findall(r"lower\.includes\('([^']+)'\)", GATE.read_text(encoding="utf-8"))


def _slug(heading: str) -> str:
    """GitHub's heading anchor: lower-case, punctuation dropped, spaces → ``-``."""
    return re.sub(r"[^\w\- ]", "", heading.strip().lower()).replace(" ", "-")


def _anchors(markdown: str) -> set[str]:
    return {_slug(h) for h in re.findall(r"^#{1,6} (.+)$", markdown, re.MULTILINE)}


def _dod_anchor() -> str:
    heading = re.search(r"^## (Definition of Done.*)$", _read("CLAUDE.md"), re.MULTILINE)
    assert heading, "CLAUDE.md に Definition of Done の節が無い"
    return _slug(heading.group(1))


def test_gate_is_what_this_test_reads() -> None:
    """前提: dod_gate.js から見出しと免除ラベルが読み出せる（読めなければ検査が空になる）。"""
    assert "Definition of Done checklist" in _gate_sections()
    assert "Acceptance Criteria" in _gate_sections()
    assert {"documentation", "no-runtime-change"} <= set(_gate_exempt_labels())


@pytest.mark.parametrize("path", CONTRIBUTING)
def test_merge_rule_is_dod_not_green_ci(path: str) -> None:
    """AC1/AC2: 「CI が通ればマージ」ではなく、DoD へのリンクがある。"""
    doc = _read(path)
    assert STALE_MERGE_RULE[path] not in doc, f"{path}: 「CI が通ればマージ」が残っている (#760)"
    assert f"CLAUDE.md#{_dod_anchor()}" in doc, (
        f"{path}: CLAUDE.md の Definition of Done（#{_dod_anchor()}）へのリンクが無い (#760)"
    )


@pytest.mark.parametrize("path", CONTRIBUTING)
def test_dod_gate_conditions_are_written(path: str) -> None:
    """AC3: dod-gate が必須チェックであること・落ちる条件・免除ラベルが書いてある。"""
    doc = _read(path)
    missing = [w for w in ("dod-gate", "Closes", "Refs") if w not in doc]
    missing += [f"## {s}" for s in _gate_sections() if f"## {s}" not in doc]
    missing += [f"`{label}`" for label in _gate_exempt_labels() if f"`{label}`" not in doc]
    assert not missing, f"{path}: dod-gate の条件の説明に足りない語: {missing} (#760)"


@pytest.mark.parametrize("path", CONTRIBUTING)
def test_evidence_tooling_is_linked(path: str) -> None:
    """AC4: 証跡の作り方（撮る → アップロード）が書いてある。"""
    doc = _read(path)
    missing = [t for t in EVIDENCE_TOOLS if t not in doc]
    assert not missing, f"{path}: 証跡の作り方が書かれていない: {missing} (#760)"
    for tool in EVIDENCE_TOOLS:
        assert (ROOT / tool).exists(), f"{tool} が存在しない — CONTRIBUTING の案内が死んでいる"


@pytest.mark.parametrize("path", CONTRIBUTING)
def test_relative_links_resolve(path: str) -> None:
    """書いたリンク（ファイル・見出しアンカー）が実在すること。"""
    src = ROOT / path
    # Code (``![...](URL)`` as an example of what the gate accepts) is not a link.
    doc = re.sub(r"```.*?```|`[^`\n]*`", "", src.read_text(encoding="utf-8"), flags=re.S)
    broken = []
    for target in _LINK.findall(doc):
        if re.match(r"[a-z]+:", target):  # http(s):, mailto:
            continue
        file_part, _, anchor = target.partition("#")
        dest = (src.parent / file_part).resolve() if file_part else src
        missing_anchor = (
            dest.exists()
            and bool(anchor)
            and dest.suffix == ".md"
            and anchor not in _anchors(dest.read_text("utf-8"))
        )
        if not dest.exists() or missing_anchor:
            broken.append(target)
    assert not broken, f"{path}: 切れているリンク: {broken}"
