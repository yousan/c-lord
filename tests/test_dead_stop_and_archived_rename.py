"""#634: two small things that lie to the user.

① A ``⏹ Stop`` button left over from a previous process. Shutdown closes the
   aiohttp session before ``StopView.disable`` can delete its message
   (``views.py:109  StopView.disable: could not delete message — Session is
   closed``, 46 occurrences in production, every one at shutdown), and nothing
   ever cleaned it up afterwards — so a button that looks live sits in the
   thread forever and answers a click with ``This interaction failed``.

② The ``[停止]`` rename being dropped in silence. ``session_close`` sends the
   rename and the archive flag as one PATCH; on a thread that is **already**
   archived Discord rejects the whole PATCH (``400 (50083) Thread is
   archived``) and the retry re-applies the archive flag only. Production,
   2026-08-31, 11 threads:

       [thread=1539552764626083840] close/reopen
       thread.edit(archived=True name='[停止] #587 特商法4項目をコマンド実装')
       failed: 400 Bad Request (error code: 50083): Thread is archived
"""

from __future__ import annotations

from unittest.mock import AsyncMock, MagicMock

import discord
import pytest

from c_lord.database.repository import SessionRecord

# ── ② archived thread must still get its [停止] name ──────────────────────────


class _ArchivedThread:
    """A thread Discord has already archived.

    Mirrors the real API: while ``archived`` is set, a PATCH carrying any other
    field is rejected unless the same PATCH also clears the flag.
    """

    def __init__(self, name: str = "W3 │ #404 認証リファクタ") -> None:
        self.id = 555
        self.parent_id = 999
        self.name = name
        self.archived = True
        self.patches: list[dict] = []

    async def edit(self, **kwargs):
        self.patches.append(dict(kwargs))
        if "name" in kwargs and self.archived and kwargs.get("archived") is not False:
            raise discord.HTTPException(
                MagicMock(status=400),
                {"code": 50083, "message": "Thread is archived"},
            )
        if "archived" in kwargs:
            self.archived = bool(kwargs["archived"])
        if "name" in kwargs:
            self.name = kwargs["name"]
        return self

    async def send(self, *a, **k):
        return MagicMock()


def _record(thread_id: int = 555, *, closed_at: str | None = None) -> SessionRecord:
    return SessionRecord(
        thread_id=thread_id,
        session_id="sess-abc",
        working_dir="/tmp/x",
        model=None,
        origin="discord",
        summary=None,
        created_at="2026-08-18 10:00:00",
        last_used_at="2026-08-18 11:00:00",
        topic="認証リファクタ",
        issue_ref="404",
        closed_at=closed_at,
    )


class TestArchivedThreadStillGetsTheStopMark:
    @pytest.mark.asyncio
    async def test_rename_lands_on_an_already_archived_thread(self) -> None:
        """RED (#634 ②): the [停止] rename was dropped without a trace.

        The auto-stop sweep (#605) archives first, so by the time the marker is
        applied the thread is already archived — which is why this started
        firing the moment #605 landed.
        """
        from c_lord.session_close import apply_closed_name
        from c_lord.thread_name import CLOSED_MARK

        repo = MagicMock()
        repo.get = AsyncMock(return_value=_record())
        thread = _ArchivedThread()

        await apply_closed_name(repo, thread)  # type: ignore[arg-type]

        assert CLOSED_MARK in thread.name, (
            "a thread that is already archived must still get its [停止] marker — "
            "otherwise a stopped thread is indistinguishable from a running one "
            "in the sidebar (#634)"
        )
        assert thread.archived is True, "the thread must end up archived either way"

    @pytest.mark.asyncio
    async def test_an_open_thread_still_renames_in_one_patch(self) -> None:
        """#512's single-PATCH rule must not regress for the normal case."""
        from c_lord.session_close import apply_closed_name

        repo = MagicMock()
        repo.get = AsyncMock(return_value=_record())
        thread = _ArchivedThread()
        thread.archived = False

        await apply_closed_name(repo, thread)  # type: ignore[arg-type]

        assert len(thread.patches) == 1, (
            "an open thread must still spend exactly one PATCH — two would burn "
            "two of the thread's ~2-per-10-minutes rename allowance (#512)"
        )
        assert thread.patches[0].get("archived") is True
        assert "name" in thread.patches[0]


# ── ① dead ⏹ Stop buttons from a previous process ────────────────────────────


class _Msg:
    def __init__(self, *, author_id: int, content: str, components=(), msg_id: int = 1) -> None:
        self.id = msg_id
        self.author = MagicMock()
        self.author.id = author_id
        self.content = content
        self.components = list(components)
        self.deleted = False

    async def delete(self) -> None:
        self.deleted = True


