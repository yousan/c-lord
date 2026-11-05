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


_COUNT_KEYS = ("MAX_CONCURRENT_SESSIONS", "SESSION_TIMEOUT_SECONDS")


@pytest.mark.parametrize("key", ["DISCORD_CHANNEL_ID", "COORDINATION_CHANNEL_ID"])
def test_id_with_surrounding_space_in_quoted_env_still_starts(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch, key: str
) -> None:
    """#1194: ``KEY="1505… "`` started before #1084 (``int()`` ignores the spaces)
    and stopped with exit 1 after it — an update alone broke a working .env. The
    counts in the same file (#1119) already allow the spaces; the IDs do now too."""
    for k in (*_ID_KEYS, *_COUNT_KEYS):
        monkeypatch.delenv(k, raising=False)
    env = tmp_path / ".env"
    lines = ["DISCORD_BOT_TOKEN=x", "DISCORD_CHANNEL_ID=1"]
    lines.append(f'{key}="1505747831447883806 "')
    env.write_text("\n".join(lines) + "\n")
    config = load_config(env)  # was: SystemExit(1)
    int(config["channel_id"])  # what main() does next
    if config["coordination_channel_id"]:
        int(config["coordination_channel_id"])


@pytest.mark.parametrize("key", _ID_KEYS)
def test_every_id_is_used_without_its_spaces(base_env: pytest.MonkeyPatch, key: str) -> None:
    base_env.setenv(key, " 1505747831447883806\t")
    config = load_config()
    loaded = {
        "DISCORD_CHANNEL_ID": config["channel_id"],
        "DISCORD_OWNER_ID": config["owner_id"],
        "COORDINATION_CHANNEL_ID": config["coordination_channel_id"],
        "EXPECTED_BOT_USER_ID": config["expected_bot_user_id"],
    }[key]
    assert loaded == "1505747831447883806"


def test_counts_in_the_same_file_accept_the_same_spaces(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    """The contrast in #1194: #1119 strips spaces for counts."""
    for k in (*_ID_KEYS, *_COUNT_KEYS):
        monkeypatch.delenv(k, raising=False)
    env = tmp_path / ".env"
    env.write_text('DISCORD_BOT_TOKEN=x\nDISCORD_CHANNEL_ID=1\nMAX_CONCURRENT_SESSIONS=" 4 "\n')
    assert load_config(env)["max_concurrent"] == "4"


# --- #1194 (review): the three places that read COORDINATION_CHANNEL_ID themselves ---


def _api(tmp_path):  # noqa: ANN001, ANN202
    from unittest.mock import MagicMock

    from c_lord.database.notification_repo import NotificationRepository
    from c_lord.ext.api_server import ApiServer

    return ApiServer(
        repo=NotificationRepository(str(tmp_path / "n.db")),
        bot=MagicMock(),
        default_channel_id=1,
        port=0,
    )


def test_api_server_lounge_uses_a_coordination_id_with_spaces(tmp_path, monkeypatch) -> None:  # noqa: ANN001
    monkeypatch.setenv("COORDINATION_CHANNEL_ID", " 1505747831447883806 ")
    assert _api(tmp_path).lounge_channel_id == 1505747831447883806


def test_claude_chat_coordination_uses_an_id_with_spaces(monkeypatch) -> None:  # noqa: ANN001
    from unittest.mock import MagicMock

    from c_lord.cogs.claude_chat import ClaudeChatCog

    monkeypatch.setenv("COORDINATION_CHANNEL_ID", " 1505747831447883806 ")
    cog = ClaudeChatCog.__new__(ClaudeChatCog)
    cog._coordination = None
    cog.bot = MagicMock(spec=[])
    svc = cog._get_coordination()
    assert getattr(svc, "channel_id", getattr(svc, "_channel_id", None)) == 1505747831447883806


@pytest.mark.parametrize("value", ["²", "１２３"])
def test_a_non_ascii_digit_coordination_id_does_not_raise(tmp_path, monkeypatch, value) -> None:  # noqa: ANN001
    """For callers of ApiServer / setup_bridge that skip load_config's #1084 check."""
    monkeypatch.setenv("COORDINATION_CHANNEL_ID", value)
    assert _api(tmp_path).lounge_channel_id is None


@pytest.mark.parametrize(
    ("value", "expected"),
    [(" 1505747831447883806 ", 1505747831447883806), ("²", None), ("１２３", None), ("", None)],
)
def test_setup_bridge_lounge_reads_the_coordination_id_the_same_way(
    monkeypatch, value, expected
) -> None:  # noqa: ANN001
    """setup.py, api_server.py and claude_chat.py share one reading (review of #1194)."""
    import inspect

    from c_lord import setup as setup_mod
    from c_lord.coordination.env import coordination_channel_id_from_env

    monkeypatch.setenv("COORDINATION_CHANNEL_ID", value)
    assert coordination_channel_id_from_env() == expected
    assert "coordination_channel_id_from_env()" in inspect.getsource(setup_mod.setup_bridge)


def test_claude_chat_coordination_ignores_a_superscript_id(monkeypatch) -> None:  # noqa: ANN001
    from unittest.mock import MagicMock

    from c_lord.cogs.claude_chat import ClaudeChatCog

    monkeypatch.setenv("COORDINATION_CHANNEL_ID", "²")
    cog = ClaudeChatCog.__new__(ClaudeChatCog)
    cog._coordination = None
    cog.bot = MagicMock(spec=[])
    svc = cog._get_coordination()
    assert getattr(svc, "channel_id", getattr(svc, "_channel_id", None)) is None
