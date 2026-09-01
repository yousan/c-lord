"""``/clord`` inside a thread: continue, recover, or refuse — #551.

``/clord`` used to check one thing before running in a thread: is *some*
repository reachable from here. Every thread under a ``/clord-init`` channel
answers yes, human conversations included, so one command cloned a session dir,
opened a tmux window and wrote the ``sessions`` row — and from that moment
``on_message`` sent every message in that thread to Claude. It happened on
yousan's instance. Undoing it needed ``/close-workspace``, which the people
affected had no reason to know.

The first cut of this fix refused any thread without a ``sessions`` row, and was
wrong: #554 deletes that row after 30 days, so it would also have refused every
c-lord thread that merely went quiet for a month — including ``W3 │ Qiita``,
whose checkout and half-written article are still on disk. That thread would
have become permanently unreachable, and the notice #545 posts would have been
pointing at the very command now rejecting it.

So the verdict has three branches, and the middle one is the point:

* **row exists** → continue, exactly as before.
* **no row, but this was c-lord's thread** → offer to *reconnect* (#538). Not a
  takeover: it reattaches to what is already on disk.
* **no trace at all** → refuse, and change nothing.

The middle test is :mod:`c_lord.thread_origin`, shared with #556 — one spelling
of "is this ours", because two would drift and that drift is #538.
"""

from __future__ import annotations

from unittest.mock import AsyncMock, MagicMock

import discord
import pytest

from c_lord.cogs.channel_repo import ChannelRepoCog
from c_lord.database.channel_repo import ChannelRepository
from c_lord.database.repository import SessionRecord
from c_lord.database.thread_repo import ThreadRepository

CHANNEL_ID = 500
THREAD_ID = 601
BOT_ID = 777
REPO_A = "https://github.com/yousan/c-lord"
REPO_B = "git@github.com:yousan/dotclaude.git"


@pytest.fixture
async def channel_repo(tmp_path) -> ChannelRepository:
    r = ChannelRepository(str(tmp_path / "channel.db"))
    await r.init_db()
    # The channel is bound — which is all it used to take.
    await r.save(channel_id=CHANNEL_ID, source_repo=REPO_A)
    return r


@pytest.fixture
async def thread_repo(tmp_path) -> ThreadRepository:
    r = ThreadRepository(str(tmp_path / "thread.db"))
    await r.init_db()
    return r


@pytest.fixture
def channel_cog(channel_repo, thread_repo, tmp_path) -> ChannelRepoCog:
    bot = MagicMock()
    bot.get_cog = MagicMock(return_value=None)
    bot.session_repo = MagicMock()
    bot.session_repo.get = AsyncMock(return_value=None)
    return ChannelRepoCog(
        bot,
        repo=channel_repo,
        thread_repo=thread_repo,
        allowed_user_ids=None,
        session_dir_base=str(tmp_path / "sessions"),
    )


def _record(*, closed_at: str | None = None) -> SessionRecord:
    return SessionRecord(
        thread_id=THREAD_ID,
        session_id="sess-abc",
        working_dir="/tmp/x",
        model=None,
        origin="discord",
        summary=None,
        created_at="2026-08-20 10:00:00",
        last_used_at="2026-08-20 11:00:00",
        closed_at=closed_at,
    )


def _make_cog(channel_cog, *, record=None, tmp_path=None):
    from c_lord.cogs.claude_chat import ClaudeChatCog

    bot = MagicMock()
    bot.channel_id = CHANNEL_ID
    bot.settings_repo = None
    bot.user = MagicMock()
    bot.user.id = BOT_ID
    bot.get_cog = MagicMock(return_value=channel_cog)
    repo = MagicMock()
    repo.get = AsyncMock(return_value=record)
    repo.save = AsyncMock()
    runner = MagicMock()
    runner.clone = MagicMock(return_value=MagicMock())
    cog = ClaudeChatCog(bot=bot, repo=repo, runner=runner)
    cog._run_claude = AsyncMock()  # type: ignore[method-assign]
    cog.spawn_session = AsyncMock()  # type: ignore[method-assign]
    if tmp_path is not None:
        cog._projects_root = tmp_path / "projects"
    return cog


