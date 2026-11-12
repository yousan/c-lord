"""Entry point for c-lord bot."""

from __future__ import annotations

import asyncio
import fcntl
import hashlib
import logging
import os
import signal
import sys
from collections.abc import Callable, Coroutine
from pathlib import Path
from typing import IO, TYPE_CHECKING, Any

from dotenv import find_dotenv, load_dotenv

from .bot import ClaudeDiscordBot
from .claude.config import ClaudeConfig
from .command_gate import is_trusted_bot_id_text
from .coordination.env import MAX_ID_DIGITS as COORDINATION_MAX_ID_DIGITS
from .git_mirrors import stop_running_git
from .session_cleanup import DirOutcome, retire_swept_workspace, sweep_days
from .setup import setup_bridge
from .shutdown_watchdog import arm_shutdown_watchdog, shutdown_timeout_from_env
from .utils.logger import setup_logging

if TYPE_CHECKING:
    from .ext.api_server import ApiServer

logger = logging.getLogger(__name__)

#: Claude CLI settings that go into the config exactly as the environment has
#: them (no validation, no trimming): variable → value used when it is unset.
_CLAUDE_ENV_DEFAULTS = {
    "CLAUDE_COMMAND": "claude",
    "CLAUDE_MODEL": "sonnet",
    "CLAUDE_PERMISSION_MODE": "acceptEdits",
    "CLAUDE_WORKING_DIR": "",
}

#: Defaults of the two numeric limits (#1119).
_DEFAULT_MAX_CONCURRENT = "3"
_DEFAULT_TIMEOUT = "300"
#: More digits than this in a limit is ignored with a WARNING (#1256): no limit
#: or timeout that large means anything, and ``int()`` of 4300+ digits raises.
_MAX_LIMIT_DIGITS = 18

#: Days of disuse after which a ``sessions`` row is swept at startup. Named here
#: rather than inlined because #554 made it user-visible: the notice posted into
#: each swept thread quotes it, and README/docs/COMMANDS.md now state it.
SESSION_CLEANUP_DAYS = 30


def _claude_env(name: str) -> str:
    """Read one of the pass-through Claude settings, falling back to its default."""
    return os.getenv(name, _CLAUDE_ENV_DEFAULTS[name])


def _require_setting(name: str, value: str) -> None:
    """Stop the process when a setting the bot cannot start without is empty."""
    if not value:
        logger.error("%s is required", name)
        sys.exit(1)


def _optional_id(text: str) -> int | None:
    """An ID from the config as a number; an empty entry means "not configured"."""
    return int(text) if text else None


def acquire_single_instance_lock(data_dir: Path) -> IO[bytes] | None:
    """Acquire an exclusive non-blocking flock on ``data_dir/.bot.lock``.

    Issue #212: prevents two bot processes with the same data dir from
    double-connecting to the Discord gateway and double-processing events
    (the staging incident on 2026-05-27).

    The returned file handle must be retained for the lifetime of the process;
    closing it releases the lock. flock auto-releases on process exit, so a
    crash does not leave a stale lock.

    Returns ``None`` when ``CLORD_ALLOW_MULTI_INSTANCE=1`` is set (advanced
    users who understand the risk). Calls ``sys.exit(1)`` if another instance
    already holds the lock.
    """
    if os.getenv("CLORD_ALLOW_MULTI_INSTANCE") == "1":
        logger.warning("CLORD_ALLOW_MULTI_INSTANCE=1 set; skipping single-instance guard")
        return None

    data_dir.mkdir(parents=True, exist_ok=True)
    lock_path = data_dir / ".bot.lock"
    # Intentionally not a context manager: the handle must outlive this function
    # so flock stays held for the process lifetime (released on process exit).
    lock_fp = open(lock_path, "wb")  # noqa: SIM115
    try:
        fcntl.flock(lock_fp.fileno(), fcntl.LOCK_EX | fcntl.LOCK_NB)
    except (BlockingIOError, OSError) as e:
        lock_fp.close()
        logger.error(
            "Another bot instance is already running with the same data dir "
            "(%s). Refusing to start to avoid Discord double-connect. "
            "Set CLORD_ALLOW_MULTI_INSTANCE=1 to bypass. (flock error: %s)",
            data_dir,
            e,
        )
        sys.exit(1)
    return lock_fp


