"""Monitor orchestration + config + CLI (Issue #404). Read-only.

Each run: scan new bot-log text (incremental) for tracebacks/errors, scan live
threads for stuck/❌/chrome, dedup against seen fingerprints, and report only the
NEW anomalies. No lease, no injection, no restart — safe against prod + fleet.
"""

from __future__ import annotations

import argparse
import glob
import json
import sys
import urllib.error
import urllib.request
from dataclasses import dataclass
from datetime import datetime
from pathlib import Path

from scripts.fuzz.oracle import Anomaly, fingerprint, severity_rank
from scripts.monitor.logscan import scan_log_text
from scripts.monitor.state import load_state, read_new_log_text, save_state
from scripts.monitor.threadscan import detect_thread_anomalies

UA = "DiscordBot (https://github.com/yousan/c-lord, 1.0)"
DISCORD_API = "https://discord.com/api/v10"
DISCORD_EPOCH_MS = 1420070400000
DEFAULT_OUT_DIR = "docs/monitor-runs"
DEFAULT_STATE = "docs/monitor-runs/state.json"
DEFAULT_STUCK_TIMEOUT = 600.0
_SEV_EMOJI = {"critical": "🟥", "high": "🟧", "medium": "🟨", "low": "⬜", "info": "⬜"}


def log(msg: str) -> None:
    print(f"[monitor] {msg}", flush=True)


def snowflake_age_s(message_id: str, *, now_ms: float) -> float:
    try:
        created = (int(message_id) >> 22) + DISCORD_EPOCH_MS
    except (TypeError, ValueError):
        return 0.0
    return max(0.0, (now_ms - created) / 1000.0)


# --------------------------------------------------------------------------
# Discord REST (read + report only)
# --------------------------------------------------------------------------
class MonitorClient:
    def __init__(self, bot_token: str) -> None:
        self._token = bot_token

    def _get(self, path: str):
        req = urllib.request.Request(f"{DISCORD_API}{path}")
        req.add_header("User-Agent", UA)
        req.add_header("Authorization", f"Bot {self._token}")
        with urllib.request.urlopen(req, timeout=30) as resp:
            return json.loads(resp.read())

    def bot_user_id(self) -> str:
        try:
            return str(self._get("/users/@me")["id"])
        except Exception:
            return ""

    def fetch_messages(self, channel_id: str, *, limit: int = 50) -> list[dict]:
        try:
            return self._get(f"/channels/{channel_id}/messages?limit={limit}")
        except (urllib.error.URLError, OSError):
            return []

    def active_threads(self, guild_id: str) -> list[dict]:
        try:
            return self._get(f"/guilds/{guild_id}/threads/active").get("threads", [])
        except (urllib.error.URLError, OSError):
            return []

    def post_message(self, channel_id: str, content: str) -> tuple[bool, str | None]:
        data = json.dumps({"content": content}).encode()
        req = urllib.request.Request(
            f"{DISCORD_API}/channels/{channel_id}/messages", data=data, method="POST"
        )
        req.add_header("User-Agent", UA)
        req.add_header("Content-Type", "application/json")
        req.add_header("Authorization", f"Bot {self._token}")
        try:
            with urllib.request.urlopen(req, timeout=30) as resp:
                return resp.status in (200, 201), None
        except urllib.error.HTTPError as exc:
            return False, f"{exc.code}: {exc.read()[:200]!r}"


_STATUS_EMOJI = {"🟢", "🟡", "❌", "⏳", "⚠️", "⚠"}


def _reaction_names(message: dict) -> list[str]:
    return [
        (r or {}).get("emoji", {}).get("name", "")
        for r in (message.get("reactions") or [])
        if (r or {}).get("emoji", {}).get("name")
    ]


def observe_thread(
    client: MonitorClient, thread_id: str, bot_id: str, *, now_ms: float
) -> tuple[list[str], str | None, float] | None:
    """Return (trigger_reactions, latest_reply_text, trigger_age_s) or None."""
    msgs = client.fetch_messages(thread_id, limit=50)
    if not msgs:
        return None
    trigger = next((m for m in msgs if set(_reaction_names(m)) & _STATUS_EMOJI), None)
    if trigger is None:
        return None  # no active turn to judge
    reactions = _reaction_names(trigger)
    age = snowflake_age_s(str(trigger["id"]), now_ms=now_ms)
    reply = None
    for m in msgs:  # newest-first; first plain-content bot message is the latest reply
        if m.get("author", {}).get("id") == bot_id and not m.get("webhook_id"):
            content = (m.get("content") or "").strip()
            if content and not content.startswith("-#"):
                reply = m.get("content")
                break
    return reactions, reply, age


