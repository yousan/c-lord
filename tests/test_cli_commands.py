"""Pin the current behaviour of the ``c-lord`` CLI (``c_lord/cli.py``).

``tests/test_cli.py`` covers the pure helpers' happy paths. This file pins what an
operator actually runs, as documented:

* README.md "Step 2 — Run the Setup Wizard" — ``c-lord setup`` (token → channel
  list → working dir → model → writes ``.env`` → offers to start)
* README.md "Start / Stop" — ``c-lord start`` / ``c-lord start --env /path/to/.env``
* docs/macos.md — ``uv run c-lord setup`` then ``uv run c-lord start``
* CHANGELOG / #255 — ``c-lord version`` prints the article-format build string
* #202 (PR #249) — ``c-lord start --env <path>`` must hand the path to ``main()``

Nothing here touches the network, real Discord, the real ``claude`` binary or a
real ``.env``: every boundary is mocked and every file lives in ``tmp_path``.
"""

from __future__ import annotations

import builtins
import sys
from collections.abc import Iterator
from pathlib import Path
from unittest.mock import AsyncMock, MagicMock, patch

import pytest

from c_lord import cli

CHANNEL_A = "111111111111111111"
CHANNEL_B = "222222222222222222"
PASTED_ID = "333333333333333333"


# ---------------------------------------------------------------------------
# helpers
# ---------------------------------------------------------------------------


def _feed(monkeypatch: pytest.MonkeyPatch, answers: list[str]) -> list[str]:
    """Replace input() with a scripted list; EOF once exhausted. Returns the prompts."""
    it: Iterator[str] = iter(answers)
    prompts: list[str] = []

    def fake_input(prompt: str = "") -> str:
        prompts.append(prompt)
        try:
            return next(it)
        except StopIteration:
            raise EOFError from None

    monkeypatch.setattr(builtins, "input", fake_input)
    return prompts


def _wizard_env(
    monkeypatch: pytest.MonkeyPatch,
    *,
    claude_ok: bool = True,
    token_results: list[str | None] | None = None,
    channels: list[tuple[str, str, str]] | None = None,
    tty: bool = False,
) -> dict[str, MagicMock]:
    """Mock every external boundary the setup wizard touches."""
    validate = AsyncMock(side_effect=token_results or ["MyBot#1234"])
    fetch = AsyncMock(
        return_value=(
            channels
            if channels is not None
            else [(CHANNEL_A, "general", "My Server"), (CHANNEL_B, "claude-code", "My Server")]
        )
    )
    run_start = AsyncMock()
    check = MagicMock(return_value=claude_ok)
    monkeypatch.setattr(cli, "validate_token", validate)
    monkeypatch.setattr(cli, "fetch_channels", fetch)
    monkeypatch.setattr(cli, "check_claude_cli", check)
    monkeypatch.setattr(cli, "_run_start", run_start)
    monkeypatch.setattr(sys.stdin, "isatty", lambda: tty, raising=False)
    return {"validate": validate, "fetch": fetch, "run_start": run_start, "check": check}


def _env_lines(path: Path) -> dict[str, str]:
    out: dict[str, str] = {}
    for line in path.read_text(encoding="utf-8").splitlines():
        if "=" in line and not line.startswith("#"):
            k, v = line.split("=", 1)
            out[k] = v
    return out


def _http_resp(status: int, data: object) -> MagicMock:
    resp = MagicMock()
    resp.status = status
    resp.json = AsyncMock(return_value=data)
    resp.__aenter__ = AsyncMock(return_value=resp)
    resp.__aexit__ = AsyncMock(return_value=False)
    return resp


def _http_session(responses: list[MagicMock]) -> MagicMock:
    session = MagicMock()
    session.get = MagicMock(side_effect=responses)
    session.__aenter__ = AsyncMock(return_value=session)
    session.__aexit__ = AsyncMock(return_value=False)
    return session


# ---------------------------------------------------------------------------
# main() — argument parsing / routing
# ---------------------------------------------------------------------------


