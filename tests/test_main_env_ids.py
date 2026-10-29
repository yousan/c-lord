"""#1084: a hand-written .env with a bad ID stops with the reason, not a traceback.

``isdigit()`` is True for ``²`` and the channel id was only checked for being
non-empty, so ``int()`` further down raised ``ValueError`` and ``c-lord start``
printed a traceback that named no setting. Every ID is now "ASCII digits only",
checked where the others are (#451), with the value and how to fix it.
"""

from __future__ import annotations

import logging
import os
import subprocess
import sys
from pathlib import Path

import pytest

from c_lord.main import load_config

_ID_KEYS = (
    "DISCORD_CHANNEL_ID",
    "DISCORD_OWNER_ID",
    "COORDINATION_CHANNEL_ID",
    "EXPECTED_BOT_USER_ID",
)


@pytest.fixture
def base_env(monkeypatch: pytest.MonkeyPatch) -> pytest.MonkeyPatch:
    monkeypatch.setenv("DISCORD_BOT_TOKEN", "tok")
    monkeypatch.setenv("DISCORD_CHANNEL_ID", "999")
    for key in _ID_KEYS[1:]:
        monkeypatch.delenv(key, raising=False)
    return monkeypatch


@pytest.mark.parametrize("key", _ID_KEYS)
@pytest.mark.parametrize("value", ["²", "１２３", "https://discord.com/channels/1/2", "12a"])
def test_a_bad_id_exits_1_naming_the_setting(
    base_env: pytest.MonkeyPatch, caplog: pytest.LogCaptureFixture, key: str, value: str
) -> None:
    base_env.setenv(key, value)
    with caplog.at_level(logging.ERROR, logger="c_lord.main"), pytest.raises(SystemExit) as exc:
        load_config()
    assert exc.value.code == 1
    joined = " ".join(r.getMessage() for r in caplog.records)
    assert key in joined and repr(value) in joined


def test_good_ids_still_load(base_env: pytest.MonkeyPatch) -> None:
    """AC2: a correct .env goes on as before."""
    base_env.setenv("DISCORD_OWNER_ID", "123456789012345678")
    base_env.setenv("COORDINATION_CHANNEL_ID", "1505747831447883806")
    base_env.setenv("EXPECTED_BOT_USER_ID", "1475105094071750818")
    config = load_config()
    assert config["channel_id"] == "999"
    assert config["owner_id"] == "123456789012345678"


@pytest.mark.parametrize(
    "lines",
    [
        ["DISCORD_CHANNEL_ID=https://discord.com/channels/1/2"],
        ["DISCORD_CHANNEL_ID=1", "DISCORD_OWNER_ID=²"],
    ],
    ids=["channel-url", "owner-superscript"],
)
def test_c_lord_start_with_that_env_prints_the_reason_not_a_traceback(
    tmp_path: Path, lines: list[str]
) -> None:
    """AC1, end to end: the Issue's two .env files, through ``python -m c_lord.main``."""
    env_file = tmp_path / ".env"
    env_file.write_text("\n".join(["DISCORD_BOT_TOKEN=x", *lines]) + "\n")
    env = {k: v for k, v in os.environ.items() if not k.startswith(("DISCORD_", "CLORD_"))}
    env["CLORD_ENV_PATH"] = str(env_file)
    for key in _ID_KEYS:
        env.pop(key, None)
    result = subprocess.run(
        [sys.executable, "-m", "c_lord.main"],
        cwd=tmp_path,
        env=env,
        capture_output=True,
        text=True,
        timeout=60,
    )
    output = result.stdout + result.stderr
    assert result.returncode == 1, output
    assert "Traceback" not in output, output
    assert "must be a numeric" in output, output