def acquire_token_lock(token: str, lock_dir: Path | None = None) -> IO[str] | None:
    """Acquire a host-global exclusive flock keyed on the bot TOKEN (#325).

    The #212 data-dir lock keys on a filesystem path, but the real hazard is
    two processes sharing one Discord token: a clone with a different data dir
    (e.g. an ``.env`` pointing at production) passes the data-dir lock and
    double-connects to the gateway anyway. This lock enforces the actual
    invariant — one process per token — before Discord is ever touched.

    The lock file is named by a SHA-256 prefix of the token (no plaintext
    token on disk) and records the holder's pid/cwd so a refused second
    process can say WHO is already running. flock auto-releases on exit.

    Returns ``None`` when ``CLORD_ALLOW_MULTI_INSTANCE=1`` (same escape hatch
    as the data-dir lock). Calls ``sys.exit(1)`` on conflict.
    """
    if os.getenv("CLORD_ALLOW_MULTI_INSTANCE") == "1":
        logger.warning("CLORD_ALLOW_MULTI_INSTANCE=1 set; skipping token-lock guard")
        return None

    if lock_dir is None:
        lock_dir = Path.home() / ".cache" / "c-lord" / "locks"
    lock_dir.mkdir(parents=True, exist_ok=True)
    digest = hashlib.sha256(token.encode()).hexdigest()[:16]
    lock_path = lock_dir / f"token-{digest}.lock"
    # Not a context manager: the handle must outlive this function so the
    # flock stays held for the process lifetime (released on process exit).
    lock_fp = open(lock_path, "a+")  # noqa: SIM115
    try:
        fcntl.flock(lock_fp.fileno(), fcntl.LOCK_EX | fcntl.LOCK_NB)
    except (BlockingIOError, OSError):
        lock_fp.seek(0)
        holder = lock_fp.read().strip() or "(unknown)"
        lock_fp.close()
        logger.error(
            "Another bot process is already running with the SAME Discord "
            "token — refusing to start to avoid a gateway double-connect "
            "(#325). Holder: %s. If this is intentional, set "
            "CLORD_ALLOW_MULTI_INSTANCE=1.",
            holder,
        )
        sys.exit(1)
        return None  # only reached in tests where sys.exit is mocked
    # Record holder info for the error message of a refused second process.
    lock_fp.seek(0)
    lock_fp.truncate()
    lock_fp.write(f"pid={os.getpid()} cwd={Path.cwd()}\n")
    lock_fp.flush()
    return lock_fp


def resolve_data_dir(env_path: Path | None) -> Path:
    """Resolve the directory that holds sessions.db / notifications.db.

    Issue #202: when launched via ``c-lord start --env <path>``, data files must
    live next to the given .env — not relative to the CWD — otherwise starting
    from a different directory silently creates an empty sessions.db and orphans
    every existing session. Standalone ``python -m c_lord.main`` (env_path=None)
    keeps the legacy CWD-relative ``data/``.
    """
    base = env_path.parent if env_path is not None else Path(".")
    return base / "data"


def _env_source(key: str, env_path: str | None, fallback: str) -> str:
    """Where *key* was read from at start-up: ``.env``, ``環境変数``, or *fallback* (#918).

    Only a label — the value is never looked at here.  ``.env`` wins over the
    process environment (``override=True`` above), so a key present in the file
    is reported as the file's even when the shell also set it.
    """
    if env_path:
        try:
            from dotenv import dotenv_values

            if dotenv_values(env_path).get(key):
                return ".env"
        except Exception:  # an unreadable .env must not stop start-up
            pass
    if os.environ.get(key):
        return "環境変数"
    return fallback


