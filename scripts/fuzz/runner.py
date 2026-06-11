"""Fuzz run orchestration + config + lease + CLI (Issue #377).

Flow (hourly cron, run from inside the staging clone):

    borrow staging lease  ──(occupied by another owner)──▶  skip, exit 0
        │ got it
        ▼
    ensure bot is up (optionally restart) ─▶ generate scenarios (claude CLI)
        ─▶ inject + observe each ─▶ detect anomalies ─▶ write json/md artifacts
        ─▶ post summary to #fuzz-report ─▶ release lease (always, in finally)

Everything outward-facing is gated by config that the operator fills in via
``.env`` (see ``.env.example`` and ``docs/specs/fuzz-harness.md``). With
``--dry-run`` the harness only generates + parses scenarios (no lease, no
injection, no Discord), which is enough to smoke the generation path.
"""

from __future__ import annotations

import argparse
import contextlib
import json
import subprocess
import sys
from dataclasses import dataclass
from datetime import datetime
from pathlib import Path

from .discord_io import FuzzClient, inject_and_observe
from .generator import GenerationError, generate_scenarios
from .oracle import detect_anomalies, fingerprint
from .report import build_report, render_discord_summary, render_markdown

DEFAULT_API_URL = "http://127.0.0.1:8080"
DEFAULT_OUT_DIR = "docs/fuzz-runs"


def log(msg: str) -> None:
    print(f"[fuzz] {msg}", flush=True)


# --------------------------------------------------------------------------
# Config
# --------------------------------------------------------------------------
def load_env_file(path: Path) -> dict[str, str]:
    """Parse a ``.env`` file into a dict (last definition wins). Missing → {}."""
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
class FuzzConfig:
    api_url: str
    api_secret: str | None
    bot_token: str | None
    inject_channel_id: str | None
    report_channel_id: str | None
    webhook_url: str | None
    webhook_thread_id: str | None
    guild_id: str | None
    lease_owner: str
    staging_dir: str | None
    out_dir: str
    claude_cmd: str
    model: str


def build_config(env: dict[str, str], args: argparse.Namespace) -> FuzzConfig:
    """Merge ``.env`` values, process env, and CLI args into a FuzzConfig.

    Precedence: explicit CLI arg > FUZZ_* key > generic fallback key > default.
    """

    def pick(*keys: str, default: str | None = None) -> str | None:
        for k in keys:
            v = env.get(k)
            if v:
                return v
        return default

    return FuzzConfig(
        api_url=pick("FUZZ_API_URL", "CLORD_API_URL", default=DEFAULT_API_URL) or DEFAULT_API_URL,
        api_secret=pick("CLORD_API_SECRET"),
        bot_token=pick("DISCORD_BOT_TOKEN"),
        inject_channel_id=args.channel or pick("FUZZ_CHANNEL_ID", "DISCORD_CHANNEL_ID"),
        report_channel_id=args.report_channel
        or pick("FUZZ_REPORT_CHANNEL_ID", "DISCORD_CHANNEL_ID"),
        webhook_url=pick("FUZZ_WEBHOOK_URL", "E2E_TEST_WEBHOOK_URL"),
        webhook_thread_id=pick("FUZZ_TEST_THREAD_ID", "E2E_TEST_THREAD_ID"),
        guild_id=pick("FUZZ_GUILD_ID", "DISCORD_GUILD_ID"),
        lease_owner=pick("CLORD_LEASE_OWNER", default="fuzz-hourly") or "fuzz-hourly",
        staging_dir=args.staging_dir or env.get("FUZZ_STAGING_CLONE_DIR"),
        out_dir=args.out_dir,
        claude_cmd=pick("CLAUDE_COMMAND", default="claude") or "claude",
        model=args.model,
    )


# --------------------------------------------------------------------------
# Lease (delegates to scripts/staging.sh in the staging clone)
# --------------------------------------------------------------------------
def _staging(cfg: FuzzConfig, *staging_args: str) -> subprocess.CompletedProcess:
    cwd = cfg.staging_dir or "."
    return subprocess.run(
        ["bash", "scripts/staging.sh", *staging_args],
        cwd=cwd,
        capture_output=True,
        text=True,
        check=False,
    )


def borrow_lease(cfg: FuzzConfig, purpose: str) -> bool:
    """Borrow the staging lease. Returns False if another owner holds it."""
    proc = _staging(
        cfg, "borrow", "--owner", cfg.lease_owner, "--purpose", purpose, "--ttl-hours", "1"
    )
    if proc.returncode == 0:
        log(proc.stdout.strip().splitlines()[-1] if proc.stdout.strip() else "leased")
        return True
    log(f"lease busy → skip this run: {proc.stderr.strip() or proc.stdout.strip()}")
    return False


