"""Unit tests for scripts.fuzz.report — run-report assembly + rendering (#377)."""

from __future__ import annotations

from scripts.fuzz.oracle import Anomaly, Observation
from scripts.fuzz.report import (
    build_report,
    render_discord_summary,
    render_markdown,
)
from scripts.fuzz.scenarios import Scenario


def _fixture() -> dict[str, object]:
    scenarios = [
        Scenario(id="s01", category="long", text="x" * 50, intent="overflow"),
        Scenario(id="s02", category="emoji", text="😀" * 5, intent="render"),
    ]
    observations = [
        Observation("s01", "long", True, "111", True, "ok", ["🟡"], 2.0, True, None),
        Observation("s02", "emoji", True, "222", False, None, ["🟢"], None, True, None),
    ]
    anomalies = [
        Anomaly("s02", "NO_RESPONSE", "high", "no reply in 180s", ""),
    ]
    return dict(scenarios=scenarios, observations=observations, anomalies=anomalies)


def test_build_report_counts() -> None:
    f = _fixture()
    rep = build_report(
        run_id="20260611-000000",
        started_at="2026-06-11T00:00:00",
        finished_at="2026-06-11T00:05:00",
        branch="feature/fuzz-harness",
        inject_mode="spawn",
        generation_raw_path="docs/fuzz-runs/20260611-000000.gen.txt",
        **f,  # type: ignore[arg-type]
    )
    assert rep["counts"]["scenarios"] == 2
    assert rep["counts"]["injected"] == 2
    assert rep["counts"]["replied"] == 1
    assert rep["counts"]["anomalies"] == 1
    assert rep["anomalies_by_kind"]["NO_RESPONSE"] == 1


def test_build_report_marks_new_vs_seen() -> None:
    f = _fixture()
    # Pre-seed the fingerprint of the one anomaly so it is "seen", not new.
    from scripts.fuzz.oracle import fingerprint

    fp = fingerprint(f["anomalies"][0])  # type: ignore[index]
    rep = build_report(
        run_id="r",
        started_at="a",
        finished_at="b",
        branch="x",
        inject_mode="spawn",
        generation_raw_path="g",
        seen_fingerprints={fp},
        **f,  # type: ignore[arg-type]
    )
    assert rep["counts"]["new_anomalies"] == 0
    assert rep["anomalies"][0]["is_new"] is False


def test_report_is_json_serializable() -> None:
    import json

    f = _fixture()
    rep = build_report(
        run_id="r",
        started_at="a",
        finished_at="b",
        branch="x",
        inject_mode="spawn",
        generation_raw_path="g",
        **f,  # type: ignore[arg-type]
    )
    json.dumps(rep)  # must not raise


def test_render_markdown_contains_key_sections() -> None:
    f = _fixture()
    rep = build_report(
        run_id="20260611-000000",
        started_at="a",
        finished_at="b",
        branch="feature/fuzz-harness",
        inject_mode="spawn",
        generation_raw_path="g",
        **f,  # type: ignore[arg-type]
    )
    md = render_markdown(rep)
    assert "20260611-000000" in md
    assert "NO_RESPONSE" in md
    assert "feature/fuzz-harness" in md


def test_discord_summary_is_within_limit_and_mentions_counts() -> None:
    f = _fixture()
    rep = build_report(
        run_id="r",
        started_at="a",
        finished_at="b",
        branch="x",
        inject_mode="spawn",
        generation_raw_path="g",
        **f,  # type: ignore[arg-type]
    )
    summary = render_discord_summary(rep, guild_id="999")
    assert len(summary) <= 2000
    assert "2" in summary  # 2 scenarios
    assert "NO_RESPONSE" in summary
    # thread URL for the anomalous scenario's thread is clickable
    assert "discord.com/channels/999/222" in summary


def test_discord_summary_clean_run_says_no_anomaly() -> None:
    rep = build_report(
        run_id="r",
        started_at="a",
        finished_at="b",
        branch="x",
        inject_mode="spawn",
        generation_raw_path="g",
        scenarios=[Scenario("s01", "c", "t", "i")],
        observations=[Observation("s01", "c", True, "1", True, "ok", ["🟡"], 1.0, True, None)],
        anomalies=[],
    )
    summary = render_discord_summary(rep, guild_id="999")
    assert "✅" in summary or "0" in summary
