"""Model / effort settings with session, channel and global scopes (#918).

``/model`` used to be one global switch.  It is now three layers, resolved for
**model and effort independently**::

    session (explicit)  >  channel (explicit)  >  global  >  startup environment

* A Discord thread is one session.  *Global* means this c-lord instance only.
* When a session is created its effective values **and their origin** are saved
  (``session_model_settings``).  A change to a higher layer never touches an
  existing session unless ``include-existing`` is asked for, so a saved value
  is never silently re-resolved against today's settings.
* "Explicit at the session" (``explicit_*``) is kept apart from "inherited and
  saved" (``model``/``effort`` + ``*_src``).  Only explicit values are protected
  from ``include-existing``; a channel's explicit value protects its sessions
  from a *global* include-existing, per field.
* A turn reads the pair in **one transaction** (:meth:`ModelScopeService.begin_turn`),
  so a change that lands while a turn starts is either wholly before or wholly
  after it — never a half-new mix.  The pair a process was launched with is
  recorded (``applied_*``); when the next turn resolves to something else, the
  caller restarts Claude with ``--resume`` and the new flags.

Why a restart and not ``/model`` typed into the pane: Claude Code's in-TUI
``/model`` and ``/effort`` *save the choice to the user-level settings file*
("saved as your default for new sessions"), i.e. they would change every other
Claude on the host.  ``--model`` / ``--effort`` on the command line do not.

Everything here is persisted in the instance's own SQLite database, so settings,
protection, saved values and pending applications survive a restart.
"""

from __future__ import annotations

import contextlib
import logging
import re
import unicodedata
from collections.abc import AsyncIterator
from dataclasses import dataclass, field
from typing import TYPE_CHECKING

import aiosqlite

if TYPE_CHECKING:
    from .database.settings_repo import SettingsRepository

logger = logging.getLogger(__name__)

#: ``claude --effort`` accepts exactly these (anything else hard-errors at start-up).
EFFORT_LEVELS: tuple[str, ...] = ("low", "medium", "high", "xhigh", "max")
INHERIT = "inherit"

SCOPE_SESSION = "session"
SCOPE_CHANNEL = "channel"
SCOPE_GLOBAL = "global"
SCOPES: tuple[str, ...] = (SCOPE_SESSION, SCOPE_CHANNEL, SCOPE_GLOBAL)
SRC_ENV = "env"

#: Global values live in the ``settings`` table.  ``claude_model`` is the key
#: ``/model set`` has always written, so existing deployments keep their value.
SETTING_GLOBAL_MODEL = "claude_model"
SETTING_GLOBAL_EFFORT = "claude_effort"

# Same allowlist the old ``/model set`` used: the model string reaches a shell
# command line (``--model {model}``), so it must be a plain alias / model ID.
_MODEL_ID_RE = re.compile(r"^[A-Za-z][A-Za-z0-9._-]{0,63}$")
MODEL_ALIASES: tuple[str, ...] = ("haiku", "sonnet", "opus")

_SRC_LABELS = {
    SCOPE_SESSION: "セッション",
    SCOPE_CHANNEL: "チャンネル",
    SCOPE_GLOBAL: "グローバル",
}


class ModelScopeError(ValueError):
    """A request that cannot be honoured; nothing was changed.  The text is user-facing."""


def is_valid_model_id(model: str) -> bool:
    return bool(_MODEL_ID_RE.fullmatch(model))


def effort_supported(model: str | None) -> bool:
    """False for models Claude Code has no effort setting for (Haiku tier)."""
    return not (model and "haiku" in model.lower())


def effort_for_launch(model: str | None, effort: str | None) -> str | None:
    """The ``--effort`` value to pass, or ``None`` to leave the CLI default."""
    if effort is None or effort not in EFFORT_LEVELS or not effort_supported(model):
        return None
    return effort