# --------------------------------------------------------------------------
# Config
# --------------------------------------------------------------------------
def load_env_file(path: Path) -> dict[str, str]:
    env: dict[str, str] = {}
    if not path.exists():
        return env
    for raw in path.read_text().splitlines():
        line = raw.strip()
        if not line or line.startswith("#") or "=" not in line:
            continue
        key, _, value = line.partition("=")
        env[key.strip()] = value.strip()
    return env


@dataclass
class MonitorConfig:
    bot_token: str | None
    logs: list[str]
    channels: list[str]
    report_channel: str | None
    guild_id: str | None
    state_file: str
    out_dir: str
    stuck_timeout: float


def _csv(val: str | None) -> list[str]:
    return [x.strip() for x in (val or "").split(",") if x.strip()]


def build_config(env: dict[str, str], args: argparse.Namespace) -> MonitorConfig:
    def pick(*keys: str, default: str | None = None) -> str | None:
        for k in keys:
            if env.get(k):
                return env[k]
        return default

    return MonitorConfig(
        bot_token=pick("DISCORD_BOT_TOKEN"),
        logs=_csv(args.logs or pick("MONITOR_LOGS")),
        channels=_csv(args.channels or pick("MONITOR_CHANNELS")),
        report_channel=args.report_channel or pick("MONITOR_REPORT_CHANNEL", "DISCORD_CHANNEL_ID"),
        guild_id=args.guild or pick("MONITOR_GUILD_ID", "FUZZ_GUILD_ID", "DISCORD_GUILD_ID"),
        state_file=args.state_file
        or pick("MONITOR_STATE_FILE", default=DEFAULT_STATE)
        or DEFAULT_STATE,
        out_dir=args.out_dir,
        stuck_timeout=args.stuck_timeout,
    )


# --------------------------------------------------------------------------
# Reporting (inline; reuses fuzz fingerprint/severity)
# --------------------------------------------------------------------------
def _thread_url(guild: str | None, thread: str | None) -> str | None:
    return f"https://discord.com/channels/{guild}/{thread}" if guild and thread else None


def render_summary(new_anoms: list[Anomaly], total: int, *, guild: str | None) -> str:
    if not new_anoms:
        return f"👁️ **traffic monitor** · スキャン異常 {total} / 新規 0 → ✅ 新しい異常なし"
    lines = [f"👁️ **traffic monitor** · 異常 {total} / **新規 {len(new_anoms)}**", ""]
    for a in sorted(new_anoms, key=lambda x: severity_rank(x.severity)):
        sev = _SEV_EMOJI.get(a.severity, "⬜")
        thread = a.fields.get("thread")
        src = a.fields.get("source", "")
        url = _thread_url(guild, thread if thread and thread != "-" else None)
        loc = f" <{url}>" if url else (f" [{src}]" if src else "")
        ev = (a.evidence or "").replace("\n", " ")[:80]
        lines.append(f"{sev} `{a.kind}` {a.detail} — `{ev}`{loc}")
    text = "\n".join(lines)
    return text[:1980] + "…" if len(text) > 1990 else text


def _anomaly_dict(a: Anomaly) -> dict:
    return {
        "kind": a.kind,
        "severity": a.severity,
        "detail": a.detail,
        "evidence": a.evidence,
        "fingerprint": fingerprint(a),
        "fields": a.fields,
    }