class TestMainRouting:
    def test_start_with_env_routes_path(self, monkeypatch: pytest.MonkeyPatch) -> None:
        """README "Start / Stop": `c-lord start --env /path/to/.env` uses that file."""
        called = MagicMock()
        monkeypatch.setattr(cli, "cmd_start", called)
        monkeypatch.setattr(sys, "argv", ["c-lord", "start", "--env", "/srv/bot/.env"])
        cli.main()
        called.assert_called_once_with(Path("/srv/bot/.env"))

    def test_start_defaults_to_cwd_env(self, monkeypatch: pytest.MonkeyPatch) -> None:
        """README: `c-lord start` reads .env in the current dir."""
        called = MagicMock()
        monkeypatch.setattr(cli, "cmd_start", called)
        monkeypatch.setattr(sys, "argv", ["c-lord", "start"])
        cli.main()
        called.assert_called_once_with(Path(".env"))

    def test_setup_default_and_custom_env(self, monkeypatch: pytest.MonkeyPatch) -> None:
        """README Step 2: `c-lord setup` writes ./.env; `--env PATH` writes elsewhere."""
        called = MagicMock()
        monkeypatch.setattr(cli, "cmd_setup", called)
        monkeypatch.setattr(sys, "argv", ["c-lord", "setup"])
        cli.main()
        monkeypatch.setattr(sys, "argv", ["c-lord", "setup", "--env", "conf/.env"])
        cli.main()
        assert [c.args for c in called.call_args_list] == [(Path(".env"),), (Path("conf/.env"),)]

    def test_version_routes_to_cmd_version(self, monkeypatch: pytest.MonkeyPatch) -> None:
        """#255 / CHANGELOG: `c-lord version` reports the running build."""
        called = MagicMock()
        monkeypatch.setattr(cli, "cmd_version", called)
        monkeypatch.setattr(sys, "argv", ["c-lord", "version"])
        cli.main()
        called.assert_called_once_with()

    def test_help_lists_all_subcommands(
        self, monkeypatch: pytest.MonkeyPatch, capsys: pytest.CaptureFixture[str]
    ) -> None:
        """`c-lord --help` exits 0 and names setup / start / version (module docstring)."""
        monkeypatch.setattr(sys, "argv", ["c-lord", "--help"])
        with pytest.raises(SystemExit) as exc:
            cli.main()
        assert exc.value.code == 0
        out = capsys.readouterr().out
        for name in ("setup", "start", "version"):
            assert name in out

    def test_unknown_option_on_start_is_usage_error(self, monkeypatch: pytest.MonkeyPatch) -> None:
        """argparse rejects unknown flags with exit 2 (before anything starts)."""
        started = MagicMock()
        monkeypatch.setattr(cli, "cmd_start", started)
        monkeypatch.setattr(sys, "argv", ["c-lord", "start", "--bogus"])
        with pytest.raises(SystemExit) as exc:
            cli.main()
        assert exc.value.code == 2
        started.assert_not_called()


# ---------------------------------------------------------------------------
# c-lord version
# ---------------------------------------------------------------------------


class TestCmdVersion:
    def test_prints_resolved_version(
        self, monkeypatch: pytest.MonkeyPatch, capsys: pytest.CaptureFixture[str]
    ) -> None:
        """#255: prints the article-format string, e.g. v1.4.0-b599631-20251203."""
        import c_lord.version as version_mod

        monkeypatch.setattr(version_mod, "resolve_version", lambda: "v1.4.0-b599631-20251203")
        cli.cmd_version()
        assert capsys.readouterr().out == "v1.4.0-b599631-20251203\n"


# ---------------------------------------------------------------------------
# c-lord start
# ---------------------------------------------------------------------------


class TestCmdStart:
    def test_passes_env_path_to_bot_main(self, tmp_path: Path) -> None:
        """#202 (PR #249): `_run_start` must hand --env through to main(env_path).

        Before the fix main() was called with no args, so data/ and load_dotenv
        followed CWD and every existing session was orphaned.
        """
        env = tmp_path / "custom.env"
        env.write_text("DISCORD_BOT_TOKEN=x\n")
        bot_main = AsyncMock()
        with patch("c_lord.main.main", bot_main):
            cli.cmd_start(env)
        bot_main.assert_awaited_once_with(env)

    def test_missing_env_names_path_and_exits_1(
        self, tmp_path: Path, capsys: pytest.CaptureFixture[str]
    ) -> None:
        """README / install-systemd.sh: missing .env → tell the operator to run setup."""
        env = tmp_path / "nope.env"
        bot_main = AsyncMock()
        with patch("c_lord.main.main", bot_main), pytest.raises(SystemExit) as exc:
            cli.cmd_start(env)
        assert exc.value.code == 1
        err = capsys.readouterr().err
        assert str(env) in err
        assert "c-lord setup" in err
        bot_main.assert_not_awaited()

    async def test_run_start_also_guards_missing_env(
        self, tmp_path: Path, capsys: pytest.CaptureFixture[str]
    ) -> None:
        """setup's "Start the bot now?" calls _run_start directly — it re-checks the file."""
        with pytest.raises(SystemExit) as exc:
            await cli._run_start(tmp_path / "gone.env")
        assert exc.value.code == 1
        assert "c-lord setup" in capsys.readouterr().err


