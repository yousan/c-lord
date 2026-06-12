"""Unit tests for fleet rotation + jsonl-bridge defaults + robustness (#395).

These cover the follow-up to #377: the harness must rotate across a staging
fleet, default to webhook+skip-health on jsonl-bridge bots (where the REST API
is not bound), and treat a missing clone dir as a skip rather than a crash.
"""

from __future__ import annotations

from scripts.fuzz.runner import (
    _staging,
    borrow_lease,
    build_config,
    candidate_clones,
    parse_args,
    run_fleet,
)


# -- candidate_clones --------------------------------------------------------
def test_candidate_clones_csv_arg() -> None:
    args = parse_args(["--staging-clones", "/a, /b ,/c"])
    assert candidate_clones(args, {}) == ["/a", "/b", "/c"]


def test_candidate_clones_env_csv() -> None:
    assert candidate_clones(parse_args([]), {"FUZZ_STAGING_CLONES": "/x,/y"}) == ["/x", "/y"]


def test_candidate_clones_single_dir() -> None:
    assert candidate_clones(parse_args(["--staging-dir", "/one"]), {}) == ["/one"]


def test_candidate_clones_default_cwd() -> None:
    assert candidate_clones(parse_args([]), {}) == ["."]


# -- jsonl-bridge inject resolution -----------------------------------------
def test_build_config_jsonl_defaults_to_webhook_skiphealth() -> None:
    cfg = build_config({"CLORD_BRIDGE_MODE": "jsonl"}, parse_args([]))
    assert cfg.inject_mode == "webhook"
    assert cfg.skip_health is True


def test_build_config_non_jsonl_defaults_spawn() -> None:
    cfg = build_config({}, parse_args([]))
    assert cfg.inject_mode == "spawn"
    assert cfg.skip_health is False


def test_build_config_explicit_inject_overrides_jsonl() -> None:
    cfg = build_config({"CLORD_BRIDGE_MODE": "jsonl"}, parse_args(["--inject", "spawn"]))
    assert cfg.inject_mode == "spawn"


def test_build_config_explicit_skip_health_flag() -> None:
    cfg = build_config({}, parse_args(["--skip-health"]))
    assert cfg.skip_health is True


# -- robustness: missing clone dir -------------------------------------------
def test_staging_missing_dir_no_traceback() -> None:
    cfg = build_config({}, parse_args([]), staging_dir="/no/such/dir/xyz123")
    proc = _staging(cfg, "status")  # must not raise FileNotFoundError
    assert proc.returncode != 0


def test_borrow_lease_missing_dir_returns_false() -> None:
    cfg = build_config({}, parse_args([]), staging_dir="/no/such/dir/xyz123")
    assert borrow_lease(cfg, "test") is False


# -- fleet rotation ----------------------------------------------------------
def test_run_fleet_tries_in_order_until_borrow_succeeds(monkeypatch) -> None:
    tried: list[str] = []
    ran: list[str] = []
    monkeypatch.setattr("scripts.fuzz.runner.load_env_file", lambda p: {})
    monkeypatch.setattr(
        "scripts.fuzz.runner.borrow_lease",
        lambda cfg, purpose: tried.append(cfg.staging_dir) or (cfg.staging_dir == "/b"),
    )
    monkeypatch.setattr(
        "scripts.fuzz.runner._run_once", lambda cfg, args: ran.append(cfg.staging_dir)
    )
    monkeypatch.setattr("scripts.fuzz.runner.release_lease", lambda cfg: None)
    rc = run_fleet(["/a", "/b", "/c"], parse_args(["--staging-clones", "/a,/b,/c"]), {})
    assert rc == 0
    assert tried == ["/a", "/b"]  # stopped at first free
    assert ran == ["/b"]


def test_run_fleet_all_busy_runs_nothing(monkeypatch) -> None:
    ran: list[int] = []
    monkeypatch.setattr("scripts.fuzz.runner.load_env_file", lambda p: {})
    monkeypatch.setattr("scripts.fuzz.runner.borrow_lease", lambda cfg, purpose: False)
    monkeypatch.setattr("scripts.fuzz.runner._run_once", lambda cfg, args: ran.append(1))
    monkeypatch.setattr("scripts.fuzz.runner.release_lease", lambda cfg: None)
    rc = run_fleet(["/a", "/b"], parse_args([]), {})
    assert rc == 0
    assert ran == []