# --------------------------------------------------------------------------
# Run
# --------------------------------------------------------------------------
def run(cfg: MonitorConfig, args: argparse.Namespace) -> int:
    run_id = datetime.now().strftime("%Y%m%d-%H%M%S")
    now_ms = datetime.now().timestamp() * 1000
    state = load_state(Path(cfg.state_file))
    anomalies: list[Anomaly] = []

    # 1) incremental log scan
    real_paths: list[Path] = []
    for pattern in cfg.logs:
        for hit in sorted(glob.glob(pattern)):
            real_paths.append(Path(hit).resolve())
    for rp in real_paths:
        text, new_off = read_new_log_text(rp, state.offsets.get(str(rp), 0))
        state.offsets[str(rp)] = new_off
        if text:
            anomalies.extend(scan_log_text(text, source=rp.name))
    log(f"log scan: {len(real_paths)} file(s) → {len(anomalies)} log anomaly(ies)")

    # 2) live thread health scan (read-only)
    if cfg.bot_token and (cfg.guild_id or args.threads):
        client = MonitorClient(cfg.bot_token)
        bot_id = client.bot_user_id()
        threads: list[dict] = []
        if args.threads:
            threads = [{"id": t, "parent_id": None} for t in _csv(args.threads)]
        elif cfg.guild_id:
            threads = client.active_threads(cfg.guild_id)
            if cfg.channels:
                allow = set(cfg.channels)
                threads = [t for t in threads if str(t.get("parent_id")) in allow]
        for t in threads[: args.thread_limit]:
            obs = observe_thread(client, str(t["id"]), bot_id, now_ms=now_ms)
            if obs is None:
                continue
            reactions, reply, age = obs
            anomalies.extend(
                detect_thread_anomalies(
                    reactions=reactions,
                    latest_reply_text=reply,
                    trigger_age_s=age,
                    stuck_timeout_s=cfg.stuck_timeout,
                    thread_id=str(t["id"]),
                    source=f"thread:{t.get('name', t['id'])}",
                )
            )
        log(f"thread scan: {len(threads)} thread(s) checked")

    # 3) dedup + report
    seen = state.seen_set
    new = [a for a in anomalies if fingerprint(a) not in seen]
    out_dir = Path(cfg.out_dir)
    out_dir.mkdir(parents=True, exist_ok=True)
    report = {
        "run_id": run_id,
        "counts": {"anomalies": len(anomalies), "new": len(new)},
        "anomalies": [_anomaly_dict(a) for a in anomalies],
    }
    (out_dir / f"{run_id}.json").write_text(json.dumps(report, ensure_ascii=False, indent=2))
    (out_dir / f"{run_id}.md").write_text(render_summary(new, len(anomalies), guild=cfg.guild_id))
    log(f"report → {out_dir}/{run_id}.json · {len(new)} new anomaly(ies)")

    for a in new:
        state.seen.append(fingerprint(a))
    if not args.dry_run:
        save_state(Path(cfg.state_file), state)
        if not args.no_report and cfg.report_channel and cfg.bot_token and new:
            ok, err = MonitorClient(cfg.bot_token).post_message(
                cfg.report_channel, render_summary(new, len(anomalies), guild=cfg.guild_id)
            )
            log(f"posted to #{cfg.report_channel}" if ok else f"report post failed: {err}")
    return 0


def parse_args(argv: list[str] | None = None) -> argparse.Namespace:
    p = argparse.ArgumentParser(
        prog="python -m scripts.monitor",
        description="Read-only traffic monitor for c-lord (Issue #404).",
    )
    p.add_argument("--logs", default=None, help="comma-separated log file paths/globs to scan")
    p.add_argument("--channels", default=None, help="comma-separated channel ids to scope threads")
    p.add_argument(
        "--threads", default=None, help="comma-separated thread ids (instead of guild scan)"
    )
    p.add_argument("--report-channel", default=None, help="channel id to post the summary")
    p.add_argument("--guild", default=None, help="guild id for active-thread discovery + links")
    p.add_argument("--state-file", default=None, help="incremental state file")
    p.add_argument("--out-dir", default=DEFAULT_OUT_DIR, help="artifact output dir")
    p.add_argument(
        "--stuck-timeout",
        type=float,
        default=DEFAULT_STUCK_TIMEOUT,
        help="🟢-without-🟡 stuck threshold (s)",
    )
    p.add_argument("--thread-limit", type=int, default=40, help="max threads to check per run")
    p.add_argument("--env-file", default=None, help="explicit .env to read")
    p.add_argument("--no-report", action="store_true", help="do not post a Discord summary")
    p.add_argument(
        "--dry-run", action="store_true", help="scan + write artifacts; no state write, no post"
    )
    return p.parse_args(argv)


def main(argv: list[str] | None = None) -> int:
    args = parse_args(argv)
    env = load_env_file(Path(args.env_file) if args.env_file else Path(".env"))
    return run(build_config(env, args), args)


if __name__ == "__main__":
    sys.exit(main())