# ---------------------------------------------------------------------------
# check_claude_cli — setup "Step 1"
# ---------------------------------------------------------------------------


class TestCheckClaudeCli:
    def test_invokes_claude_version_without_shell(self) -> None:
        """README Step 1 "claude found": probe is `claude --version`, argv list, 5s timeout."""
        with patch("subprocess.run", return_value=MagicMock(returncode=0)) as run:
            assert cli.check_claude_cli() is True
        args, kwargs = run.call_args
        assert args[0] == ["claude", "--version"]
        assert kwargs.get("timeout") == 5
        assert not kwargs.get("shell")

    def test_nonzero_exit_is_not_installed(self) -> None:
        """A broken `claude` (non-zero exit) counts as missing."""
        with patch("subprocess.run", return_value=MagicMock(returncode=1)):
            assert cli.check_claude_cli() is False

    def test_timeout_is_not_installed(self) -> None:
        """A hanging `claude --version` must not hang the wizard."""
        import subprocess

        with patch("subprocess.run", side_effect=subprocess.TimeoutExpired("claude", 5)):
            assert cli.check_claude_cli() is False


# ---------------------------------------------------------------------------
# validate_token / fetch_channels — Discord REST boundaries
# ---------------------------------------------------------------------------


class TestDiscordHelpers:
    async def test_validate_adds_bot_prefix_to_raw_token(self) -> None:
        """README Step 2: the user pastes the raw token from the Developer Portal."""
        session = _http_session([_http_resp(200, {"username": "MyBot"})])
        with patch("aiohttp.ClientSession", return_value=session):
            name = await cli.validate_token("raw.token")
        _, kwargs = session.get.call_args
        assert kwargs["headers"] == {"Authorization": "Bot raw.token"}
        assert session.get.call_args.args[0] == "https://discord.com/api/v10/users/@me"
        # New-style usernames have no discriminator: falls back to "#0"
        assert name == "MyBot#0"

    async def test_validate_keeps_existing_bot_prefix(self) -> None:
        """A token pasted as "Bot xxx" is not double-prefixed for validation."""
        session = _http_session([_http_resp(200, {"username": "B", "discriminator": "1"})])
        with patch("aiohttp.ClientSession", return_value=session):
            await cli.validate_token("Bot already")
        assert session.get.call_args.kwargs["headers"] == {"Authorization": "Bot already"}

    async def test_validate_without_aiohttp_dies(
        self, monkeypatch: pytest.MonkeyPatch, capsys: pytest.CaptureFixture[str]
    ) -> None:
        """cli.py header: aiohttp is optional; the wizard surfaces a clear error."""
        monkeypatch.setitem(sys.modules, "aiohttp", None)
        with pytest.raises(SystemExit) as exc:
            await cli.validate_token("t")
        assert exc.value.code == 1
        assert "aiohttp is required" in capsys.readouterr().err

    async def test_fetch_without_aiohttp_returns_empty(
        self, monkeypatch: pytest.MonkeyPatch
    ) -> None:
        """No aiohttp → [] so the wizard falls back to manual channel entry."""
        monkeypatch.setitem(sys.modules, "aiohttp", None)
        assert await cli.fetch_channels("t") == []

    async def test_fetch_skips_unreadable_guild_and_keeps_others(self) -> None:
        """README Step 3 lists channels across every server the bot is in;
        a guild whose channel list is forbidden is skipped, not fatal."""
        session = _http_session(
            [
                _http_resp(200, [{"id": "g1", "name": "Locked"}, {"id": "g2", "name": "Open"}]),
                _http_resp(403, {"message": "Missing Access"}),
                _http_resp(200, [{"id": CHANNEL_A, "name": "general", "type": 0}]),
            ]
        )
        with patch("aiohttp.ClientSession", return_value=session):
            result = await cli.fetch_channels("raw")
        assert result == [(CHANNEL_A, "general", "Open")]
        urls = [c.args[0] for c in session.get.call_args_list]
        assert urls == [
            "https://discord.com/api/v10/users/@me/guilds",
            "https://discord.com/api/v10/guilds/g1/channels",
            "https://discord.com/api/v10/guilds/g2/channels",
        ]

    async def test_fetch_network_error_returns_empty(self) -> None:
        """Any exception mid-listing → [] (manual entry), never a traceback."""
        session = _http_session([RuntimeError("boom")])  # type: ignore[list-item]
        with patch("aiohttp.ClientSession", return_value=session):
            assert await cli.fetch_channels("raw") == []