@dataclass(frozen=True)
class StartupEnv:
    """What the process read at start-up (the ``.env`` row of the display).

    Frozen at construction: a later edit to ``.env`` on disk is *not* reflected,
    because it has not been applied.  ``*_source`` says where the value came from
    (``.env`` / 環境変数 / 組み込み既定 / 未設定) so the row never claims a file it
    did not come from.  No secret is ever stored here — only a model alias and an
    effort level.
    """

    model: str | None = "sonnet"
    effort: str | None = None
    model_source: str = "起動設定"
    effort_source: str = "未設定"


@dataclass(frozen=True)
class Resolved:
    """One effective model/effort pair with the layer each field came from."""

    model: str | None
    model_src: str
    effort: str | None
    effort_src: str


@dataclass(frozen=True)
class TurnSettings:
    """What a turn starts with, decided once (:meth:`ModelScopeService.begin_turn`)."""

    model: str | None
    effort: str | None
    model_src: str
    effort_src: str
    #: The pair the Claude process was last launched with, or ``None`` if unknown
    #: (first turn, or a session from before this feature).
    previously_applied: tuple[str | None, str | None] | None = None

    @property
    def needs_restart(self) -> bool:
        """The running Claude (if any) carries a different pair than this turn needs."""
        return self.previously_applied is not None and self.previously_applied != (
            self.model,
            self.effort,
        )


@dataclass
class FieldOutcome:
    """What one field's change did (reported back to the user)."""

    name: str  # "model" | "effort"
    action: str  # "set" | "inherit"
    value: str | None = None
    applied: int = 0  # existing sessions whose saved value changed
    unchanged: int = 0  # existing sessions already holding the value
    kept_session: int = 0  # protected by a session-explicit value
    kept_channel: int = 0  # protected by a channel-explicit value (global change only)


@dataclass
class ChangeResult:
    scope: str
    fields: list[FieldOutcome] = field(default_factory=list)
    include_existing: bool = False


@dataclass(frozen=True)
class ScopeView:
    """Everything the display needs, read in one go."""

    in_thread: bool
    session_explicit: tuple[str | None, str | None] = (None, None)
    session_saved: Resolved | None = None
    applied: tuple[str | None, str | None] | None = None
    channel: tuple[str | None, str | None] = (None, None)
    global_: tuple[str | None, str | None] = (None, None)
    env: StartupEnv = field(default_factory=StartupEnv)
    #: What a *new* session in this channel gets right now.
    new_session: Resolved = field(default_factory=lambda: Resolved(None, "", None, ""))


def _resolve_field(
    session: str | None,
    channel: str | None,
    global_: str | None,
    env: str | None,
) -> tuple[str | None, str]:
    if session is not None:
        return session, SCOPE_SESSION
    if channel is not None:
        return channel, SCOPE_CHANNEL
    if global_ is not None:
        return global_, SCOPE_GLOBAL
    return env, SRC_ENV


def _valid_or_none(value: str | None, *, effort: bool) -> str | None:
    """A stored value that fails today's validation is treated as unset, not trusted."""
    if value is None or value == "":
        return None
    if effort:
        return value if value in EFFORT_LEVELS else None
    return value if is_valid_model_id(value) else None