def _thread_with(messages: list[_Msg]) -> MagicMock:
    thread = MagicMock(spec=discord.Thread)
    thread.id = 555

    def history(limit: int = 50, **_kw):
        class _It:
            def __aiter__(self):
                self._i = iter(messages[:limit])
                return self

            async def __anext__(self):
                try:
                    return next(self._i)
                except StopIteration:  # pragma: no cover - iterator protocol
                    raise StopAsyncIteration from None

        return _It()

    thread.history = history
    return thread


class TestDeadStopButtonsAreSweptOnStartup:
    @pytest.mark.asyncio
    async def test_leftover_stop_message_is_deleted(self) -> None:
        """RED (#634 ①): the residue survived every restart.

        ``StopView.disable`` deletes the message when a turn ends normally, but
        at shutdown the aiohttp session is already closed and the delete raises
        — leaving a clickable button wired to a runner that no longer exists.
        """
        from c_lord.discord_ui.views import STOP_MESSAGE_PREFIX
        from c_lord.stale_stop_buttons import sweep_dead_stop_buttons

        stale = _Msg(
            author_id=42,
            content=f"{STOP_MESSAGE_PREFIX} (`w1`)",
            components=[MagicMock()],
            msg_id=1,
        )
        prose = _Msg(author_id=42, content="実装しました。", msg_id=2)
        human = _Msg(author_id=7, content=f"{STOP_MESSAGE_PREFIX} (`w9`)", msg_id=3)

        thread = _thread_with([stale, prose, human])
        bot = MagicMock()
        bot.user = MagicMock()
        bot.user.id = 42
        bot.get_channel = MagicMock(return_value=thread)
        repo = MagicMock()
        repo.list_alive = AsyncMock(return_value=[_record()])

        removed = await sweep_dead_stop_buttons(bot, repo)

        assert removed == 1
        assert stale.deleted is True
        assert prose.deleted is False, "ordinary messages must be left alone"
        assert human.deleted is False, "only this bot's own stop messages are ours to delete"

    @pytest.mark.asyncio
    async def test_a_stop_message_without_buttons_is_left_alone(self) -> None:
        """No components → nothing to click → nothing to clean up."""
        from c_lord.discord_ui.views import STOP_MESSAGE_PREFIX
        from c_lord.stale_stop_buttons import sweep_dead_stop_buttons

        plain = _Msg(author_id=42, content=STOP_MESSAGE_PREFIX, msg_id=1)
        thread = _thread_with([plain])
        bot = MagicMock()
        bot.user = MagicMock()
        bot.user.id = 42
        bot.get_channel = MagicMock(return_value=thread)
        repo = MagicMock()
        repo.list_alive = AsyncMock(return_value=[_record()])

        assert await sweep_dead_stop_buttons(bot, repo) == 0
        assert plain.deleted is False

    @pytest.mark.asyncio
    async def test_one_unreadable_thread_does_not_stop_the_sweep(self) -> None:
        """A thread we cannot read must not strand the residue in every other."""
        from c_lord.discord_ui.views import STOP_MESSAGE_PREFIX
        from c_lord.stale_stop_buttons import sweep_dead_stop_buttons

        stale = _Msg(author_id=42, content=STOP_MESSAGE_PREFIX, components=[MagicMock()], msg_id=1)
        good = _thread_with([stale])
        bot = MagicMock()
        bot.user = MagicMock()
        bot.user.id = 42
        bot.get_channel = MagicMock(side_effect=[None, good])
        bot.fetch_channel = AsyncMock(side_effect=RuntimeError("gone"))
        repo = MagicMock()
        repo.list_alive = AsyncMock(return_value=[_record(1), _record(2)])

        assert await sweep_dead_stop_buttons(bot, repo) == 1
        assert stale.deleted is True


class TestStopMessagePrefixIsShared:
    def test_the_send_sites_use_the_same_constant(self) -> None:
        """The sweep matches on the prefix, so it must not be duplicated text."""
        from pathlib import Path

        from c_lord.discord_ui.views import STOP_MESSAGE_PREFIX

        root = Path(__file__).resolve().parent.parent / "c_lord"
        literal = f'"{STOP_MESSAGE_PREFIX}'
        offenders = [
            p.relative_to(root).as_posix()
            for p in root.rglob("*.py")
            if literal in p.read_text() and p.name != "views.py"
        ]
        assert offenders == [], (
            f"these files hard-code the stop-message text instead of importing "
            f"STOP_MESSAGE_PREFIX: {offenders} — the startup sweep matches on it, "
            f"so a copy that drifts becomes residue nobody cleans up (#634)"
        )
