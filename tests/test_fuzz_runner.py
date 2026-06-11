"""Unit tests for config/env handling in scripts.fuzz.runner (Issue #377)."""

from __future__ import annotations

from pathlib import Path

from scripts.fuzz.runner import build_config, load_env_file, parse_args


def test_load_env_file_parses_and_ignores_comments(tmp_path: Path) -> None:
    p = tmp_path / ".env"
    p.write_text("# comment\nFOO=bar\n\nBAZ = qux \nNOEQ\n")
    env = load_env_file(p)
    assert env["FOO"] == "bar"
    assert env["BAZ"] == "qux"
    assert "NOEQ" not in env


def test_load_env_file_missing_returns_empty(tmp_path: Path) -> None:
    assert load_env_file(tmp_path / "nope.env") == {}


def test_build_config_precedence_cli_over_env() -> None:
    env = {
        "FUZZ_CHANNEL_ID": "111",
        "DISCORD_CHANNEL_ID": "222",
        "CLORD_API_URL": "http://staging:9",
        "DISCORD_BOT_TOKEN": "tok",
    }
    args = parse_args(["--channel", "777"])
    cfg = build_config(env, args)
    assert cfg.inject_channel_id == "777"  # CLI wins
    assert cfg.api_url == "http://staging:9"
    assert cfg.bot_token == "tok"


def test_build_config_fuzz_key_beats_generic_fallback() -> None:
    env = {"FUZZ_CHANNEL_ID": "111", "DISCORD_CHANNEL_ID": "222"}
    cfg = build_config(env, parse_args([]))
    assert cfg.inject_channel_id == "111"


def test_build_config_defaults_when_absent() -> None:
    cfg = build_config({}, parse_args([]))
    assert cfg.api_url == "http://127.0.0.1:8080"
    assert cfg.lease_owner == "fuzz-hourly"
    assert cfg.bot_token is None


def test_parse_args_defaults() -> None:
    args = parse_args([])
    assert args.count == 8
    assert args.inject == "spawn"
    assert args.dry_run is False
    assert args.budget == 1200.0
