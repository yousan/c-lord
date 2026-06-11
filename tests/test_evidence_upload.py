"""Tests for the dev-only evidence uploader (Issue #390).

Exercises ``scripts/evidence_upload.py`` — a standalone dev tool that is
intentionally NOT part of the importable ``c_lord`` package. Loaded by file path
so the test never depends on the runtime package layout. The pure
name/URL/markdown helpers are covered directly; the ``gh`` shell-out is covered
with ``_run`` monkeypatched so no network/CLI is touched.
"""

from __future__ import annotations

import importlib.util
import subprocess
from pathlib import Path

import pytest

_SCRIPT_PATH = Path(__file__).resolve().parent.parent / "scripts" / "evidence_upload.py"


def _load():
    spec = importlib.util.spec_from_file_location("evidence_upload", _SCRIPT_PATH)
    assert spec is not None and spec.loader is not None
    module = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(module)
    return module


mod = _load()


def test_sanitize_strips_unsafe_chars():
    assert mod.sanitize("red shot!") == "red-shot"
    assert mod.sanitize("a/b\\c") == "a-b-c"
    assert mod.sanitize("--__.") == "evidence"  # all stripped -> fallback


def test_asset_name_basic():
    assert (
        mod.asset_name(390, "red", "screenshot", "20260611T124500Z")
        == "i390-red-screenshot-20260611T124500Z.png"
    )


def test_asset_name_empty_label_omitted():
    assert mod.asset_name(390, "", "green", "20260611T124500Z") == "i390-green-20260611T124500Z.png"


def test_asset_name_dedupes_label_equal_stem():
    # label == stem must not appear twice
    assert mod.asset_name(7, "red", "red", "TS") == "i7-red-TS.png"


def test_asset_name_index_suffix_disambiguates():
    a = mod.asset_name(1, "", "shot", "TS", idx=0)
    b = mod.asset_name(1, "", "shot", "TS", idx=1)
    assert a != b
    assert a.endswith("-00.png") and b.endswith("-01.png")


def test_download_url_is_deterministic_release_asset_url():
    assert (
        mod.download_url("yousan/c-lord", "evidence", "i390-red-TS.png")
        == "https://github.com/yousan/c-lord/releases/download/evidence/i390-red-TS.png"
    )


def test_markdown_for():
    assert mod.markdown_for("red", "http://x/y.png") == "![red](http://x/y.png)"


def test_ensure_release_skips_when_release_exists(monkeypatch):
    calls: list[list[str]] = []

    def fake_run(cmd):
        calls.append(cmd)
        return subprocess.CompletedProcess(cmd, 0, stdout='{"tagName":"evidence"}', stderr="")

    monkeypatch.setattr(mod, "_run", fake_run)
    mod.ensure_release("yousan/c-lord", "evidence")
    # only the `release view` probe ran; no `release create`
    assert len(calls) == 1
    assert calls[0][:3] == ["gh", "release", "view"]


def test_ensure_release_creates_when_absent(monkeypatch):
    calls: list[list[str]] = []

    def fake_run(cmd):
        calls.append(cmd)
        # view fails (absent) -> create succeeds
        rc = 1 if cmd[2] == "view" else 0
        return subprocess.CompletedProcess(cmd, rc, stdout="", stderr="not found")

    monkeypatch.setattr(mod, "_run", fake_run)
    mod.ensure_release("yousan/c-lord", "evidence")
    assert [c[2] for c in calls] == ["view", "create"]
    create = calls[1]
    assert "--prerelease" in create  # must be a prerelease so upgrades ignore it


def test_upload_returns_url_and_clobbers(monkeypatch, tmp_path):
    seen: list[list[str]] = []

    def fake_run(cmd):
        seen.append(cmd)
        return subprocess.CompletedProcess(cmd, 0, stdout="", stderr="")

    monkeypatch.setattr(mod, "_run", fake_run)
    png = tmp_path / "red.png"
    png.write_bytes(b"\x89PNG\r\n")
    url = mod.upload("yousan/c-lord", "evidence", png, "i390-red-TS.png")
    assert url == "https://github.com/yousan/c-lord/releases/download/evidence/i390-red-TS.png"
    assert seen and "--clobber" in seen[0]


def test_upload_dies_on_gh_failure(monkeypatch, tmp_path):
    def fake_run(cmd):
        return subprocess.CompletedProcess(cmd, 1, stdout="", stderr="boom")

    monkeypatch.setattr(mod, "_run", fake_run)
    png = tmp_path / "red.png"
    png.write_bytes(b"x")
    with pytest.raises(SystemExit):
        mod.upload("yousan/c-lord", "evidence", png, "n.png")
