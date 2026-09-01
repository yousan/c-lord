"""Unit tests for scripts.monitor.threadscan — live-thread health (#404)."""

from __future__ import annotations

from scripts.monitor.threadscan import detect_thread_anomalies


def _d(**kw):
    base = dict(
        reactions=["🟡"],
        latest_reply_text="all good",
        trigger_age_s=30.0,
        stuck_timeout_s=300.0,
        thread_id="100",
        source="#chan",
    )
    base.update(kw)
    return detect_thread_anomalies(**base)


def test_healthy_thread_no_anomaly() -> None:
    assert _d() == []


def test_stuck_running_past_timeout() -> None:
    out = _d(reactions=["🟢"], latest_reply_text=None, trigger_age_s=600.0, stuck_timeout_s=300.0)
    assert "THREAD_STUCK" in {a.kind for a in out}


def test_running_but_within_timeout_is_ok() -> None:
    out = _d(reactions=["🟢"], latest_reply_text=None, trigger_age_s=60.0, stuck_timeout_s=300.0)
    assert out == []


def test_running_and_waiting_not_stuck() -> None:
    # both 🟢 and 🟡 present (finished) → not stuck even if old
    out = _d(reactions=["🟢", "🟡"], trigger_age_s=9999.0)
    assert "THREAD_STUCK" not in {a.kind for a in out}


def test_error_reaction_flagged() -> None:
    assert "ERROR_REACTION" in {a.kind for a in _d(reactions=["❌"])}


def test_stall_reaction_flagged() -> None:
    assert "STALL" in {a.kind for a in _d(reactions=["⚠️"])}


def test_chrome_leak_in_reply() -> None:
    assert "CHROME_LEAK" in {a.kind for a in _d(latest_reply_text="hi\nModel: x\nbye")}


def test_traceback_leak_in_reply() -> None:
    out = _d(latest_reply_text="oops\nTraceback (most recent call last):\n  File ...")
    assert "EXCEPTION_LEAK" in {a.kind for a in out}


def test_thread_id_and_source_recorded() -> None:
    out = _d(reactions=["❌"], thread_id="555", source="#bugs")
    a = next(x for x in out if x.kind == "ERROR_REACTION")
    assert a.fields.get("thread") == "555"
    assert a.fields.get("source") == "#bugs"
