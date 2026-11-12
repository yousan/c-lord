"""The building blocks of the ``c-lord`` command (``c_lord/cli.py``).

What the setup wizard is made of, each piece on its own: asking Discord whether
a token is good, listing the channels the bot can see, writing ``.env``,
checking for the ``claude`` binary, and ``main()``'s handling of a missing or
unknown subcommand.

The wizard as an operator walks through it, and the routing of each
subcommand, are in ``tests/test_cli_commands.py``; the wizard's input guards
are in ``tests/test_cli_setup_guards.py``.

No test reaches Discord, runs ``claude`` or writes outside ``tmp_path``.
"""

from __future__ import annotations

import sys
from pathlib import Path
from typing import Any
from unittest.mock import AsyncMock, MagicMock, patch

import pytest

from c_lord import cli

API = "https://discord.com/api/v10"


class FakeDiscord:
    """Stands in for ``aiohttp.ClientSession``: answers GETs from a table of routes.

    *routes* maps a URL to ``(status, json body)``. A URL that is not in the
    table answers 404, and every request is recorded in :attr:`requests` as
    ``(url, headers)``.
    """

    def __init__(self, routes: dict[str, tuple[int, Any]]) -> None:
        self.routes = routes
        self.requests: list[tuple[str, dict[str, str]]] = []

    async def __aenter__(self) -> FakeDiscord:
        return self

    async def __aexit__(self, *_exc: object) -> bool:
        return False

    def get(self, url: str, *, headers: dict[str, str]) -> MagicMock:
        self.requests.append((url, headers))
        status, body = self.routes.get(url, (404, {"message": "Not Found"}))
        response = MagicMock()
        response.status = status
        response.json = AsyncMock(return_value=body)
        answer = MagicMock()
        answer.__aenter__ = AsyncMock(return_value=response)
        answer.__aexit__ = AsyncMock(return_value=False)
        return answer


@pytest.fixture
def discord_api(monkeypatch: pytest.MonkeyPatch) -> FakeDiscord:
    """Route every ``aiohttp.ClientSession()`` the CLI opens to one :class:`FakeDiscord`."""
    import aiohttp

    fake = FakeDiscord({})
    monkeypatch.setattr(aiohttp, "ClientSession", lambda: fake)
    return fake


def _channel(channel_id: str, name: str, kind: int = 0) -> dict[str, Any]:
    return {"id": channel_id, "name": name, "type": kind}


# ---------------------------------------------------------------------------
# validate_token
# ---------------------------------------------------------------------------


class TestValidateToken:
    async def test_an_accepted_token_gives_the_bots_name(self, discord_api: FakeDiscord) -> None:
        discord_api.routes[f"{API}/users/@me"] = (
            200,
            {"username": "MyBot", "discriminator": "1234"},
        )

        assert await cli.validate_token("abc.def") == "MyBot#1234"

    async def test_asks_discord_who_the_token_belongs_to(self, discord_api: FakeDiscord) -> None:
        """One request, to ``/users/@me``, carrying nothing but the token."""
        discord_api.routes[f"{API}/users/@me"] = (200, {"username": "MyBot"})

        await cli.validate_token("abc.def")

        assert discord_api.requests == [(f"{API}/users/@me", {"Authorization": "Bot abc.def"})]

    async def test_missing_name_fields_fall_back_to_placeholders(
        self, discord_api: FakeDiscord
    ) -> None:
        """New-style accounts have no discriminator; the answer still names something."""
        discord_api.routes[f"{API}/users/@me"] = (200, {})

        assert await cli.validate_token("abc.def") == "Unknown#0"

    @pytest.mark.parametrize("status", [401, 403, 500])
    async def test_a_refused_token_gives_none(self, discord_api: FakeDiscord, status: int) -> None:
        discord_api.routes[f"{API}/users/@me"] = (status, {"message": "401: Unauthorized"})

        assert await cli.validate_token("wrong") is None

    async def test_a_network_failure_gives_none(self, monkeypatch: pytest.MonkeyPatch) -> None:
        """Offline is reported as "not validated", never as a traceback in the wizard."""
        import aiohttp

        def unreachable() -> None:
            raise aiohttp.ClientConnectionError("cannot connect to discord.com")

        monkeypatch.setattr(aiohttp, "ClientSession", unreachable)

        assert await cli.validate_token("abc.def") is None

    async def test_an_unreadable_answer_gives_none(self, discord_api: FakeDiscord) -> None:
        """A 200 whose body is not the expected object is not a valid login either."""
        discord_api.routes[f"{API}/users/@me"] = (200, ["not", "an", "object"])

        assert await cli.validate_token("abc.def") is None