def _thread(*, owner_id: int = 42) -> MagicMock:
    t = MagicMock(spec=discord.Thread)
    t.id = THREAD_ID
    t.parent_id = CHANNEL_ID
    t.owner_id = owner_id
    t.send = AsyncMock(return_value=MagicMock())
    return t


def _said(respond: AsyncMock) -> str:
    return " ".join(str(c.args[0]) for c in respond.await_args_list if c.args)


async def _clord(cog, channel, respond, **kw):
    await cog._clord_impl(
        channel=channel,
        channel_id_fallback=getattr(channel, "id", None),
        user=MagicMock(),
        prompt="やること",
        respond=respond,
        ack=AsyncMock(),
        **kw,
    )


# ── branch 3: a plain conversation thread is refused ─────────────────────────


class TestPlainThreadIsRefused:
    async def test_no_session_is_started(self, channel_cog, tmp_path) -> None:
        """AC1: not a clone, not a tmux window, not a row, not a mirror.
        ``_run_claude`` is the door all four go through."""
        cog = _make_cog(channel_cog, tmp_path=tmp_path)
        thread = _thread()
        respond = AsyncMock()

        await _clord(cog, thread, respond)

        cog._run_claude.assert_not_awaited()
        thread.send.assert_not_awaited()  # not even the seed message

    async def test_the_error_says_why_and_what_to_do_instead(self, channel_cog, tmp_path) -> None:
        """AC3: a refusal with no next step is how people end up editing the DB."""
        cog = _make_cog(channel_cog, tmp_path=tmp_path)
        respond = AsyncMock()

        await _clord(cog, _thread(), respond)

        said = _said(respond)
        assert "c-lord のスレッドではない" in said, said
        assert "チャンネルで" in said, said


# ── branch 2: a c-lord thread that lost its row is offered recovery ──────────


class TestFormerClordThreadIsOfferedRecovery:
    async def test_it_is_not_taken_over(self, channel_cog, tmp_path) -> None:
        """AC1b: the #554 victim must not be silently re-created as a new
        session — that would discard the checkout it still has."""
        cog = _make_cog(channel_cog, tmp_path=tmp_path)
        (tmp_path / "sessions" / str(CHANNEL_ID) / str(THREAD_ID)).mkdir(parents=True)
        thread = _thread()
        respond = AsyncMock()

        await _clord(cog, thread, respond)

        cog._run_claude.assert_not_awaited()

    async def test_it_is_offered_the_reconnect_button(self, channel_cog, tmp_path) -> None:
        """AC1b/AC3: 'refused' is the wrong answer here — the work is right there."""
        cog = _make_cog(channel_cog, tmp_path=tmp_path)
        (tmp_path / "sessions" / str(CHANNEL_ID) / str(THREAD_ID)).mkdir(parents=True)
        thread = _thread()

        await _clord(cog, thread, AsyncMock())

        thread.send.assert_awaited_once()
        assert thread.send.await_args.kwargs.get("view") is not None
        assert "再接続" in str(thread.send.await_args.args[0])

    async def test_a_bot_created_thread_counts_even_with_nothing_on_disk(
        self, channel_cog, tmp_path
    ) -> None:
        """owner_id is the widest signal — bindings are 21 against 243 rows."""
        cog = _make_cog(channel_cog, tmp_path=tmp_path)
        respond = AsyncMock()

        await _clord(cog, _thread(owner_id=BOT_ID), respond)

        cog._run_claude.assert_not_awaited()
        assert "c-lord のスレッドではない" not in _said(respond), _said(respond)


# ── branch 1 / AC5: live threads are untouched ───────────────────────────────


class TestExistingClordThreadsKeepWorking:
    async def test_open_session_still_runs(self, channel_cog, tmp_path) -> None:
        """AC5: the four human-created threads with rows on yousan's instance
        (``W5 │ かたぼの管理`` and friends) must not notice this change."""
        cog = _make_cog(channel_cog, record=_record(), tmp_path=tmp_path)

        await _clord(cog, _thread(), AsyncMock())

        cog._run_claude.assert_awaited_once()
        assert cog._run_claude.await_args.kwargs["session_id"] == "sess-abc"

    async def test_closed_session_still_runs(self, channel_cog, tmp_path) -> None:
        cog = _make_cog(
            channel_cog, record=_record(closed_at="2026-08-20 12:00:00"), tmp_path=tmp_path
        )

        await _clord(cog, _thread(), AsyncMock())

        cog._run_claude.assert_awaited_once()


