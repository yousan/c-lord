"""ChannelRepoCog: the decision paths ``tests/test_channel_repo.py`` leaves open.

Every scenario here is one an Issue reported (cited per test): which tmux
session a thread is said to live in (#618 / #427), which session names the
menu watchdog treats as ours (#438), who may bind a repo (#551), what the
``/clord-init`` / ``/clord-thread-init`` replies say, and that a URL with a
request glued onto it never reaches the binding table (#923).

Real SQLite in ``tmp_path``; no tmux server is touched (``TmuxSessionManager``
only stores the session name until a method that shells out is called).
"""

from __future__ import annotations

from unittest.mock import AsyncMock, MagicMock

import discord
import pytest

from c_lord.cogs.channel_repo import ChannelRepoCog
from c_lord.database.channel_repo import ChannelRepository
from c_lord.database.repository import SessionRecord
from c_lord.database.thread_repo import ThreadRepository
from c_lord.discord_ui.authorization import Authorizer
from c_lord.session_resume import NOT_A_CLORD_THREAD_BINDING

# ---------------------------------------------------------------------------
# Fixtures (same shape as tests/test_channel_repo.py)
# ---------------------------------------------------------------------------


@pytest.fixture
async def repo(tmp_path) -> ChannelRepository:
    r = ChannelRepository(str(tmp_path / "channel.db"))
    await r.init_db()
    return r


@pytest.fixture
async def thread_repo(tmp_path) -> ThreadRepository:
    r = ThreadRepository(str(tmp_path / "thread.db"))
    await r.init_db()
    return r


def _session_record(thread_id: int = 0) -> SessionRecord:
    return SessionRecord(
        thread_id=thread_id,
        session_id="sess-existing",
        working_dir="/tmp/x",
        model=None,
        origin="discord",
        summary=None,
        created_at="2026-08-20 10:00:00",
        last_used_at="2026-08-20 11:00:00",
        closed_at=None,
    )


def _make_bot(*, clord_thread: bool = True) -> MagicMock:
    bot = MagicMock()
    bot.get_cog = MagicMock(return_value=None)
    bot.session_repo = MagicMock()
    bot.session_repo.get = AsyncMock(return_value=_session_record() if clord_thread else None)
    return bot


def _cog(
    repo: ChannelRepository,
    thread_repo: ThreadRepository | None,
    tmp_path,
    *,
    bot: MagicMock | None = None,
) -> ChannelRepoCog:
    return ChannelRepoCog(
        bot or _make_bot(),
        repo=repo,
        thread_repo=thread_repo,
        # not an authorization test — see tests/test_default_authorization.py
        authorizer=Authorizer(allow_anyone=True),
        session_dir_base=str(tmp_path / "sessions"),
    )


@pytest.fixture
def cog(repo: ChannelRepository, thread_repo: ThreadRepository, tmp_path) -> ChannelRepoCog:
    return _cog(repo, thread_repo, tmp_path)


@pytest.fixture
def cog_no_threads(repo: ChannelRepository, tmp_path) -> ChannelRepoCog:
    """A consumer that never wired a ThreadRepository."""
    return _cog(repo, None, tmp_path)


def _ctx(channel_id: int, *, channel: object | None = None) -> MagicMock:
    ctx = MagicMock()
    ctx.send = AsyncMock()
    ctx.author = MagicMock()
    ctx.author.id = 1
    ctx.channel = channel if channel is not None else MagicMock()
    ctx.channel.id = channel_id
    ctx.bot = MagicMock()
    ctx.bot.get_channel = MagicMock(return_value=MagicMock())
    return ctx


def _thread(thread_id: int, parent_id: int) -> MagicMock:
    t = MagicMock(spec=discord.Thread)
    t.id = thread_id
    t.parent_id = parent_id
    return t


def _sent(ctx: MagicMock) -> str:
    return ctx.send.await_args.args[0]


# ===========================================================================
# #618: which tmux session a thread lives in, when it is not the channel's
# ===========================================================================


