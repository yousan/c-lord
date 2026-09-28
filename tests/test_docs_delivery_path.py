"""理念と周辺ドキュメントが、削除済みの配信経路を前提にしていないこと (#759)。

#712 で「Claude が Skill 経由で ``POST /api/reply`` する」経路 (経路A) は削除され、
Claude Code 自身の jsonl を c-lord が読んで転送するミラーが唯一の配信経路になった。
ところが「迷ったらここに照らす」と指定された ``docs/PHILOSOPHY.md`` は経路A を前提に
書かれたまま残り、証跡の主従 (#243/#350) も逆のままだった。

ドキュメントの drift は CI では見つからない (#758 と同じ隠れ方) ので、Issue の再現
コマンドをそのままテストにしておく。
"""

from __future__ import annotations

from pathlib import Path

DOCS = Path(__file__).resolve().parent.parent / "docs"
PHILOSOPHY = DOCS / "PHILOSOPHY.md"


def _philosophy() -> str:
    return PHILOSOPHY.read_text(encoding="utf-8")


def test_philosophy_does_not_describe_skill_reply_path() -> None:
    """AC1: 理念から ``POST /api/reply`` / Skill 経由の配信が消えている。"""
    text = _philosophy()
    assert "api/reply" not in text
    assert "Skill 経由で自分の最終回答" not in text


def test_philosophy_names_jsonl_mirror_as_the_only_delivery_path() -> None:
    """AC1: jsonl ミラーが唯一の配信経路だと書いてある (CLAUDE.md 決定1 と一致)。"""
    text = _philosophy()
    assert "jsonl" in text
    assert "唯一の配信経路" in text


def test_philosophy_evidence_is_taken_by_ai_first() -> None:
    """AC2: 証跡は AI 自身が撮るのが主・人間提供は補助 (#243/#350)。"""
    text = _philosophy()
    assert "ユーザー提供のスクリーンショットが主" not in text
    assert "ユーザースクショが主" not in text
    assert "scripts/discord_evidence_shot.sh" in text


def test_no_doc_describes_api_reply_as_the_final_answer_path() -> None:
    """AC3: ``/api/reply`` を「最終回答の出口」として説明している箇所が無い。

    エンドポイント一覧や経緯 (DESIGN_DECISIONS の決定記録など) としての記載は
    残してよいので、「最終回答」と同じ行で語っている箇所だけを拾う。
    """
    offenders: list[str] = []
    for path in sorted(DOCS.rglob("*.md")):
        for lineno, line in enumerate(path.read_text(encoding="utf-8").splitlines(), 1):
            if "api/reply" in line and "最終回答" in line:
                offenders.append(f"{path.relative_to(DOCS.parent)}:{lineno}: {line.strip()}")
    assert not offenders, "削除済みの配信経路を最終回答の出口として説明している:\n" + "\n".join(
        offenders
    )
