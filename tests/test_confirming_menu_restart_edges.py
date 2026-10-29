"""#786 / #911: which ⏳ 確認中 menus a restarted process picks up — and what it skips.

``tests/test_confirming_menu_restart.py`` pins the happy path (a ⏳ menu turns
✅ / ⚠️ once the transcript records the result). This file pins the
*selection* and *isolation* rules ``resume_confirming_menus`` promises:

* "Never raises": one guild whose active threads cannot be listed, or one
  thread whose history cannot be read, logs "its ⏳ menus stay until the next
  start" and the others are still picked up — production restarts up to 6
  times a day (#786), so a menu skipped now is retried soon.
* only threads active within the watch lifetime (``_LATE_CONFIRM_TIMEOUT``),
  only threads with a session row (the row's ``working_dir`` names the
  transcript), only the bot's own ⏳ menus, and history is read back only as
  far as the watch lifetime.
* #911: a menu the CLI drew itself writes no ask; the pane is the only thing
  that can say it closed. A pane that cannot be resolved must not lose the
  menu — the watch starts without one.
"""

from __future__ import annotations

import datetime as dt
from unittest.mock import AsyncMock, MagicMock

import discord
import pytest

from c_lord import ask_confirm_recovery
from c_lord.ask_confirm_recovery import resume_confirming_menus
from c_lord.database.repository import SessionRecord
from c_lord.discord_ui import ask_handler
from c_lord.discord_ui.embeds import ask_confirming_embed

UTC = dt.timezone.utc  # noqa: UP017 — dt.UTC is 3.11+, we support 3.10
BOT_ID = 4242
STARTED = dt.datetime(2026, 9, 28, 14, 30, tzinfo=UTC)
LIFETIME = dt.timedelta(seconds=ask_handler._LATE_CONFIRM_TIMEOUT)
Q = "#686 罫線はどうしますか？"


def _message(at: dt.datetime, *, author_id: int = BOT_ID, menu: bool = True) -> MagicMock:
    msg = MagicMock(spec=discord.Message)
    msg.id = discord.utils.time_snowflake(at)
    msg.created_at = at
    msg.author = MagicMock()
    msg.author.id = author_id
    msg.embeds = [ask_confirming_embed(Q, "罫線", ["長すぎたら畳む"])] if menu else []
    msg.edit = AsyncMock()
    return msg


def _thread(thread_id: int, *messages: MagicMock, last: int | None = -1) -> MagicMock:
    thread = MagicMock(spec=discord.Thread)
    thread.id = thread_id
    thread.last_message_id = (messages[0].id if messages else None) if last == -1 else last

    def _history(**_kwargs: object) -> object:
        async def _gen():  # type: ignore[no-untyped-def]
            for m in messages:
                yield m

        return _gen()

    thread.history = MagicMock(side_effect=_history)
    return thread


def _guild(*threads: MagicMock) -> MagicMock:
    guild = MagicMock()
    guild.id = 1
    guild.active_threads = AsyncMock(return_value=list(threads))
    return guild


def _bot(*guilds: MagicMock) -> MagicMock:
    bot = MagicMock()
    bot.user = MagicMock()
    bot.user.id = BOT_ID
    bot.guilds = list(guilds)
    return bot


def _repo(*with_rows: int) -> MagicMock:
    def _get(tid: int) -> SessionRecord | None:
        if tid not in with_rows:
            return None
        return SessionRecord(
            thread_id=tid,
            session_id="s",
            working_dir=f"/work/thread-{tid}",
            model=None,
            origin="discord",
            summary=None,
            created_at="2026-09-28 10:00:00",
            last_used_at="2026-09-28 10:00:00",
        )

    repo = MagicMock()
    repo.get = AsyncMock(side_effect=_get)
    return repo


@pytest.fixture
def watched(monkeypatch: pytest.MonkeyPatch) -> list[tuple[int, int, object]]:
    """Record (thread_id, message_id, pane) instead of starting a real watcher."""
    seen: list[tuple[int, int, object]] = []

    async def _record(message, menu, project_dir, thread_id, pane=None) -> None:  # type: ignore[no-untyped-def]
        seen.append((thread_id, message.id, pane))

    monkeypatch.setattr(ask_confirm_recovery, "_resume_one", _record)
    return seen


async def _start(bot: MagicMock, repo: MagicMock, **kwargs: object) -> int:
    kwargs.setdefault("runner_factory", AsyncMock(return_value=None))
    return await resume_confirming_menus(bot, repo, started_at=STARTED, **kwargs)  # type: ignore[arg-type]


RECENT = STARTED - dt.timedelta(minutes=20)


# ── isolation: one failure does not cost the other menus ────────────────────


