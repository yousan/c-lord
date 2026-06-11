"""Anomaly detection oracle for the fuzzer (Issue #377).

Pure functions only — no I/O. Given an :class:`Observation` of what happened
when one scenario was injected, :func:`detect_anomalies` returns a list of
:class:`Anomaly` *candidates*. They are not confirmed bugs; a human triages the
report. The point is to surface every deviation from the healthy baseline:

    a non-empty reply arrives, the seed lamp ends 🟡, no TUI chrome leaked,
    no traceback leaked, and ``/api/health`` is up.

Status emoji are mirrored from ``c_lord/discord_ui/status.py``; the harness
collects the *union* of reactions seen on the seed message while waiting, so a
transient ❌/⏳/⚠️ that the lamp later overwrites is still caught.
"""

from __future__ import annotations

import hashlib
from dataclasses import dataclass, field

# --- mirror of c_lord/discord_ui/status.py emoji (kept in sync deliberately) --
EMOJI_RUNNING = "🟢"
EMOJI_WAITING = "🟡"
EMOJI_ERROR = "❌"
EMOJI_STALL_SOFT = "⏳"
EMOJI_STALL_HARD = "⚠️"
EMOJI_COMPACT = "🗜️"

# TUI chrome that earlier bugs (#23–#50) leaked into Discord. A reply must never
# contain any of these.
CHROME_BLACKLIST = (
    "Model: ",
    "Cost: $",
    "⎇ ",
    "Tip: Use Plan Mode",
    "skill descriptions dropped",
    "-- INSERT --",
    "· /effort",
    "esc to interrupt",
    "? for shortcuts",
)

# High-precision markers of a leaked Python/lib traceback. Chosen to (almost)
# never occur in a healthy natural-language answer.
EXCEPTION_MARKERS = (
    "Traceback (most recent call last)",
    '\n  File "',
    "discord.errors.",
    "aiohttp.client_exceptions",
    "asyncio.exceptions.",
    "aiosqlite",
)

_SEVERITY_ORDER = {"critical": 0, "high": 1, "medium": 2, "low": 3, "info": 4}


@dataclass
class Observation:
    """What the harness saw for one injected scenario."""

    scenario_id: str
    category: str
    injected: bool
    thread_id: str | None
    replied: bool
    reply_text: str | None
    reactions: list[str]
    latency_s: float | None
    health_ok: bool
    inject_error: str | None = None


@dataclass(frozen=True)
class Anomaly:
    """One anomaly candidate surfaced by the oracle."""

    scenario_id: str
    kind: str
    severity: str
    detail: str
    evidence: str
    fields: dict = field(default_factory=dict, compare=False)


def _strip_vs(s: str) -> str:
    """Drop emoji variation selectors so ⚠ and ⚠️ compare equal."""
    return s.replace("️", "")


def severity_rank(severity: str) -> int:
    return _SEVERITY_ORDER.get(severity, 99)


def _chrome_reason(text: str) -> str | None:
    for sub in CHROME_BLACKLIST:
        if sub in text:
            return sub
    for line in text.splitlines():
        if line.strip() == "❯":
            return "bare ❯ prompt char"
    return None


def _exception_reason(text: str) -> str | None:
    for marker in EXCEPTION_MARKERS:
        if marker in text:
            return marker
    return None


def detect_anomalies(obs: Observation) -> list[Anomaly]:
    """Return all anomaly candidates for one observation (possibly empty)."""
    out: list[Anomaly] = []

    # Injection failure: nothing else can be observed reliably.
    if not obs.injected:
        out.append(
            Anomaly(
                obs.scenario_id,
                "SPAWN_FAILED",
                "high",
                "scenario could not be injected",
                obs.inject_error or "",
            )
        )
        if not obs.health_ok:
            out.append(
                Anomaly(obs.scenario_id, "HEALTH_DOWN", "critical", "/api/health not OK", "")
            )
        return out

    reactions = {_strip_vs(r) for r in obs.reactions}
    if _strip_vs(EMOJI_ERROR) in reactions:
        out.append(
            Anomaly(
                obs.scenario_id, "ERROR_REACTION", "high", "bot marked the turn ❌", EMOJI_ERROR
            )
        )
    if reactions & {_strip_vs(EMOJI_STALL_SOFT), _strip_vs(EMOJI_STALL_HARD)}:
        out.append(
            Anomaly(
                obs.scenario_id, "STALL", "medium", "bot stalled (⏳/⚠️) during the turn", "stall"
            )
        )

    if not obs.replied:
        still_running = _strip_vs(EMOJI_RUNNING) in reactions
        detail = "no reply within the timeout" + (
            " (lamp still 🟢 — turn never finished)" if still_running else ""
        )
        out.append(Anomaly(obs.scenario_id, "NO_RESPONSE", "high", detail, ""))
    else:
        text = obs.reply_text or ""
        if not text.strip():
            out.append(Anomaly(obs.scenario_id, "EMPTY_REPLY", "medium", "reply was empty", ""))
        else:
            chrome = _chrome_reason(text)
            if chrome is not None:
                out.append(
                    Anomaly(
                        obs.scenario_id,
                        "CHROME_LEAK",
                        "high",
                        "TUI chrome leaked into reply",
                        chrome,
                    )
                )
            exc = _exception_reason(text)
            if exc is not None:
                out.append(
                    Anomaly(
                        obs.scenario_id,
                        "EXCEPTION_LEAK",
                        "high",
                        "traceback leaked into reply",
                        exc,
                    )
                )

    if not obs.health_ok:
        out.append(
            Anomaly(
                obs.scenario_id,
                "HEALTH_DOWN",
                "critical",
                "/api/health not OK after the scenario (possible crash)",
                "",
            )
        )

    return out


def fingerprint(anomaly: Anomaly) -> str:
    """Stable signature for cross-run dedup: ``kind`` + normalized evidence.

    Deliberately scenario-independent so the *same* failure signature (e.g. the
    same leaked chrome substring) collapses to one fingerprint across scenarios
    and runs. Evidence-less kinds (NO_RESPONSE) fold to a kind-only fingerprint.
    """
    sig = f"{anomaly.kind}:{_strip_vs(anomaly.evidence).strip().lower()}"
    return hashlib.sha1(sig.encode("utf-8")).hexdigest()[:12]
