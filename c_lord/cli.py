"""c-lord CLI — interactive setup wizard and bot launcher.

Subcommands:
    c-lord setup   — interactive wizard: creates .env from prompts
    c-lord start   — start the bot (reads .env)
    c-lord         — show help

Without installing anything first:
    uvx --from git+https://github.com/yousan/c-lord c-lord setup

``aiohttp`` (only needed to talk to Discord during setup) and the bot itself
are imported where they are used, so ``c-lord version`` and ``--help`` work
on an install that cannot start the bot yet.
"""

from __future__ import annotations

import argparse
import asyncio
import os
import subprocess
import sys
from collections.abc import Callable
from dataclasses import dataclass
from pathlib import Path
from typing import Any

_DISCORD_API = "https://discord.com/api/v10"
#: Discord's channel type number for an ordinary text channel.
_TEXT_CHANNEL = 0

_BOX_TOP = "╔══════════════════════════════════════════════════════╗"
_BOX_BOTTOM = "╚══════════════════════════════════════════════════════╝"


# ---------------------------------------------------------------------------
# Terminal I/O
# ---------------------------------------------------------------------------


def _print(msg: str = "", end: str = "\n") -> None:
    """Write to stdout and flush, so a line without a newline shows at once."""
    sys.stdout.write(msg + end)
    sys.stdout.flush()


def _die(msg: str, code: int = 1) -> None:
    """Say why on stderr, set off by an empty line, and end the process."""
    for line in ("", f"❌  {msg}"):
        print(line, file=sys.stderr)
    raise SystemExit(code)


def _read_answer(question: str) -> str:
    """One trimmed line from the operator. EOF or Ctrl-C at the prompt cancels setup."""
    try:
        return input(question).strip()
    except (EOFError, KeyboardInterrupt):
        _print()  # the cursor is still on the question's line
        _die("Setup cancelled.")
        raise


def _prompt(prompt: str, default: str = "") -> str:
    """Ask one question; an empty answer means *default*, shown in brackets if there is one."""
    hint = f" [{default}]" if default else ""
    return _read_answer(f"{prompt}{hint}: ") or default


def _prompt_choice(prompt: str, choices: list[str], default: str = "") -> str:
    """Ask until one of *choices* is given (any case); returns it lower-cased."""
    accepted = {choice.lower() for choice in choices}
    shown = "/".join(
        choice.upper() if choice.lower() == default.lower() else choice for choice in choices
    )
    while True:
        answer = _prompt(f"{prompt} ({shown})", default).lower()
        if answer in accepted:
            return answer
        _print(f"  Please enter one of: {', '.join(choices)}")


# ---------------------------------------------------------------------------
# Discord REST — just enough to check a token and list channels
# ---------------------------------------------------------------------------


def _auth_headers(token: str) -> dict[str, str]:
    """The Authorization header for *token*, pasted with or without ``Bot ``."""
    return {"Authorization": token if token.startswith("Bot ") else f"Bot {token}"}


class _NotOk:
    """What :func:`_get_json` returns for any status but 200 — never a JSON value."""


_NOT_OK = _NotOk()


async def _get_json(session: Any, path: str, token: str) -> Any:
    """GET *path* from the Discord API: the decoded body, or ``_NOT_OK`` unless it answers 200."""
    async with session.get(f"{_DISCORD_API}{path}", headers=_auth_headers(token)) as resp:
        if resp.status != 200:
            return _NOT_OK
        return await resp.json()


async def validate_token(token: str) -> str | None:
    """The bot's ``name#discriminator`` if Discord accepts *token*, else ``None``."""
    try:
        import aiohttp
    except ImportError:
        _die("aiohttp is required for token validation.  Install with: pip install aiohttp")
        return None

    try:
        async with aiohttp.ClientSession() as session:
            me = await _get_json(session, "/users/@me", token)
        if me is _NOT_OK:
            return None
        return f"{me.get('username', 'Unknown')}#{me.get('discriminator', '0')}"
    except Exception:
        return None


