"""Run-report assembly + rendering (Issue #377).

Pure functions only. :func:`build_report` turns the run's scenarios /
observations / anomalies into a JSON-serializable dict (persisted to
``docs/fuzz-runs/<ts>.json``). :func:`render_markdown` renders the human-readable
``<ts>.md`` artifact. :func:`render_discord_summary` renders the short message
posted to ``#fuzz-report`` (kept under Discord's 2000-char limit).
"""

from __future__ import annotations

from collections import Counter

from .oracle import Anomaly, Observation, fingerprint, severity_rank
from .scenarios import Scenario

_SEVERITY_EMOJI = {
    "critical": "🟥",
    "high": "🟧",
    "medium": "🟨",
    "low": "⬜",
    "info": "⬜",
}


def build_report(
    *,
    run_id: str,
    started_at: str,
    finished_at: str,
    branch: str,
    inject_mode: str,
    scenarios: list[Scenario],
    observations: list[Observation],
    anomalies: list[Anomaly],
    generation_raw_path: str,
    seen_fingerprints: set[str] | None = None,
) -> dict:
    """Assemble the full run report as a JSON-serializable dict."""
    seen = seen_fingerprints or set()
    injected = sum(1 for o in observations if o.injected)
    replied = sum(1 for o in observations if o.replied)
    by_kind = Counter(a.kind for a in anomalies)

    anomaly_rows = []
    new_count = 0
    for a in sorted(anomalies, key=lambda x: (severity_rank(x.severity), x.kind)):
        fp = fingerprint(a)
        is_new = fp not in seen
        new_count += int(is_new)
        anomaly_rows.append(
            {
                "scenario_id": a.scenario_id,
                "kind": a.kind,
                "severity": a.severity,
                "detail": a.detail,
                "evidence": a.evidence,
                "fingerprint": fp,
                "is_new": is_new,
            }
        )

    obs_by_id = {o.scenario_id: o for o in observations}

    return {
        "run_id": run_id,
        "started_at": started_at,
        "finished_at": finished_at,
        "branch": branch,
        "inject_mode": inject_mode,
        "generation_raw": generation_raw_path,
        "counts": {
            "scenarios": len(scenarios),
            "injected": injected,
            "replied": replied,
            "anomalies": len(anomalies),
            "new_anomalies": new_count,
        },
        "anomalies_by_kind": dict(by_kind),
        "scenarios": [
            {"id": s.id, "category": s.category, "text": s.text, "intent": s.intent}
            for s in scenarios
        ],
        "observations": [
            {
                "scenario_id": o.scenario_id,
                "category": o.category,
                "injected": o.injected,
                "thread_id": o.thread_id,
                "replied": o.replied,
                "reply_text": o.reply_text,
                "reactions": o.reactions,
                "latency_s": o.latency_s,
                "health_ok": o.health_ok,
                "inject_error": o.inject_error,
            }
            for o in observations
        ],
        "anomalies": [
            {**row, "thread_id": (obs_by_id.get(row["scenario_id"]) or _NoThread).thread_id}
            for row in anomaly_rows
        ],
    }


class _NoThread:
    thread_id = None


def _thread_url(guild_id: str | None, thread_id: str | None) -> str | None:
    if not guild_id or not thread_id:
        return None
    return f"https://discord.com/channels/{guild_id}/{thread_id}"


def render_markdown(report: dict) -> str:
    """Render the full human-readable Markdown artifact."""
    c = report["counts"]
    lines: list[str] = [
        f"# Fuzz run `{report['run_id']}`",
        "",
        f"- branch: `{report['branch']}`  ·  inject: `{report['inject_mode']}`",
        f"- window: {report['started_at']} → {report['finished_at']}",
        f"- scenarios: **{c['scenarios']}**  ·  injected: {c['injected']}  ·  "
        f"replied: {c['replied']}",
        f"- anomalies: **{c['anomalies']}** (new: {c['new_anomalies']})",
        f"- generation raw: `{report['generation_raw']}`",
        "",
        "## Anomalies",
        "",
    ]
    if not report["anomalies"]:
        lines.append("_None — clean run._")
    else:
        lines.append("| sev | kind | scenario | detail | evidence | new |")
        lines.append("|-----|------|----------|--------|----------|-----|")
        for a in report["anomalies"]:
            ev = (a["evidence"] or "").replace("|", "\\|").replace("\n", " ")[:60]
            det = a["detail"].replace("|", "\\|").replace("\n", " ")[:80]
            lines.append(
                f"| {a['severity']} | {a['kind']} | {a['scenario_id']} | {det} | "
                f"`{ev}` | {'🆕' if a['is_new'] else ''} |"
            )

    lines += ["", "## Scenarios fired", ""]
    for s in report["scenarios"]:
        snippet = s["text"].replace("\n", "\\n")
        if len(snippet) > 120:
            snippet = snippet[:117] + "…"
        lines.append(f"- `{s['id']}` [{s['category']}] {snippet}")
        if s["intent"]:
            lines.append(f"  - intent: {s['intent']}")
    lines.append("")
    return "\n".join(lines)


def render_discord_summary(report: dict, *, guild_id: str | None = None) -> str:
    """Render the concise summary posted to the report channel (<2000 chars)."""
    c = report["counts"]
    header = (
        f"🧪 **Fuzz run `{report['run_id']}`** · branch `{report['branch']}`\n"
        f"撃ち {c['scenarios']} / 注入 {c['injected']} / 返信 {c['replied']} / "
        f"**異常 {c['anomalies']}**（new {c['new_anomalies']}）"
    )
    if not report["anomalies"]:
        return header + "\n✅ アノマリ無し（クリーン）"

    lines = [header, ""]
    # Group by kind for the headline, then list each anomaly with a thread link.
    kinds = ", ".join(f"{k}×{v}" for k, v in report["anomalies_by_kind"].items())
    lines.append(f"内訳: {kinds}")
    for a in report["anomalies"]:
        sev_emoji = _SEVERITY_EMOJI.get(a["severity"], "⬜")
        url = _thread_url(guild_id, a.get("thread_id"))
        link = f" <{url}>" if url else ""
        ev = (a["evidence"] or "").replace("\n", " ")[:50]
        new = "🆕 " if a["is_new"] else ""
        line = f"{sev_emoji} {new}`{a['kind']}` [{a['scenario_id']}] {a['detail']}"
        if ev:
            line += f" — `{ev}`"
        line += link
        lines.append(line)

    text = "\n".join(lines)
    if len(text) > 1990:
        text = text[:1987] + "…"
    return text