class ModelScopeService:
    """Reads and writes the model/effort layers in the instance database."""

    def __init__(self, db_path: str, env: StartupEnv | None = None) -> None:
        self.db_path = db_path
        self.env = env or StartupEnv()

    # ── plumbing ────────────────────────────────────────────────────────────

    @contextlib.asynccontextmanager
    async def _tx(self) -> AsyncIterator[aiosqlite.Connection]:
        """One write transaction (``BEGIN IMMEDIATE``): all of it or none of it.

        ``IMMEDIATE`` takes the write lock up front, which is what serialises a
        settings change against a turn reading its pair.
        """
        async with aiosqlite.connect(self.db_path, isolation_level=None) as db:
            db.row_factory = aiosqlite.Row
            await db.execute("PRAGMA busy_timeout = 10000")
            await db.execute("BEGIN IMMEDIATE")
            try:
                yield db
            except BaseException:
                await db.execute("ROLLBACK")
                raise
            else:
                await db.execute("COMMIT")

    @staticmethod
    async def _global(db: aiosqlite.Connection) -> tuple[str | None, str | None]:
        cur = await db.execute(
            "SELECT key, value FROM settings WHERE key IN (?, ?)",
            (SETTING_GLOBAL_MODEL, SETTING_GLOBAL_EFFORT),
        )
        got = {r["key"]: r["value"] for r in await cur.fetchall()}
        return (
            _valid_or_none(got.get(SETTING_GLOBAL_MODEL), effort=False),
            _valid_or_none(got.get(SETTING_GLOBAL_EFFORT), effort=True),
        )

    @staticmethod
    async def _channel(
        db: aiosqlite.Connection, channel_id: int | None
    ) -> tuple[str | None, str | None]:
        if channel_id is None:
            return (None, None)
        cur = await db.execute(
            "SELECT model, effort FROM channel_model_settings WHERE channel_id = ?",
            (channel_id,),
        )
        row = await cur.fetchone()
        if row is None:
            return (None, None)
        return (
            _valid_or_none(row["model"], effort=False),
            _valid_or_none(row["effort"], effort=True),
        )

    def _resolve(
        self,
        session: tuple[str | None, str | None],
        channel: tuple[str | None, str | None],
        global_: tuple[str | None, str | None],
    ) -> Resolved:
        env_model = _valid_or_none(self.env.model, effort=False)
        env_effort = _valid_or_none(self.env.effort, effort=True)
        model, model_src = _resolve_field(session[0], channel[0], global_[0], env_model)
        effort, effort_src = _resolve_field(session[1], channel[1], global_[1], env_effort)
        return Resolved(model, model_src, effort, effort_src)

    # ── reads ───────────────────────────────────────────────────────────────

    async def view(self, *, channel_id: int | None, thread_id: int | None = None) -> ScopeView:
        """Read-only: never writes, so a bare ``/model`` cannot change anything."""
        async with aiosqlite.connect(self.db_path) as db:
            db.row_factory = aiosqlite.Row
            global_ = await self._global(db)
            channel = await self._channel(db, channel_id)
            new = self._resolve((None, None), channel, global_)
            if thread_id is None:
                return ScopeView(
                    in_thread=False, channel=channel, global_=global_, env=self.env, new_session=new
                )
            cur = await db.execute(
                "SELECT * FROM session_model_settings WHERE thread_id = ?", (thread_id,)
            )
            row = await cur.fetchone()
            if row is None:
                # Not started yet: it will be created from today's layers.
                return ScopeView(
                    in_thread=True,
                    channel=channel,
                    global_=global_,
                    env=self.env,
                    new_session=new,
                    session_saved=None,
                )
            applied = (
                (row["applied_model"], row["applied_effort"])
                if row["applied_model"] is not None
                else None
            )
            saved = Resolved(row["model"], row["model_src"], row["effort"], row["effort_src"])
            return ScopeView(
                in_thread=True,
                session_explicit=(row["explicit_model"], row["explicit_effort"]),
                session_saved=saved,
                applied=applied,
                channel=channel,
                global_=global_,
                env=self.env,
                new_session=new,
            )

    async def resolve_new(self, channel_id: int | None) -> Resolved:
        return (await self.view(channel_id=channel_id)).new_session

    # ── the turn ────────────────────────────────────────────────────────────

    async def begin_turn(self, thread_id: int, channel_id: int | None) -> TurnSettings:
        """Fix the model/effort pair for the turn that is starting — once, atomically.

        Creates the session's row on the first turn (saving effective values and
        origin), and records the pair as the one the process is launched with.
        """
        async with self._tx() as db:
            cur = await db.execute(
                "SELECT * FROM session_model_settings WHERE thread_id = ?", (thread_id,)
            )
            row = await cur.fetchone()
            if row is None:
                res = self._resolve(
                    (None, None),
                    await self._channel(db, channel_id),
                    await self._global(db),
                )
                await db.execute(
                    "INSERT INTO session_model_settings "
                    "(thread_id, channel_id, model, model_src, effort, effort_src, "
                    " applied_model, applied_effort) VALUES (?,?,?,?,?,?,?,?)",
                    (
                        thread_id,
                        channel_id,
                        res.model,
                        res.model_src,
                        res.effort,
                        res.effort_src,
                        res.model,
                        res.effort,
                    ),
                )
                return TurnSettings(res.model, res.effort, res.model_src, res.effort_src, None)

            previous = (
                (row["applied_model"], row["applied_effort"])
                if row["applied_model"] is not None
                else None
            )
            await db.execute(
                "UPDATE session_model_settings SET applied_model = ?, applied_effort = ?, "
                "channel_id = COALESCE(?, channel_id), updated_at = datetime('now','localtime') "
                "WHERE thread_id = ?",
                (row["model"], row["effort"], channel_id, thread_id),
            )
            return TurnSettings(
                row["model"], row["effort"], row["model_src"], row["effort_src"], previous
            )

    # ── changes ─────────────────────────────────────────────────────────────

    def validate(
        self,
        *,
        scope: str,
        model: str | None,
        effort: str | None,
        include_existing: bool,
        in_thread: bool,
    ) -> None:
        """Every rule that does not need the database.  Raises before anything is written."""
        if scope not in SCOPES:
            raise ModelScopeError(
                f"scope `{scope}` は使えません。`session` / `channel` / `global` のどれかです。"
            )
        if scope == SCOPE_SESSION and not in_thread:
            raise ModelScopeError(
                "`scope:session` はスレッドの中でだけ使えます。チャンネル直下では "
                "`scope:channel` か `scope:global` を指定してください。"
            )
        if include_existing and scope == SCOPE_SESSION:
            raise ModelScopeError(
                "`include-existing` は `channel` / `global` の変更にだけ使えます。"
                "セッションの変更は、そのセッション自身にだけ効きます。"
            )
        if include_existing and model is None and effort is None:
            raise ModelScopeError(
                "`include-existing` だけでは何も変わりません。model か effort も指定してください。"
            )
        if model is not None and model != INHERIT and not is_valid_model_id(model):
            raise ModelScopeError(
                f"モデル `{model}` は使えません。エイリアス（{'/'.join(MODEL_ALIASES)}）か "
                "`claude-fable-5` のようなモデル ID（英数字と `.` `-` `_`、空白なし）を"
                "指定してください。"
            )
        if effort is not None and effort != INHERIT and effort not in EFFORT_LEVELS:
            raise ModelScopeError(
                f"effort `{effort}` は使えません。{' / '.join(EFFORT_LEVELS)} か `inherit` です。"
            )
        if (
            model is not None
            and model != INHERIT
            and effort not in (None, INHERIT)
            and not effort_supported(model)
        ):
            raise ModelScopeError(
                f"`{model}` は effort に対応していません。"
                "effort を外すか、別のモデルを指定してください。"
            )

    async def change(
        self,
        *,
        scope: str,
        model: str | None = None,
        effort: str | None = None,
        include_existing: bool = False,
        thread_id: int | None = None,
        channel_id: int | None = None,
        channel_map: dict[int, int | None] | None = None,
    ) -> ChangeResult:
        """Apply one change as a single transaction; a rejected change writes nothing.

        ``model`` / ``effort``: ``None`` leave alone, ``"inherit"`` clear the
        layer's explicit value, anything else set it.  ``channel_map`` says which
        channel each session from before this feature (no row yet, see
        :meth:`legacy_thread_ids`) belongs to; looked up by the caller *before*
        the transaction so no network call is made while the write lock is held.
        """
        self.validate(
            scope=scope,
            model=model,
            effort=effort,
            include_existing=include_existing,
            in_thread=thread_id is not None,
        )
        if scope in (SCOPE_SESSION,) and thread_id is None:
            raise ModelScopeError("セッションが特定できません。")
        if scope == SCOPE_CHANNEL and channel_id is None:
            raise ModelScopeError("チャンネルが特定できません。")

        result = ChangeResult(scope=scope, include_existing=include_existing)
        requested = [("model", model), ("effort", effort)]
        for name, value in requested:
            if value is not None:
                result.fields.append(
                    FieldOutcome(
                        name,
                        "inherit" if value == INHERIT else "set",
                        None if value == INHERIT else value,
                    )
                )

        async with self._tx() as db:
            if scope == SCOPE_SESSION:
                await self._change_session(db, result, thread_id, channel_id, model, effort)
            else:
                # Sessions from before this feature have no row.  Give them one
                # *first*, from the layers as they were before this change (which
                # is what their process was launched with), so the change below
                # neither silently drags them along nor is invisible to them.
                await self._materialize_legacy(db, channel_map or {})
                if scope == SCOPE_CHANNEL:
                    await self._write_channel(db, channel_id, model, effort)
                else:
                    await self._write_global(db, model, effort)
                if include_existing:
                    await self._reflect(db, result, scope, channel_id)
        return result

    async def _change_session(
        self,
        db: aiosqlite.Connection,
        result: ChangeResult,
        thread_id: int | None,
        channel_id: int | None,
        model: str | None,
        effort: str | None,
    ) -> None:
        assert thread_id is not None
        channel = await self._channel(db, channel_id)
        global_ = await self._global(db)
        cur = await db.execute(
            "SELECT * FROM session_model_settings WHERE thread_id = ?", (thread_id,)
        )
        row = await cur.fetchone()
        if row is None:
            # A session that has not had its first turn: nothing to be launched
            # yet, but the choice must stick.  Created from today's layers.
            res = self._resolve((None, None), channel, global_)
            await db.execute(
                "INSERT INTO session_model_settings "
                "(thread_id, channel_id, model, model_src, effort, effort_src) "
                "VALUES (?,?,?,?,?,?)",
                (thread_id, channel_id, res.model, res.model_src, res.effort, res.effort_src),
            )
            cur = await db.execute(
                "SELECT * FROM session_model_settings WHERE thread_id = ?", (thread_id,)
            )
            row = await cur.fetchone()
        assert row is not None
        exp_model, exp_effort = row["explicit_model"], row["explicit_effort"]
        if model is not None:
            exp_model = None if model == INHERIT else model
        if effort is not None:
            exp_effort = None if effort == INHERIT else effort
        # Whatever pair the session ends up with must be launchable (a model with
        # no effort setting cannot carry an explicit effort).
        resolved = self._resolve((exp_model, exp_effort), channel, global_)
        if (
            model not in (None, INHERIT)
            and exp_effort is not None
            and not effort_supported(resolved.model)
        ):
            raise ModelScopeError(
                f"`{resolved.model}` は effort に対応していません。"
                "このセッションに effort の指定が残っています。`effort:inherit` で外してください。"
            )
        # Fields not touched keep their *saved* value (a stored inherited value is
        # not re-resolved); fields touched take the new explicit value, or — on
        # inherit — today's upper layers.
        new_model, new_model_src = row["model"], row["model_src"]
        new_effort, new_effort_src = row["effort"], row["effort_src"]
        if model is not None:
            new_model, new_model_src = resolved.model, resolved.model_src
        if effort is not None:
            new_effort, new_effort_src = resolved.effort, resolved.effort_src
        await db.execute(
            "UPDATE session_model_settings SET explicit_model=?, explicit_effort=?, "
            "model=?, model_src=?, effort=?, effort_src=?, "
            "channel_id = COALESCE(?, channel_id), updated_at = datetime('now','localtime') "
            "WHERE thread_id = ?",
            (
                exp_model,
                exp_effort,
                new_model,
                new_model_src,
                new_effort,
                new_effort_src,
                channel_id,
                thread_id,
            ),
        )

    async def _write_channel(
        self,
        db: aiosqlite.Connection,
        channel_id: int | None,
        model: str | None,
        effort: str | None,
    ) -> None:
        cur_m, cur_e = await self._channel(db, channel_id)
        new_m = cur_m if model is None else (None if model == INHERIT else model)
        new_e = cur_e if effort is None else (None if effort == INHERIT else effort)
        if new_m is not None and new_e is not None and not effort_supported(new_m):
            raise ModelScopeError(
                f"`{new_m}` は effort に対応していません。このチャンネルに effort の指定が"
                "残っています。`effort:inherit` で外してください。"
            )
        if new_m is None and new_e is None:
            await db.execute(
                "DELETE FROM channel_model_settings WHERE channel_id = ?", (channel_id,)
            )
            return
        await db.execute(
            "INSERT INTO channel_model_settings (channel_id, model, effort) VALUES (?,?,?) "
            "ON CONFLICT(channel_id) DO UPDATE SET model = excluded.model, "
            "effort = excluded.effort, updated_at = datetime('now','localtime')",
            (channel_id, new_m, new_e),
        )

    async def _write_global(
        self, db: aiosqlite.Connection, model: str | None, effort: str | None
    ) -> None:
        cur_m, cur_e = await self._global(db)
        new_m = cur_m if model is None else (None if model == INHERIT else model)
        new_e = cur_e if effort is None else (None if effort == INHERIT else effort)
        if new_m is not None and new_e is not None and not effort_supported(new_m):
            raise ModelScopeError(
                f"`{new_m}` は effort に対応していません。グローバルに effort の指定が"
                "残っています。`effort:inherit` で外してください。"
            )
        for key, value, touched in (
            (SETTING_GLOBAL_MODEL, new_m, model is not None),
            (SETTING_GLOBAL_EFFORT, new_e, effort is not None),
        ):
            if not touched:
                continue
            if value is None:
                await db.execute("DELETE FROM settings WHERE key = ?", (key,))
            else:
                await db.execute(
                    "INSERT INTO settings (key, value) VALUES (?, ?) "
                    "ON CONFLICT(key) DO UPDATE SET value = excluded.value",
                    (key, value),
                )

    async def legacy_thread_ids(self) -> list[int]:
        """Open sessions that have no model row yet (they predate #918)."""
        async with aiosqlite.connect(self.db_path) as db:
            cur = await db.execute(
                "SELECT s.thread_id FROM sessions s WHERE s.closed_at IS NULL "
                "AND s.closed_reason IS NOT 'swept' AND NOT EXISTS "
                "(SELECT 1 FROM session_model_settings m WHERE m.thread_id = s.thread_id)"
            )
            return [r[0] for r in await cur.fetchall()]

    async def _materialize_legacy(
        self, db: aiosqlite.Connection, channel_map: dict[int, int | None]
    ) -> None:
        cur = await db.execute(
            "SELECT s.thread_id FROM sessions s WHERE s.closed_at IS NULL "
            "AND s.closed_reason IS NOT 'swept' AND NOT EXISTS "
            "(SELECT 1 FROM session_model_settings m WHERE m.thread_id = s.thread_id)"
        )
        legacy = [r["thread_id"] for r in await cur.fetchall()]
        global_ = await self._global(db)
        for tid in legacy:
            ch = channel_map.get(tid)
            res = self._resolve((None, None), await self._channel(db, ch), global_)
            # ``applied`` = the best known account of what its process runs, so a
            # reflected change makes the next turn restart it.
            await db.execute(
                "INSERT INTO session_model_settings "
                "(thread_id, channel_id, model, model_src, effort, effort_src, "
                " applied_model, applied_effort) VALUES (?,?,?,?,?,?,?,?)",
                (
                    tid,
                    ch,
                    res.model,
                    res.model_src,
                    res.effort,
                    res.effort_src,
                    res.model,
                    res.effort,
                ),
            )

    async def _reflect(
        self,
        db: aiosqlite.Connection,
        result: ChangeResult,
        scope: str,
        channel_id: int | None,
    ) -> None:
        """Push the (already written) layer into live sessions, per field, with protection."""
        global_ = await self._global(db)

        where = "m.channel_id = ?" if scope == SCOPE_CHANNEL else "1 = 1"
        params: tuple[object, ...] = (channel_id,) if scope == SCOPE_CHANNEL else ()
        cur = await db.execute(
            "SELECT m.* FROM session_model_settings m JOIN sessions s ON s.thread_id = m.thread_id "
            "WHERE s.closed_at IS NULL AND s.closed_reason IS NOT 'swept' AND " + where,
            params,
        )
        rows = await cur.fetchall()
        channel_cache: dict[int | None, tuple[str | None, str | None]] = {}
        for row in rows:
            ch = row["channel_id"]
            if ch not in channel_cache:
                channel_cache[ch] = await self._channel(db, ch)
            channel = channel_cache[ch]
            resolved = self._resolve(
                (row["explicit_model"], row["explicit_effort"]), channel, global_
            )
            updates: dict[str, str | None] = {}
            for out in result.fields:
                idx = 0 if out.name == "model" else 1
                explicit = row["explicit_model" if idx == 0 else "explicit_effort"]
                if explicit is not None:
                    out.kept_session += 1
                    continue
                if scope == SCOPE_GLOBAL and channel[idx] is not None:
                    out.kept_channel += 1
                    continue
                new_val = resolved.model if idx == 0 else resolved.effort
                new_src = resolved.model_src if idx == 0 else resolved.effort_src
                old_val = row["model" if idx == 0 else "effort"]
                if old_val == new_val and row[f"{out.name}_src"] == new_src:
                    out.unchanged += 1
                    continue
                if old_val == new_val:
                    out.unchanged += 1
                else:
                    out.applied += 1
                updates[out.name] = new_val
                updates[f"{out.name}_src"] = new_src
            if updates:
                sets = ", ".join(f"{k} = ?" for k in updates)
                await db.execute(
                    f"UPDATE session_model_settings SET {sets}, "  # noqa: S608 - fixed column names
                    "updated_at = datetime('now','localtime') WHERE thread_id = ?",
                    (*updates.values(), row["thread_id"]),
                )

    # ── display ─────────────────────────────────────────────────────────────

    def source_label(self, src: str, *, effort: bool) -> str:
        if src == SRC_ENV:
            return self.env.effort_source if effort else self.env.model_source
        return _SRC_LABELS.get(src, src)