# ---------------------------------------------------------------------------
# fetch_channels
# ---------------------------------------------------------------------------


class TestFetchChannels:
    async def test_lists_text_channels_with_their_server(self, discord_api: FakeDiscord) -> None:
        discord_api.routes.update(
            {
                f"{API}/users/@me/guilds": (200, [{"id": "900", "name": "My Server"}]),
                f"{API}/guilds/900/channels": (
                    200,
                    [_channel("111", "general"), _channel("222", "claude-code")],
                ),
            }
        )

        assert await cli.fetch_channels("abc.def") == [
            ("111", "general", "My Server"),
            ("222", "claude-code", "My Server"),
        ]

    async def test_only_text_channels_are_offered(self, discord_api: FakeDiscord) -> None:
        """Voice channels, categories and the like cannot host the bot's threads."""
        discord_api.routes.update(
            {
                f"{API}/users/@me/guilds": (200, [{"id": "900", "name": "S"}]),
                f"{API}/guilds/900/channels": (
                    200,
                    [
                        _channel("1", "voice", kind=2),
                        _channel("2", "text"),
                        _channel("3", "category", kind=4),
                        {"id": "4", "name": "typeless"},
                    ],
                ),
            }
        )

        assert await cli.fetch_channels("abc.def") == [("2", "text", "S")]

    async def test_servers_and_channels_keep_discords_order(self, discord_api: FakeDiscord) -> None:
        """The wizard numbers the list, so the order must be stable and predictable."""
        discord_api.routes.update(
            {
                f"{API}/users/@me/guilds": (
                    200,
                    [{"id": "2", "name": "Second"}, {"id": "1", "name": "First"}],
                ),
                f"{API}/guilds/2/channels": (200, [_channel("20", "b"), _channel("21", "a")]),
                f"{API}/guilds/1/channels": (200, [_channel("10", "z")]),
            }
        )

        assert await cli.fetch_channels("abc.def") == [
            ("20", "b", "Second"),
            ("21", "a", "Second"),
            ("10", "z", "First"),
        ]

    async def test_every_request_is_authorised_with_the_token(
        self, discord_api: FakeDiscord
    ) -> None:
        discord_api.routes.update(
            {
                f"{API}/users/@me/guilds": (200, [{"id": "900", "name": "S"}]),
                f"{API}/guilds/900/channels": (200, []),
            }
        )

        await cli.fetch_channels("Bot abc.def")

        assert discord_api.requests == [
            (f"{API}/users/@me/guilds", {"Authorization": "Bot abc.def"}),
            (f"{API}/guilds/900/channels", {"Authorization": "Bot abc.def"}),
        ]

    async def test_a_refused_server_list_gives_nothing(self, discord_api: FakeDiscord) -> None:
        discord_api.routes[f"{API}/users/@me/guilds"] = (401, {"message": "401: Unauthorized"})

        assert await cli.fetch_channels("wrong") == []
        assert len(discord_api.requests) == 1  # no point asking for channels

    async def test_a_bot_in_no_server_gives_nothing(self, discord_api: FakeDiscord) -> None:
        discord_api.routes[f"{API}/users/@me/guilds"] = (200, [])

        assert await cli.fetch_channels("abc.def") == []

    async def test_a_malformed_answer_discards_what_was_collected(
        self, discord_api: FakeDiscord
    ) -> None:
        """Half a list would be numbered as if it were whole; manual entry is safer."""
        discord_api.routes.update(
            {
                f"{API}/users/@me/guilds": (
                    200,
                    [{"id": "1", "name": "Good"}, {"id": "2", "name": "Broken"}],
                ),
                f"{API}/guilds/1/channels": (200, [_channel("10", "general")]),
                f"{API}/guilds/2/channels": (200, [{"type": 0}]),  # no id, no name
            }
        )

        assert await cli.fetch_channels("abc.def") == []