def release_lease(cfg: FuzzConfig) -> None:
    proc = _staging(cfg, "release", "--owner", cfg.lease_owner)
    log(proc.stdout.strip() or proc.stderr.strip() or "released")


def restart_bot(cfg: FuzzConfig) -> None:
    log("bot health down → restarting staging (current branch)")
    proc = _staging(cfg, "restart")
    log(proc.stdout.strip().splitlines()[-1] if proc.stdout.strip() else "restart attempted")


# --------------------------------------------------------------------------
# Run
# --------------------------------------------------------------------------
def _run_once(cfg: FuzzConfig, args: argparse.Namespace) -> dict:
    """Generate → inject → observe → detect → report. Returns the report dict."""
    run_id = datetime.now().strftime("%Y%m%d-%H%M%S")
    started_at = datetime.now().isoformat(timespec="seconds")
    out_dir = Path(cfg.out_dir)
    out_dir.mkdir(parents=True, exist_ok=True)

    log(f"generating {args.count} scenario(s) via {cfg.claude_cmd} ({cfg.model})")
    scenarios, raw = generate_scenarios(
        args.count,
        focus=args.focus,
        claude_cmd=cfg.claude_cmd,
        model=cfg.model,
        timeout=args.gen_timeout,
    )
    gen_path = out_dir / f"{run_id}.gen.txt"
    gen_path.write_text(raw)
    log(f"generated {len(scenarios)} scenario(s); raw → {gen_path}")

    if not scenarios:
        log(f"WARNING: zero scenarios parsed — inspect raw at {gen_path}")
        return build_report(
            run_id=run_id,
            started_at=started_at,
            finished_at=datetime.now().isoformat(timespec="seconds"),
            branch=_git_branch(cfg.staging_dir),
            inject_mode=args.inject if not args.dry_run else "dry-run",
            scenarios=[],
            observations=[],
            anomalies=[],
            generation_raw_path=str(gen_path),
        )

    if args.dry_run:
        for s in scenarios:
            log(f"  [{s.id}] ({s.category}) {s.text[:80]!r}")
        return build_report(
            run_id=run_id,
            started_at=started_at,
            finished_at=datetime.now().isoformat(timespec="seconds"),
            branch=_git_branch(cfg.staging_dir),
            inject_mode="dry-run",
            scenarios=scenarios,
            observations=[],
            anomalies=[],
            generation_raw_path=str(gen_path),
        )

    client = FuzzClient(
        bot_token=cfg.bot_token or "",
        api_url=cfg.api_url,
        api_secret=cfg.api_secret,
        webhook_url=cfg.webhook_url,
    )
    bot_id = _bot_user_id(client)

    if not client.health():
        if args.restart_if_down and cfg.staging_dir:
            restart_bot(cfg)
        if not client.health():
            log("WARNING: bot /api/health is down — injections will likely fail")

    deadline = _monotonic() + args.budget
    observations = []
    for s in scenarios:
        if _monotonic() > deadline:
            log(f"budget {args.budget}s exhausted — skipping remaining scenarios")
            break
        log(f"injecting [{s.id}] ({s.category}) via {args.inject}")
        try:
            obs = inject_and_observe(
                client,
                s,
                mode=args.inject,
                channel_id=cfg.inject_channel_id or "",
                bot_id=bot_id,
                webhook_thread_id=cfg.webhook_thread_id,
                timeout=args.timeout,
                poll=args.poll,
            )
        except Exception as exc:  # one bad scenario must not kill the run
            from .oracle import Observation

            obs = Observation(
                s.id, s.category, False, None, False, None, [], None, client.health(), str(exc)
            )
        observations.append(obs)
        anomalies_here = detect_anomalies(obs)
        verdict = (
            "clean"
            if not anomalies_here
            else "ANOMALY: " + ",".join(a.kind for a in anomalies_here)
        )
        log(f"  [{s.id}] replied={obs.replied} reactions={obs.reactions} → {verdict}")

    anomalies = [a for o in observations for a in detect_anomalies(o)]
    seen_path = out_dir / "seen.json"
    seen = _load_seen(seen_path)
    report = build_report(
        run_id=run_id,
        started_at=started_at,
        finished_at=datetime.now().isoformat(timespec="seconds"),
        branch=_git_branch(cfg.staging_dir),
        inject_mode=args.inject,
        scenarios=scenarios,
        observations=observations,
        anomalies=anomalies,
        generation_raw_path=str(gen_path),
        seen_fingerprints=seen,
    )

    (out_dir / f"{run_id}.json").write_text(json.dumps(report, ensure_ascii=False, indent=2))
    (out_dir / f"{run_id}.md").write_text(render_markdown(report))
    _save_seen(seen_path, seen | {fingerprint(a) for a in anomalies})
    log(
        f"report → {out_dir}/{run_id}.json (.md) · "
        f"{report['counts']['anomalies']} anomaly candidate(s)"
    )

    if not args.no_report and cfg.report_channel_id and cfg.bot_token:
        summary = render_discord_summary(report, guild_id=cfg.guild_id)
        ok, err = client.post_message(cfg.report_channel_id, summary)
        log(f"posted summary to #{cfg.report_channel_id}" if ok else f"report post failed: {err}")

    return report


