"""The bot's built-in REST API: its control plane, served over HTTP with aiohttp.

Scripts, CI jobs and the Claude sessions themselves use it to make the bot act —
send or queue a notification, register a recurring task, read and write the AI
Lounge, open a new session thread, mark a thread to be resumed after a restart,
post into a thread.

This is an optional extension: it needs aiohttp, which comes with::

    pip install c-lord[api]

Who can reach it:

- The listening address is ``127.0.0.1`` unless the operator passes another
  host, so by default nothing outside this machine can connect.
- Every request must come from the bot's own Unix user (#457), or carry
  ``Authorization: Bearer <api_secret>`` when one is configured
  (``CLORD_API_SECRET``).
"""

from __future__ import annotations

import hmac
import io
import logging
import os
import sqlite3
import stat
import unicodedata
from collections.abc import Awaitable, Callable
from datetime import datetime
from typing import TYPE_CHECKING, Any

from aiohttp import web

from .. import api_endpoint
from ..log_sampler import LogSampler
from ..utils.logger import log_ctx
from .peer_uid import resolve_peer_uid

if TYPE_CHECKING:
    import discord
    from discord.ext.commands import Bot

    from ..database.lounge_repo import LoungeRepository
    from ..database.notification_repo import NotificationRepository
    from ..database.repository import SessionRepository
    from ..database.resume_repo import PendingResumeRepository
    from ..database.task_repo import TaskRepository

logger = logging.getLogger(__name__)

# How many ports from the requested one an ``auto_port`` server tries before it
# lets the OS pick (#258). Twenty covers a host full of c-lord clones while
# keeping their ports next to each other, where a person looks for them.
AUTO_PORT_TRIES = 20


def _current_uid() -> int | None:
    """Our own UID, or ``None`` where the platform has no such notion."""
    getuid = getattr(os, "getuid", None)
    return getuid() if getuid is not None else None


def _not_a_boolean(field: str) -> web.Response:
    """400 for a flag that is not JSON ``true``/``false`` (#1038).

    ``bool("false")`` is True, so guessing at a string turned "stop this task"
    into "start it" — with a 200. Refusing is the only reading that never
    does the opposite of what was asked.
    """
    return web.json_response({"error": f"{field} must be true or false"}, status=400)


async def _read_json_object(
    request: web.Request,
) -> tuple[dict[str, Any], web.Response | None]:
    """The request body as a JSON object, or the 400 to answer with.

    Valid JSON that is not an object (``[1]``, ``"text"``) used to reach
    ``data.get`` and come back as a 500 (#1037).
    """
    try:
        data = await request.json()
    except ValueError:
        # ``JSONDecodeError`` and also ``UnicodeDecodeError`` — a body that is
        # not UTF-8 is a ``ValueError`` but not a ``JSONDecodeError`` (#1082).
        return {}, web.json_response({"error": "Invalid JSON"}, status=400)
    if not isinstance(data, dict):
        return {}, web.json_response({"error": "Body must be a JSON object"}, status=400)
    return data, None


# SQLite's INTEGER is 64-bit signed. A larger value passed ``int()`` and then
# failed in the INSERT with ``OverflowError`` — a 500, after any earlier write
# of the same request had already landed (#1081).
_INT64_MIN = -(2**63)
_INT64_MAX = 2**63 - 1


#: ``_open_inside`` results that are not an open file.
_OUTSIDE = "outside"
_UNOPENABLE = "unopenable"


def _open_inside(base: str | None, requested: object) -> io.BufferedReader | str:
    """Open ``requested`` only if the file it names is under ``base`` (#1039/#1106).

    Returns an open binary file, :data:`_OUTSIDE` (not under ``base``, not a
    usable path, or no ``base`` known) or :data:`_UNOPENABLE` (under ``base``
    but missing / not a regular file).

    The check and the open must be the same act (#1106): checking the
    ``realpath`` string and then opening by name again let a component be
    swapped for an outward symlink in between. So the resolved path is only
    used to decide *which* components to walk; the walk itself starts from a
    descriptor of ``base`` and opens each component relative to the previous
    one with ``O_NOFOLLOW``. A component that became a symlink after the
    check fails to open instead of being followed. ``openat`` + ``O_NOFOLLOW``
    is POSIX, so this holds on macOS too, not only where ``/proc`` exists.
    """
    if not base:
        return _OUTSIDE
    try:
        root = os.path.realpath(base)
        candidate = os.path.realpath(str(requested))
    except (ValueError, OSError):  # e.g. a NUL in the value (#1105)
        return _OUTSIDE
    prefix = root.rstrip(os.sep) + os.sep
    if not candidate.startswith(prefix):
        return _OUTSIDE
    parts = [p for p in candidate[len(prefix) :].split(os.sep) if p]
    if not parts:
        return _UNOPENABLE
    nofollow = getattr(os, "O_NOFOLLOW", 0)
    directory = getattr(os, "O_DIRECTORY", 0)
    cloexec = getattr(os, "O_CLOEXEC", 0)
    # #1185: a directory is only walked through, never listed — so open it for
    # search (O_PATH on Linux, O_SEARCH elsewhere), not for reading. O_RDONLY
    # needed read permission and turned a file under a 0311 dir into a 400.
    walk = getattr(os, "O_PATH", 0) or getattr(os, "O_SEARCH", 0) or os.O_RDONLY
    try:
        dir_fd = os.open(root, walk | directory | cloexec)
    except OSError:
        return _UNOPENABLE
    try:
        for name in parts[:-1]:
            nxt = os.open(name, walk | directory | nofollow | cloexec, dir_fd=dir_fd)
            os.close(dir_fd)
            dir_fd = nxt
        # O_NONBLOCK: a FIFO planted here must not hang the request.
        fd = os.open(
            parts[-1],
            os.O_RDONLY | nofollow | cloexec | getattr(os, "O_NONBLOCK", 0),
            dir_fd=dir_fd,
        )
    except OSError:
        return _UNOPENABLE
    finally:
        os.close(dir_fd)
    try:
        if not stat.S_ISREG(os.fstat(fd).st_mode):
            os.close(fd)
            return _UNOPENABLE
        return os.fdopen(fd, "rb")
    except OSError:
        os.close(fd)
        return _UNOPENABLE


def _as_int(value: object) -> int | None:
    """``int(value)``, or ``None`` when it is not one SQLite can hold.

    ``json.loads`` accepts ``Infinity`` / ``NaN``, and ``int(float("inf"))``
    raises ``OverflowError`` — which no ``except (TypeError, ValueError)`` caught,
    so the caller got a 500 with no reason (#1065).
    """
    try:
        number = int(value)  # type: ignore[call-overload]
    except (TypeError, ValueError, OverflowError):
        return None
    return number if _INT64_MIN <= number <= _INT64_MAX else None


def _task_int(value: object) -> int | None:
    """:func:`_as_int` for a task's ``interval_seconds`` / ``channel_id``, minus booleans.

    ``true`` was saved as ``1`` — a task that ran every second (#1184). The
    mirror of #1038: a boolean field takes no ``0`` / ``1``, so an integer field
    takes no ``true`` / ``false``. A decimal string and a fraction stay
    accepted (#1126 — Discord ids often travel as strings).
    """
    if isinstance(value, bool):
        return None
    if isinstance(value, str) and not (value.isascii() and value.isdigit()):
        # #1184 (review): "a string of decimal digits", as the docs say — not
        # what ``int()`` also reads (``"１２０"``, ``"1_000"``, ``"+60"``, ``" 60 "``).
        # The same rule as the .env ids (#1084).
        return None
    return _as_int(value)