# ---------------------------------------------------------------------------
# prompt helpers
# ---------------------------------------------------------------------------


class TestPrompts:
    def test_prompt_shows_default_and_returns_it_on_enter(
        self, monkeypatch: pytest.MonkeyPatch
    ) -> None:
        """Step 5 "Working directory [<cwd>]": Enter accepts the default."""
        prompts = _feed(monkeypatch, ["   "])
        assert cli._prompt("  Working directory", default="/proj") == "/proj"
        assert prompts == ["  Working directory [/proj]: "]

    def test_prompt_eof_cancels_setup(
        self, monkeypatch: pytest.MonkeyPatch, capsys: pytest.CaptureFixture[str]
    ) -> None:
        """Ctrl-D / Ctrl-C at any prompt → "Setup cancelled." exit 1."""
        _feed(monkeypatch, [])
        with pytest.raises(SystemExit) as exc:
            cli._prompt("  Bot Token")
        assert exc.value.code == 1
        assert "Setup cancelled." in capsys.readouterr().err

    def test_prompt_keyboard_interrupt_cancels_setup(self, monkeypatch: pytest.MonkeyPatch) -> None:
        def boom(_p: str = "") -> str:
            raise KeyboardInterrupt

        monkeypatch.setattr(builtins, "input", boom)
        with pytest.raises(SystemExit) as exc:
            cli._prompt("x")
        assert exc.value.code == 1

    def test_choice_uppercases_default_and_reprompts_on_invalid(
        self, monkeypatch: pytest.MonkeyPatch, capsys: pytest.CaptureFixture[str]
    ) -> None:
        """README "Start the bot now? [Y/n]": default shown upper-case; input is
        case-insensitive; anything else re-asks."""
        prompts = _feed(monkeypatch, ["maybe", "N"])
        assert cli._prompt_choice("  Start the bot now?", ["y", "n"], default="y") == "n"
        assert prompts[0] == "  Start the bot now? (Y/n) [y]: "
        assert len(prompts) == 2
        assert "Please enter one of: y, n" in capsys.readouterr().out


# ---------------------------------------------------------------------------
# c-lord setup — the wizard end to end (README "Step 2")
# ---------------------------------------------------------------------------


