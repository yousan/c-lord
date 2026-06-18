"""Bot-log anomaly detection (Issue #404). Pure functions only.

Scans a chunk of new log text and returns :class:`Anomaly` candidates for
tracebacks, ERROR-level lines, and identity mismatches — the clearest signals
that a real user just hit a bug. The structured ``[thread=/session=/task=]``
context (from ``log_ctx``) is attached so the anomaly points back at the
offending thread, and a normalized signature is used as the evidence so the same
recurring error dedups across runs (changing timestamps / thread ids fold away).
"""

from __future__ import annotations

import re

from scripts.fuzz.oracle import Anomaly

_CTX_KEYS = ("thread", "session", "task", "channel")
_TS_RE = re.compile(r"^\s*\d{4}-\d\d-\d\d[ T]\d\d:\d\d:\d\d[.,]?\d*\s*")
_LOG_LINE_RE = re.compile(r"^\d{4}-\d\d-\d\d[ T]\d\d:\d\d:\d\d")
_ID_RE = re.compile(r"\b\d{15,}\b")


def _extract_ctx(line: str) -> dict[str, str]:
    ctx: dict[str, str] = {}
    for key in _CTX_KEYS:
        m = re.search(rf"{key}=([^\s\]]+)", line)
        if m:
            ctx[key] = m.group(1)
    return ctx


def _normalize(text: str) -> str:
    """Strip the variable parts (timestamp, ids, context) for a stable signature."""
    out = _TS_RE.sub("", text)
    for key in _CTX_KEYS:
        out = re.sub(rf"{key}=[^\s\]]+", "", out)
    out = _ID_RE.sub("<id>", out)
    return re.sub(r"\s+", " ", out).strip()


def _error_message(line: str) -> str:
    tail = line.split("[ERROR]", 1)[1] if "[ERROR]" in line else line
    return tail.strip()


def _read_traceback(lines: list[str], start: int) -> tuple[str, str]:
    """Return (exception_signature, raw_block) for a traceback starting at *start*."""
    block = [lines[start]]
    exc = ""
    j = start + 1
    while j < len(lines):
        ln = lines[j]
        if _LOG_LINE_RE.match(ln):  # next real log entry → traceback ended
            break
        block.append(ln)
        if ln.strip() and not ln.startswith((" ", "\t")):
            exc = ln.strip()  # last non-indented line = the exception type+message
        j += 1
    return (exc or lines[start].strip()), "\n".join(block).strip()


def scan_log_text(text: str, *, source: str = "") -> list[Anomaly]:
    """Find anomalies in a chunk of new log lines (possibly empty)."""
    out: list[Anomaly] = []
    ctx: dict[str, str] = {}
    lines = text.splitlines()
    for i, line in enumerate(lines):
        found = _extract_ctx(line)
        if found:
            ctx = found
        sid = ctx.get("thread", "-")
        base_fields = {**ctx, "source": source, "raw": line.strip()}

        if "IDENTITY MISMATCH" in line:
            out.append(
                Anomaly(
                    sid,
                    "IDENTITY_MISMATCH",
                    "critical",
                    "bot logged in as the wrong identity",
                    _normalize(line),
                    fields=base_fields,
                )
            )
        elif "[ERROR]" in line:
            out.append(
                Anomaly(
                    sid,
                    "LOG_ERROR",
                    "medium",
                    "ERROR-level log line",
                    _normalize(_error_message(line)),
                    fields=base_fields,
                )
            )

        if "Traceback (most recent call last)" in line:
            exc, raw_block = _read_traceback(lines, i)
            out.append(
                Anomaly(
                    sid,
                    "LOG_TRACEBACK",
                    "high",
                    "traceback in bot log",
                    _normalize(exc),
                    fields={**ctx, "source": source, "raw": raw_block},
                )
            )
    return out