# ── rendering (pure) ────────────────────────────────────────────────────────


def _w(text: str) -> int:
    return sum(2 if unicodedata.east_asian_width(c) in "WF" else 1 for c in text)


def _pad(text: str, width: int) -> str:
    return text + " " * max(0, width - _w(text))


def _cell(value: str | None) -> str:
    return value if value else "—"


def render_view(view: ScopeView, service: ModelScopeService) -> str:
    """The compact table from the Issue.  Shows layers, never a config file or a secret."""
    env = view.env
    env_model = _valid_or_none(env.model, effort=False)
    env_effort = _valid_or_none(env.effort, effort=True)
    rows: list[tuple[str, str, str]] = []
    if view.in_thread:
        rows.append(
            ("セッション", _cell(view.session_explicit[0]), _cell(view.session_explicit[1]))
        )
    rows.append(("チャンネル", _cell(view.channel[0]), _cell(view.channel[1])))
    rows.append(("グローバル", _cell(view.global_[0]), _cell(view.global_[1])))
    rows.append((".env", _cell(env_model), _cell(env_effort)))
    lw = max(_w(r[0]) for r in rows + [("", "", "")]) + 2
    mw = max(_w(r[1]) for r in rows + [("model", "", "")]) + 2
    lines = [_pad("", lw) + _pad("model", mw) + "effort"]
    lines += [_pad(a, lw) + _pad(b, mw) + c for a, b, c in rows]

    new = view.new_session
    out = ["```text", *lines, "```"]
    if view.in_thread and view.session_saved is not None:
        s = view.session_saved
        out.append(f"このセッション → {_cell(s.model)} / {_cell(s.effort)}")
        out.append(
            f"設定元 → {service.source_label(s.model_src, effort=False)} / "
            f"{service.source_label(s.effort_src, effort=True)}"
        )
        if view.applied is not None and view.applied != (s.model, s.effort):
            out.append(
                f"実行中 → {_cell(view.applied[0])} / {_cell(view.applied[1])}"
                f"（次のターンから {_cell(s.model)} / {_cell(s.effort)}）"
            )
    else:
        label = "このスレッドの開始時" if view.in_thread else "新規に適用"
        out.append(f"{label} → {_cell(new.model)} / {_cell(new.effort)}")
        out.append(
            f"設定元 → {service.source_label(new.model_src, effort=False)} / "
            f"{service.source_label(new.effort_src, effort=True)}"
        )
    notes = []
    if env.model_source != ".env" or env.effort_source not in (".env", "未設定"):
        notes.append(f".env 行の出所: model={env.model_source} / effort={env.effort_source}")
    if notes:
        out.append("※ " + "、".join(notes))
    out.append("-# `.env` 行は起動時に読み込んだ値です（編集しただけでは反映されません）。")
    return "\n".join(out)