def _strict_int(value: object) -> int | None:
    """*value* if it is a JSON integer SQLite can hold — not a bool, string or float.

    For the fields that never took anything else (#1083): ``/api/schedule``
    saved ``"1"``, ``1.5`` or ``Infinity`` as a channel id with a 200.
    """
    if isinstance(value, bool) or not isinstance(value, int):
        return None
    return value if _INT64_MIN <= value <= _INT64_MAX else None


def _optional_channel_and_color(
    data: dict[str, Any],
) -> tuple[int | None, int | None, web.Response | None]:
    """``channel_id`` and ``color`` of a notify / schedule body, or the 400 (#1083).

    Both optional. A channel id is a positive integer; a colour is an RGB
    integer, 0 to 0xFFFFFF — anything else reached ``discord.Embed`` (a 500) or
    was saved as is.
    """
    channel_id: int | None = None
    # Falsy (absent, ``null``, ``0``) means "the default channel", as before.
    if data.get("channel_id"):
        channel_id = _strict_int(data["channel_id"])
        if channel_id is None or channel_id <= 0:
            return (
                None,
                None,
                web.json_response({"error": "channel_id must be a positive integer"}, status=400),
            )
    color: int | None = None
    if data.get("color") is not None:
        color = _strict_int(data["color"])
        if color is None or not 0 <= color <= 0xFFFFFF:
            return (
                None,
                None,
                web.json_response(
                    {"error": "color must be an integer from 0 to 0xFFFFFF"}, status=400
                ),
            )
    return channel_id, color, None


# What counts as "nothing visible" (#1238 / #1258). A hand list of five
# characters (ZWSP, ZWNJ, ZWJ, WJ, BOM) let direction marks, the soft hyphen, a
# lone variation selector, the Hangul filler… through, so it is decided by
# Unicode instead: control (Cc), format (Cf — direction marks, soft hyphen,
# U+180E, U+2061, tags, ZW*, BOM) and separator (Zs / Zl / Zp) categories, plus
# the Default_Ignorable_Code_Point characters outside them, plus the braille
# blank, which renders as an empty cell. Unassigned code points (Cn) are NOT
# blank as a class: an emoji newer than Python's Unicode data is Cn here.
_INVISIBLE_CATEGORIES = frozenset({"Cc", "Cf", "Zs", "Zl", "Zp"})
_INVISIBLE_RANGES = (
    (0x034F, 0x034F),  # COMBINING GRAPHEME JOINER (Mn)
    (0x115F, 0x1160),  # HANGUL CHOSEONG / JUNGSEONG FILLER (Lo)
    (0x17B4, 0x17B5),  # KHMER VOWEL INHERENT AQ / AA (Mn)
    (0x180B, 0x180F),  # MONGOLIAN FREE VARIATION SELECTORS (Mn; U+180E is Cf)
    (0x2065, 0x2065),  # reserved default-ignorable (Cn)
    (0x2800, 0x2800),  # BRAILLE PATTERN BLANK (So) — renders as nothing
    (0x3164, 0x3164),  # HANGUL FILLER (Lo)
    (0xFE00, 0xFE0F),  # VARIATION SELECTOR-1..16 (Mn)
    (0xFFA0, 0xFFA0),  # HALFWIDTH HANGUL FILLER (Lo)
    (0xFFF0, 0xFFF8),  # reserved default-ignorable (Cn)
    (0xE0000, 0xE0FFF),  # tags (Cf), VARIATION SELECTOR-17..256 (Mn), reserved
)


def _is_invisible(c: str) -> bool:
    """Whether the single character *c* shows nothing on its own (#1258)."""
    if c.isspace() or unicodedata.category(c) in _INVISIBLE_CATEGORIES:
        return True
    cp = ord(c)
    return any(lo <= cp <= hi for lo, hi in _INVISIBLE_RANGES)


def _is_blank(text: str | None) -> bool:
    """True when *text* has nothing visible — whitespace and invisible marks only.

    A character that is visible makes the whole text not blank, whatever marks
    surround it: a ZWJ emoji sequence or a letter + VS16 is accepted (#1258).
    """
    # Linear: each distinct character is looked at once (``set`` is one C-speed
    # pass). Peeling whitespace and marks in turn was quadratic on input like
    # " \u200b" * n and blocked the event loop (#1238).
    return all(_is_invisible(c) for c in set(text or ""))


def _text(data: dict[str, Any], field: str) -> tuple[str | None, web.Response | None]:
    """``data[field]`` as text — ``None`` when absent or ``null`` — or the 400 naming it.

    A number, list or object in a text field reached ``.strip()`` or SQLite and
    came back as a 500 with no reason (#1183).
    """
    value = data.get(field)
    if value is None or isinstance(value, str):
        return value, None
    return None, web.json_response({"error": f"{field} must be a string"}, status=400)


def _message_and_title(data: dict[str, Any]) -> tuple[str, str | None, web.Response | None]:
    """``message`` (required) and ``title`` (optional) as text, or the 400.

    Anything else reached SQLite or ``discord.Embed`` unchecked — an integer
    past 64 bits, a list or an object was a 500 (found reviewing #1083).
    """
    message = data.get("message")
    if not message:
        return "", None, web.json_response({"error": "message is required"}, status=400)
    if not isinstance(message, str):
        return "", None, web.json_response({"error": "message must be a string"}, status=400)
    if _is_blank(message):
        # #1288: an embed of zero-width spaces was posted (notify) or queued (schedule).
        return "", None, web.json_response({"error": "message is required"}, status=400)
    title = data.get("title")
    if title is not None and not isinstance(title, str):
        return "", None, web.json_response({"error": "title must be a string"}, status=400)
    return message, title, None


# What PATCH /api/tasks/{id} can change — named in the 400 for a body with none (#1066).
_PATCHABLE_TASK_FIELDS = ("enabled", "prompt", "interval_seconds", "working_dir")
_INTERVAL_ERROR = "interval_seconds must be an integer of 1 or more"


_SCHEDULED_AT_ERROR = "scheduled_at must be ISO 8601 format"
_STORED_TIME_FORMAT = "%Y-%m-%dT%H:%M:%S"
_DEFAULT_EMBED_COLOR = 0x00BFFF
_LOUNGE_FIELDS = ("id", "label", "message", "posted_at")
_LOUNGE_LIMIT_DEFAULT = "10"
_LOUNGE_LIMIT_MAX = 50


def _error(reason: str, status: int = 400) -> web.Response:
    """The one shape every refusal of this API has: ``{"error": reason}``."""
    return web.json_response({"error": reason}, status=status)


def _is_present(value: object) -> bool:
    """Whether a cache lookup found something (``None`` is the only miss)."""
    return value is not None


def _lounge_clock(posted_at: str) -> str:
    """The ``HH:MM`` of a ``YYYY-MM-DD HH:MM:SS`` stamp; a shorter text as it is."""
    return posted_at[11:16] if len(posted_at) >= 16 else posted_at


def _lounge_row(row: Any) -> dict[str, Any]:
    """A lounge message as the JSON object both lounge endpoints answer with."""
    return {field: getattr(row, field) for field in _LOUNGE_FIELDS}


