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
    inject_mode: str
    skip_health: bool


def build_config(
    env: dict[str, str], args: argparse.Namespace, *, staging_dir: str | None = None
) -> FuzzConfig:
    """Merge ``.env`` values and CLI args into a FuzzConfig for one staging clone.

    Precedence: explicit CLI arg > FUZZ_* key > generic fallback key > default.

    ``staging_dir`` (when given) is the specific clone this config targets — each
    fleet clone has its own ``.env``, so ``env`` should be that clone's. The
    injection mode is resolved here: a clone running ``CLORD_BRIDGE_MODE=jsonl``
    does not bind its REST API, so ``spawn`` + ``/api/health`` are unreachable —
    we default such a clone to ``webhook`` + ``skip_health`` unless the operator
    passed an explicit ``--inject``.
    """

    def pick(*keys: str, default: str | None = None) -> str | None:
        for k in keys:
            v = env.get(k)
            if v:
                return v
        return default

    bridge_jsonl = (env.get("CLORD_BRIDGE_MODE") or "").strip().lower() == "jsonl"
    explicit_inject = getattr(args, "inject", None)
    inject_mode = explicit_inject or ("webhook" if bridge_jsonl else "spawn")
    skip_health = bool(getattr(args, "skip_health", False)) or (
        bridge_jsonl and inject_mode == "webhook"
    )
    resolved_staging = (
        staging_dir
        if staging_dir is not None
        else (args.staging_dir or env.get("FUZZ_STAGING_CLONE_DIR"))
    )

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
        staging_dir=resolved_staging,
        out_dir=args.out_dir,
        claude_cmd=pick("CLAUDE_COMMAND", default="claude") or "claude",
        model=args.model,
        inject_mode=inject_mode,
        skip_health=skip_health,
    )


def candidate_clones(args: argparse.Namespace, env: dict[str, str]) -> list[str]:
    """Ordered list of staging clone dirs to try (fleet rotation).

    ``--staging-clones`` / ``FUZZ_STAGING_CLONES`` (comma-separated) wins; else a
    single ``--staging-dir`` / ``FUZZ_STAGING_CLONE_DIR``; else the cwd (``.``).
    """
    raw = getattr(args, "staging_clones", None) or env.get("FUZZ_STAGING_CLONES")
    if raw:
        clones = [c.strip() for c in raw.split(",") if c.strip()]
        if clones:
            return clones
    single = args.staging_dir or env.get("FUZZ_STAGING_CLONE_DIR")
    return [single] if single else ["."]


# --------------------------------------------------------------------------
# Lease (delegates to scripts/staging.sh in the staging clone)
# --------------------------------------------------------------------------
def _staging(cfg: FuzzConfig, *staging_args: str) -> subprocess.CompletedProcess:
    cwd = cfg.staging_dir or "."
    # A clone dir can vanish under us (the fleet rename did exactly this) — treat
    # a missing cwd as a non-zero result, not a FileNotFoundError traceback.
    if not Path(cwd).is_dir():
        return subprocess.CompletedProcess(
            args=list(staging_args), returncode=127, stdout="", stderr=f"no such clone dir: {cwd}"
        )
    try:
        return subprocess.run(
            ["bash", "scripts/staging.sh", *staging_args],
            cwd=cwd,
            capture_output=True,
            text=True,
            check=False,
        )
    except (FileNotFoundError, NotADirectoryError) as exc:
        return subprocess.CompletedProcess(
            args=list(staging_args), returncode=127, stdout="", stderr=str(exc)
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
            inject_mode=cfg.inject_mode if not args.dry_run else "dry-run",
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
        skip_health=cfg.skip_health,
    )
    bot_id = _bot_user_id(client)

    if not client.health():
        if args.restart_if_down and cfg.staging_dir:
            restart_bot(cfg)
        if not client.health():
            log(
                f"WARNING: bot /api/health is unreachable at {cfg.api_url} — spawn injections "
                "will fail. Set FUZZ_API_URL to the bot's actual API port, or use "
                "`--inject webhook --skip-health`."
            )

    deadline = _monotonic() + args.budget
    observations = []
    for s in scenarios:
        if _monotonic() > deadline:
            log(f"budget {args.budget}s exhausted — skipping remaining scenarios")
            break
        log(f"injecting [{s.id}] ({s.category}) via {cfg.inject_mode}")
        try:
            obs = inject_and_observe(
                client,
                s,
                mode=cfg.inject_mode,
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
        inject_mode=cfg.inject_mode,
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


def _config_for(clone: str, args: argparse.Namespace, root_env: dict[str, str]) -> FuzzConfig:
    """Build the config for one fleet clone, reading that clone's own ``.env``."""
    env = load_env_file(Path(clone) / ".env") if clone not in (".", None) else root_env
    return build_config(env, args, staging_dir=clone)


def run_fleet(clones: list[str], args: argparse.Namespace, root_env: dict[str, str]) -> int:
    """Try each staging clone in order; run on the first one we can lease.

    ``dry-run`` / ``no-lease`` use the first clone's config and skip the lease
    dance. When every clone is busy or absent, skip cleanly (exit 0).
    """
    if args.dry_run or args.no_lease:
        _run_once(_config_for(clones[0], args, root_env), args)
        return 0
    for clone in clones:
        cfg = _config_for(clone, args, root_env)
        if borrow_lease(cfg, purpose=args.purpose):
            try:
                _run_once(cfg, args)
            finally:
                release_lease(cfg)
            return 0
    log(f"all {len(clones)} staging target(s) busy/absent → skip this round")
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
        default=None,
        help="spawn = fresh thread/scenario; webhook = multi-turn into one thread. "
        "Default: auto — webhook on CLORD_BRIDGE_MODE=jsonl clones (API unbound), else spawn.",
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
    p.add_argument("--staging-dir", default=None, help="single staging clone dir for lease mgmt")
    p.add_argument(
        "--staging-clones",
        default=None,
        help="comma-separated staging clone dirs (fleet); runs on the first one it can lease",
    )
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
    p.add_argument(
        "--skip-health",
        action="store_true",
        help="skip the /api/health probe (webhook-only injection, or unreachable API)",
    )
    return p.parse_args(argv)


def main(argv: list[str] | None = None) -> int:
    args = parse_args(argv)
    # Root env supplies the fleet list (FUZZ_STAGING_CLONES) and single-clone
    # fallbacks; per-clone tokens/channels are read from each clone's own .env.
    root_env_path = (
        Path(args.env_file)
        if args.env_file
        else (Path(args.staging_dir) / ".env" if args.staging_dir else Path(".env"))
    )
    root_env = load_env_file(root_env_path)
    clones = candidate_clones(args, root_env)
    try:
        return run_fleet(clones, args, root_env)
    except GenerationError as exc:
        log(f"generation failed: {exc}")
        return 1


if __name__ == "__main__":
    sys.exit(main())