def run(cfg: FuzzConfig, args: argparse.Namespace) -> int:
    if args.dry_run or args.no_lease:
        _run_once(cfg, args)
        return 0
    if not borrow_lease(cfg, purpose=args.purpose):
        return 0  # occupied by another owner — skip this hour cleanly
    try:
        _run_once(cfg, args)
    finally:
        release_lease(cfg)
    return 0


# --------------------------------------------------------------------------
# small helpers (kept thin; the testable logic lives in the pure modules)
# --------------------------------------------------------------------------
def _monotonic() -> float:
    import time

    return time.monotonic()


def _bot_user_id(client: FuzzClient) -> str:
    try:
        return str(client._discord_get("/users/@me")["id"])  # noqa: SLF001 (internal helper)
    except Exception:
        return ""


def _git_branch(cwd: str | None) -> str:
    try:
        proc = subprocess.run(
            ["git", "branch", "--show-current"],
            cwd=cwd or ".",
            capture_output=True,
            text=True,
            check=False,
        )
        return proc.stdout.strip() or "(unknown)"
    except Exception:
        return "(unknown)"


def _load_seen(path: Path) -> set[str]:
    if not path.exists():
        return set()
    try:
        return set(json.loads(path.read_text()))
    except (ValueError, OSError):
        return set()


def _save_seen(path: Path, fps: set[str]) -> None:
    with contextlib.suppress(OSError):
        path.write_text(json.dumps(sorted(fps), indent=1))


# --------------------------------------------------------------------------
# CLI
# --------------------------------------------------------------------------
def parse_args(argv: list[str] | None = None) -> argparse.Namespace:
    p = argparse.ArgumentParser(
        prog="python -m scripts.fuzz",
        description="Natural-language fuzzing harness for c-lord (Issue #377).",
    )
    p.add_argument("-n", "--count", type=int, default=8, help="scenarios per run (default 8)")
    p.add_argument(
        "--inject",
        choices=("spawn", "webhook"),
        default="spawn",
        help="spawn = fresh thread per scenario (default); webhook = multi-turn into one thread",
    )
    p.add_argument("--focus", default=None, help="bias the generated batch toward a theme")
    p.add_argument("--model", default="haiku", help="claude model for generation (default haiku)")
    p.add_argument("--timeout", type=float, default=150.0, help="per-scenario reply timeout (s)")
    p.add_argument("--poll", type=float, default=4.0, help="reply poll interval (s)")
    p.add_argument(
        "--budget", type=float, default=1200.0, help="overall wall-clock budget for injection (s)"
    )
    p.add_argument(
        "--gen-timeout", type=float, default=180.0, help="scenario generation timeout (s)"
    )
    p.add_argument("--channel", default=None, help="override inject channel id")
    p.add_argument("--report-channel", default=None, help="override report channel id")
    p.add_argument("--staging-dir", default=None, help="staging clone dir for lease management")
    p.add_argument(
        "--env-file",
        default=None,
        help="explicit .env to read (default: <staging-dir>/.env or ./.env)",
    )
    p.add_argument("--out-dir", default=DEFAULT_OUT_DIR, help="artifact output dir")
    p.add_argument("--purpose", default="hourly fuzz run (#377)", help="lease purpose string")
    p.add_argument(
        "--dry-run", action="store_true", help="generate + parse only; no lease/inject/report"
    )
    p.add_argument("--no-lease", action="store_true", help="skip staging lease (local/dev)")
    p.add_argument("--no-report", action="store_true", help="do not post a Discord summary")
    p.add_argument(
        "--restart-if-down", action="store_true", help="restart staging if /api/health is down"
    )
    return p.parse_args(argv)


def main(argv: list[str] | None = None) -> int:
    args = parse_args(argv)
    env_path = (
        Path(args.env_file)
        if args.env_file
        else (Path(args.staging_dir) / ".env" if args.staging_dir else Path(".env"))
    )
    env = load_env_file(env_path)
    cfg = build_config(env, args)
    try:
        return run(cfg, args)
    except GenerationError as exc:
        log(f"generation failed: {exc}")
        return 1


if __name__ == "__main__":
    sys.exit(main())