class TestDivergentSessionName:
    """#618: the sidebar said ``W1 │ OpenClawのQiita記事執筆`` and yousan looked
    for ``claude_base:w1`` — the real window was ``qiita-article:w1``. The name
    must carry the session only for threads that are NOT in their channel's
    session, and stay byte-identical for everyone else (no rename burst)."""

    async def test_thread_bound_to_another_repo_reports_its_session(
        self, cog: ChannelRepoCog, repo: ChannelRepository, thread_repo: ThreadRepository
    ) -> None:
        await repo.save(channel_id=1, source_repo="https://github.com/yousan/claude_base.git")
        await thread_repo.save(
            thread_id=2, source_repo="https://github.com/yousan/qiita-article.git", channel_id=1
        )
        assert await cog.divergent_session_name(1, 2) == "qiita-article"

    async def test_thread_in_its_channels_repo_reports_nothing(
        self, cog: ChannelRepoCog, repo: ChannelRepository
    ) -> None:
        """The common case: no thread binding → falls back to the channel's
        session → same name → None, so the thread is not renamed."""
        await repo.save(channel_id=1, source_repo="https://github.com/yousan/claude_base.git")
        assert await cog.divergent_session_name(1, 3) is None

    async def test_thread_bound_to_the_same_repo_as_channel_reports_nothing(
        self, cog: ChannelRepoCog, repo: ChannelRepository, thread_repo: ThreadRepository
    ) -> None:
        """A thread binding that names the channel's own repo (e.g. via
        ``/clord repo:`` with the default) is not "elsewhere"."""
        await repo.save(channel_id=1, source_repo="https://github.com/yousan/monitoring.git")
        await thread_repo.save(
            thread_id=4, source_repo="git@github.com:yousan/monitoring.git", channel_id=1
        )
        assert await cog.divergent_session_name(1, 4) is None

    async def test_unbound_channel_compares_against_default_clord_session(
        self, cog: ChannelRepoCog, thread_repo: ThreadRepository
    ) -> None:
        """#514 AC3 / #618: an unbound channel's threads live in ``clord``, so a
        thread bound to a repo there IS elsewhere and must say so."""
        await thread_repo.save(
            thread_id=6, source_repo="git@github.com:yousan/dotclaude.git", channel_id=5
        )
        assert await cog.divergent_session_name(5, 6) == "dotclaude"

    async def test_nothing_bound_anywhere_reports_nothing(self, cog: ChannelRepoCog) -> None:
        assert await cog.divergent_session_name(7, 8) is None


# ===========================================================================
# #438 / #427: session names the watchdog treats as ours
# ===========================================================================


class TestManagedSessionNames:
    async def test_default_session_is_always_managed(self, cog_no_threads: ChannelRepoCog) -> None:
        """#420/#438: windows of an unbound channel live in ``clord`` — without
        it in the set the watchdog would treat our own menus as a foreign bot's."""
        assert await cog_no_threads.managed_session_names() == {"clord"}

    async def test_channel_bindings_without_thread_repo(
        self, cog_no_threads: ChannelRepoCog, repo: ChannelRepository
    ) -> None:
        await repo.save(channel_id=1, source_repo="https://github.com/org/games.git")
        assert await cog_no_threads.managed_session_names() == {"clord", "games"}

    async def test_failing_thread_repo_still_returns_channel_sessions(
        self, repo: ChannelRepository, tmp_path
    ) -> None:
        """A broken thread table must not blind the watchdog to the channel
        sessions it already knows (#438 guard stays usable)."""
        broken = MagicMock()
        broken.list_all = AsyncMock(side_effect=RuntimeError("database is locked"))
        cog = _cog(repo, broken, tmp_path)
        await repo.save(channel_id=1, source_repo="https://github.com/org/games.git")
        assert await cog.managed_session_names() == {"clord", "games"}


# ===========================================================================
# has_thread_binding (#556) / is_clord_thread (#551)
# ===========================================================================


class TestHasThreadBinding:
    async def test_without_thread_repo_is_false(self, cog_no_threads: ChannelRepoCog) -> None:
        assert await cog_no_threads.has_thread_binding(1) is False

    async def test_reports_a_saved_binding(
        self, cog: ChannelRepoCog, thread_repo: ThreadRepository
    ) -> None:
        await thread_repo.save(thread_id=9, source_repo="https://github.com/o/r.git", channel_id=1)
        assert await cog.has_thread_binding(9) is True
        assert await cog.has_thread_binding(10) is False

    async def test_db_error_is_no_not_a_raise(self, repo: ChannelRepository, tmp_path) -> None:
        """#556: callers use this to decide whether to speak in a thread; a DB
        hiccup must read as "no evidence", never take the caller down."""
        broken = MagicMock()
        broken.get = AsyncMock(side_effect=RuntimeError("database is locked"))
        cog = _cog(repo, broken, tmp_path)
        assert await cog.has_thread_binding(1) is False


