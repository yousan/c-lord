"""Unit tests for scripts.fuzz.oracle — anomaly detection (Issue #377).

The oracle is the heart of the fuzzer: given one Observation (what happened when
a scenario was injected), it returns the list of anomaly *candidates*. These are
not confirmed bugs — a human triages the report — but they flag every deviation
from "clean reply, healthy lamp, no chrome".
"""

from __future__ import annotations

from scripts.fuzz.oracle import (
    Observation,
    detect_anomalies,
    fingerprint,
)


def _obs(**kw: object) -> Observation:
    base: dict[str, object] = dict(
        scenario_id="s01",
        category="x",
        injected=True,
        thread_id="100",
        replied=True,
        reply_text="all good",
        reactions=["🟡"],  # EMOJI_WAITING — healthy terminal state
        latency_s=3.0,
        health_ok=True,
        inject_error=None,
    )
    base.update(kw)
    return Observation(**base)  # type: ignore[arg-type]


def test_clean_reply_yields_no_anomaly() -> None:
    assert detect_anomalies(_obs()) == []


def test_spawn_failure_short_circuits() -> None:
    out = detect_anomalies(
        _obs(
            injected=False,
            inject_error="connection refused",
            replied=False,
            reply_text=None,
            reactions=[],
        )
    )
    assert [a.kind for a in out] == ["SPAWN_FAILED"]
    assert "connection refused" in out[0].evidence


def test_health_down_is_critical() -> None:
    out = detect_anomalies(_obs(health_ok=False))
    by = {a.kind: a for a in out}
    assert "HEALTH_DOWN" in by
    assert by["HEALTH_DOWN"].severity == "critical"


def test_no_response_when_not_replied() -> None:
    out = detect_anomalies(_obs(replied=False, reply_text=None, reactions=["🟢"]))
    assert "NO_RESPONSE" in {a.kind for a in out}


def test_error_reaction_detected() -> None:
    out = detect_anomalies(_obs(reactions=["❌"]))
    assert "ERROR_REACTION" in {a.kind for a in out}


def test_stall_reaction_detected_soft_and_hard() -> None:
    for emoji in ("⏳", "⚠️"):
        out = detect_anomalies(_obs(reactions=[emoji]))
        assert "STALL" in {a.kind for a in out}, emoji


def test_stall_warning_without_variation_selector() -> None:
    # Discord may return ⚠ (U+26A0) without the U+FE0F variation selector.
    out = detect_anomalies(_obs(reactions=["⚠"]))
    assert "STALL" in {a.kind for a in out}


def test_chrome_leak_substring() -> None:
    out = detect_anomalies(_obs(reply_text="hi there\nModel: claude-x\nbye"))
    assert "CHROME_LEAK" in {a.kind for a in out}


def test_bare_prompt_char_is_chrome_leak() -> None:
    out = detect_anomalies(_obs(reply_text="answer\n❯\nmore"))
    assert "CHROME_LEAK" in {a.kind for a in out}


def test_exception_traceback_leak() -> None:
    out = detect_anomalies(
        _obs(reply_text='oops\nTraceback (most recent call last):\n  File "x.py", line 1')
    )
    assert "EXCEPTION_LEAK" in {a.kind for a in out}


def test_empty_reply_detected() -> None:
    out = detect_anomalies(_obs(reply_text="   \n  "))
    assert "EMPTY_REPLY" in {a.kind for a in out}


def test_multiple_anomalies_coexist() -> None:
    out = detect_anomalies(_obs(reply_text="Model: x", reactions=["⚠️"]))
    kinds = {a.kind for a in out}
    assert {"CHROME_LEAK", "STALL"} <= kinds


def test_running_lamp_at_end_does_not_double_flag_when_replied() -> None:
    # 🟢 still-running at observation time but a clean reply arrived → no anomaly
    assert detect_anomalies(_obs(reactions=["🟢"])) == []


def test_fingerprint_is_signature_stable_across_scenarios() -> None:
    a1 = detect_anomalies(_obs(reply_text="Model: x"))[0]
    a2 = detect_anomalies(_obs(reply_text="Model: x", scenario_id="s99"))[0]
    assert fingerprint(a1) == fingerprint(a2)


def test_fingerprint_differs_by_kind() -> None:
    chrome = detect_anomalies(_obs(reply_text="Model: x"))[0]
    err = detect_anomalies(_obs(reactions=["❌"]))[0]
    assert fingerprint(chrome) != fingerprint(err)