def load_config(env_path: Path | None = None) -> dict[str, str]:
    """Load and validate configuration from environment.

    Issue #324: ``override=True`` — keys defined in the .env file always beat
    inherited process env. Claude sessions spawned by a bot inherit that bot's
    DISCORD_* vars; with the python-dotenv default (override=False) those
    silently won over the local .env and a "staging" launch booted as the
    production identity (#322). Directory == identity: the .env file is the
    single source of truth for every key it defines. Keys absent from the
    file still fall back to the process env (env-var-only setups keep working).
    """
    resolved_env_path: str | None = None
    if env_path is not None:
        load_dotenv(env_path, override=True)
        resolved_env_path = str(Path(env_path).resolve())
    else:
        # find_dotenv(usecwd=True): search from the CURRENT DIRECTORY upward,
        # not from this package's location. README documents the bare launch as
        # "reads .env in current dir" — the package-location walk only matched
        # that contract by accident (repo-clone deployments) and diverges for
        # any CWD outside the package tree.
        found = find_dotenv(usecwd=True)
        if found:
            load_dotenv(found, override=True)
            resolved_env_path = str(Path(found).resolve())

    # Issue #259: expose the resolved .env path so the injected discord-read
    # skill can tell Claude where to read DISCORD_BOT_TOKEN from at runtime.
    # Only the path is shared — never the token. Skip if no .env file was used
    # (env-var-only setups); the read skill then renders its path-less variant.
    if resolved_env_path:
        os.environ.setdefault("CLORD_ENV_PATH", resolved_env_path)

    # Not stripped: the token is handed to Discord exactly as it was written.
    token = os.getenv("DISCORD_BOT_TOKEN", "")
    _require_setting("DISCORD_BOT_TOKEN", token)

    # #1194: spaces around an ID (``KEY="123 "``) started before #1084 — ``int()``
    # ignores them — so strip them here, as the counts below do (#1119).
    channel_id = os.getenv("DISCORD_CHANNEL_ID", "").strip()
    _require_setting("DISCORD_CHANNEL_ID", channel_id)

    # Every Discord ID is "ASCII digits only" (#1084). ``isdigit()`` alone is
    # True for ``²`` and full-width digits, and the channel id was only checked
    # for being non-empty — so ``int()`` further down raised ValueError and the
    # user got a traceback that named no setting. A garbage value must fail
    # loudly (#323), with the value and how to fix it (#451).
    expected_bot_user_id = os.getenv("EXPECTED_BOT_USER_ID", "").strip()
    owner_id_raw = os.getenv("DISCORD_OWNER_ID", "").strip()
    coordination_raw = os.getenv("COORDINATION_CHANNEL_ID", "").strip()
    for key, value, what in (
        ("DISCORD_CHANNEL_ID", channel_id, "channel ID"),
        ("DISCORD_OWNER_ID", owner_id_raw, "user ID (snowflake)"),
        ("COORDINATION_CHANNEL_ID", coordination_raw, "channel ID"),
        ("EXPECTED_BOT_USER_ID", expected_bot_user_id, "user id"),
    ):
        if value and not (value.isascii() and value.isdigit()):
            logger.error(
                "%s must be a numeric Discord %s, got %r."
                " Enable Developer Mode in Discord, then right-click it → Copy ID"
                " (the number, not a name or a link).",
                key,
                what,
                value,
            )
            sys.exit(1)
    # #1256: a value longer than any Discord id (``int()`` of 4300+ digits
    # raises further down) is not an id. The channel is optional, so it is
    # said once and left unset — like a bad CLORD_TRUSTED_BOT_IDS entry (#1237).
    if len(coordination_raw) > COORDINATION_MAX_ID_DIGITS:
        logger.warning(
            "COORDINATION_CHANNEL_ID must be a Discord channel ID of at most %d digits,"
            " got %d digits — ignored; no coordination channel this run.",
            COORDINATION_MAX_ID_DIGITS,
            len(coordination_raw),
        )
        coordination_raw = ""

    # #1218: a companion bot id that is not ASCII digits is ignored, said once
    # here (``command_gate`` reads the list per message and must not raise).
    # Not a stop: a word or a link was silently ignored before, and the rest of
    # the list still works (#1194 — an update alone must not break a working .env).
    # Full-width digits (``１２３``) were read as 123 before; now they are ignored
    # too, like every other ID (#1084), and this WARNING says so.
    bad_trusted = [
        x
        for x in (p.strip() for p in os.getenv("CLORD_TRUSTED_BOT_IDS", "").split(","))
        if x and not is_trusted_bot_id_text(x)
    ]
    if bad_trusted:
        logger.warning(
            "CLORD_TRUSTED_BOT_IDS must be comma-separated numeric Discord user ids; ignoring %s.",
            ", ".join(repr(x) for x in bad_trusted),
        )

    # #1119: the two numeric limits must be whole numbers of at least 1. ``0``
    # used to start a bot on which every turn waited forever behind a
    # Semaphore(0) — no error, no log line; ``-1`` and ``three`` died with a
    # traceback that named no setting. Fail with the key, like the IDs above.
    limits: dict[str, str] = {}
    for key, default in (
        ("MAX_CONCURRENT_SESSIONS", _DEFAULT_MAX_CONCURRENT),
        ("SESSION_TIMEOUT_SECONDS", _DEFAULT_TIMEOUT),
    ):
        value = os.getenv(key, default)
        text = value.strip()
        is_number = text.isascii() and text.isdigit()
        # #1256: ``int()`` of more than 4300 digits raises a ValueError that
        # names no setting. A number that long means nothing as a limit, so it
        # is said once and the default is used — like CLORD_API_PORT (#1237).
        # Leading zeros don't count (``0007`` was always 7).
        digits = (text.lstrip("0") or "0") if is_number else ""
        if is_number and len(digits) > _MAX_LIMIT_DIGITS:
            logger.warning(
                "%s must be a whole number of at most %d digits, got %d digits — "
                "ignored; using the default %s.",
                key,
                _MAX_LIMIT_DIGITS,
                len(digits),
                default,
            )
            digits = default
        elif not (is_number and int(digits) >= 1):
            logger.error("%s must be a whole number of at least 1, got %r.", key, value)
            sys.exit(1)
        limits[key] = digits
    max_concurrent = limits["MAX_CONCURRENT_SESSIONS"]
    timeout = limits["SESSION_TIMEOUT_SECONDS"]

    return {
        "token": token,
        "channel_id": channel_id,
        "expected_bot_user_id": expected_bot_user_id,
        "claude_command": _claude_env("CLAUDE_COMMAND"),
        "claude_model": _claude_env("CLAUDE_MODEL"),
        "claude_effort": os.getenv("CLAUDE_EFFORT", ""),
        # #918: where those two came from, for the /model display.
        "claude_model_source": _env_source("CLAUDE_MODEL", resolved_env_path, "組み込み既定"),
        "claude_effort_source": _env_source("CLAUDE_EFFORT", resolved_env_path, "未設定"),
        "claude_permission_mode": _claude_env("CLAUDE_PERMISSION_MODE"),
        "claude_working_dir": _claude_env("CLAUDE_WORKING_DIR"),
        "max_concurrent": max_concurrent,
        "timeout": timeout,
        "owner_id": owner_id_raw,
        "coordination_channel_id": coordination_raw,
    }