class TestIsClordThreadWithoutRepo:
    async def test_no_session_repository_refuses(
        self, repo: ChannelRepository, thread_repo: ThreadRepository, tmp_path
    ) -> None:
        """#551: an instance that cannot tell whose thread this is must not
        hand it over — the answer is False, not "probably"."""
        bot = MagicMock(spec=["get_cog", "user"])  # no session_repo attribute
        cog = _cog(repo, thread_repo, tmp_path, bot=bot)
        assert await cog.is_clord_thread(123, _thread(123, 1)) is False


# ===========================================================================
# bind_thread (#514) and the #923 newline guard
# ===========================================================================


class TestBindThread:
    async def test_without_thread_repo_returns_none(self, cog_no_threads: ChannelRepoCog) -> None:
        assert (
            await cog_no_threads.bind_thread(1, "https://github.com/o/r.git", channel_id=2) is None
        )

    async def test_url_with_request_glued_on_is_refused_and_not_saved(
        self, cog: ChannelRepoCog, thread_repo: ThreadRepository
    ) -> None:
        """#923: ``!clord repo:<URL>`` followed by a newline put the request text
        into the repo name, so the clone failed after the thread opened. The
        binding layer refuses a multi-line URL instead of storing it."""
        with pytest.raises(ValueError, match="改行"):
            await cog.bind_thread(
                11, "https://github.com/yousan/c-lord\nこの不具合を直して", channel_id=1
            )
        assert await thread_repo.get(11) is None

    async def test_normalizes_and_replaces_cached_session(
        self, cog: ChannelRepoCog, thread_repo: ThreadRepository
    ) -> None:
        """#514: writing the binding by hand would leave the cached tmux manager
        pointing at the old repo; bind_thread must drop it."""
        await thread_repo.save(
            thread_id=12, source_repo="https://github.com/o/before.git", channel_id=1
        )
        first = await cog.resolve_tmux_manager(1, thread_id=12)
        assert first is not None and first.session_name == "before"

        saved = await cog.bind_thread(12, "https://github.com/o/after/pull/3", channel_id=1)
        assert saved == "https://github.com/o/after.git"  # #88 PR URL → repo root
        second = await cog.resolve_tmux_manager(1, thread_id=12)
        assert second is not None and second.session_name == "after"


# ===========================================================================
# /clord repo: autocomplete source
# ===========================================================================


class TestKnownReposAndChannelRepo:
    async def test_known_repos_merges_channel_and_thread_bindings_without_duplicates(
        self, cog: ChannelRepoCog, repo: ChannelRepository, thread_repo: ThreadRepository
    ) -> None:
        await repo.save(channel_id=1, source_repo="https://github.com/o/a.git")
        await repo.save(channel_id=2, source_repo="https://github.com/o/b.git")
        await thread_repo.save(thread_id=3, source_repo="https://github.com/o/a.git", channel_id=1)
        await thread_repo.save(thread_id=4, source_repo="https://github.com/o/c.git", channel_id=1)
        known = await cog.known_repos()
        assert sorted(known) == [
            "https://github.com/o/a.git",
            "https://github.com/o/b.git",
            "https://github.com/o/c.git",
        ]

    async def test_known_repos_survives_broken_tables(self, tmp_path) -> None:
        broken = MagicMock()
        broken.list_all = AsyncMock(side_effect=RuntimeError("locked"))
        cog = _cog(broken, broken, tmp_path)
        assert await cog.known_repos() == []

    async def test_channel_repo(self, cog: ChannelRepoCog, repo: ChannelRepository) -> None:
        await repo.save(channel_id=1, source_repo="https://github.com/o/a.git")
        assert await cog.channel_repo(1) == "https://github.com/o/a.git"
        assert await cog.channel_repo(2) is None


# ===========================================================================
# /clord-init replies (#209 text twin, #88 normalization, #923)
# ===========================================================================