async def fetch_channels(token: str) -> list[tuple[str, str, str]]:
    """``(channel id, channel name, server name)`` for every text channel the bot can list.

    A server that refuses to list its channels (any status but 200) is skipped.
    Anything else that goes wrong — a network error, an answer that is not
    shaped as expected, a missing ``aiohttp`` — gives an empty list for the
    whole call, which sends the wizard to manual entry.
    """
    try:
        import aiohttp
    except ImportError:
        return []

    found: list[tuple[str, str, str]] = []
    try:
        async with aiohttp.ClientSession() as session:
            guilds = await _get_json(session, "/users/@me/guilds", token)
            if guilds is _NOT_OK:
                return []
            for guild in guilds:
                channels = await _get_json(session, f"/guilds/{guild['id']}/channels", token)
                if channels is _NOT_OK:
                    continue
                guild_name = guild["name"]
                found.extend(
                    (channel["id"], channel["name"], guild_name)
                    for channel in channels
                    if channel.get("type") == _TEXT_CHANNEL
                )
    except Exception:
        return []
    return found


# ---------------------------------------------------------------------------
# Local checks and the .env file
# ---------------------------------------------------------------------------


def check_claude_cli() -> bool:
    """Whether ``claude --version`` runs and exits 0."""
    try:
        probe = subprocess.run(["claude", "--version"], capture_output=True, text=True, timeout=5)
    except (FileNotFoundError, subprocess.TimeoutExpired):
        return False
    return probe.returncode == 0


def write_env(
    *,
    path: Path,
    token: str,
    channel_id: str,
    owner_id: str,
    working_dir: str,
    model: str,
    overwrite: bool = False,
) -> None:
    """Write the ``.env`` the bot starts from. Refuses an existing file unless *overwrite*."""
    if path.exists() and not overwrite:
        raise FileExistsError(f"{path} already exists")

    # The file, as groups of KEY=value separated by an empty line. Values go in
    # exactly as given: no quoting, and an empty one still gets its line.
    groups: tuple[dict[str, str], ...] = (
        {
            "DISCORD_BOT_TOKEN": token,
            "DISCORD_CHANNEL_ID": channel_id,
            "DISCORD_OWNER_ID": owner_id,
        },
        {
            "CLAUDE_COMMAND": "claude",
            "CLAUDE_MODEL": model,
            "CLAUDE_PERMISSION_MODE": "acceptEdits",
            "CLAUDE_WORKING_DIR": working_dir,
        },
        {
            "MAX_CONCURRENT_SESSIONS": "3",
            "SESSION_TIMEOUT_SECONDS": "300",
        },
    )
    lines = [
        "# Generated by `c-lord setup`",
        "\n\n".join(
            "\n".join(f"{key}={value}" for key, value in group.items()) for group in groups
        ),
    ]
    path.write_text("\n".join(lines) + "\n", encoding="utf-8")


# ---------------------------------------------------------------------------
# The setup wizard, one step per function, in the order they are asked
# ---------------------------------------------------------------------------
def _is_id(raw: str) -> bool:
    """ASCII digits only — what Discord IDs are, and what ``int()`` / main.py accept.

    ``str.isdigit()`` alone is True for ``²``, which ``int()`` rejects with a
    traceback (#1063); full-width digits would be written to ``.env`` as typed.
    """
    return raw.isascii() and raw.isdigit()


def _show_title() -> None:
    _print()
    _print(_BOX_TOP)
    _print("║         c-lord setup — interactive wizard             ║")
    _print(_BOX_BOTTOM)
    _print()


def _confirm_overwrite(env_path: Path) -> bool:
    """Whether the ``.env`` may be replaced. Stops the wizard if the answer is no."""
    if not env_path.exists():
        return False
    _print(f"  ⚠️  {env_path} already exists.")
    if _prompt_choice("  Overwrite it?", ["y", "n"], default="n") != "y":
        _die("Setup cancelled. Existing .env was not modified.")
    return True


def _report_claude_cli() -> None:
    _print()
    _print("Step 1 — Claude Code CLI")
    if check_claude_cli():
        _print("  ✅  claude found")
        return
    _print("  ⚠️  'claude' command not found.")
    _print("      Install Claude Code: https://docs.anthropic.com/en/docs/claude-code")
    _print("      (You can continue setup now and install Claude Code later.)")


async def _try_token(token: str) -> str | None:
    """Check one answer with Discord, saying how it went; the bot's name if it is good."""
    if not token:
        _print("  Token cannot be empty.")
        return None
    _print("  Validating token… ", end="")
    bot_name = await validate_token(token)
    verdict = (
        f"✅  Logged in as {bot_name}"
        if bot_name
        else "❌  Invalid token. Please check and try again."
    )
    _print(verdict)
    return bot_name