# Where the REST API listens when ``CLORD_API_PORT`` is not set.
DEFAULT_API_PORT = 8080
#: The largest TCP port; a larger CLORD_API_PORT is refused, not a crash (#1237).
_MAX_PORT = 65535


async def build_api_server(
    bot: ClaudeDiscordBot,
    *,
    default_channel_id: int,
    data_dir: Path,
    port_override: int | None = None,
) -> ApiServer | None:
    """Build the REST API control plane.

    Unconditional by design (#712/#543). The API server started life as the
    endpoint the ``discord-reply`` skill posted to, so it used to be gated on
    that skill being active — which meant the default (jsonl) configuration had
    no ``/api/spawn``, no ``/api/tasks`` and no ``/api/notify`` at all, silently
    contradicting Key Design Decisions #7 and #8. Delivery and control plane are
    separate concerns; the control plane is always on.

    Args:
        bot: The Discord bot the endpoints act through.
        default_channel_id: Channel used when a request names none.
        data_dir: Directory holding ``notifications.db``.
        port_override: Bind port, bypassing ``CLORD_API_PORT`` (tests).

    Returns:
        The server, or ``None`` when aiohttp is not installed (logged).
    """
    try:
        from .database.notification_repo import NotificationRepository
        from .ext.api_server import ApiServer
    except ImportError:
        logger.warning(
            "aiohttp is not installed; the REST API will NOT start — /api/spawn, "
            "/api/tasks and /api/notify are unavailable this run. "
            "Install with `uv add aiohttp`."
        )
        return None

    notif_repo = NotificationRepository(str(data_dir / "notifications.db"))
    await notif_repo.init_db()

    # #258: a port nobody chose may move to the next free one; a pinned port
    # (env or override) is used as-is or not at all.
    auto_port = False
    if port_override is not None:
        api_port = port_override
    else:
        # #1218: ASCII digits only (#1084), spaces around them ignored (#1194).
        # ``"²".isdigit()`` is True and ``int("²")`` crashed the start with a
        # traceback that named no setting. Anything else is the unset case, said.
        raw_port = os.getenv("CLORD_API_PORT", "")
        api_port_env = raw_port.strip()
        if api_port_env.isascii() and api_port_env.isdigit():
            # #1237: ``int()`` of more than 4300 digits raises; any value past
            # 65535 is refused the same way, so a long one is kept as 65536.
            # Leading zeros don't count: ``008080`` was always 8080.
            digits = api_port_env.lstrip("0") or "0"
            api_port = int(digits) if len(digits) <= 5 else _MAX_PORT + 1
            if api_port > _MAX_PORT:
                # Still the port that was set (#258: pinned = as-is or not at
                # all) — start_api_server reports it and the bot runs without it.
                logger.warning(
                    "CLORD_API_PORT must be 0-%d, got %r — the REST API will not start this run.",
                    _MAX_PORT,
                    raw_port if len(raw_port) <= 40 else raw_port[:40] + "…",
                )
        else:
            if api_port_env:
                logger.warning(
                    "CLORD_API_PORT must be a port number, got %r — ignored; "
                    "the REST API picks a free port from %d as if it were unset.",
                    raw_port,
                    DEFAULT_API_PORT,
                )
            api_port = DEFAULT_API_PORT
            auto_port = True

    return ApiServer(
        repo=notif_repo,
        bot=bot,
        default_channel_id=default_channel_id,
        host=os.getenv("CLORD_API_HOST", "127.0.0.1"),
        port=api_port,
        auto_port=auto_port,
        api_secret=os.getenv("CLORD_API_SECRET") or None,
        # #457: loopback is not a UID boundary. The API only serves the Unix
        # user running the bot; this opens it to anyone who can reach the port.
        allow_any_peer=os.getenv("CLORD_API_ALLOW_ANY_PEER", "false").strip().lower()
        in ("1", "true", "yes", "on"),
        # #372: OGP/URL link previews are OFF by default; opt back in.
        show_url_embeds=os.getenv("CLORD_SHOW_URL_EMBEDS", "false").strip().lower()
        in ("1", "true", "yes", "on"),
    )