class TestSetupWizard:
    def test_happy_path_writes_env_from_channel_list(
        self,
        tmp_path: Path,
        monkeypatch: pytest.MonkeyPatch,
        capsys: pytest.CaptureFixture[str],
    ) -> None:
        """README Step 2: token → pick channel by number → defaults → .env written."""
        mocks = _wizard_env(monkeypatch)
        monkeypatch.chdir(tmp_path)
        env = tmp_path / ".env"
        # token, channel #2, owner id (skip), working dir (default cwd), model (default)
        _feed(monkeypatch, ["tok.en", "2", "", "", ""])
        cli.cmd_setup(env)

        vals = _env_lines(env)
        assert vals["DISCORD_BOT_TOKEN"] == "tok.en"
        assert vals["DISCORD_CHANNEL_ID"] == CHANNEL_B
        assert vals["DISCORD_OWNER_ID"] == ""
        assert vals["CLAUDE_WORKING_DIR"] == str(tmp_path)
        assert vals["CLAUDE_MODEL"] == "sonnet"
        assert vals["CLAUDE_PERMISSION_MODE"] == "acceptEdits"
        out = capsys.readouterr().out
        assert "Logged in as MyBot#1234" in out
        assert "Found 2 text channel(s)" in out
        assert " 2. #claude-code  (My Server)" in out
        assert f"#claude-code ({CHANNEL_B})" in out
        assert f"Written: {env}" in out
        assert "c-lord start" in out
        mocks["fetch"].assert_awaited_once_with("tok.en")
        # stdin not a TTY → no "Start the bot now?" offer
        mocks["run_start"].assert_not_awaited()

    def test_custom_answers_are_written(
        self, tmp_path: Path, monkeypatch: pytest.MonkeyPatch
    ) -> None:
        """README Step 2 item 3: working directory and model preference are asked."""
        _wizard_env(monkeypatch)
        env = tmp_path / ".env"
        _feed(monkeypatch, ["t", "1", "999", "/work/proj", "gpt", "OPUS"])
        cli.cmd_setup(env)
        vals = _env_lines(env)
        assert vals["DISCORD_CHANNEL_ID"] == CHANNEL_A
        assert vals["DISCORD_OWNER_ID"] == "999"
        assert vals["CLAUDE_WORKING_DIR"] == "/work/proj"
        assert vals["CLAUDE_MODEL"] == "opus"

    def test_existing_env_declined_is_left_untouched(
        self,
        tmp_path: Path,
        monkeypatch: pytest.MonkeyPatch,
        capsys: pytest.CaptureFixture[str],
    ) -> None:
        """Re-running setup must not clobber a working .env unless confirmed (default n)."""
        mocks = _wizard_env(monkeypatch)
        env = tmp_path / ".env"
        env.write_text("DISCORD_BOT_TOKEN=keep\n")
        _feed(monkeypatch, [""])  # Enter → default "n"
        with pytest.raises(SystemExit) as exc:
            cli.cmd_setup(env)
        assert exc.value.code == 1
        assert env.read_text() == "DISCORD_BOT_TOKEN=keep\n"
        assert "Existing .env was not modified" in capsys.readouterr().err
        mocks["validate"].assert_not_awaited()

    def test_existing_env_confirmed_is_overwritten(
        self, tmp_path: Path, monkeypatch: pytest.MonkeyPatch
    ) -> None:
        _wizard_env(monkeypatch)
        env = tmp_path / ".env"
        env.write_text("DISCORD_BOT_TOKEN=old\n")
        _feed(monkeypatch, ["y", "new", "1", "", "/w", ""])
        cli.cmd_setup(env)
        assert _env_lines(env)["DISCORD_BOT_TOKEN"] == "new"

    def test_missing_claude_warns_but_continues(
        self,
        tmp_path: Path,
        monkeypatch: pytest.MonkeyPatch,
        capsys: pytest.CaptureFixture[str],
    ) -> None:
        """Step 1: setup can finish before Claude Code is installed."""
        _wizard_env(monkeypatch, claude_ok=False)
        env = tmp_path / ".env"
        _feed(monkeypatch, ["t", "1", "", "/w", ""])
        cli.cmd_setup(env)
        out = capsys.readouterr().out
        assert "'claude' command not found" in out
        assert "install Claude Code later" in out
        assert env.exists()

    def test_invalid_token_then_valid(
        self,
        tmp_path: Path,
        monkeypatch: pytest.MonkeyPatch,
        capsys: pytest.CaptureFixture[str],
    ) -> None:
        """README Step 2 item 1: token is validated; a bad one is re-asked."""
        mocks = _wizard_env(monkeypatch, token_results=[None, "Good#1"])
        env = tmp_path / ".env"
        _feed(monkeypatch, ["bad", "good", "1", "", "/w", ""])
        cli.cmd_setup(env)
        assert "Invalid token" in capsys.readouterr().out
        assert _env_lines(env)["DISCORD_BOT_TOKEN"] == "good"
        mocks["fetch"].assert_awaited_once_with("good")

    def test_three_invalid_tokens_abort(
        self,
        tmp_path: Path,
        monkeypatch: pytest.MonkeyPatch,
        capsys: pytest.CaptureFixture[str],
    ) -> None:
        """Three failed validations → exit 1, nothing written."""
        _wizard_env(monkeypatch, token_results=[None, None, None])
        env = tmp_path / ".env"
        _feed(monkeypatch, ["a", "b", "c"])
        with pytest.raises(SystemExit) as exc:
            cli.cmd_setup(env)
        assert exc.value.code == 1
        assert "Too many failed attempts" in capsys.readouterr().err
        assert not env.exists()

    def test_empty_token_is_reasked(
        self,
        tmp_path: Path,
        monkeypatch: pytest.MonkeyPatch,
        capsys: pytest.CaptureFixture[str],
    ) -> None:
        """An empty paste is rejected without calling Discord."""
        mocks = _wizard_env(monkeypatch)
        env = tmp_path / ".env"
        _feed(monkeypatch, ["", "t", "1", "", "/w", ""])
        cli.cmd_setup(env)
        assert "Token cannot be empty." in capsys.readouterr().out
        mocks["validate"].assert_awaited_once_with("t")

    def test_channel_selection_reasks_then_accepts_pasted_id(
        self,
        tmp_path: Path,
        monkeypatch: pytest.MonkeyPatch,
        capsys: pytest.CaptureFixture[str],
    ) -> None:
        """Step 3 prompt "Select channel [1-N] or paste ID directly": out-of-range
        numbers and short junk re-ask; a 17+ digit ID is taken verbatim."""
        _wizard_env(monkeypatch)
        env = tmp_path / ".env"
        _feed(monkeypatch, ["t", "9", "abc", PASTED_ID, "", "/w", ""])
        cli.cmd_setup(env)
        out = capsys.readouterr().out
        assert "Enter a number between 1 and 2, or paste the ID." in out
        assert "That doesn't look like a valid channel ID." in out
        assert f"Channel ID: {PASTED_ID}" in out
        assert _env_lines(env)["DISCORD_CHANNEL_ID"] == PASTED_ID

    def test_no_channels_falls_back_to_manual_entry(
        self,
        tmp_path: Path,
        monkeypatch: pytest.MonkeyPatch,
        capsys: pytest.CaptureFixture[str],
    ) -> None:
        """Step 3 fallback: bot not in any server yet → Developer Mode tip and a
        digits-only (17+) channel ID prompt."""
        _wizard_env(monkeypatch, channels=[])
        env = tmp_path / ".env"
        _feed(monkeypatch, ["t", "12345", "x" * 18, PASTED_ID, "", "/w", ""])
        cli.cmd_setup(env)
        out = capsys.readouterr().out
        assert "switching to manual entry" in out
        assert "Developer Mode" in out
        assert out.count("Channel IDs are 17-19 digit numbers") == 2
        assert _env_lines(env)["DISCORD_CHANNEL_ID"] == PASTED_ID

    def test_tty_offers_to_start_and_starts_on_yes(
        self, tmp_path: Path, monkeypatch: pytest.MonkeyPatch
    ) -> None:
        """README Step 2 item 4: "offer to start the bot immediately" (default Y)."""
        mocks = _wizard_env(monkeypatch, tty=True)
        env = tmp_path / ".env"
        _feed(monkeypatch, ["t", "1", "", "/w", "", ""])  # last Enter → default y
        cli.cmd_setup(env)
        mocks["run_start"].assert_awaited_once_with(env)

    def test_tty_offer_declined_does_not_start(
        self, tmp_path: Path, monkeypatch: pytest.MonkeyPatch
    ) -> None:
        mocks = _wizard_env(monkeypatch, tty=True)
        env = tmp_path / ".env"
        _feed(monkeypatch, ["t", "1", "", "/w", "", "n"])
        cli.cmd_setup(env)
        mocks["run_start"].assert_not_awaited()
        assert env.exists()

    def test_cancel_mid_wizard_writes_nothing(
        self,
        tmp_path: Path,
        monkeypatch: pytest.MonkeyPatch,
        capsys: pytest.CaptureFixture[str],
    ) -> None:
        """Ctrl-D before the end → no half-written .env."""
        _wizard_env(monkeypatch)
        env = tmp_path / ".env"
        _feed(monkeypatch, ["t", "1"])  # EOF at "Your User ID"
        with pytest.raises(SystemExit) as exc:
            cli.cmd_setup(env)
        assert exc.value.code == 1
        assert "Setup cancelled." in capsys.readouterr().err
        assert not env.exists()

    def test_setup_subcommand_end_to_end_via_main(
        self, tmp_path: Path, monkeypatch: pytest.MonkeyPatch
    ) -> None:
        """docs/macos.md: `uv run c-lord setup` (here with --env) produces the file."""
        _wizard_env(monkeypatch)
        env = tmp_path / "sub" / ".env"
        env.parent.mkdir()
        _feed(monkeypatch, ["t", "1", "", "/w", ""])
        monkeypatch.setattr(sys, "argv", ["c-lord", "setup", "--env", str(env)])
        cli.main()
        assert _env_lines(env)["DISCORD_CHANNEL_ID"] == CHANNEL_A