class TestClordInitReplies:
    async def test_remove_without_binding_says_so(self, cog: ChannelRepoCog) -> None:
        ctx = _ctx(100)
        await cog.clord_init_text.callback(cog, ctx, arg="remove")
        assert _sent(ctx) == "No binding found for this channel."

    async def test_show_lists_channel_and_thread_bindings_with_tmux_name(
        self, cog: ChannelRepoCog, repo: ChannelRepository, thread_repo: ThreadRepository
    ) -> None:
        """The no-arg form is how a user finds out which tmux session to attach
        to — the channel line has to carry the derived session name (#618)."""
        await repo.save(channel_id=100, source_repo="https://github.com/org/games.git")
        await thread_repo.save(
            thread_id=101, source_repo="https://github.com/org/monitoring.git", channel_id=100
        )
        ctx = _ctx(100)
        await cog.clord_init_text.callback(cog, ctx, arg=None)
        lines = _sent(ctx).splitlines()
        assert lines[0] == "<#100> → `https://github.com/org/games.git` (tmux: `games`)"
        assert lines[1] == (
            "  thread <#101> (channel <#100>) → `https://github.com/org/monitoring.git`"
        )

    async def test_show_with_only_channel_bindings_and_no_thread_repo(
        self, cog_no_threads: ChannelRepoCog, repo: ChannelRepository
    ) -> None:
        await repo.save(channel_id=100, source_repo="https://github.com/org/games.git")
        ctx = _ctx(100)
        await cog_no_threads.clord_init_text.callback(cog_no_threads, ctx, arg=None)
        assert "(tmux: `games`)" in _sent(ctx)

    async def test_bind_with_newline_url_is_refused(
        self, cog: ChannelRepoCog, repo: ChannelRepository
    ) -> None:
        """#923: the same "request glued to the URL" must not become a channel
        binding either — the user gets the reason, nothing is saved."""
        ctx = _ctx(100)
        await cog._clord_init_impl(
            channel_id=100,
            user=ctx.author,
            repo="https://github.com/yousan/c-lord\nこの不具合を直して",
            remove=False,
            respond=cog._ctx_respond(ctx),
            message=None,
        )
        assert _sent(ctx).startswith("⚠️ リポジトリ URL が不正です")
        assert await repo.get(100) is None

    async def test_rebind_evicts_the_cached_tmux_session(
        self, cog: ChannelRepoCog, repo: ChannelRepository
    ) -> None:
        """#427 family: a cached manager surviving a rebind would keep sending
        the channel's threads into the old repo's session."""
        await repo.save(channel_id=100, source_repo="https://github.com/org/old.git")
        before = await cog.resolve_tmux_manager(100, thread_id=None)
        assert before is not None and before.session_name == "old"

        ctx = _ctx(100)
        await cog.clord_init_text.callback(cog, ctx, arg="https://github.com/org/new/issues/5")
        assert _sent(ctx) == ("Bound <#100> → `https://github.com/org/new.git` (tmux: `new`)")
        after = await cog.resolve_tmux_manager(100, thread_id=None)
        assert after is not None and after.session_name == "new"

    async def test_slash_command_binds_via_interaction(
        self, cog: ChannelRepoCog, repo: ChannelRepository
    ) -> None:
        interaction = MagicMock(spec=discord.Interaction)
        interaction.channel_id = 200
        interaction.user = MagicMock()
        interaction.response = MagicMock()
        interaction.response.send_message = AsyncMock()
        await cog.clord_init.callback(
            cog, interaction, repo="https://github.com/org/r.git", remove=False
        )
        binding = await repo.get(200)
        assert binding is not None
        interaction.response.send_message.assert_awaited_once()
        assert interaction.response.send_message.await_args.kwargs["ephemeral"] is True


# ===========================================================================
# /clord-thread-init replies (#551, #88, #923)
# ===========================================================================


