"""Tests for ChannelRepository, ThreadRepository, and ChannelRepoCog."""

from __future__ import annotations

from unittest.mock import AsyncMock, MagicMock

import pytest

from c_lord.cogs.channel_repo import ChannelRepoCog
from c_lord.database.channel_repo import (
    ChannelRepository,
    derive_session_name,
    normalize_repo_url,
)
from c_lord.database.repository import SessionRecord
from c_lord.database.thread_repo import ThreadRepository

# ---------------------------------------------------------------------------
# Fixtures
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


def _session_record() -> SessionRecord:
    """A minimal open ``sessions`` row — enough to read as a c-lord thread."""
    return SessionRecord(
        thread_id=0,
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
    bot.loop = MagicMock()
    bot.get_cog = MagicMock(return_value=None)
    # #551 gates ``/clord-thread-init repo:`` on the thread being c-lord's.
    # These tests are about URL handling and channel access, so the thread is
    # one of c-lord's own unless a test says otherwise.
    bot.session_repo = MagicMock()
    bot.session_repo.get = AsyncMock(return_value=_session_record() if clord_thread else None)
    return bot


@pytest.fixture
def cog(repo: ChannelRepository, thread_repo: ThreadRepository, tmp_path) -> ChannelRepoCog:
    return ChannelRepoCog(
        _make_bot(),
        repo=repo,
        thread_repo=thread_repo,
        allowed_user_ids=None,
        session_dir_base=str(tmp_path / "sessions"),
    )


# ===========================================================================
# ChannelRepository tests
# ===========================================================================


class TestChannelRepoSave:
    async def test_save_and_get(self, repo: ChannelRepository) -> None:
        await repo.save(channel_id=100, source_repo="https://github.com/org/repo.git")
        binding = await repo.get(100)
        assert binding is not None
        assert binding["channel_id"] == 100
        assert binding["source_repo"] == "https://github.com/org/repo.git"

    async def test_save_upsert(self, repo: ChannelRepository) -> None:
        await repo.save(channel_id=300, source_repo="https://github.com/org/old.git")
        await repo.save(channel_id=300, source_repo="https://github.com/org/new.git")
        binding = await repo.get(300)
        assert binding is not None
        assert binding["source_repo"] == "https://github.com/org/new.git"


class TestChannelRepoGet:
    async def test_get_nonexistent(self, repo: ChannelRepository) -> None:
        binding = await repo.get(999)
        assert binding is None


class TestChannelRepoDelete:
    async def test_delete_existing(self, repo: ChannelRepository) -> None:
        await repo.save(channel_id=400, source_repo="https://github.com/org/repo.git")
        deleted = await repo.delete(400)
        assert deleted is True
        assert await repo.get(400) is None

    async def test_delete_nonexistent(self, repo: ChannelRepository) -> None:
        deleted = await repo.delete(999)
        assert deleted is False


class TestChannelRepoListAll:
    async def test_list_empty(self, repo: ChannelRepository) -> None:
        bindings = await repo.list_all()
        assert bindings == []

    async def test_list_all(self, repo: ChannelRepository) -> None:
        await repo.save(channel_id=500, source_repo="https://github.com/org/a.git")
        await repo.save(channel_id=600, source_repo="https://github.com/org/b.git")
        bindings = await repo.list_all()
        assert len(bindings) == 2
        ids = {b["channel_id"] for b in bindings}
        assert ids == {500, 600}


# ===========================================================================
# ChannelRepoCog tests
# ===========================================================================


class TestChannelRepoCogResolveManager:
    async def test_resolve_returns_none_without_binding(self, cog: ChannelRepoCog) -> None:
        manager = await cog.resolve_manager(999)
        assert manager is None

    async def test_resolve_creates_manager_from_binding(
        self, cog: ChannelRepoCog, repo: ChannelRepository
    ) -> None:
        await repo.save(channel_id=100, source_repo="/tmp/fake-repo.git")
        manager = await cog.resolve_manager(100)
        assert manager is not None
        assert manager.source_repo == "/tmp/fake-repo.git"

    async def test_resolve_caches_manager(
        self, cog: ChannelRepoCog, repo: ChannelRepository
    ) -> None:
        await repo.save(channel_id=100, source_repo="/tmp/fake-repo.git")
        m1 = await cog.resolve_manager(100)
        m2 = await cog.resolve_manager(100)
        assert m1 is m2  # same object from cache


# ===========================================================================
# derive_session_name tests
# ===========================================================================


class TestDeriveSessionName:
    def test_github_https_url(self) -> None:
        assert derive_session_name("https://github.com/org/my-project.git") == "my-project"

    def test_github_https_url_no_dot_git(self) -> None:
        assert derive_session_name("https://github.com/org/my-project") == "my-project"

    def test_trailing_slash(self) -> None:
        assert derive_session_name("https://github.com/org/my-project/") == "my-project"

    def test_ssh_url(self) -> None:
        assert derive_session_name("git@github.com:org/my-project.git") == "my-project"

    def test_local_path(self) -> None:
        assert derive_session_name("/home/user/repos/my-project") == "my-project"

    def test_empty_string_returns_fallback(self) -> None:
        assert derive_session_name("") == "clord"

    def test_dot_git_only_returns_fallback(self) -> None:
        assert derive_session_name(".git") == "clord"

    def test_dotted_repo_name_sanitized_for_tmux(self) -> None:
        # tmux forbids '.' in session names and silently rewrites it to '_'
        # (its target syntax `session:window.pane` uses '.'/':' as separators).
        # derive_session_name must mirror that rewrite so the name c-lord hands
        # to `tmux -t` matches the one tmux actually stored — otherwise every
        # window op for a dotted repo targets a non-existent session and Claude
        # never starts ("Failed to start Claude in tmux"). (#474)
        assert derive_session_name("git@github.com:sakana1235/NiyaReco.love.git") == "NiyaReco_love"
        assert derive_session_name("https://github.com/foo/site.dev") == "site_dev"

    def test_colon_in_name_sanitized_for_tmux(self) -> None:
        # ':' is likewise illegal in a tmux session name.
        assert derive_session_name("/home/user/repos/weird:name") == "weird_name"


# ===========================================================================
# normalize_repo_url tests (#88)
# ===========================================================================


class TestNormalizeRepoUrl:
    """A derived GitHub URL (pull/issues/blob/tree/...) is shrunk to owner/repo.git."""

    def test_pull_request_url(self) -> None:
        assert (
            normalize_repo_url("https://github.com/owner/repo/pull/2")
            == "https://github.com/owner/repo.git"
        )

    def test_issues_url(self) -> None:
        assert (
            normalize_repo_url("https://github.com/owner/repo/issues/5")
            == "https://github.com/owner/repo.git"
        )

    def test_blob_url(self) -> None:
        assert (
            normalize_repo_url("https://github.com/owner/repo/blob/main/foo.py")
            == "https://github.com/owner/repo.git"
        )

    def test_tree_url(self) -> None:
        assert (
            normalize_repo_url("https://github.com/owner/repo/tree/main/src")
            == "https://github.com/owner/repo.git"
        )

    def test_bare_repo_url_gets_dot_git(self) -> None:
        assert (
            normalize_repo_url("https://github.com/owner/repo")
            == "https://github.com/owner/repo.git"
        )

    def test_already_dot_git_unchanged(self) -> None:
        assert (
            normalize_repo_url("https://github.com/owner/repo.git")
            == "https://github.com/owner/repo.git"
        )

    def test_trailing_slash(self) -> None:
        assert (
            normalize_repo_url("https://github.com/owner/repo/")
            == "https://github.com/owner/repo.git"
        )

    def test_ssh_with_dot_git_unchanged(self) -> None:
        assert (
            normalize_repo_url("git@github.com:owner/repo.git") == "git@github.com:owner/repo.git"
        )

    def test_ssh_without_dot_git_gets_dot_git(self) -> None:
        assert normalize_repo_url("git@github.com:owner/repo") == "git@github.com:owner/repo.git"

    def test_gitlab_derived_url(self) -> None:
        assert (
            normalize_repo_url("https://gitlab.com/owner/repo/-/merge_requests/3")
            == "https://gitlab.com/owner/repo.git"
        )

    def test_bitbucket_derived_url(self) -> None:
        assert (
            normalize_repo_url("https://bitbucket.org/owner/repo/pull-requests/7")
            == "https://bitbucket.org/owner/repo.git"
        )

    def test_local_path_unchanged(self) -> None:
        assert normalize_repo_url("/home/user/repos/my-project") == "/home/user/repos/my-project"

    def test_empty_string_unchanged(self) -> None:
        assert normalize_repo_url("") == ""

    def test_whitespace_stripped(self) -> None:
        assert (
            normalize_repo_url("  https://github.com/owner/repo/pull/2  ")
            == "https://github.com/owner/repo.git"
        )


# ===========================================================================
# Bind handlers normalize the URL before persisting (#88)
# ===========================================================================


class TestBindNormalizesUrl:
    async def test_clord_init_persists_normalized_repo(
        self, cog: ChannelRepoCog, repo: ChannelRepository
    ) -> None:
        await cog._clord_init_impl(
            channel_id=100,
            user=MagicMock(),
            repo="https://github.com/owner/repo/pull/2",
            remove=False,
            respond=AsyncMock(),
        )
        binding = await repo.get(100)
        assert binding is not None
        assert binding["source_repo"] == "https://github.com/owner/repo.git"

    async def test_clord_thread_init_persists_normalized_repo(
        self, cog: ChannelRepoCog, thread_repo: ThreadRepository
    ) -> None:
        channel = MagicMock()
        # Not a discord.Thread → channel_id falls back to thread_id; skips parent access check.
        await cog._clord_thread_init_impl(
            thread_id=200,
            channel=channel,
            client=MagicMock(),
            user=MagicMock(),
            repo="https://github.com/owner/repo/issues/5",
            remove=False,
            respond=AsyncMock(),
        )
        binding = await thread_repo.get(200)
        assert binding is not None
        assert binding["source_repo"] == "https://github.com/owner/repo.git"


# ===========================================================================
# ChannelRepoCog tmux manager tests
# ===========================================================================


class TestChannelRepoCogResolveTmuxManager:
    async def test_resolve_returns_none_without_binding(self, cog: ChannelRepoCog) -> None:
        manager = await cog.resolve_tmux_manager(999, thread_id=None)
        assert manager is None

    async def test_resolve_auto_derives_from_repo(
        self, cog: ChannelRepoCog, repo: ChannelRepository
    ) -> None:
        await repo.save(
            channel_id=200,
            source_repo="https://github.com/org/my-project.git",
        )
        manager = await cog.resolve_tmux_manager(200, thread_id=None)
        assert manager is not None
        assert manager.session_name == "my-project"

    async def test_resolve_cached(self, cog: ChannelRepoCog, repo: ChannelRepository) -> None:
        await repo.save(
            channel_id=300,
            source_repo="https://github.com/org/repo.git",
        )
        m1 = await cog.resolve_tmux_manager(300, thread_id=None)
        m2 = await cog.resolve_tmux_manager(300, thread_id=None)
        assert m1 is m2  # same object from cache


class TestChannelRepoCogEvictCache:
    async def test_evict_removes_from_cache(
        self, cog: ChannelRepoCog, repo: ChannelRepository
    ) -> None:
        await repo.save(channel_id=100, source_repo="/tmp/fake-repo.git")
        await cog.resolve_manager(100)
        assert 100 in cog._manager_cache
        cog.evict_cache(100)
        assert 100 not in cog._manager_cache

    async def test_evict_removes_tmux_cache(
        self, cog: ChannelRepoCog, repo: ChannelRepository
    ) -> None:
        await repo.save(channel_id=100, source_repo="https://github.com/org/repo.git")
        await cog.resolve_tmux_manager(100, thread_id=None)
        assert 100 in cog._tmux_cache
        cog.evict_cache(100)
        assert 100 not in cog._tmux_cache


# ===========================================================================
# ThreadRepository tests
# ===========================================================================


class TestThreadRepoSave:
    async def test_save_and_get(self, thread_repo: ThreadRepository) -> None:
        await thread_repo.save(
            thread_id=999, source_repo="https://github.com/org/repo.git", channel_id=100
        )
        binding = await thread_repo.get(999)
        assert binding is not None
        assert binding["thread_id"] == 999
        assert binding["source_repo"] == "https://github.com/org/repo.git"
        assert binding["channel_id"] == 100

    async def test_save_upsert(self, thread_repo: ThreadRepository) -> None:
        await thread_repo.save(thread_id=111, source_repo="https://github.com/org/old.git")
        await thread_repo.save(thread_id=111, source_repo="https://github.com/org/new.git")
        binding = await thread_repo.get(111)
        assert binding is not None
        assert binding["source_repo"] == "https://github.com/org/new.git"

    async def test_save_without_channel_id(self, thread_repo: ThreadRepository) -> None:
        await thread_repo.save(thread_id=222, source_repo="https://github.com/org/repo.git")
        binding = await thread_repo.get(222)
        assert binding is not None
        assert binding["channel_id"] is None


class TestThreadRepoGet:
    async def test_get_nonexistent(self, thread_repo: ThreadRepository) -> None:
        binding = await thread_repo.get(9999)
        assert binding is None


class TestThreadRepoDelete:
    async def test_delete_existing(self, thread_repo: ThreadRepository) -> None:
        await thread_repo.save(thread_id=333, source_repo="https://github.com/org/repo.git")
        deleted = await thread_repo.delete(333)
        assert deleted is True
        assert await thread_repo.get(333) is None

    async def test_delete_nonexistent(self, thread_repo: ThreadRepository) -> None:
        deleted = await thread_repo.delete(9999)
        assert deleted is False


class TestThreadRepoListByChannel:
    async def test_list_empty(self, thread_repo: ThreadRepository) -> None:
        bindings = await thread_repo.list_by_channel(100)
        assert bindings == []

    async def test_list_by_channel(self, thread_repo: ThreadRepository) -> None:
        await thread_repo.save(
            thread_id=1001, source_repo="https://github.com/org/a.git", channel_id=100
        )
        await thread_repo.save(
            thread_id=1002, source_repo="https://github.com/org/b.git", channel_id=100
        )
        await thread_repo.save(
            thread_id=2001, source_repo="https://github.com/org/c.git", channel_id=200
        )
        bindings = await thread_repo.list_by_channel(100)
        assert len(bindings) == 2
        ids = {b["thread_id"] for b in bindings}
        assert ids == {1001, 1002}


# ===========================================================================
# Thread-aware resolve_manager tests
# ===========================================================================


class TestResolveManagerThreadOverride:
    async def test_channel_only_no_thread_id(
        self, cog: ChannelRepoCog, repo: ChannelRepository
    ) -> None:
        await repo.save(channel_id=100, source_repo="https://github.com/org/channel.git")
        manager = await cog.resolve_manager(channel_id=100)
        assert manager is not None
        assert manager.source_repo == "https://github.com/org/channel.git"

    async def test_channel_only_with_thread_id_no_thread_bind(
        self, cog: ChannelRepoCog, repo: ChannelRepository
    ) -> None:
        await repo.save(channel_id=100, source_repo="https://github.com/org/channel.git")
        manager = await cog.resolve_manager(channel_id=100, thread_id=999)
        assert manager is not None
        assert manager.source_repo == "https://github.com/org/channel.git"

    async def test_thread_override(
        self,
        cog: ChannelRepoCog,
        repo: ChannelRepository,
        thread_repo: ThreadRepository,
    ) -> None:
        await repo.save(channel_id=100, source_repo="https://github.com/org/channel.git")
        await thread_repo.save(
            thread_id=999,
            source_repo="https://github.com/org/thread.git",
            channel_id=100,
        )
        manager = await cog.resolve_manager(channel_id=100, thread_id=999)
        assert manager is not None
        assert manager.source_repo == "https://github.com/org/thread.git"

    async def test_thread_bind_without_channel_bind(
        self, cog: ChannelRepoCog, thread_repo: ThreadRepository
    ) -> None:
        await thread_repo.save(thread_id=999, source_repo="https://github.com/org/thread.git")
        manager = await cog.resolve_manager(channel_id=100, thread_id=999)
        assert manager is not None
        assert manager.source_repo == "https://github.com/org/thread.git"

    async def test_no_binding_returns_none(self, cog: ChannelRepoCog) -> None:
        manager = await cog.resolve_manager(channel_id=100, thread_id=999)
        assert manager is None

    async def test_thread_manager_cached(
        self,
        cog: ChannelRepoCog,
        thread_repo: ThreadRepository,
    ) -> None:
        await thread_repo.save(thread_id=999, source_repo="https://github.com/org/thread.git")
        m1 = await cog.resolve_manager(channel_id=100, thread_id=999)
        m2 = await cog.resolve_manager(channel_id=100, thread_id=999)
        assert m1 is m2

    async def test_evict_thread_cache(
        self,
        cog: ChannelRepoCog,
        thread_repo: ThreadRepository,
    ) -> None:
        await thread_repo.save(thread_id=999, source_repo="https://github.com/org/thread.git")
        await cog.resolve_manager(channel_id=100, thread_id=999)
        assert 999 in cog._thread_manager_cache
        cog.evict_thread_cache(999)
        assert 999 not in cog._thread_manager_cache


# ===========================================================================
# /clord-thread-init access check tests
# ===========================================================================


def _make_thread_interaction(
    thread_id: int = 9001,
    parent_id: int = 5000,
    *,
    bot_can_access: bool = True,
) -> MagicMock:
    """Return a mock Interaction inside a discord.Thread."""
    import discord

    bot = MagicMock()
    if bot_can_access:
        bot.get_channel = MagicMock(return_value=MagicMock(spec=discord.TextChannel))
    else:
        bot.get_channel = MagicMock(return_value=None)
        bot.fetch_channel = AsyncMock(side_effect=discord.Forbidden(MagicMock(), "Missing Access"))

    interaction = MagicMock(spec=discord.Interaction)
    interaction.user = MagicMock()
    interaction.user.id = 42
    interaction.client = bot

    thread = MagicMock(spec=discord.Thread)
    thread.id = thread_id
    thread.parent_id = parent_id
    interaction.channel = thread
    interaction.channel_id = thread_id

    interaction.response = MagicMock()
    interaction.response.send_message = AsyncMock()
    return interaction


class TestClordThreadInitAccessCheck:
    async def test_bind_succeeds_when_bot_has_access(
        self, cog: ChannelRepoCog, thread_repo: ThreadRepository
    ) -> None:
        interaction = _make_thread_interaction(bot_can_access=True)
        await cog.clord_thread_init.callback(
            cog, interaction, repo="https://github.com/org/repo.git", remove=False
        )
        binding = await thread_repo.get(9001)
        assert binding is not None
        assert binding["source_repo"] == "https://github.com/org/repo.git"
        interaction.response.send_message.assert_called_once()
        msg = interaction.response.send_message.call_args[0][0]
        assert "Missing Access" not in msg

    async def test_bind_fails_when_bot_has_no_access(
        self, cog: ChannelRepoCog, thread_repo: ThreadRepository
    ) -> None:
        interaction = _make_thread_interaction(bot_can_access=False)
        await cog.clord_thread_init.callback(
            cog, interaction, repo="https://github.com/org/repo.git", remove=False
        )
        # Nothing saved to DB
        binding = await thread_repo.get(9001)
        assert binding is None
        # Error message shown
        interaction.response.send_message.assert_called_once()
        msg = interaction.response.send_message.call_args[1].get(
            "content", interaction.response.send_message.call_args[0][0]
        )
        assert "アクセス" in msg or "access" in msg.lower()


# ===========================================================================
# Text/mention twins (#209 follow-up) — webhook-invokable config commands
# ===========================================================================


def _make_ctx(channel_id: int = 555, author_id: int = 1) -> MagicMock:
    ctx = MagicMock()
    ctx.send = AsyncMock()
    ctx.author = MagicMock()
    ctx.author.id = author_id
    ctx.channel = MagicMock()
    ctx.channel.id = channel_id
    ctx.bot = MagicMock()
    ctx.bot.get_channel = MagicMock(return_value=MagicMock())
    return ctx


class TestClordInitTextTwin:
    async def test_show_no_bindings(self, cog: ChannelRepoCog) -> None:
        ctx = _make_ctx()
        await cog.clord_init_text.callback(cog, ctx, arg=None)
        ctx.send.assert_called_once()

    async def test_bind(self, cog: ChannelRepoCog, repo: ChannelRepository) -> None:
        ctx = _make_ctx(channel_id=777)
        await cog.clord_init_text.callback(cog, ctx, arg="https://github.com/org/r.git")
        binding = await repo.get(777)
        assert binding is not None
        assert "Bound" in ctx.send.call_args.args[0]

    async def test_remove(self, cog: ChannelRepoCog, repo: ChannelRepository) -> None:
        await repo.save(channel_id=888, source_repo="https://x/y.git")
        ctx = _make_ctx(channel_id=888)
        await cog.clord_init_text.callback(cog, ctx, arg="remove")
        assert await repo.get(888) is None


class TestClordThreadInitTextTwin:
    async def test_show_no_binding(self, cog: ChannelRepoCog) -> None:
        ctx = _make_ctx(channel_id=5555)
        await cog.clord_thread_init_text.callback(cog, ctx, arg=None)
        ctx.send.assert_called_once()
        assert "thread" in ctx.send.call_args.args[0].lower()

    async def test_bind(self, cog: ChannelRepoCog, thread_repo: ThreadRepository) -> None:
        ctx = _make_ctx(channel_id=5555)
        await cog.clord_thread_init_text.callback(cog, ctx, arg="https://github.com/org/t.git")
        binding = await thread_repo.get(5555)
        assert binding is not None
        assert "Bound thread" in ctx.send.call_args.args[0]


# ===========================================================================
# #427: tmux manager must honour thread-level bindings
# ===========================================================================


class TestResolveTmuxManagerThreadBinding:
    """`resolve_tmux_manager` resolved only channel bindings, so a thread bound
    to another repo got its session_dir from the thread repo but its tmux
    window in the *parent channel's* session (#427)."""

    async def test_thread_binding_wins_over_channel(
        self, cog: ChannelRepoCog, repo: ChannelRepository, thread_repo: ThreadRepository
    ) -> None:
        await repo.save(channel_id=10, source_repo="https://github.com/org/games.git")
        await thread_repo.save(
            thread_id=11, source_repo="https://github.com/org/monitoring.git", channel_id=10
        )
        manager = await cog.resolve_tmux_manager(10, thread_id=11)
        assert manager is not None
        assert manager.session_name == "monitoring"

    async def test_falls_back_to_channel_without_thread_binding(
        self, cog: ChannelRepoCog, repo: ChannelRepository
    ) -> None:
        await repo.save(channel_id=20, source_repo="https://github.com/org/games.git")
        manager = await cog.resolve_tmux_manager(20, thread_id=21)
        assert manager is not None
        assert manager.session_name == "games"

    async def test_thread_binding_without_channel_binding(
        self, cog: ChannelRepoCog, thread_repo: ThreadRepository
    ) -> None:
        """Unbound channel + thread binding → still get a tmux session (#514 AC3)."""
        await thread_repo.save(
            thread_id=31, source_repo="git@github.com:yousan/dotclaude.git", channel_id=30
        )
        manager = await cog.resolve_tmux_manager(30, thread_id=31)
        assert manager is not None
        assert manager.session_name == "dotclaude"

    async def test_thread_manager_is_cached(
        self, cog: ChannelRepoCog, thread_repo: ThreadRepository
    ) -> None:
        await thread_repo.save(
            thread_id=41, source_repo="https://github.com/org/repo.git", channel_id=40
        )
        m1 = await cog.resolve_tmux_manager(40, thread_id=41)
        m2 = await cog.resolve_tmux_manager(40, thread_id=41)
        assert m1 is m2

    async def test_evict_thread_cache_drops_tmux_manager(
        self, cog: ChannelRepoCog, thread_repo: ThreadRepository
    ) -> None:
        await thread_repo.save(
            thread_id=51, source_repo="https://github.com/org/before.git", channel_id=50
        )
        first = await cog.resolve_tmux_manager(50, thread_id=51)
        assert first is not None and first.session_name == "before"

        await thread_repo.save(
            thread_id=51, source_repo="https://github.com/org/after.git", channel_id=50
        )
        cog.evict_thread_cache(51)
        second = await cog.resolve_tmux_manager(50, thread_id=51)
        assert second is not None
        assert second.session_name == "after"

    async def test_managed_session_names_includes_thread_bindings(
        self, cog: ChannelRepoCog, repo: ChannelRepository, thread_repo: ThreadRepository
    ) -> None:
        """The menu watchdog ignores sessions it does not manage (#438); a
        thread-derived session must not look like another bot's."""
        await repo.save(channel_id=60, source_repo="https://github.com/org/games.git")
        await thread_repo.save(
            thread_id=61, source_repo="https://github.com/org/monitoring.git", channel_id=60
        )
        names = await cog.managed_session_names()
        assert {"clord", "games", "monitoring"} <= names