async def start_api_server(api_server: ApiServer | None) -> bool:
    """Bind the REST API, reporting rather than raising when it cannot.

    Now that every bot binds a port by default (#712), two clones on one host
    that both leave ``CLORD_API_PORT`` unset is an ordinary mistake — and the
    bot's real job (answering Discord) does not depend on the API. So a bind
    failure costs the API, not the process, and says why (#543 AC4).

    Returns:
        True if the API is listening.
    """
    if api_server is None:
        return False
    try:
        await api_server.start()
    except OverflowError as exc:
        # #1237: a port past 65535 (a typo in CLORD_API_PORT) is not an OSError;
        # it used to stop the whole bot. It costs the API, like a taken port.
        logger.warning(
            "REST API could not bind %s:%d (%s) — /api/* is unavailable this run. "
            "Set CLORD_API_PORT to a port between 0 and %d.",
            api_server.host,
            api_server.port,
            exc,
            _MAX_PORT,
        )
        return False
    except OSError as exc:
        logger.warning(
            "REST API could not bind %s:%d (%s) — /api/* is unavailable this run. "
            "Another process is probably using the port; set CLORD_API_PORT to a free one.",
            api_server.host,
            api_server.port,
            exc,
        )
        return False
    logger.info("REST API enabled (host=%s port=%d)", api_server.host, api_server.port)
    return True


