"""Natural-language fuzzing harness for c-lord (Issue #377).

An LLM (the ``claude`` CLI) generates adversarial natural-language scenarios;
the harness injects each into a running c-lord bot (default: ``/api/spawn`` →
fresh thread per scenario), observes the bot's reply, the status reactions on
the seed message, and ``/api/health``, then runs a pure-logic *oracle* that
flags anomalies (no-response, TUI-chrome leak, error reaction, stall,
exception leak, …). Results are written to ``docs/fuzz-runs/<ts>.{json,md}``
and a summary is posted to a Discord report channel.

The pure logic (scenario parsing, anomaly detection, report rendering) lives in
``scenarios``, ``oracle`` and ``report`` and is unit-tested. The I/O glue
(``generator``, ``discord_io``, ``runner``) is exercised on staging.
"""

from __future__ import annotations

from .oracle import Anomaly, Observation, detect_anomalies, fingerprint
from .scenarios import Scenario, parse_scenarios

__all__ = [
    "Anomaly",
    "Observation",
    "Scenario",
    "detect_anomalies",
    "fingerprint",
    "parse_scenarios",
]