def render_result(result: ChangeResult) -> str:
    scope_label = _SRC_LABELS[result.scope]
    lines = [f"✅ モデル設定を更新しました（{scope_label}）"]
    for f in result.fields:
        what = f"{f.name}: " + (
            f"`{f.value}` に変更" if f.action == "set" else "指定を解除（上位の設定を使用）"
        )
        if result.scope == SCOPE_SESSION:
            lines.append(f"- {what} — このセッションの次のターンから")
        elif not result.include_existing:
            lines.append(f"- {what} — 新規セッションのみ（既存セッションは変更なし）")
        else:
            kept = []
            if f.kept_session:
                kept.append(f"個別指定のため維持 {f.kept_session} 件（セッション）")
            if f.kept_channel:
                kept.append(f"個別指定のため維持 {f.kept_channel} 件（チャンネル）")
            tail = f"既存 {f.applied} 件へ反映（次のターンから）"
            if f.unchanged:
                tail += f"、すでに同じ値 {f.unchanged} 件"
            lines.append(
                f"- {what} — 新規セッション + {tail}" + ("、" + "、".join(kept) if kept else "")
            )
    return "\n".join(lines)


def service_for(bot: object, settings_repo: SettingsRepository | None, runner: object | None):
    """The process-wide service, created on first use (zero-config for consumers)."""
    existing = getattr(bot, "_clord_model_scopes", None)
    if isinstance(existing, ModelScopeService):
        return existing
    db_path = getattr(settings_repo, "db_path", None)
    if not isinstance(db_path, str):
        return None

    def _str(name: str, default: str | None) -> str | None:
        value = getattr(runner, name, default) if runner is not None else default
        return value if isinstance(value, str) and value else default

    env = StartupEnv(
        model=_str("model", "sonnet"),
        effort=_str("effort", None),
        model_source=_str("model_source", None) or "起動設定",
        effort_source=_str("effort_source", None) or "未設定",
    )
    svc = ModelScopeService(db_path, env)
    with contextlib.suppress(Exception):
        bot._clord_model_scopes = svc  # type: ignore[attr-defined]
    return svc


async def begin_turn_for(
    bot: object,
    settings_repo: SettingsRepository | None,
    runner: object | None,
    thread_id: int,
    channel_id: int | None,
) -> TurnSettings | None:
    """Fix a turn's pair, or ``None`` when the settings cannot be read (#918).

    ``None`` means "launch with the instance defaults" (``runner.model`` /
    ``runner.effort``) — a settings hiccup must never be the reason a turn does
    not run, the same rule the old global-model lookup followed.
    """
    svc = service_for(bot, settings_repo, runner)
    if svc is None:
        return None
    try:
        return await svc.begin_turn(thread_id, channel_id)
    except Exception:
        logger.warning(
            "model scopes: could not fix the model/effort for thread=%s — using instance defaults",
            thread_id,
            exc_info=True,
        )
        return None