def install_shutdown_signal_handlers(
    loop: asyncio.AbstractEventLoop,
    shutdown: Callable[[], Coroutine[Any, Any, None]],
) -> None:
    """On SIGINT/SIGTERM: arm the shutdown watchdog, then run *shutdown* (#699).

    The watchdog is armed first and off the loop, so even a shutdown that
    never finishes — or one that finishes on the loop and then hangs in
    ``asyncio.run()``'s executor join — ends with a stack dump and an exit
    instead of a silent, live process.

    Only the first signal starts *shutdown* (#877). Under systemd's default
    ``KillMode=control-group`` both ``uv`` and python get SIGTERM and ``uv``
    forwards its copy, so every restart delivered two — and two shutdowns raced
    each other through ``ApiServer.stop()`` (``'NoneType' object has no
    attribute 'pre_shutdown'``). The unit now uses ``KillMode=mixed``; this
    holds for whoever runs c-lord another way.
    """
    started = False

    def _on_signal() -> None:
        nonlocal started
        if started:
            logger.info("shutdown already in progress; ignoring the repeated signal (#877)")
            return
        started = True
        arm_shutdown_watchdog(shutdown_timeout_from_env())
        # ミラー更新の git は別のプロセスグループで走るので停止シグナルが届かない。
        # ここで止めないと bot が終わったあとも受信を続ける (#1174)。
        stop_running_git()
        loop.create_task(shutdown())

    for sig in (signal.SIGINT, signal.SIGTERM):
        loop.add_signal_handler(sig, _on_signal)


