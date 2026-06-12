"""Live-thread health detection (Issue #404). Pure functions only.

Reuses the fuzz oracle's chrome/exception checks and status-emoji constants so
the two stay in sync. Adds THREAD_STUCK: a trigger still 🟢 (running) with no 🟡
(done) past a timeout — i.e. the bot took the turn but never finished it (a hang
/ no-response that a single fuzz injection would also have caught, but here seen
on real traffic).
"""

from __future__ import annotations

from scripts.fuzz.oracle import (
    EMOJI_ERROR,
    EMOJI_RUNNING,
    EMOJI_STALL_HARD,
    EMOJI_STALL_SOFT,
    EMOJI_WAITING,
    Anomaly,
    _chrome_reason,
    _exception_reason,
    _strip_vs,
)


def detect_thread_anomalies(
    *,
    reactions: list[str],
    latest_reply_text: str | None,
    trigger_age_s: float,
    stuck_timeout_s: float,
    thread_id: str,
    source: str = "",
) -> list[Anomaly]:
    """Anomaly candidates for one live thread's trigger + latest reply."""
    out: list[Anomaly] = []
    fields = {"thread": thread_id, "source": source}
    rset = {_strip_vs(r) for r in reactions}
    running = _strip_vs(EMOJI_RUNNING) in rset
    waiting = _strip_vs(EMOJI_WAITING) in rset

    if _strip_vs(EMOJI_ERROR) in rset:
        out.append(
            Anomaly(
                thread_id, "ERROR_REACTION", "high", "trigger marked ❌", EMOJI_ERROR, fields=fields
            )
        )
    if rset & {_strip_vs(EMOJI_STALL_SOFT), _strip_vs(EMOJI_STALL_HARD)}:
        out.append(
            Anomaly(
                thread_id, "STALL", "medium", "trigger shows ⏳/⚠️ (stall)", "stall", fields=fields
            )
        )
    if running and not waiting and trigger_age_s > stuck_timeout_s:
        out.append(
            Anomaly(
                thread_id,
                "THREAD_STUCK",
                "high",
                f"running 🟢 with no 🟡 after {int(trigger_age_s)}s (hang/no-response)",
                "stuck",
                fields=fields,
            )
        )

    text = latest_reply_text or ""
    chrome = _chrome_reason(text)
    if chrome is not None:
        out.append(
            Anomaly(thread_id, "CHROME_LEAK", "high", "TUI chrome in reply", chrome, fields=fields)
        )
    exc = _exception_reason(text)
    if exc is not None:
        out.append(
            Anomaly(thread_id, "EXCEPTION_LEAK", "high", "traceback in reply", exc, fields=fields)
        )
    return out
