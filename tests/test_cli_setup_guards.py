"""``c-lord setup`` must not report success over a ``.env`` the bot cannot start with.

Each case used to end in "Setup complete" and a ``.env`` that ``c-lord start``
then failed on:

- #1029 — three empty answers at "Bot Token" ran out the attempts through
  ``continue``; only the *invalid*-token branch stopped the wizard, so it went on
  with ``token == ""`` and wrote ``DISCORD_BOT_TOKEN=``.
- #1030 — on the channel list, ``elif len(raw) >= 17`` took any long string (a
  pasted channel URL) as the ID; the manual-entry screen already required digits.
- #1031 — a token pasted as ``Bot xxx`` validated (``validate_token`` accepts the
  prefix) and was written verbatim; discord.py sends ``'Bot ' + token`` at login,
  so ``c-lord start`` sent ``Bot Bot xxx`` and got 401.
"""

from __future__ import annotations

import builtins
import sys
from pathlib import Path
from unittest.mock import AsyncMock, MagicMock

import pytest

from c_lord import cli

CHANNEL = "1" * 18


def _setup(
    monkeypatch: pytest.MonkeyPatch, answers: list[str], *, validate: AsyncMock | None = None
) -> AsyncMock:
    it = iter(answers)

    def fake_input(_prompt: str = "") -> str:
        try:
            return next(it)
        except StopIteration:
            raise EOFError from None

    validate = validate or AsyncMock(return_value="C-lord#0")
    monkeypatch.setattr(builtins, "input", fake_input)
    monkeypatch.setattr(cli, "validate_token", validate)
    monkeypatch.setattr(cli, "fetch_channels", AsyncMock(return_value=[(CHANNEL, "general", "S")]))
    monkeypatch.setattr(cli, "check_claude_cli", MagicMock(return_value=True))
    monkeypatch.setattr(cli, "_run_start", AsyncMock())
    monkeypatch.setattr(sys.stdin, "isatty", lambda: False, raising=False)
    return validate


def _values(path: Path) -> dict[str, str]:
    lines = path.read_text().splitlines()
    return dict(line.split("=", 1) for line in lines if "=" in line and not line.startswith("#"))


def test_three_empty_tokens_stop_without_writing(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    env = tmp_path / ".env"
    _setup(monkeypatch, ["", "", "", "1", "", "/w", ""])

    with pytest.raises(SystemExit) as exc:
        cli.cmd_setup(env)

    assert exc.value.code == 1
    assert not env.exists()


def test_an_empty_answer_then_a_valid_token_goes_on(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    env = tmp_path / ".env"
    _setup(monkeypatch, ["", "abc.def", "1", "", "/w", ""])

    cli.cmd_setup(env)

    assert _values(env)["DISCORD_BOT_TOKEN"] == "abc.def"


def test_a_pasted_channel_url_is_asked_again(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch, capsys: pytest.CaptureFixture[str]
) -> None:
    env = tmp_path / ".env"
    url = "https://discord.com/channels/1285582352596209694/1505747831447883806"
    _setup(monkeypatch, ["abc.def", url, "1", "", "/w", ""])

    cli.cmd_setup(env)

    assert _values(env)["DISCORD_CHANNEL_ID"] == CHANNEL
    assert "doesn't look like a valid channel ID" in capsys.readouterr().out


def test_a_pasted_numeric_channel_id_is_still_taken(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    env = tmp_path / ".env"
    _setup(monkeypatch, ["abc.def", "1505747831447883806", "", "/w", ""])

    cli.cmd_setup(env)

    assert _values(env)["DISCORD_CHANNEL_ID"] == "1505747831447883806"


def test_a_bot_prefixed_token_is_written_without_the_prefix(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    env = tmp_path / ".env"
    validate = _setup(monkeypatch, ["Bot abc.def", "1", "", "/w", ""])

    cli.cmd_setup(env)

    assert _values(env)["DISCORD_BOT_TOKEN"] == "abc.def"
    validate.assert_awaited_once_with("abc.def")
