"""Tests for c_lord.usage_limit — the plan-limit banner vocabulary (#631).

The fixture (`tests/fixtures/transcripts/i631_usage_limit_banners.jsonl`) holds
real captures of what Claude Code writes to its transcript when the account is
rate limited, plus the two things that merely *look* like it and must never be
folded: the percentage warning, and Claude quoting the banner inside a real
answer (this repo's own threads discuss the banner, so that one is live).
"""

from __future__ import annotations

import json
from pathlib import Path

import pytest

from c_lord.usage_limit import (
    banner_only,
    count_usage_limit,
    extract_usage_limit,
    folded_notice,
    is_rate_limit_event,
    is_refusal_shaped,
    usage_limit_notices,
)

_FIXTURE = Path(__file__).parent / "fixtures" / "transcripts" / "i631_usage_limit_banners.jsonl"


def _events() -> dict[str, dict]:
    out: dict[str, dict] = {}
    for line in _FIXTURE.read_text("utf-8").splitlines():
        if line.strip():
            event = json.loads(line)
            out[event["uuid"]] = event
    return out


def _text(event: dict) -> str:
    return "\n".join(
        b["text"] for b in event["message"]["content"] if b.get("type") == "text"
    ).strip()


# ---------------------------------------------------------------------------
# AC10: the four labels the CLI prints, fixed by fixture.
# ---------------------------------------------------------------------------


@pytest.mark.parametrize(
    ("uuid", "scope", "resets_at"),
    [
        ("i631-weekly", "weekly limit", "Aug 29, 4pm (Asia/Tokyo)"),
        ("i631-session", "session limit", "2:20pm (Asia/Tokyo)"),
        ("i631-opus", "Opus limit", "Sep 5, 9am (Asia/Tokyo)"),
        ("i631-monthly-spend", "monthly spend limit", None),
        ("i631-weekly-no-quota", "weekly limit", "Sep 4, 6:10pm (Asia/Tokyo)"),
    ],
)
def test_banner_only_parses_every_label(uuid: str, scope: str, resets_at: str | None) -> None:
    limit = banner_only(_text(_events()[uuid]))
    assert limit is not None, uuid
    assert limit.scope == scope
    assert limit.resets_at == resets_at


@pytest.mark.parametrize("uuid", ["i631-warning", "i631-quoted"])
def test_banner_only_rejects_lookalikes(uuid: str) -> None:
    """A warning, and Claude quoting the banner in prose, are not limits."""
    assert banner_only(_text(_events()[uuid])) is None


def test_banner_only_rejects_banner_with_prose_appended() -> None:
    """ "Only the banner" means only the banner — a trailing sentence disqualifies."""
    assert (
        banner_only(
            "You've hit your weekly limit · resets Aug 29, 4pm (Asia/Tokyo)\n"
            "というわけで、代わりに手元の情報だけで答えます。"
        )
        is None
    )


def test_banner_only_rejects_a_banner_with_prose_above_it() -> None:
    """Folding replaces the whole message, so anything else in it must veto."""
    assert (
        banner_only("先に結論だけ:\nYou've hit your weekly limit · resets Aug 29, 4pm (Asia/Tokyo)")
        is None
    )


def test_extract_still_finds_a_banner_inside_a_pane() -> None:
    """The pane path keeps searching anywhere in the capture (unchanged)."""
    pane = "some chrome\n⏺ You've hit your session limit · resets 3pm (Asia/Tokyo)\nmore chrome"
    limit = extract_usage_limit(pane)
    assert limit is not None
    assert limit.scope == "session limit"


# ---------------------------------------------------------------------------
# The scope/reset strings end up in a plain (pinging) Discord message.
# ---------------------------------------------------------------------------


def test_banner_only_rejects_a_scope_carrying_a_mention() -> None:
    assert banner_only("You've hit your @everyone limit · resets 3pm") is None


def test_folded_notice_never_echoes_the_english_banner() -> None:
    limit = banner_only(_text(_events()["i631-session"]))
    assert limit is not None
    notice = folded_notice(limit)
    assert "You've hit" not in notice
    assert "2:20pm (Asia/Tokyo)" in notice
    assert notice.startswith("⏳")