async def main(env_path: Path | None = None) -> None:
    """Bring the bot up and stay connected until it is told to stop."""
    log_level_name = os.getenv("LOG_LEVEL", "INFO").upper()
    log_level = getattr(logging, log_level_name, logging.INFO)
    setup_logging(log_level)
    config = load_config(env_path)

    data_dir = resolve_data_dir(env_path)
    data_dir.mkdir(parents=True, exist_ok=True)

    # Issue #212: refuse to start if another bot with the same data dir is
    # already running. The handle must be retained for the process lifetime —
    # flock auto-releases on close/exit. Assigning to a local that lives until
    # main() returns is sufficient; we don't need to reference it again.
    _instance_lock = acquire_single_instance_lock(data_dir)  # noqa: F841

    # Issue #325: additionally refuse if another process already holds THIS
    # TOKEN (host-global). The data-dir lock alone cannot stop a same-token
    # double-connect launched from a different clone/directory.
    _token_lock = acquire_token_lock(config["token"])  # noqa: F841

    db_path = str(data_dir / "sessions.db")

    runner = ClaudeConfig(
        command=config["claude_command"],
        model=config["claude_model"],
        effort=config["claude_effort"] or None,
        model_source=config.get("claude_model_source"),
        effort_source=config.get("claude_effort_source"),
        permission_mode=config["claude_permission_mode"],
        working_dir=config["claude_working_dir"] or None,
        timeout_seconds=int(config["timeout"]),
    )

    owner_id = _optional_id(config["owner_id"])
    coordination_channel_id = _optional_id(config["coordination_channel_id"])
    allowed_user_ids = {owner_id} if owner_id else None
    allowed_role_name = os.getenv("CLORD_ALLOWED_ROLE") or None

    bot = ClaudeDiscordBot(
        channel_id=int(config["channel_id"]),
        owner_id=owner_id,
        coordination_channel_id=coordination_channel_id,
        expected_bot_user_id=(
            int(config["expected_bot_user_id"]) if config["expected_bot_user_id"] else None
        ),
    )

    api_server = await build_api_server(
        bot,
        default_channel_id=int(config["channel_id"]),
        data_dir=data_dir,
    )

    async with bot:
        components = await setup_bridge(
            bot,
            runner,
            api_server=api_server,
            session_db_path=db_path,
            task_db_path=str(data_dir / "tasks.db"),
            allowed_user_ids=allowed_user_ids,
            allowed_role_name=allowed_role_name,
            claude_channel_id=int(config["channel_id"]),
            enable_scheduler=True,
            max_concurrent=int(config["max_concurrent"]),
        )

        await start_api_server(api_server)

        # Cleanup old sessions on startup.
        # #554: this used to be silent — a count in the log, nothing in Discord —
        # so a user returning to a month-old thread was told there was no session
        # with no way to learn that one had been deleted. The sweep still runs
        # here, before the connection, so it cannot race TranscriptMirrorCog's
        # on_ready walk of the same table; the cog posts the notices once the bot
        # is connected. It logs the thread ids, so the count line is gone.
        # #575: the period follows Claude Code's own transcript retention
        # (cleanupPeriodDays) rather than a constant c-lord chose. Once Claude
        # has forgotten the conversation, the checkout that existed to serve it
        # has nothing left to serve — and keeping the two in step closes the
        # window that produced 118 GB of stranded directories.
        sweep_days_value = sweep_days()
        deleted = await components.session_repo.cleanup_old(days=sweep_days_value)

        # The rows are kept as swept tombstones (#818), but no longer claim their
        # directories, so the sweep removes those here. Clean checkouts go;
        # anything with uncommitted or untracked work is kept (#575). Only the
        # thread's own clone is ever deleted: a fixed checkout, or a dir a live
        # row still points at, is someone else's (#902).
        claimed = await components.session_repo.all_working_dirs()
        for record in deleted:
            # #595: a workspace deleted here also frees its thread's w{N}.
            outcome = await asyncio.to_thread(
                retire_swept_workspace,
                record,
                sessions_root=components.session_dir_base,
                claimed=claimed,
            )
            if outcome is DirOutcome.KEPT_DIRTY:
                logger.info(
                    "Session cleanup: kept %s (uncommitted work) thread=%s",
                    record.working_dir,
                    record.thread_id,
                )
        cleanup_cog = bot.get_cog("SessionCleanupCog")
        if cleanup_cog is not None:
            cleanup_cog.announce(deleted)  # type: ignore[attr-defined]
        elif deleted:
            logger.info(
                "Cleaned up %d session(s) unused for %d+ days: threads=%s (no notice cog)",
                len(deleted),
                sweep_days_value,
                [r.thread_id for r in deleted],
            )

        # Signals have to be hooked up before start() below takes over.
        loop = asyncio.get_running_loop()

        async def _shutdown() -> None:
            if api_server is not None:
                await api_server.stop()
            await bot.close()

        install_shutdown_signal_handlers(loop, _shutdown)

        await bot.start(config["token"])


if __name__ == "__main__":
    asyncio.run(main())