# ---------------------------------------------------------------------------
# picking a channel from the list
# ---------------------------------------------------------------------------

_LISTED = [
    ("111111111111111111", "general", "Guild"),
    ("222222222222222222", "ops", "Guild"),
    ("333333333333333333", "claude", "Guild"),
]


class TestPickingAChannelByNumber:
    """The numbers on the list run from 1 to however many channels there are — no further.

    Picking the last number is the wizard walk-through in
    ``tests/test_cli_commands.py``; these are the edges around it.
    """

    @staticmethod
    async def _answering(monkeypatch: pytest.MonkeyPatch, *answers: str) -> tuple[str, list[str]]:
        """Walk the channel step with *answers*; return (chosen id, answers left unread)."""
        import builtins

        waiting = list(answers)
        monkeypatch.setattr(cli, "fetch_channels", AsyncMock(return_value=list(_LISTED)))
        monkeypatch.setattr(builtins, "input", lambda _question="": waiting.pop(0))
        return await cli._ask_channel("abc.def"), waiting

    async def test_the_first_number_picks_the_first_channel(
        self, monkeypatch: pytest.MonkeyPatch
    ) -> None:
        chosen, _left = await self._answering(monkeypatch, "1")

        assert chosen == "111111111111111111"

    @pytest.mark.parametrize("off_the_list", ["4", "0"], ids=["one-past-the-end", "zero"])
    async def test_a_number_just_off_the_list_is_asked_again(
        self,
        monkeypatch: pytest.MonkeyPatch,
        capsys: pytest.CaptureFixture[str],
        off_the_list: str,
    ) -> None:
        """Not an error and not a wrap-around: the question is simply put again."""
        chosen, left = await self._answering(monkeypatch, off_the_list, "2")

        assert chosen == "222222222222222222"
        assert left == []
        assert "Enter a number between 1 and 3, or paste the ID." in capsys.readouterr().out


# ---------------------------------------------------------------------------
# write_env
# ---------------------------------------------------------------------------


def _write(path: Path, **overrides: Any) -> None:
    """``write_env`` with one complete set of answers; *overrides* replace some."""
    answers: dict[str, Any] = {
        "token": "abc.def",
        "channel_id": "111111111111111111",
        "owner_id": "222222222222222222",
        "working_dir": "/srv/project",
        "model": "sonnet",
    }
    answers.update(overrides)
    cli.write_env(path=path, **answers)