async def _ask_token() -> str:
    """A token Discord accepted, without the ``Bot `` prefix."""
    _print()
    _print("Step 2 — Discord Bot Token")
    _print("  Where to get it: discord.com/developers/applications → Bot → Reset Token")
    _print()

    token = ""
    bot_name: str | None = None
    for _attempt in range(3):
        # discord.py sends "Bot " + token at login, so a pasted "Bot xxx" must be
        # stored without the prefix — validate_token accepts both (#1031).
        token = _prompt("  Bot Token").removeprefix("Bot ").strip()
        bot_name = await _try_token(token)
        if bot_name:
            break
    if not bot_name:
        # Empty answers use up the attempts too; going on would write an empty
        # DISCORD_BOT_TOKEN and still print "Setup complete" (#1029).
        _die("Too many failed attempts. Please verify your bot token.")
    return token


async def _ask_channel(token: str) -> str:
    """The channel the bot will listen in: picked from Discord's list, or typed."""
    _print()
    _print("Step 3 — Discord Channel ID")
    _print("  Fetching channels via Discord API… ", end="")
    channels = await fetch_channels(token)

    if channels:
        _print(f"✅  Found {len(channels)} text channel(s)")
        _print()
        for number, (_channel_id, name, guild) in enumerate(channels, start=1):
            _print(f"  {number:2d}. #{name}  ({guild})")
        _print()
        while True:
            raw = _prompt(f"  Select channel [1-{len(channels)}] or paste ID directly")
            if _is_id(raw):
                number = int(raw)
                if 1 <= number <= len(channels):
                    channel_id, name, _guild = channels[number - 1]
                    _print(f"  ✅  #{name} ({channel_id})")
                    return channel_id
                # Out of range but as long as an ID: taken as one. Only digits get
                # this far, so a pasted URL is never an ID (#1030).
                if len(raw) >= 17:
                    _print(f"  ✅  Channel ID: {raw}")
                    return raw
                _print(f"  Enter a number between 1 and {len(channels)}, or paste the ID.")
            else:
                _print("  That doesn't look like a valid channel ID. Try again.")
    else:
        _print("(bot not in any server yet — switching to manual entry)")
        _print()
        _print("  Tip: In Discord → Settings → Advanced → enable Developer Mode,")
        _print("       then right-click the channel → Copy Channel ID.")
        while True:
            raw = _prompt("  Channel ID")
            if len(raw) >= 17 and _is_id(raw):
                return raw
            _print("  Channel IDs are 17-19 digit numbers. Please try again.")


def _ask_owner_id() -> str:
    """The operator's own user ID, or an empty string to skip."""
    _print()
    _print("Step 4 — Your Discord User ID  (optional — used for @-mentions)")
    _print("  Tip: Right-click your username → Copy User ID  (Developer Mode required)")
    while True:
        owner_id = _prompt("  Your User ID")
        # main.py refuses a non-numeric DISCORD_OWNER_ID (#451) — a username
        # here used to be written anyway and fail only at ``c-lord start`` (#1062).
        if not owner_id or _is_id(owner_id):
            break
        _print("  User IDs are numeric (not your username). Leave empty to skip.")
    return owner_id


def _ask_working_dir() -> str:
    _print()
    _print("Step 5 — Working Directory")
    _print("  The directory where Claude Code will run (your project root).")
    return _prompt("  Working directory", default=os.getcwd())


def _ask_model() -> str:
    _print()
    _print("Step 6 — Claude Model")
    return _prompt_choice("  Model", ["sonnet", "opus", "haiku"], default="sonnet")