class ApiServer:
    """The HTTP control plane of one bot.

    Building one only wires the aiohttp application (``app``); no socket is
    opened until :meth:`start`. The repositories and ``session_dir_base`` are
    plain attributes that may be assigned after construction — handlers read
    them at request time.

    Example::

        from c_lord.database.notification_repo import NotificationRepository
        from c_lord.ext.api_server import ApiServer

        api = ApiServer(NotificationRepository("notifications.db"), bot)
        await api.start()
        ...
        await api.stop()
    """

    def __init__(
        self,
        repo: NotificationRepository,
        bot: Bot,
        default_channel_id: int | None = None,
        host: str = "127.0.0.1",
        port: int = 8080,
        api_secret: str | None = None,
        task_repo: TaskRepository | None = None,
        lounge_repo: LoungeRepository | None = None,
        lounge_channel_id: int | None = None,
        resume_repo: PendingResumeRepository | None = None,
        session_repo: SessionRepository | None = None,
        show_url_embeds: bool = False,
        owner_uid: int | None = None,
        allow_any_peer: bool = False,
        auto_port: bool = False,
    ) -> None:
        self.repo = repo
        self.bot = bot
        self.default_channel_id = default_channel_id
        # #372: when False (default) Discord URL/OGP link previews are
        # suppressed on /api/reply so a link in Claude's answer doesn't expand
        # into a tall preview card. Opt back in with CLORD_SHOW_URL_EMBEDS=true.
        self.show_url_embeds = show_url_embeds
        self.host = host
        self.port = port
        # #258: when the port was not chosen by anyone, a taken one is no
        # reason to run without an API — walk to the next free one. A port the
        # operator pinned is never walked past (their curl expects it there).
        self.auto_port = auto_port
        self.api_secret = api_secret
        self.task_repo = task_repo
        self.lounge_repo = lounge_repo
        self.resume_repo = resume_repo
        self.session_repo = session_repo
        # Where c-lord clones session dirs; ``/api/reply``'s progress_file may
        # only point under it (#1039). Wired by ``BridgeComponents``.
        self.session_dir_base: str | None = None
        if lounge_channel_id is None:
            from ..coordination.env import coordination_channel_id_from_env

            lounge_channel_id = coordination_channel_id_from_env()  # #1194
        self.lounge_channel_id = lounge_channel_id

        # #457: the UID this bot runs as. Only that user (and root, which can
        # read the bot's .env anyway) may drive the control plane. Resolved at
        # construction so a test can move it instead of becoming another user.
        self.owner_uid = _current_uid() if owner_uid is None else owner_uid
        self.allow_any_peer = allow_any_peer
        self._denial_log = LogSampler()

        self.app = web.Application()
        # Unconditional: without a secret this used to be no middleware at all,
        # which is how every user on the host reached /api/spawn (#457).
        self.app.middlewares.append(self._auth_middleware)
        self._setup_routes()
        self._runner: web.AppRunner | None = None

    def _setup_routes(self) -> None:
        """Attach every endpoint to ``app``. ``add_get`` also answers HEAD."""
        router = self.app.router
        router.add_get("/api/health", self.health)
        # Notifications: one now, or one queued for later
        router.add_post("/api/notify", self.notify)
        router.add_post("/api/schedule", self.schedule)
        router.add_get("/api/scheduled", self.list_scheduled)
        router.add_delete("/api/scheduled/{id}", self.cancel_scheduled)
        # Recurring tasks run by SchedulerCog
        router.add_post("/api/tasks", self.create_task)
        router.add_get("/api/tasks", self.list_tasks)
        router.add_delete("/api/tasks/{id}", self.delete_task)
        router.add_patch("/api/tasks/{id}", self.patch_task)
        # AI Lounge
        router.add_get("/api/lounge", self.get_lounge)
        router.add_post("/api/lounge", self.post_lounge)
        # A new session thread without a human message
        router.add_post("/api/spawn", self.spawn)
        # Thread message route (CLI input forwarding)
        self.app.router.add_post("/api/threads/{thread_id}/messages", self.post_thread_message)
        # #955: staging's release puts its verification windows to sleep
        self.app.router.add_post("/api/threads/{thread_id}/sleep", self.sleep_thread)
        # Issue #52: Skill-based final reply (clean content, no source prefix)
        self.app.router.add_post("/api/reply", self.reply)
        # Issue #63: Skill-based interactive choice prompt
        self.app.router.add_post("/api/prompt-choice", self.prompt_choice)
        # Resume this thread once the bot is back from a restart
        self.app.router.add_post("/api/mark-resume", self.mark_resume)

    @web.middleware
    async def _auth_middleware(
        self,
        request: web.Request,
        handler: Callable[[web.Request], Awaitable[web.StreamResponse]],
    ) -> web.StreamResponse:
        """Refuse anything that is neither our own Unix user nor holding the secret."""
        denial = self._authorize(request)
        if denial is not None:
            return denial
        return await handler(request)

    def _authorize(self, request: web.Request) -> web.Response | None:
        """``None`` to let the request through, otherwise the refusal to send.

        Two independent ways in, because they answer different questions:

        * **Same UID** — the caller already *is* the bot's user, so it can read
          ``.env``, the session dirs and the secret itself. Demanding a header
          from it would protect nothing and would break the control plane
          (``CLORD_API_SECRET`` is stripped from the tmux environment by #353,
          so a Claude session cannot read it from ``env`` anyway).
        * **``CLORD_API_SECRET``** — the explicit way to let *someone else* in:
          another Unix user, or another host. Holding it is the proof.

        ``/api/health`` is gated too. It used to answer 200 unconditionally,
        which turned a port scan into an inventory of every c-lord on the host.
        """
        auth_header = request.headers.get("Authorization", "")
        if self.api_secret and auth_header.startswith("Bearer "):
            if self._token_matches(auth_header[7:]):
                return None
            return _error("Invalid token", 401)

        uid = self._peer_uid(request)
        # root is not a distinct trust level here: it can read the bot's .env,
        # ptrace the process, or just become the bot's user.
        if uid is not None and (uid == self.owner_uid or uid == 0):
            return None
        if self.allow_any_peer:
            return None

        # The refusal itself says nothing: a foreign caller should not be able to
        # tell a c-lord from any other 403 on the host. The operator, who does
        # need to know, has the log line below.
        sample = self._denial_log.sample(uid)
        if sample.emit:
            logger.warning(
                "REST API denied %s %s from uid=%s (this bot runs as uid=%s). "
                "Set CLORD_API_SECRET and send `Authorization: Bearer …` to allow "
                "another user or host in%s",
                request.method,
                request.path,
                "unknown" if uid is None else uid,
                self.owner_uid,
                sample.suffix,
            )
        return web.json_response({"error": "Forbidden"}, status=403)

    def _token_matches(self, token: str) -> bool:
        """Constant-time comparison against ``api_secret``.

        ``==`` short-circuits on the first differing byte and leaks the token's
        prefix through timing. ``compare_digest`` raises ``TypeError`` on a
        non-ASCII string, and the value comes straight off the wire — so a
        header of ``Bearer ñ`` would otherwise be a 500 anyone could trigger.
        """
        try:
            return hmac.compare_digest(token, self.api_secret or "")
        except TypeError:
            return False

    def _peer_uid(self, request: web.Request) -> int | None:
        """UID behind this request's socket, or ``None`` when unprovable."""
        transport = request.transport
        if transport is None:
            return None
        return resolve_peer_uid(
            transport.get_extra_info("peername"),
            transport.get_extra_info("sockname"),
        )

    async def start(self) -> None:
        """Begin listening. A port that cannot be bound raises, with nothing left running."""
        self._runner = web.AppRunner(self.app)
        await self._runner.setup()
        try:
            self.port = await self._bind()
        except (OSError, OverflowError):  # OverflowError: a port past 65535 (#1237)
            await self._runner.cleanup()
            self._runner = None
            raise
        # #258: Claude in each session is told this address — the port that was
        # actually bound, which after a walk is not the one asked for.
        api_endpoint.advertise(self.host, self.port)
        logger.info("REST API started: http://%s:%d", self.host, self.port)
        # #457: who may drive the control plane is never left implicit — an
        # opened gate says so at every start, not only in the docs.
        log = (
            logger.info
            if (self.owner_uid is not None and not self.allow_any_peer)
            else logger.warning
        )
        log("REST API access: %s", self._gate_summary())

    def _gate_summary(self) -> str:
        """One line saying who may call this API — printed at every start (#457)."""
        if self.allow_any_peer:
            return (
                "CLORD_API_ALLOW_ANY_PEER=1: ANY user on this host may spawn Claude "
                "sessions as you — unset it unless you meant this"
            )
        if self.owner_uid is None:
            return (
                "peer UID cannot be verified on this platform; only callers holding "
                "CLORD_API_SECRET are served. Set CLORD_API_ALLOW_ANY_PEER=1 to accept "
                "unverified local callers instead"
            )
        # Named `note`, not `secret`: the value is one of two constants, but a
        # variable called `secret` in a logged f-string trips CodeQL's
        # clear-text-logging heuristic (py/clear-text-logging-sensitive-data).
        note = "or holding CLORD_API_SECRET" if self.api_secret else "no secret configured"
        return f"serving uid={self.owner_uid} only, {note}"

    async def _bind(self) -> int:
        """Listen on ``self.port`` — or, for ``auto_port``, the next free one (#258).

        Returns the port actually bound. Raises the last ``OSError`` when no
        port could be bound (always the case for a taken, operator-pinned port).
        """
        assert self._runner is not None
        if not self.auto_port:
            candidates = [self.port]
        else:
            # Then port 0: the OS picks one that is certainly free.
            candidates = [self.port + i for i in range(AUTO_PORT_TRIES)] + [0]
        error: OSError | None = None
        for port in candidates:
            site = web.TCPSite(self._runner, self.host, port)
            try:
                await site.start()
            except OSError as exc:
                error = exc
                continue
            bound = _bound_port(site) or port
            if bound != self.port:
                # INFO, not WARNING: a second instance on one host is the case
                # this exists for, not a fault.
                logger.info(
                    "REST API: port %d on %s is taken — listening on %d instead. "
                    "Set CLORD_API_PORT to pin a port (#258).",
                    self.port,
                    self.host,
                    bound,
                )
            return bound
        assert error is not None
        raise error

    async def stop(self) -> None:
        """Stop the API server. Safe to call more than once, even concurrently (#877)."""
        api_endpoint.clear()
        # Taken before the await: ``AppRunner.cleanup()`` yields between checking
        # its server and using it, so a second caller reaching it meanwhile hit
        # ``'NoneType' object has no attribute 'pre_shutdown'``.
        runner, self._runner = self._runner, None
        if runner:
            await runner.cleanup()

    # ------------------------------------------------------------------
    # Liveness
    # ------------------------------------------------------------------

    async def health(self, request: web.Request) -> web.Response:
        """GET /api/health — ``{"status": "ok", "timestamp": <local time, ISO 8601>}``."""
        return web.json_response({"status": "ok", "timestamp": datetime.now().isoformat()})

    # ------------------------------------------------------------------
    # Notifications (/api/notify, /api/schedule, /api/scheduled)
    # ------------------------------------------------------------------

    async def notify(self, request: web.Request) -> web.Response:
        """POST /api/notify — send one embed right away.

        Body (JSON): ``message`` (required), ``title``, ``channel_id`` (falls
        back to the default channel), ``color``.

        Returns (200): ``{"status": "sent"}``
        """
        data, err = await _read_json_object(request)
        if err is not None:
            return err

        message, title, err = _message_and_title(data)
        if err is not None:
            return err

        explicit_channel, color, err = _optional_channel_and_color(data)
        if err is not None:
            return err
        channel_id = explicit_channel or self.default_channel_id
        if not channel_id:
            return _error("No channel specified")
        channel, failure = await self._lookup_channel(channel_id, trust_cached=bool)
        if failure is not None:
            return _error(str(failure), 500)
        if not hasattr(channel, "send"):
            return _error("Channel is not messageable")

        embed = self._build_embed(message=message, title=title, color=color)
        await channel.send(embed=embed)
        return web.json_response({"status": "sent"})

    async def schedule(self, request: web.Request) -> web.Response:
        """POST /api/schedule — queue a notification for a later time.

        Body (JSON): ``message`` and ``scheduled_at`` (ISO 8601) are required;
        ``title``, ``channel_id`` and ``color`` are optional. Nothing is sent
        here — the row waits in the database for the scheduler.

        Returns (200): ``{"status": "scheduled", "id": <row id>}``
        """
        data, err = await _read_json_object(request)
        if err is not None:
            return err

        message, title, err = _message_and_title(data)
        if err is not None:
            return err

        scheduled_at = data.get("scheduled_at")
        if not scheduled_at:
            return _error("scheduled_at is required")
        if not isinstance(scheduled_at, str):
            # ``fromisoformat`` raised TypeError on a number or ``true`` — a 500 (#1083).
            return _error(_SCHEDULED_AT_ERROR)

        channel_id, color, err = _optional_channel_and_color(data)
        if err is not None:
            return err

        try:
            when = datetime.fromisoformat(scheduled_at)
        except ValueError:
            return _error(_SCHEDULED_AT_ERROR)

        # Stored to the second, as written: an offset is dropped, not converted.
        notification_id = await self.repo.create(
            message=message,
            scheduled_at=when.strftime(_STORED_TIME_FORMAT),
            title=title,
            color=0x00BFFF if color is None else color,
            source="api",
            channel_id=channel_id,
        )
        return web.json_response({"status": "scheduled", "id": notification_id})

    async def list_scheduled(self, request: web.Request) -> web.Response:
        """GET /api/scheduled — the notifications still waiting to be sent."""
        return web.json_response({"notifications": await self.repo.get_pending()})

    async def cancel_scheduled(self, request: web.Request) -> web.Response:
        """DELETE /api/scheduled/{id} — withdraw a notification that has not gone out."""
        notification_id = _as_int(request.match_info.get("id"))
        if notification_id is None:
            return _error("Invalid ID")
        if not await self.repo.cancel(notification_id):
            return _error("Not found or already processed", 404)
        return web.json_response({"status": "cancelled"})

    # ------------------------------------------------------------------
    # Recurring tasks (/api/tasks) — executed by SchedulerCog
    # ------------------------------------------------------------------

    def _require_task_repo(self) -> web.Response | None:
        """The 503 for a bot without a task store, or ``None`` when there is one."""
        if self.task_repo is None:
            return _error("SchedulerCog not configured (task_repo is None)", 503)
        return None

    async def create_task(self, request: web.Request) -> web.Response:
        """POST /api/tasks — register a prompt to run every ``interval_seconds``.

        Body (JSON): ``name``, ``prompt``, ``interval_seconds`` and
        ``channel_id`` are required; ``working_dir`` and ``run_immediately``
        (default true) are optional.

        Returns (201): ``{"status": "created", "id": <task id>}``; 409 when the
        name is taken.
        """
        unavailable = self._require_task_repo()
        if unavailable is not None:
            return unavailable

        data, err = await _read_json_object(request)
        if err is not None:
            return err

        for field in ("name", "prompt", "interval_seconds", "channel_id"):
            if not data.get(field):
                return _error(f"{field} is required")

        for field in ("name", "prompt"):
            # #1208: ``["x"]`` was saved through ``str()`` as the prompt ``['x']``.
            text, err = _text(data, field)
            if err is not None:
                return err
            if _is_blank(text):
                # #1238: ``"   "`` was saved and woke Claude with no instruction
                # on every run; /api/spawn already strips before this check.
                return _error(f"{field} is required")
        run_immediately = data.get("run_immediately", True)
        if not isinstance(run_immediately, bool):
            return _not_a_boolean("run_immediately")

        working_dir, err = _text(data, "working_dir")
        if err is not None:
            return err

        numbers: dict[str, int] = {}
        for field in ("interval_seconds", "channel_id"):
            number = _task_int(data[field])
            if number is None:
                # Not a name clash: answering 409 here sent callers looking for a
                # task that did not exist (#1037).
                return web.json_response({"error": f"{field} must be an integer"}, status=400)
            numbers[field] = number
        if numbers["interval_seconds"] < 1:
            # #1064: PATCH refuses it too — one rule for both.
            return web.json_response({"error": _INTERVAL_ERROR}, status=400)

        try:
            task_id = await self.task_repo.create(  # type: ignore[union-attr]
                name=data["name"],
                prompt=data["prompt"],
                interval_seconds=numbers["interval_seconds"],
                channel_id=numbers["channel_id"],
                working_dir=working_dir,
                run_immediately=run_immediately,
            )
        except sqlite3.IntegrityError as exc:
            # The UNIQUE constraint on name.
            logger.warning("Failed to create task: %s", exc)
            return _error("Task name already exists", 409)
        logger.info("Task registered via API: id=%s, name=%s", task_id, data["name"])
        return web.json_response({"status": "created", "id": task_id}, status=201)

    async def list_tasks(self, request: web.Request) -> web.Response:
        """GET /api/tasks — every registered task, enabled or not."""
        unavailable = self._require_task_repo()
        if unavailable is not None:
            return unavailable
        tasks = await self.task_repo.get_all()  # type: ignore[union-attr]
        return web.json_response({"tasks": tasks})

    async def delete_task(self, request: web.Request) -> web.Response:
        """DELETE /api/tasks/{id} — remove a task for good."""
        unavailable = self._require_task_repo()
        if unavailable is not None:
            return unavailable

        task_id = _as_int(request.match_info.get("id"))
        if task_id is None:
            return _error("Invalid ID")
        if not await self.task_repo.delete(task_id):  # type: ignore[union-attr]
            return _error("Task not found", 404)
        return web.json_response({"status": "deleted"})

    async def patch_task(self, request: web.Request) -> web.Response:
        """PATCH /api/tasks/{id} — change ``enabled``, ``prompt``, ``interval_seconds``
        and / or ``working_dir``. Either every given value is applied or none is.

        Returns (200): ``{"status": "updated"}``
        """
        unavailable = self._require_task_repo()
        if unavailable is not None:
            return unavailable

        task_id = _as_int(request.match_info.get("id"))
        if task_id is None:
            return _error("Invalid ID")

        data, err = await _read_json_object(request)
        if err is not None:
            return err

        # Check every value before writing any: a 400 used to arrive after
        # ``enabled`` had already been saved (#1064).
        if "enabled" in data and not isinstance(data["enabled"], bool):
            return _not_a_boolean("enabled")
        patch_kwargs: dict[str, Any] = {}
        if data.get("prompt") is not None:
            prompt, err = _text(data, "prompt")  # #1208: no more str(["x"]) -> "['x']"
            if err is not None:
                return err
            if _is_blank(prompt):
                # #1229: POST refuses an empty prompt; PATCH saved it with a 200,
                # and every run after that woke Claude with no instruction.
                # #1238: whitespace only is empty too, as for POST and /api/spawn.
                return _error("prompt is required")
            patch_kwargs["prompt"] = prompt
        if "interval_seconds" in data:
            interval = _task_int(data["interval_seconds"])
            if interval is None or interval < 1:
                # #1064: 0 / negative / false were saved with a 200.
                return web.json_response({"error": _INTERVAL_ERROR}, status=400)
            patch_kwargs["interval_seconds"] = interval
        if data.get("working_dir") is not None:
            # #1183: a list was saved as the text "['x']" — same rule as on create.
            # ``null`` is "not given", as everywhere (it was saved as "None").
            if not isinstance(data["working_dir"], str):
                return web.json_response({"error": "working_dir must be a string"}, status=400)
            patch_kwargs["working_dir"] = data["working_dir"]

        if await self.task_repo.get(task_id) is None:  # type: ignore[union-attr]
            return _error("Task not found", 404)
        if "enabled" not in data and not patch_kwargs:
            # #1066: "nothing to update" is not "no such task" — a caller told 404
            # for a typo (``enable``) went off to re-create a task that exists.
            return web.json_response(
                {
                    "error": "No field to update. Accepted fields: "
                    + ", ".join(_PATCHABLE_TASK_FIELDS)
                },
                status=400,
            )

        if "enabled" in data:
            await self.task_repo.set_enabled(task_id, enabled=data["enabled"])  # type: ignore[union-attr]
        if patch_kwargs:
            await self.task_repo.update(task_id, **patch_kwargs)  # type: ignore[union-attr]
        return web.json_response({"status": "updated"})

    # ------------------------------------------------------------------
    # AI Lounge (/api/lounge)
    # ------------------------------------------------------------------

    def _require_lounge_repo(self) -> web.Response | None:
        """The 503 for a bot without a lounge store, or ``None`` when there is one."""
        if self.lounge_repo is None:
            return _error("AI Lounge not configured (lounge_repo is None)", 503)
        return None

    async def get_lounge(self, request: web.Request) -> web.Response:
        """GET /api/lounge?limit=N — the latest lounge messages (N: 1 to 50, default 10).

        Returns (200): ``{"messages": [{"id", "label", "message", "posted_at"}, ...]}``
        """
        unavailable = self._require_lounge_repo()
        if unavailable is not None:
            return unavailable
        try:
            asked = int(request.query.get("limit", _LOUNGE_LIMIT_DEFAULT))
        except ValueError:
            return _error("limit must be an integer")
        limit = min(max(asked, 1), _LOUNGE_LIMIT_MAX)
        rows = await self.lounge_repo.get_recent(limit=limit)  # type: ignore[union-attr]
        return web.json_response({"messages": [_lounge_row(row) for row in rows]})

    async def post_lounge(self, request: web.Request) -> web.Response:
        """POST /api/lounge — leave a message in the lounge.

        Body (JSON): ``message`` (required), ``label`` (who is speaking;
        ``"AI"`` when left out). The saved message is also shown in the lounge's
        Discord channel when one is configured.

        Returns (201): ``{"status": "posted"}`` plus the saved message's fields.
        """
        unavailable = self._require_lounge_repo()
        if unavailable is not None:
            return unavailable

        data, err = await _read_json_object(request)
        if err is not None:
            return err

        raw_message, err = _text(data, "message")
        if err is not None:
            return err
        message = (raw_message or "").strip()
        # #1288: invisible-only text was posted to the lounge — the #1258 test.
        if _is_blank(message):
            return _error("message is required")

        raw_label, err = _text(data, "label")  # #1208
        if err is not None:
            return err
        label = (raw_label or "").strip() or "AI"

        saved = await self.lounge_repo.post(message=message, label=label)  # type: ignore[union-attr]
        if self.lounge_channel_id:
            await self._send_lounge_to_discord(saved.label, saved.message, saved.posted_at)
        return web.json_response({"status": "posted", **_lounge_row(saved)}, status=201)

    async def _send_lounge_to_discord(self, label: str, message: str, posted_at: str) -> None:
        """Show a saved lounge message in the lounge channel. Never raises.

        The message is already stored when this runs, so a Discord failure is
        logged and the caller still answers 201.
        """
        try:
            line = f"**[{label}]** {message} *({_lounge_clock(posted_at)})*"
            channel, failure = await self._lookup_channel(
                self.lounge_channel_id  # type: ignore[arg-type]
            )
            if failure is not None:
                raise failure
            if hasattr(channel, "send"):
                await channel.send(line)
        except Exception:
            logger.warning("Failed to forward lounge message to Discord", exc_info=True)

    # ------------------------------------------------------------------
    # Session spawn (/api/spawn)
    # ------------------------------------------------------------------

    async def spawn(self, request: web.Request) -> web.Response:
        """POST /api/spawn — open a new thread and start a Claude session in it.

        Body (JSON):
            prompt: What Claude is asked to do (required).
            channel_id: The text channel to open the thread in (optional;
                the default channel when left out).
            thread_name: Custom thread title (optional; defaults to a
                readable topic derived from *prompt* — #721: one line, no
                markdown or URLs, ``…`` when it had to be cut short).

        Returns (201):
            ``{"status": "spawned", "thread_id": "<id as text>", "thread_name": "..."}``
        """
        data, err = await _read_json_object(request)
        if err is not None:
            return err

        raw_prompt, err = _text(data, "prompt")
        if err is not None:
            return err
        prompt = (raw_prompt or "").strip()
        if _is_blank(prompt):
            return _error("prompt is required")

        raw_channel_id = data.get("channel_id") or self.default_channel_id
        if not raw_channel_id:
            return _error("No channel specified")
        channel_id = _as_int(raw_channel_id)
        if channel_id is None:
            return _error("channel_id must be an integer")

        raw_thread_name, err = _text(data, "thread_name")  # #1183 (review)
        if err is not None:
            return err

        # Asked before any Discord call: without the cog nothing can be started.
        cog: Any = self.bot.cogs.get("ClaudeChatCog")
        if cog is None:
            return _error("ClaudeChatCog is not loaded", 503)

        channel, failure = await self._lookup_channel(channel_id)
        if failure is not None:
            return _error(str(failure), 500)

        import discord

        if not isinstance(channel, discord.TextChannel):
            return _error("Channel must be a text channel that supports threads")

        thread_name: str | None = raw_thread_name or None
        try:
            thread = await cog.spawn_session(channel, prompt, thread_name=thread_name)
        except Exception as exc:
            logger.exception("spawn_session failed: %s", exc)
            return _error(str(exc), 500)

        logger.info("Spawned new Claude session in thread %s (%s)", thread.id, thread.name)
        return web.json_response(
            {"status": "spawned", "thread_id": str(thread.id), "thread_name": thread.name},
            status=201,
        )

    # ------------------------------------------------------------------
    # Resume after restart (/api/mark-resume)
    # ------------------------------------------------------------------

    def _require_resume_repo(self) -> web.Response | None:
        """The 503 for a bot without a resume store, or ``None`` when there is one."""
        if self.resume_repo is None:
            return _error("PendingResumeRepository not configured (resume_repo is None)", 503)
        return None

    async def _recorded_session_id(self, thread_id: int) -> str | None:
        """The session id on record for this thread, if the bot knows one.

        Best effort: without a session store, without a record, or when the
        lookup fails (logged), the answer is ``None`` and the caller goes on.
        """
        if self.session_repo is None:
            return None
        try:
            record = await self.session_repo.get(thread_id)
            if record is None:
                return None
            found = record.session_id
            logger.debug("mark-resume: auto-resolved session_id=%s for thread %s", found, thread_id)
            return found
        except Exception:
            logger.warning(
                "mark-resume: failed to auto-resolve session_id for thread %s",
                thread_id,
                exc_info=True,
            )
            return None

    async def mark_resume(self, request: web.Request) -> web.Response:
        """POST /api/mark-resume — have this thread picked up again after the bot restarts.

        Body (JSON):
            thread_id: The thread to resume (required).
            session_id: The Claude session to continue (optional; looked up
                from the thread's session record when left out).
            reason: Why the restart happens (optional; ``"self_restart"``).
            resume_prompt: What to tell Claude on resume (optional).

        Returns (201): ``{"status": "marked", "id": <row id>}``
        """
        unavailable = self._require_resume_repo()
        if unavailable is not None:
            return unavailable

        data, err = await _read_json_object(request)
        if err is not None:
            return err

        _, err = _text(data, "reason")  # #1208: ``str()`` saved ["x"] as "['x']"
        if err is not None:
            return err

        raw_thread_id = data.get("thread_id")
        if not raw_thread_id:
            return _error("thread_id is required")
        thread_id = _as_int(raw_thread_id)
        if thread_id is None:
            return _error("thread_id must be an integer")

        raw_session_id, err = _text(data, "session_id")
        if err is not None:
            return err
        raw_resume_prompt, err = _text(data, "resume_prompt")
        if err is not None:
            return err
        session_id: str | None = raw_session_id or None
        reason = data.get("reason") or "self_restart"
        resume_prompt: str | None = raw_resume_prompt or None

        if session_id is None:
            session_id = await self._recorded_session_id(thread_id)

        row_id = await self.resume_repo.mark(  # type: ignore[union-attr]
            thread_id, session_id=session_id, reason=reason, resume_prompt=resume_prompt
        )
        logger.info(
            "Thread %s marked for resume (reason=%s, session_id=%s)", thread_id, reason, session_id
        )
        return web.json_response({"status": "marked", "id": row_id}, status=201)

    # ------------------------------------------------------------------
    # Thread sleep endpoint (/api/threads/{thread_id}/sleep) — #955
    # ------------------------------------------------------------------

    async def sleep_thread(self, request: web.Request) -> web.Response:
        """POST /api/threads/{thread_id}/sleep — put one workspace to sleep (#955).

        The same sleep as the 4-hour idle one (#572): the mirror is stopped
        before the window is killed (#379) and the next message resumes
        without a word. ``scripts/staging.sh release`` calls this for the
        threads it spawned during its lease, so a finished verification stops
        holding ~300 MB per window. Never a second implementation of sleep —
        it goes through ``SessionManageCog._sleep_workspace_impl``.

        Returns (200): ``{"status": "slept" | "not_resident", "thread_id": "..."}``
        — ``not_resident`` when the thread had no window to close.
        Returns (503): ``{"status": "failed", "thread_id": "..."}`` when the sleep
        could not close a window that may still be there (#1214) — a failed kill,
        or a tmux that did not answer. A window this sleep cannot reach (in
        another tmux session of ours, no tmux manager, not a thread) is
        ``not_resident``, as before #1214, so release closes the pane by id.
        """
        thread_id = _as_int(request.match_info.get("thread_id"))
        if thread_id is None:
            return _error("thread_id must be an integer")

        from ..workspace_notice import WorkspaceReason

        cog = self.bot.cogs.get("SessionManageCog")
        impl = getattr(cog, "_sleep_workspace_impl", None)
        if impl is None:
            return web.json_response({"error": "SessionManageCog is not loaded"}, status=503)

        channel = self.bot.get_channel(thread_id)
        if channel is None:
            try:
                channel = await self.bot.fetch_channel(thread_id)
            except Exception as exc:
                logger.info(
                    "%s sleep: thread is not reachable (%s)", log_ctx(thread_id=thread_id), exc
                )
                return web.json_response({"error": "thread not found"}, status=404)

        try:
            # Review of #1214: only a failed kill (or a tmux that did not answer)
            # is ``failed``; a window this sleep could not reach is ``not_resident``
            # as before, so ``staging.sh release`` closes that pane by id.
            slept = await impl(
                channel=channel, reason=WorkspaceReason.MANUAL, unkillable_is_absent=True
            )
        except Exception as exc:
            logger.exception("%s sleep: failed", log_ctx(thread_id=thread_id))
            return _error(str(exc), 500)

        # #1214: three answers, not two. ``False`` is "could not close it, the
        # window may still be there" (#1192); folding it into ``not_resident``
        # told ``staging.sh release`` the bot had no window, so nobody noticed it.
        if slept:
            status, code = "slept", 200
        elif slept is None:
            status, code = "not_resident", 200
        else:
            status, code = "failed", 503
        logger.info("%s sleep via API: %s", log_ctx(thread_id=thread_id), status)
        return web.json_response({"status": status, "thread_id": str(thread_id)}, status=code)

    # ------------------------------------------------------------------
    # Thread message endpoint (/api/threads/{thread_id}/messages)
    # ------------------------------------------------------------------

    async def post_thread_message(self, request: web.Request) -> web.Response:
        """POST /api/threads/{thread_id}/messages — post a message to a Discord thread.

        Enables external Claude Code instances (CLI, tmux) to forward human
        inputs to a Discord thread, creating a complete conversation log.

        Body (JSON):
            content: The message text to post (required).
            source: Label for the input source (optional; "cli", "tmux", "api").
                    Defaults to "cli".

        Returns (200):
            ``{"status": "sent"}``
        """
        thread_id = _as_int(request.match_info.get("thread_id"))
        if thread_id is None:
            return web.json_response({"error": "Invalid thread_id"}, status=400)

        data, err = await _read_json_object(request)
        if err is not None:
            return err

        raw_content, err = _text(data, "content")
        if err is not None:
            return err
        content = (raw_content or "").strip()
        # #1288: invisible-only text was posted to the thread — the #1258 test.
        if _is_blank(content):
            return web.json_response({"error": "content is required"}, status=400)

        raw_source, err = _text(data, "source")  # #1208
        if err is not None:
            return err
        source = (raw_source or "").strip() or "cli"

        # Resolve the thread/channel
        raw = self.bot.get_channel(thread_id)
        if raw is None:
            try:
                raw = await self.bot.fetch_channel(thread_id)
            except Exception:
                return web.json_response({"error": "Thread not found"}, status=404)

        if not hasattr(raw, "send"):
            return web.json_response({"error": "Channel is not messageable"}, status=400)

        # Format and send — use subdued markdown so it's visually distinct
        # from bot/Claude messages but doesn't overwhelm the thread.
        formatted = f"-# \U0001f4bb ({source}) {content}"
        await raw.send(formatted)  # type: ignore[union-attr]

        return web.json_response({"status": "sent"})

    async def reply(self, request: web.Request) -> web.Response:
        """POST /api/reply — Skill-based final reply (#52 Phase 1).

        Used by the ``discord-reply`` skill that c-lord injects into each
        Claude session directory. Unlike ``post_thread_message``, the content
        is posted *as-is* (no ``-# 💻 (cli)`` prefix) because this represents
        Claude's actual answer, not a CLI forwarding decoration.

        Body (JSON):
            thread_id: Target Discord thread ID (required).
            content: Message body, Discord markdown OK (required, non-empty).
            progress_file: Absolute path on the server to a file to attach
                (optional). Used for progress.txt-style detail dumps (#38).
                Only a file under c-lord's session dirs is attached; anything
                else is left off with a WARNING and ``"progress_file":
                "ignored"`` in the answer (#1039).

        Returns:
            200 ``{"status": "sent"}`` on success.
            400 on validation errors (missing / empty fields, missing file).
            404 if the thread cannot be resolved.
        """
        data, err = await _read_json_object(request)
        if err is not None:
            return err

        thread_id_raw = data.get("thread_id")
        if thread_id_raw is None:
            return _error("thread_id is required")
        thread_id = _as_int(thread_id_raw)
        if thread_id is None:
            return _error("thread_id must be an integer")

        raw_content, err = _text(data, "content")
        if err is not None:
            return err
        content = (raw_content or "").strip()
        if _is_blank(content):  # #1288
            return web.json_response({"error": "content is required"}, status=400)

        # Optional attachment — resolve and validate before touching Discord.
        progress_file = data.get("progress_file")
        progress_fp: io.BufferedReader | None = None
        progress_ignored = False
        if progress_file:
            # #1039: only a file under the session dirs, symlinks and ``..``
            # resolved — checked before the file is even looked at, so a path
            # outside does not learn whether it exists either. No base (never
            # wired) means nothing is inside: an unknown boundary must not
            # become "anywhere". #1106: checked and opened in one walk.
            opened = _open_inside(self.session_dir_base, progress_file)
            if opened == _OUTSIDE:
                # Not an error the caller can fix by retrying, and the answer
                # itself should still arrive — so post without it (#1039).
                # A value that is not a path at all (NUL, #1105) lands here too.
                progress_ignored = True
                logger.warning(
                    # %r: the value comes from the caller; a newline in it must
                    # not start a fake log line (#1107).
                    "%s /api/reply: progress_file %r is outside the session dirs (%s) — "
                    "not attached (#1039)",
                    log_ctx(thread_id=thread_id),
                    progress_file,
                    self.session_dir_base or "unknown",
                )
            elif isinstance(opened, str):
                return web.json_response(
                    {"error": f"progress_file not found: {progress_file}"},
                    status=400,
                )
            else:
                progress_fp = opened
        attachment = None
        try:
            if progress_fp is not None:
                import discord

                attachment = discord.File(
                    progress_fp, filename=os.path.basename(str(progress_file))
                )
            return await self._send_reply(thread_id, content, attachment, progress_ignored)
        finally:
            # discord.File neither owns nor closes a file object it was handed
            # (it stubs out ``close`` until its own ``close``), so give the real
            # ``close`` back and close it on every path — 404 / 400 included
            # (#1106).
            if attachment is not None:
                attachment.close()
            if progress_fp is not None:
                progress_fp.close()

    async def _send_reply(
        self,
        thread_id: int,
        content: str,
        attachment: discord.File | None,
        progress_ignored: bool,
    ) -> web.Response:
        """The Discord half of :meth:`reply`, after the attachment is open."""
        # Issue #201: import unconditionally — `discord` is referenced later for
        # MessageReference / table images even when progress_file is absent. A
        # conditional import made the name a function-local that was unbound on
        # the no-attachment path, raising UnboundLocalError -> 500.
        import discord

        raw = self.bot.get_channel(thread_id)
        if raw is None:
            try:
                raw = await self.bot.fetch_channel(thread_id)
            except Exception:
                return web.json_response({"error": "Thread not found"}, status=404)

        if not hasattr(raw, "send"):
            return web.json_response({"error": "Channel is not messageable"}, status=400)

        # Optional table-image rendering (CLORD_RENDER_TABLE_IMAGES=true)
        from io import BytesIO

        from ..discord_ui.table_renderer import get_table_images

        table_files: list[discord.File] = [
            discord.File(BytesIO(img), filename=fname) for fname, img in get_table_images(content)
        ]

        all_files = table_files + ([attachment] if attachment else [])

        # Issue: long replies were cut off at Discord's 2000-char limit. Split
        # the body into multiple sequential messages. The quote-reply reference
        # rides on the first chunk; attachments ride on the last.
        from ..discord_ui.reply_chunker import chunk_discord_content

        chunks = chunk_discord_content(content)

        # Issue #115: reply to the trigger message that started this Claude turn.
        from ..transcript.mirror import reply_to_trigger_enabled

        reference = None
        if reply_to_trigger_enabled() and self.session_repo is not None:
            record = await self.session_repo.get(thread_id)
            trigger_id = getattr(record, "trigger_message_id", None) if record else None
            if trigger_id is not None:
                reference = discord.MessageReference(
                    message_id=trigger_id,
                    channel_id=thread_id,
                    fail_if_not_exists=False,
                )

        last_idx = len(chunks) - 1
        last_sent: discord.Message | None = None
        for idx, chunk in enumerate(chunks):
            # #372: suppress URL OGP/link-preview cards by default. Setting the
            # SUPPRESS_EMBEDS flag is non-destructive (it never mutates the body)
            # and is a no-op when the chunk has no URL.
            send_kwargs: dict = {"content": chunk, "suppress_embeds": not self.show_url_embeds}
            if idx == 0 and reference is not None:
                send_kwargs["reference"] = reference
                send_kwargs["mention_author"] = False
            if idx == last_idx and all_files:
                if len(all_files) == 1:
                    send_kwargs["file"] = all_files[0]
                else:
                    send_kwargs["files"] = all_files
            last_sent = await raw.send(**send_kwargs)  # type: ignore[union-attr]

        # Record the final message object so post-turn helpers (e.g. the
        # context-usage line) can edit it in place instead of creating a new
        # bubble.
        from ..skills.reply_tracker import record_reply_message

        if last_sent is not None:
            record_reply_message(thread_id, last_sent)

        if progress_ignored:
            return web.json_response({"status": "sent", "progress_file": "ignored"})
        return web.json_response({"status": "sent"})

    async def prompt_choice(self, request: web.Request) -> web.Response:
        """POST /api/prompt-choice — Skill-based interactive choice (#63).

        Used by the ``discord-prompt-choice`` skill so Claude can push a
        numbered choice menu to the Discord thread. Without this path,
        post-#58 the user never sees TUI-rendered option lists.

        Body (JSON):
            thread_id: Target Discord thread ID (required, int).
            question: Prompt text shown above the choices (required, non-empty).
            choices: Non-empty list of ``{"label": str, "description"?: str}``.

        Returns:
            200 ``{"status": "sent"}`` on success.
            400 on validation errors.
            404 if the thread cannot be resolved.
        """
        data, err = await _read_json_object(request)
        if err is not None:
            return err

        thread_id_raw = data.get("thread_id")
        if thread_id_raw is None:
            return _error("thread_id is required")
        thread_id = _as_int(thread_id_raw)
        if thread_id is None:
            return _error("thread_id must be an integer")

        raw_question, err = _text(data, "question")
        if err is not None:
            return err
        question = (raw_question or "").strip()
        if _is_blank(question):  # #1288
            return web.json_response({"error": "question is required"}, status=400)

        choices = data.get("choices")
        if not isinstance(choices, list) or not choices:
            return web.json_response({"error": "choices must be a non-empty array"}, status=400)

        # Validate each choice has at least a label.
        normalized: list[tuple[str, str]] = []
        for i, ch in enumerate(choices):
            if not isinstance(ch, dict):
                return web.json_response({"error": f"choices[{i}] must be an object"}, status=400)
            # #1208 (review): text, or absent / null — never ``str()`` of a list.
            raw_label, raw_desc = ch.get("label"), ch.get("description")
            for key, value in (("label", raw_label), ("description", raw_desc)):
                if value is not None and not isinstance(value, str):
                    return web.json_response(
                        {"error": f"choices[{i}].{key} must be a string"}, status=400
                    )
            label = (raw_label or "").strip()
            if _is_blank(label):  # #1288: nothing visible to reply with
                return web.json_response({"error": f"choices[{i}].label is required"}, status=400)
            desc = (raw_desc or "").strip()
            normalized.append((label, desc))

        raw = self.bot.get_channel(thread_id)
        if raw is None:
            try:
                raw = await self.bot.fetch_channel(thread_id)
            except Exception:
                return web.json_response({"error": "Thread not found"}, status=404)

        if not hasattr(raw, "send"):
            return web.json_response({"error": "Channel is not messageable"}, status=400)

        # Format the message. Bullets use the raw label so the user can copy
        # it verbatim; description (if present) gives context.
        lines = [f"🤔 **{question}**", ""]
        for label, desc in normalized:
            if desc:
                lines.append(f"- `{label}` — {desc}")
            else:
                lines.append(f"- `{label}`")
        lines.append("")
        lines.append("_Reply with the label to continue._")
        content = "\n".join(lines)

        await raw.send(content=content)  # type: ignore[union-attr]
        return web.json_response({"status": "sent"})

    # ------------------------------------------------------------------
    # Shared by the handlers above
    # ------------------------------------------------------------------

    async def _lookup_channel(
        self,
        channel_id: int,
        *,
        trust_cached: Callable[[Any], bool] = _is_present,
    ) -> tuple[Any, Exception | None]:
        """The channel with this id — from the cache, else asked of Discord.

        ``trust_cached`` decides whether what the cache gave is an answer:
        by default anything but ``None`` is; ``/api/notify`` passes ``bool``
        and asks Discord for anything falsy.

        Returns ``(channel, None)``, or ``(None, error)`` when Discord refused;
        what to answer for that differs per endpoint.
        """
        cached = self.bot.get_channel(channel_id)
        if trust_cached(cached):
            return cached, None
        try:
            return await self.bot.fetch_channel(channel_id), None
        except Exception as exc:
            return None, exc

    @staticmethod
    def _build_embed(
        message: str, title: str | None = None, color: int | None = None
    ) -> discord.Embed:
        """The embed a notification is sent as; no title or no (or zero) colour gets a default."""
        import discord

        return discord.Embed(
            title=title or "Notification",
            description=message,
            color=color or _DEFAULT_EMBED_COLOR,
            timestamp=datetime.now(),
        )


def _bound_port(site: web.TCPSite) -> int | None:
    """The port *site* is actually listening on (differs from the asked-for 0)."""
    server = getattr(site, "_server", None)
    sockets = getattr(server, "sockets", None) or ()
    for sock in sockets:
        name = sock.getsockname()
        if isinstance(name, tuple) and len(name) >= 2:
            return int(name[1])
    return None