class TestWriteEnv:
    def test_writes_everything_the_bot_needs_to_start(self, tmp_path: Path) -> None:
        """The whole file, line for line: this is what ``c-lord start`` reads back."""
        env = tmp_path / ".env"

        _write(env)

        assert env.read_text(encoding="utf-8") == (
            "# Generated by `c-lord setup`\n"
            "DISCORD_BOT_TOKEN=abc.def\n"
            "DISCORD_CHANNEL_ID=111111111111111111\n"
            "DISCORD_OWNER_ID=222222222222222222\n"
            "\n"
            "CLAUDE_COMMAND=claude\n"
            "CLAUDE_MODEL=sonnet\n"
            "CLAUDE_PERMISSION_MODE=acceptEdits\n"
            "CLAUDE_WORKING_DIR=/srv/project\n"
            "\n"
            "MAX_CONCURRENT_SESSIONS=3\n"
            "SESSION_TIMEOUT_SECONDS=300\n"
        )

    def test_a_skipped_owner_id_is_left_empty(self, tmp_path: Path) -> None:
        """Skipping the optional step writes no ID — the key stays, with nothing after it."""
        env = tmp_path / ".env"

        _write(env, owner_id="")

        assert "DISCORD_OWNER_ID=" in env.read_text(encoding="utf-8").splitlines()

    def test_values_are_written_exactly_as_given(self, tmp_path: Path) -> None:
        """No quoting and no escaping: a path with spaces goes in as typed."""
        env = tmp_path / ".env"

        _write(env, working_dir="/home/me/my project", model="opus")

        lines = env.read_text(encoding="utf-8").splitlines()
        assert "CLAUDE_WORKING_DIR=/home/me/my project" in lines
        assert "CLAUDE_MODEL=opus" in lines

    def test_an_existing_file_is_refused_and_left_alone(self, tmp_path: Path) -> None:
        """A second ``c-lord setup`` must not silently wipe a hand-edited ``.env``."""
        env = tmp_path / ".env"
        env.write_text("DISCORD_BOT_TOKEN=keep-me\n", encoding="utf-8")

        with pytest.raises(FileExistsError, match="already exists"):
            _write(env)

        assert env.read_text(encoding="utf-8") == "DISCORD_BOT_TOKEN=keep-me\n"

    def test_overwrite_replaces_an_existing_file(self, tmp_path: Path) -> None:
        env = tmp_path / ".env"
        env.write_text("DISCORD_BOT_TOKEN=old\nEXTRA=1\n", encoding="utf-8")

        _write(env, token="new.token", overwrite=True)

        text = env.read_text(encoding="utf-8")
        assert "DISCORD_BOT_TOKEN=new.token" in text
        assert "old" not in text and "EXTRA" not in text

    def test_arguments_must_be_named(self, tmp_path: Path) -> None:
        """Seven strings in a row are too easy to swap; positional calls are rejected."""
        with pytest.raises(TypeError):
            cli.write_env(tmp_path / ".env", "t", "1", "2", "/w", "sonnet")  # type: ignore[misc]


# ---------------------------------------------------------------------------
# check_claude_cli
# ---------------------------------------------------------------------------


class TestCheckClaudeCli:
    def test_true_when_claude_runs(self) -> None:
        with patch("subprocess.run", return_value=MagicMock(returncode=0)):
            assert cli.check_claude_cli() is True

    def test_false_when_claude_is_not_on_the_path(self) -> None:
        with patch("subprocess.run", side_effect=FileNotFoundError("claude")):
            assert cli.check_claude_cli() is False


# ---------------------------------------------------------------------------
# main / cmd_start
# ---------------------------------------------------------------------------


class TestMain:
    def test_without_a_subcommand_prints_help_and_succeeds(
        self, capsys: pytest.CaptureFixture[str]
    ) -> None:
        with patch("sys.argv", ["c-lord"]), pytest.raises(SystemExit) as exc_info:
            cli.main()

        assert exc_info.value.code == 0
        out = capsys.readouterr().out
        assert "usage: c-lord" in out
        assert "setup" in out and "start" in out

    def test_an_unknown_subcommand_is_a_usage_error(
        self, capsys: pytest.CaptureFixture[str]
    ) -> None:
        with patch("sys.argv", ["c-lord", "nonexistent"]), pytest.raises(SystemExit) as exc_info:
            cli.main()

        assert exc_info.value.code not in (0, None)
        assert "nonexistent" in capsys.readouterr().err

    def test_main_reads_the_command_line_at_call_time(
        self, monkeypatch: pytest.MonkeyPatch, tmp_path: Path
    ) -> None:
        """The console script calls ``main()`` with no arguments; ``sys.argv`` is the input."""
        seen: list[Path] = []
        monkeypatch.setattr(cli, "cmd_setup", seen.append)
        monkeypatch.setattr(sys, "argv", ["c-lord", "setup", "--env", str(tmp_path / "x.env")])

        cli.main()

        assert seen == [tmp_path / "x.env"]


class TestStartWithoutEnv:
    def test_start_stops_with_directions_when_there_is_no_env_file(
        self, tmp_path: Path, capsys: pytest.CaptureFixture[str]
    ) -> None:
        """A first-time operator who runs ``start`` before ``setup`` is told what to do."""
        missing = tmp_path / "absent.env"

        with patch("c_lord.main.main", AsyncMock()) as bot_main, pytest.raises(SystemExit) as exc:
            cli.cmd_start(missing)

        assert exc.value.code == 1
        err = capsys.readouterr().err
        assert f"No .env file found at {missing}." in err
        assert "c-lord setup" in err
        bot_main.assert_not_called()