def test_folded_notice_says_so_when_no_reset_time_was_reported() -> None:
    limit = banner_only(_text(_events()["i631-monthly-spend"]))
    assert limit is not None
    notice = folded_notice(limit)
    assert "monthly spend limit" in notice
    assert "回復時刻" in notice


def test_folded_notice_without_a_parsed_banner_is_generic_and_safe() -> None:
    """A rate-limit event whose wording we cannot vouch for still gets a line."""
    notice = folded_notice(None)
    assert notice.startswith("⏳")
    assert "You've hit" not in notice


# ---------------------------------------------------------------------------
# The transcript's own structural markers (#631 AC7).
# ---------------------------------------------------------------------------


@pytest.mark.parametrize(
    "uuid",
    ["i631-weekly", "i631-session", "i631-opus", "i631-monthly-spend", "i631-weekly-no-quota"],
)
def test_is_rate_limit_event_true_for_every_capture(uuid: str) -> None:
    assert is_rate_limit_event(_events()[uuid]) is True


@pytest.mark.parametrize("uuid", ["i631-warning", "i631-quoted"])
def test_is_rate_limit_event_false_for_ordinary_answers(uuid: str) -> None:
    assert is_rate_limit_event(_events()[uuid]) is False


def test_is_rate_limit_event_survives_a_malformed_event() -> None:
    """Runs on every tailed line — a weird shape must not raise."""
    assert is_rate_limit_event({}) is False
    assert is_rate_limit_event({"error": ["rate_limit"]}) is False


def test_refusal_shape_bounds_what_the_marked_path_may_fold() -> None:
    """The envelope path folds without reading the wording — so cap the size.

    Its whole job is to survive a wording change, which means it cannot ask
    whether the text is a banner.  The cap is what keeps that from being able
    to replace a real answer that somehow carried the marker.
    """
    assert is_refusal_shaped("You've hit your session limit · resets 3pm (Asia/Tokyo)") is True
    assert is_refusal_shaped("") is False
    assert is_refusal_shaped("調べました。\n結論は次のとおりです。") is False
    assert is_refusal_shaped("あ" * 201) is False


def test_the_scan_stays_linear_on_chrome_heavy_text() -> None:
    """A line of dashes must not be able to hang the scan (catastrophic backtracking).

    The first version of the gutter pattern nested its quantifiers, so a run of
    dashes — a markdown rule, a table border, an ASCII box, all of which Claude
    writes constantly — took exponential time: 28 dashes cost 10 seconds, and
    the whole CI test matrix hung on it.  Reachable from the pane since #666 and,
    once the mirror folds banners, from every assistant message and every
    transcript rescue scan.
    """
    import time

    for probe in ("-" * 4000, "|" * 4000, "*-" * 2000, ("| a | b |\n|---|---|\n" * 2000)):
        start = time.monotonic()
        assert extract_usage_limit(probe) is None
        assert count_usage_limit(probe) == 0
        assert banner_only(probe) is None
        assert time.monotonic() - start < 1.0, "scan is not linear"


def test_a_long_answer_is_rejected_without_scanning_it() -> None:
    """banner_only caps its input: the mirror calls it on every assistant message."""
    assert banner_only("え" * 500_000) is None


# ---------------------------------------------------------------------------
# AC8: the registry that tells the mirror c-lord already said this in Japanese.
# ---------------------------------------------------------------------------


def test_notices_registry_is_per_thread_and_one_shot() -> None:
    usage_limit_notices.clear_thread(4001)
    usage_limit_notices.clear_thread(4002)
    assert usage_limit_notices.announced(4001) is False
    usage_limit_notices.note(4001)
    assert usage_limit_notices.announced(4001) is True
    assert usage_limit_notices.announced(4002) is False
    usage_limit_notices.clear_thread(4001)
    assert usage_limit_notices.announced(4001) is False


def test_notices_registry_expires() -> None:
    usage_limit_notices.clear_thread(4003)
    usage_limit_notices.note(4003, at=0.0)
    assert usage_limit_notices.announced(4003, now=1.0) is True
    assert usage_limit_notices.announced(4003, now=1e9) is False