# ── AC4: the text twin closes with the same key ──────────────────────────────


class TestTextTwin:
    async def test_bang_clord_is_stopped_too(self, channel_cog, tmp_path) -> None:
        """AC4: ``!clord`` is reachable from webhooks, so a slash-only gate would
        leave the hole open to exactly the automated caller."""
        cog = _make_cog(channel_cog, tmp_path=tmp_path)
        ctx = MagicMock()
        ctx.channel = _thread()
        ctx.author = MagicMock()
        ctx.message = MagicMock(spec=discord.Message)
        ctx.message.author = ctx.author
        ctx.message.author.bot = False
        ctx.message.author.id = 7
        ctx.message.webhook_id = None
        ctx.send = AsyncMock()

        await cog.clord_text.callback(cog, ctx, prompt="やること")

        cog._run_claude.assert_not_awaited()
        said = " ".join(str(c.args[0]) for c in ctx.send.await_args_list if c.args)
        assert "c-lord のスレッドではない" in said, said


# ── AC6: channels are not affected ───────────────────────────────────────────


class TestChannelInvocationIsUnaffected:
    async def test_clord_in_a_channel_still_spawns(self, channel_cog, tmp_path) -> None:
        cog = _make_cog(channel_cog, tmp_path=tmp_path)
        channel = MagicMock(spec=discord.TextChannel)
        channel.id = CHANNEL_ID

        await _clord(cog, channel, AsyncMock())

        cog.spawn_session.assert_awaited_once()

    async def test_clord_repo_in_a_channel_still_spawns(self, channel_cog, tmp_path) -> None:
        cog = _make_cog(channel_cog, tmp_path=tmp_path)
        channel = MagicMock(spec=discord.TextChannel)
        channel.id = CHANNEL_ID

        await _clord(cog, channel, AsyncMock(), repo=REPO_B)

        cog.spawn_session.assert_awaited_once()


# ── AC2: /clord-thread-init binds only threads that are already ours ─────────


class TestThreadInit:
    async def test_plain_thread_is_not_bound(self, channel_cog, thread_repo) -> None:
        """AC2: binding a human thread was step one of the same takeover."""
        respond = AsyncMock()

        await channel_cog._clord_thread_init_impl(
            thread_id=THREAD_ID,
            channel=_thread(),
            client=MagicMock(),
            user=MagicMock(),
            repo=REPO_B,
            remove=False,
            respond=respond,
        )

        assert await thread_repo.get(THREAD_ID) is None, "binding must not be written"
        assert "c-lord のスレッドではない" in _said(respond), _said(respond)

    async def test_existing_clord_thread_can_still_rebind(self, channel_cog, thread_repo) -> None:
        """AC5: changing an existing session's repo is what the command is for."""
        channel_cog.bot.session_repo.get = AsyncMock(return_value=_record())
        respond = AsyncMock()

        await channel_cog._clord_thread_init_impl(
            thread_id=THREAD_ID,
            channel=_thread(),
            client=MagicMock(),
            user=MagicMock(),
            repo=REPO_B,
            remove=False,
            respond=respond,
        )

        binding = await thread_repo.get(THREAD_ID)
        assert binding is not None
        assert binding["source_repo"] == REPO_B


# ── AC9: the #545 guidance must not point at a command that now refuses ──────


class TestGuidanceMatchesBehaviour:
    def test_the_untracked_notice_does_not_send_people_to_clord_in_the_thread(self) -> None:
        """#545 told the reader to run ``/clord`` right here. This PR makes that
        a refusal — so the notice has to change with it, or c-lord instructs
        people to run the command it rejects."""
        from c_lord.session_resume import UNTRACKED_NOTICE, stopped_hint, ThreadResume

        for text in (UNTRACKED_NOTICE, stopped_hint(ThreadResume.UNTRACKED)):
            assert "このスレッドで新しく始める" not in text, text
            assert "チャンネルで" in text, text
