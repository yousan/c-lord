"""dod-gate のロジック (.github/scripts/dod_gate.js) を node 経由で検証する。

dod-gate は GitHub Actions の必須チェックで、全 PR のマージを止める。ロジックを
壊すと全 PR がブロック（誤検知）または穴が残る（見逃し）ため、決定論的にテストする。
node を subprocess 起動し、GITHUB_EVENT_PATH に PR イベント JSON を渡して exit code を見る。
"""

from __future__ import annotations

import json
import os
import shutil
import subprocess
from pathlib import Path

import pytest

SCRIPT = Path(__file__).resolve().parents[1] / ".github" / "scripts" / "dod_gate.js"

# node が無い環境（ローカルのみ）では skip。CI の ubuntu-latest には node が入っている。
pytestmark = pytest.mark.skipif(shutil.which("node") is None, reason="node not available")

# 全項目チェック済みの DoD セクション（Rule 1 を満たす）。
_DOD_OK = (
    "## Definition of Done checklist\n\n"
    "- [x] Every Acceptance Criterion above is checked\n"
    "- [x] No unrelated changes\n"
    "- [x] Closes discipline\n"
)


def _run_gate(body: str, labels: list[str], tmp_path: Path) -> subprocess.CompletedProcess[str]:
    event = {"pull_request": {"body": body, "labels": [{"name": n} for n in labels]}}
    event_path = tmp_path / "event.json"
    event_path.write_text(json.dumps(event), encoding="utf-8")
    return subprocess.run(
        ["node", str(SCRIPT)],
        env={**os.environ, "GITHUB_EVENT_PATH": str(event_path)},
        capture_output=True,
        text=True,
    )


def test_proper_ac_heading_all_checked_with_closes_passes(tmp_path: Path) -> None:
    body = "## Acceptance Criteria\n\n- [x] AC1\n\n" + _DOD_OK + "\nCloses #1\n"
    result = _run_gate(body, [], tmp_path)
    assert result.returncode == 0, result.stderr


def test_h3_ac_heading_with_closes_is_fail_closed(tmp_path: Path) -> None:
    # PR #265 のバイパス再現: AC が h3 見出し (### AC) だと '## Acceptance Criteria'
    # セクションとして認識されず、AC 検証がゼロで Closes が素通りしていた。
    # 修正後は「Closes 使用 + AC セクション不在」を fail-closed で落とすこと。
    body = "### AC (Issue #250)\n\n- [x] AC1\n\n" + _DOD_OK + "\nCloses #250\n"
    result = _run_gate(body, [], tmp_path)
    assert result.returncode == 1, (
        f"expected fail-closed (rc=1) for the #265 h3-heading bypass, got rc={result.returncode}\n"
        f"stdout={result.stdout!r} stderr={result.stderr!r}"
    )
    assert "Acceptance Criteria" in result.stderr


def test_unchecked_ac_with_closes_fails(tmp_path: Path) -> None:
    body = "## Acceptance Criteria\n\n- [ ] AC1\n\n" + _DOD_OK + "\nCloses #1\n"
    result = _run_gate(body, [], tmp_path)
    assert result.returncode == 1, result.stdout


def test_documentation_label_exempts_dod_completion(tmp_path: Path) -> None:
    # documentation ラベルは Rule 1（DoD 全チェック）を免除。Closes 無しなので Rule 2 も不発。
    body = (
        "## Acceptance Criteria\n\n- [x] AC1\n\n## Definition of Done checklist\n\n- [ ] not yet\n"
    )
    result = _run_gate(body, ["documentation"], tmp_path)
    assert result.returncode == 0, result.stderr


def test_missing_dod_section_fails(tmp_path: Path) -> None:
    body = "## Acceptance Criteria\n\n- [x] AC1\n\nbody without a DoD section\n"
    result = _run_gate(body, [], tmp_path)
    assert result.returncode == 1, result.stdout