class TestClordThreadInitReplies:
    async def test_without_thread_repo_says_not_enabled(
        self, cog_no_threads: ChannelRepoCog
    ) -> None:
        ctx = _ctx(300)
        await cog_no_threads.clord_thread_init_text.callback(cog_no_threads, ctx, arg=None)
        assert _sent(ctx) == "Thread-level bindings are not enabled on this bot."

    async def test_remove_existing_binding_and_evict(
        self, cog: ChannelRepoCog, repo: ChannelRepository, thread_repo: ThreadRepository
    ) -> None:
        """After remove the thread goes back to its channel's session (#427)."""
        await repo.save(channel_id=30, source_repo="https://github.com/org/games.git")
        await thread_repo.save(
            thread_id=301, source_repo="https://github.com/org/monitoring.git", channel_id=30
        )
        bound = await cog.resolve_tmux_manager(30, thread_id=301)
        assert bound is not None and bound.session_name == "monitoring"

        ctx = _ctx(301, channel=_thread(301, 30))
        await cog.clord_thread_init_text.callback(cog, ctx, arg="remove")
        assert _sent(ctx) == "Removed thread repository binding for <#301>."
        assert await thread_repo.get(301) is None
        back = await cog.resolve_tmux_manager(30, thread_id=301)
        assert back is not None and back.session_name == "games"

    async def test_remove_without_binding_says_so(self, cog: ChannelRepoCog) -> None:
        ctx = _ctx(302)
        await cog.clord_thread_init_text.callback(cog, ctx, arg="remove")
        assert _sent(ctx) == "No thread binding found."

    async def test_remove_is_allowed_on_a_non_clord_thread(
        self, repo: ChannelRepository, thread_repo: ThreadRepository, tmp_path
    ) -> None:
        """#551: only *bind* is gated — a stale binding on a thread that lost
        its row must still be clearable."""
        cog = _cog(repo, thread_repo, tmp_path, bot=_make_bot(clord_thread=False))
        await thread_repo.save(
            thread_id=303, source_repo="https://github.com/o/r.git", channel_id=3
        )
        ctx = _ctx(303)
        await cog.clord_thread_init_text.callback(cog, ctx, arg="remove")
        assert await thread_repo.get(303) is None

    async def test_show_existing_binding(
        self, cog: ChannelRepoCog, thread_repo: ThreadRepository
    ) -> None:
        await thread_repo.save(
            thread_id=304, source_repo="https://github.com/o/r.git", channel_id=3
        )
        ctx = _ctx(304)
        await cog.clord_thread_init_text.callback(cog, ctx, arg=None)
        assert _sent(ctx) == "Thread <#304> → `https://github.com/o/r.git`"

    async def test_bind_on_a_human_thread_is_refused(
        self, repo: ChannelRepository, thread_repo: ThreadRepository, tmp_path
    ) -> None:
        """#551: binding a human conversation thread was step one of turning it
        into a Claude session. Not c-lord's → refusal text, nothing saved."""
        bot = _make_bot(clord_thread=False)
        bot.user = MagicMock(id=999)
        cog = _cog(repo, thread_repo, tmp_path, bot=bot)
        thread = _thread(305, 3)
        thread.owner_id = 42  # a human opened it
        ctx = _ctx(305, channel=thread)
        await cog.clord_thread_init_text.callback(cog, ctx, arg="https://github.com/o/r.git")
        assert _sent(ctx) == NOT_A_CLORD_THREAD_BINDING
        assert await thread_repo.get(305) is None

    async def test_parent_channel_lookup_network_failure_still_binds(
        self, cog: ChannelRepoCog, thread_repo: ThreadRepository
    ) -> None:
        """Only ``Forbidden`` means "bot cannot see the parent" (the access
        check). A transient network failure (#930's DISCORD_CALL_FAILURES) is
        not evidence of that, so the bind goes ahead with the thread's parent."""
        ctx = _ctx(306, channel=_thread(306, 30))
        ctx.bot.get_channel = MagicMock(return_value=None)
        ctx.bot.fetch_channel = AsyncMock(side_effect=OSError("Temporary failure in name res"))
        await cog.clord_thread_init_text.callback(cog, ctx, arg="https://github.com/o/r.git")
        binding = await thread_repo.get(306)
        assert binding is not None
        assert binding["channel_id"] == 30
        assert _sent(ctx) == "Bound thread <#306> → `https://github.com/o/r.git`"

    async def test_bind_with_newline_url_is_refused(
        self, cog: ChannelRepoCog, thread_repo: ThreadRepository
    ) -> None:
        """#923 on the thread-level command."""
        ctx = _ctx(307, channel=_thread(307, 30))
        await cog._clord_thread_init_impl(
            thread_id=307,
            channel=ctx.channel,
            client=ctx.bot,
            user=ctx.author,
            repo="https://github.com/yousan/c-lord\nこの不具合を直して",
            remove=False,
            respond=cog._ctx_respond(ctx),
        )
        assert _sent(ctx).startswith("⚠️ リポジトリ URL が不正です")
        assert await thread_repo.get(307) is None
