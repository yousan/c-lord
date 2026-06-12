"""Unit tests for scripts.monitor.logscan — bot-log anomaly detection (#404).

The log scan is the monitor's highest-signal source: a traceback / ERROR line is
emitted exactly when a real user hits a bug. Parsing must extract the structured
``[thread=/session=]`` context and produce a *stable* fingerprint (so the same
recurring error dedups across runs despite changing timestamps / thread ids).
"""

from __future__ import annotations

from scripts.fuzz.oracle import fingerprint
from scripts.monitor.logscan import scan_log_text


def test_detects_traceback_with_thread_context() -> None:
    text = (
        "2026-06-12 10:00:00 [INFO] c_lord.bot: ok\n"
        "2026-06-12 10:00:01 [ERROR] c_lord.cogs.claude_chat: run failed "
        "[thread=12345 session=abcd1234]\n"
        "Traceback (most recent call last):\n"
        '  File "/x/claude_chat.py", line 10, in run\n'
        "    do()\n"
        "RuntimeError: boom happened\n"
        "2026-06-12 10:00:02 [INFO] c_lord.bot: continuing\n"
    )
    anoms = scan_log_text(text, source="staging-1.log")
    kinds = {a.kind for a in anoms}
    assert "LOG_TRACEBACK" in kinds
    assert "LOG_ERROR" in kinds
    tb = next(a for a in anoms if a.kind == "LOG_TRACEBACK")
    assert "RuntimeError: boom happened" in tb.evidence
    assert tb.fields.get("thread") == "12345"
    assert tb.fields.get("source") == "staging-1.log"


def test_clean_log_yields_no_anomaly() -> None:
    text = "2026-06-12 10:00:00 [INFO] c_lord.bot: ok\n... [WARNING] minor thing\n"
    assert scan_log_text(text) == []


def test_identity_mismatch_is_critical() -> None:
    anoms = scan_log_text("2026-06-12 10:00:00 [ERROR] IDENTITY MISMATCH: as 1 expected 2\n")
    assert any(a.kind == "IDENTITY_MISMATCH" and a.severity == "critical" for a in anoms)


def test_fingerprint_stable_across_timestamp_and_thread() -> None:
    a1 = scan_log_text(
        "2026-06-12 10:00:01 [ERROR] c_lord.x: db is locked [thread=111]\n"
        "Traceback (most recent call last):\n"
        "aiosqlite.OperationalError: database is locked\n"
    )
    a2 = scan_log_text(
        "2026-06-12 11:30:59 [ERROR] c_lord.x: db is locked [thread=222]\n"
        "Traceback (most recent call last):\n"
        "aiosqlite.OperationalError: database is locked\n"
    )
    tb1 = next(a for a in a1 if a.kind == "LOG_TRACEBACK")
    tb2 = next(a for a in a2 if a.kind == "LOG_TRACEBACK")
    assert fingerprint(tb1) == fingerprint(tb2)
    err1 = next(a for a in a1 if a.kind == "LOG_ERROR")
    err2 = next(a for a in a2 if a.kind == "LOG_ERROR")
    assert fingerprint(err1) == fingerprint(err2)  # thread id normalized out


def test_error_line_extracts_session_context() -> None:
    anoms = scan_log_text(
        "2026-06-12 10:00:01 [ERROR] c_lord.cogs.scheduler: task blew up "
        "[thread=999 session=deadbeef task=7]\n"
    )
    err = next(a for a in anoms if a.kind == "LOG_ERROR")
    assert err.fields.get("session") == "deadbeef"
    assert err.fields.get("thread") == "999"