@pytest.mark.asyncio
async def test_a_guild_whose_threads_cannot_be_listed_does_not_stop_the_others(
    watched: list, caplog: pytest.LogCaptureFixture
) -> None:
    broken = MagicMock()
    broken.id = 2
    broken.active_threads = AsyncMock(side_effect=OSError("Temporary failure in name resolution"))
    ok = _guild(_thread(501, _message(RECENT)))
    with caplog.at_level("INFO", logger="c_lord.ask_confirm_recovery"):
        assert await _start(_bot(broken, ok), _repo(501)) == 1
    assert [t for t, _, _ in watched] == [501]
    assert any("could not list active threads" in r.getMessage() for r in caplog.records)


@pytest.mark.asyncio
async def test_a_thread_that_cannot_be_read_does_not_stop_the_others(
    watched: list, caplog: pytest.LogCaptureFixture
) -> None:
    unreadable = _thread(501, _message(RECENT))
    unreadable.history = MagicMock(
        side_effect=discord.Forbidden(MagicMock(status=403), "Missing Access")
    )
    readable = _thread(502, _message(RECENT))
    with caplog.at_level("INFO", logger="c_lord.ask_confirm_recovery"):
        assert await _start(_bot(_guild(unreadable, readable)), _repo(501, 502)) == 1
    assert [t for t, _, _ in watched] == [502]
    assert any("could not read this thread" in r.getMessage() for r in caplog.records)


@pytest.mark.asyncio
async def test_no_bot_user_yet_means_nothing_to_resume(watched: list) -> None:
    bot = _bot(_guild(_thread(501, _message(RECENT))))
    bot.user = None
    assert await _start(bot, _repo(501)) == 0
    assert watched == []


# ── selection: which threads and messages are read ──────────────────────────


@pytest.mark.asyncio
async def test_threads_idle_longer_than_the_watch_lifetime_are_not_read(watched: list) -> None:
    long_ago = STARTED - LIFETIME - dt.timedelta(minutes=1)
    stale = _thread(501, _message(long_ago))
    never = _thread(502, last=None)
    assert await _start(_bot(_guild(stale, never)), _repo(501, 502)) == 0
    stale.history.assert_not_called()
    never.history.assert_not_called()


@pytest.mark.asyncio
async def test_a_thread_without_a_session_row_is_not_read(watched: list) -> None:
    """No row → no ``working_dir`` → no transcript to watch."""
    thread = _thread(501, _message(RECENT))
    assert await _start(_bot(_guild(thread)), _repo()) == 0
    thread.history.assert_not_called()


@pytest.mark.asyncio
async def test_only_the_bots_own_confirming_menus_are_picked_up(watched: list) -> None:
    own_menu = _message(RECENT)
    own_plain = _message(RECENT - dt.timedelta(minutes=1), menu=False)
    someone_elses = _message(RECENT - dt.timedelta(minutes=2), author_id=999)
    thread = _thread(501, own_menu, own_plain, someone_elses)
    assert await _start(_bot(_guild(thread)), _repo(501)) == 1
    assert [m for _, m, _ in watched] == [own_menu.id]


@pytest.mark.asyncio
async def test_history_is_read_back_only_as_far_as_the_watch_lifetime(watched: list) -> None:
    """A ⏳ older than the lifetime is past its watch — and the scan stops there."""
    recent = _message(RECENT)
    too_old = _message(STARTED - LIFETIME - dt.timedelta(minutes=1))
    after_it = _message(STARTED - LIFETIME - dt.timedelta(minutes=2))
    thread = _thread(501, recent, too_old, after_it)
    assert await _start(_bot(_guild(thread)), _repo(501)) == 1
    assert [m for _, m, _ in watched] == [recent.id]


# ── #911: the pane ───────────────────────────────────────────────────────────


@pytest.mark.asyncio
async def test_the_threads_pane_is_handed_to_the_watch(watched: list) -> None:
    pane = object()
    factory = AsyncMock(return_value=pane)
    bot = _bot(_guild(_thread(501, _message(RECENT))))
    assert await _start(bot, _repo(501), runner_factory=factory) == 1
    factory.assert_awaited_once_with(bot, 501)
    assert watched[0][2] is pane


@pytest.mark.asyncio
async def test_a_pane_that_cannot_be_resolved_still_watches_the_menu(watched: list) -> None:
    """#911: without the pane only the transcript can close it — but it is still watched."""
    factory = AsyncMock(side_effect=RuntimeError("tmux server not running"))
    bot = _bot(_guild(_thread(501, _message(RECENT))))
    assert await _start(bot, _repo(501), runner_factory=factory) == 1
    assert watched == [(501, watched[0][1], None)]
