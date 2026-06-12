"""Unit tests for scripts.monitor.state — incremental scan state (#404)."""

from __future__ import annotations

from pathlib import Path

from scripts.monitor.state import MonitorState, load_state, read_new_log_text, save_state


def test_state_roundtrip(tmp_path: Path) -> None:
    p = tmp_path / "state.json"
    save_state(p, MonitorState(offsets={"/a.log": 42}, seen=["fp1", "fp2"]))
    s = load_state(p)
    assert s.offsets == {"/a.log": 42}
    assert s.seen_set == {"fp1", "fp2"}


def test_load_missing_returns_empty(tmp_path: Path) -> None:
    s = load_state(tmp_path / "nope.json")
    assert s.offsets == {} and s.seen == []


def test_read_new_from_offset(tmp_path: Path) -> None:
    log = tmp_path / "bot.log"
    log.write_text("line1\nline2\n")
    first, off = read_new_log_text(log, 0)
    assert first == "line1\nline2\n"
    log.write_text("line1\nline2\nline3\n")
    nxt, off2 = read_new_log_text(log, off)
    assert nxt == "line3\n"
    assert off2 > off


def test_truncation_reads_from_start(tmp_path: Path) -> None:
    log = tmp_path / "bot.log"
    log.write_text("a much longer original content\n")
    _, off = read_new_log_text(log, 0)
    log.write_text("short\n")  # rotated/truncated: smaller than recorded offset
    text, _ = read_new_log_text(log, off)
    assert text == "short\n"


def test_missing_file_is_empty(tmp_path: Path) -> None:
    text, off = read_new_log_text(tmp_path / "absent.log", 99)
    assert text == "" and off == 0