async def _run_setup(env_path: Path) -> None:
    """Ask for everything ``.env`` needs, write it, and offer to start the bot."""
    _show_title()
    overwrite = _confirm_overwrite(env_path)
    _report_claude_cli()
    token = await _ask_token()
    channel_id = await _ask_channel(token)
    owner_id = _ask_owner_id()
    working_dir = _ask_working_dir()
    model = _ask_model()

    _print()
    write_env(
        path=env_path,
        token=token,
        channel_id=channel_id,
        owner_id=owner_id,
        working_dir=working_dir,
        model=model,
        overwrite=overwrite,
    )
    _print(f"  ✅  Written: {env_path}")

    _print()
    _print(_BOX_TOP)
    _print("║  Setup complete! Start the bot with:                 ║")
    _print("║                                                      ║")
    _print("║    c-lord start                                      ║")
    _print("║                                                      ║")
    _print("║  Then send a message in your Discord channel.        ║")
    _print(_BOX_BOTTOM)
    _print()

    # Only offer when someone is there to answer; piped input just ends here.
    if sys.stdin.isatty() and _prompt_choice("  Start the bot now?", ["y", "n"], "y") == "y":
        await _run_start(env_path)


# ---------------------------------------------------------------------------
# Subcommands
# ---------------------------------------------------------------------------


def cmd_setup(env_path: Path = Path(".env")) -> None:
    """Entry point for `c-lord setup`."""
    asyncio.run(_run_setup(env_path))


def _env_is_missing(env_path: Path) -> bool:
    """Whether *env_path* is absent — and if so, say it on stderr (the caller adds what to do)."""
    if env_path.exists():
        return False
    print(f"❌  No .env file found at {env_path}.", file=sys.stderr)
    return True


async def _run_start(env_path: Path) -> None:
    """Run the bot on *env_path* until it stops. Exits 1 if the file is missing."""
    if _env_is_missing(env_path):
        print("    Run 'c-lord setup' first to create it.", file=sys.stderr)
        raise SystemExit(1)
    # Import and run the existing main() from c_lord.main
    from c_lord.main import main as bot_main

    await bot_main(env_path)


def cmd_start(env_path: Path = Path(".env")) -> None:
    """Entry point for `c-lord start`."""
    if _env_is_missing(env_path):
        print("    Run 'c-lord setup' first.", file=sys.stderr)
        raise SystemExit(1)
    asyncio.run(_run_start(env_path))


# ---------------------------------------------------------------------------
# cmd_version — print the running build's article-format version
# ---------------------------------------------------------------------------


def cmd_version() -> None:
    """Entry point for `c-lord version` — prints e.g. v1.4.0-b599631-20251203."""
    from c_lord.version import resolve_version

    print(resolve_version())


# ---------------------------------------------------------------------------
# main — the ``c-lord`` console script
# ---------------------------------------------------------------------------


@dataclass(frozen=True)
class _Subcommand:
    """One ``c-lord <name>``: how it reads in ``--help`` and what it runs."""

    name: str
    help: str
    #: Help for its ``--env PATH`` option; ``None`` for a subcommand that takes no file.
    env_help: str | None
    #: Called with the parsed arguments. The ``cmd_*`` function is named inside
    #: the lambda so that it is looked up when the subcommand runs.
    run: Callable[[argparse.Namespace], None]


_SUBCOMMANDS: tuple[_Subcommand, ...] = (
    _Subcommand(
        name="setup",
        help="Interactive setup wizard — creates .env",
        env_help="Path to write the .env file (default: .env)",
        run=lambda args: cmd_setup(Path(args.env)),
    ),
    _Subcommand(
        name="start",
        help="Start the bot (reads .env)",
        env_help="Path to the .env file (default: .env)",
        run=lambda args: cmd_start(Path(args.env)),
    ),
    _Subcommand(
        name="version",
        help="Print the running c-lord build version",
        env_help=None,
        run=lambda args: cmd_version(),
    ),
)


def _build_parser() -> argparse.ArgumentParser:
    parser = argparse.ArgumentParser(
        prog="c-lord",
        description="C-lord CLI",
    )
    sub = parser.add_subparsers(dest="command", metavar="COMMAND")
    for subcommand in _SUBCOMMANDS:
        sub_parser = sub.add_parser(subcommand.name, help=subcommand.help)
        if subcommand.env_help is not None:
            sub_parser.add_argument(
                "--env", default=".env", metavar="PATH", help=subcommand.env_help
            )
    return parser


def main() -> None:
    """Parse ``sys.argv`` and run the chosen subcommand; no subcommand prints help."""
    parser = _build_parser()
    args = parser.parse_args()
    by_name = {subcommand.name: subcommand for subcommand in _SUBCOMMANDS}
    chosen = by_name.get(args.command)
    if chosen is None:
        parser.print_help()
        raise SystemExit(0)
    chosen.run(args)
