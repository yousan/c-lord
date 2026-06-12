"""Passive traffic monitor for c-lord (Issue #404).

Direction A from the #377 retrospective: the natural-language fuzzer tests the
thinnest, most-hardened layer (single-turn text → reply) and its oracle only
catches structural breaks, so it misses the real bug surface (interactive UI
flows, lifecycle/state, rendering, timing). The real bugs are already being hit
by real users in real Discord threads — so instead of synthesizing inputs, this
*read-only* monitor watches the real traffic for anomalies:

  * bot logs — new tracebacks / ERROR lines / identity mismatches (the clearest
    "a user just hit a bug" signal), with the structured ``[thread=/session=]``
    context extracted;
  * thread health — a trigger still 🟢 (running) with no 🟡 past a timeout
    (stuck/no-response/hang), ❌ reactions, or chrome/traceback leaked into a
    reply (reusing the fuzz oracle).

It must be external (not an in-bot Cog): a hung bot cannot report its own hang.
Anomalies are fingerprint-deduped and only *new* ones are reported.
"""

from __future__ import annotations

from .logscan import scan_log_text

__all__ = ["scan_log_text"]
