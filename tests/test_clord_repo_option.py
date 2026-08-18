"""#514: ``/clord repo:<URL> prompt:<...>`` — start a thread on a chosen repo.

Before this the repo came from the channel binding only, so working on another
repo meant: create a thread by hand → ``/clord-thread-init`` → ``/clord``. The
option writes the thread binding *before* the session dir is cloned, which is
the one thing the three-step dance was buying.
"""

from __future__ import annotations

import asyncio
from unittest.mock import AsyncMock, MagicMock, patch

import discord
import pytest

from c_lord.cogs.channel_repo import ChannelRepoCog
from c_lord.cogs.claude_chat import ClaudeChatCog
from c_lord.database.channel_repo import ChannelRepository
from c_lord.database.thread_repo import ThreadRepository

REPO_B = "git@github.com:yousan/dotclaude.git"


@pytest.fixture
async def channel_repo(tmp_path) -> ChannelRepository:
    r = ChannelRepository(str(tmp_path / "channel.db"))
    await r.init_db()
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
    return ChannelRepoCog(
        bot,
        repo=channel_repo,
        thread_repo=thread_repo,
        allowed_user_ids=None,
        session_dir_base=str(tmp_path / "sessions"),
    )


def _make_cog(channel_cog: ChannelRepoCog | None = None) -> ClaudeChatCog:
    bot = MagicMock()
    bot.channel_id = 999
    bot.settings_repo = None
    bot.get_cog = MagicMock(return_value=channel_cog)
    repo = MagicMock()
    repo.get = AsyncMock(return_value=None)
    repo.save = AsyncMock()
    runner = MagicMock()
    runner.clone = MagicMock(return_value=MagicMock())
    return ClaudeChatCog(bot=bot, repo=repo, runner=runner)


def _make_text_channel(channel_id: int = 500) -> MagicMock:
    channel = MagicMock(spec=discord.TextChannel)
    channel.id = channel_id
    thread = MagicMock(spec=discord.Thread)
    thread.id = 501
    thread.parent_id = channel_id
    thread.send = AsyncMock(return_value=MagicMock())
    channel.create_thread = AsyncMock(return_value=thread)
    return channel


class TestSpawnSessionRepoOption:
    async def test_binds_the_thread_to_the_given_repo(self, channel_cog, thread_repo) -> None:
        cog = _make_cog(channel_cog)
        channel = _make_text_channel()
        cog._run_claude = AsyncMock()  # type: ignore[method-assign]

        thread = await cog.spawn_session(channel, "やること", repo=REPO_B)

        binding = await thread_repo.get(thread.id)
        assert binding is not None
        assert binding["source_repo"] == REPO_B
        assert binding["channel_id"] == channel.id

    async def test_binding_is_written_before_claude_runs(self, channel_cog, thread_repo) -> None:
        """The clone happens inside _run_claude — a binding written after it
        would land too late and the thread would get the channel's repo."""
        cog = _make_cog(channel_cog)
        channel = _make_text_channel()
        seen: dict = {}

        async def fake_run(*_a, **_kw):
            seen["binding"] = await thread_repo.get(501)

        cog._run_claude = fake_run  # type: ignore[method-assign]
        await cog.spawn_session(channel, "やること", repo=REPO_B)
        # spawn_session fires _run_claude as a background task; drain it rather
        # than sleeping a guessed amount.
        pending = [t for t in asyncio.all_tasks() if t is not asyncio.current_task()]
        await asyncio.gather(*pending)

        assert seen.get("binding") is not None, "binding must exist by the time Claude starts"
        assert seen["binding"]["source_repo"] == REPO_B

    async def test_normalizes_a_derived_url(self, channel_cog, thread_repo) -> None:
        cog = _make_cog(channel_cog)
        channel = _make_text_channel()
        cog._run_claude = AsyncMock()  # type: ignore[method-assign]

        thread = await cog.spawn_session(
            channel, "やること", repo="https://github.com/yousan/dotclaude/pull/12"
        )

        binding = await thread_repo.get(thread.id)
        assert binding["source_repo"] == "https://github.com/yousan/dotclaude.git"

    async def test_without_repo_no_thread_binding_is_created(self, channel_cog, thread_repo) -> None:
        cog = _make_cog(channel_cog)
        channel = _make_text_channel()
        cog._run_claude = AsyncMock()  # type: ignore[method-assign]

        thread = await cog.spawn_session(channel, "やること")

        assert await thread_repo.get(thread.id) is None


class TestClordImplRepoOption:
    async def test_repo_lets_an_unbound_channel_start_a_thread(self, channel_cog) -> None:
        """AC3: no /clord-init needed when the repo is named explicitly."""
        cog = _make_cog(channel_cog)
        channel = _make_text_channel()
        cog.spawn_session = AsyncMock(return_value=channel.create_thread.return_value)  # type: ignore[method-assign]
        respond = AsyncMock()

        await cog._clord_impl(
            channel=channel,
            channel_id_fallback=channel.id,
            user=MagicMock(),
            prompt="やること",
            repo=REPO_B,
            respond=respond,
            ack=AsyncMock(),
        )

        cog.spawn_session.assert_awaited_once()
        assert cog.spawn_session.await_args.kwargs["repo"] == REPO_B
        said = " ".join(str(c.args[0]) for c in respond.await_args_list if c.args)
        assert "リポジトリが紐づけられていません" not in said

    async def test_unbound_channel_without_repo_still_refuses(self, channel_cog) -> None:
        cog = _make_cog(channel_cog)
        channel = _make_text_channel()
        cog.spawn_session = AsyncMock()  # type: ignore[method-assign]
        respond = AsyncMock()

        await cog._clord_impl(
            channel=channel,
            channel_id_fallback=channel.id,
            user=MagicMock(),
            prompt="やること",
            repo=None,
            respond=respond,
            ack=AsyncMock(),
        )

        cog.spawn_session.assert_not_awaited()
        said = " ".join(str(c.args[0]) for c in respond.await_args_list if c.args)
        assert "リポジトリが紐づけられていません" in said

    async def test_repo_inside_a_thread_is_refused_with_guidance(self, channel_cog) -> None:
        """AC5: the session dir is already cloned, so a rebind here would look
        like it did nothing. Point at the command that actually rebinds."""
        cog = _make_cog(channel_cog)
        thread = MagicMock(spec=discord.Thread)
        thread.id = 601
        thread.parent_id = 500
        thread.send = AsyncMock()
        cog._run_claude = AsyncMock()  # type: ignore[method-assign]
        respond = AsyncMock()

        await cog._clord_impl(
            channel=thread,
            channel_id_fallback=None,
            user=MagicMock(),
            prompt="やること",
            repo=REPO_B,
            respond=respond,
            ack=AsyncMock(),
        )

        cog._run_claude.assert_not_awaited()
        said = " ".join(str(c.args[0]) for c in respond.await_args_list if c.args)
        assert "/clord-thread-init" in said


class TestClordTextTwinRepoOption:
    async def test_parses_a_leading_repo_token(self, channel_cog) -> None:
        cog = _make_cog(channel_cog)
        ctx = MagicMock()
        ctx.channel = _make_text_channel()
        ctx.author = MagicMock()
        ctx.send = AsyncMock()
        cog._clord_impl = AsyncMock()  # type: ignore[method-assign]

        await cog.clord_text.callback(cog, ctx, prompt=f"repo:{REPO_B} Claude 5 系に対応する")

        kwargs = cog._clord_impl.await_args.kwargs
        assert kwargs["repo"] == REPO_B
        assert kwargs["prompt"] == "Claude 5 系に対応する"

    async def test_plain_prompt_is_unchanged(self, channel_cog) -> None:
        cog = _make_cog(channel_cog)
        ctx = MagicMock()
        ctx.channel = _make_text_channel()
        ctx.author = MagicMock()
        ctx.send = AsyncMock()
        cog._clord_impl = AsyncMock()  # type: ignore[method-assign]

        await cog.clord_text.callback(cog, ctx, prompt="repository の話をしたい")

        kwargs = cog._clord_impl.await_args.kwargs
        assert kwargs["repo"] is None
        assert kwargs["prompt"] == "repository の話をしたい"


# ===========================================================================
# #514 security: repo: is the first user-supplied string to reach `git clone`
# ===========================================================================


class TestRepoUrlValidation:
    """``/clord`` is gated by ``_is_allowed`` only — unlike ``/clord-init``, it
    carries no ``manage_guild`` requirement. So ``repo:`` puts an end-user
    string in front of ``git clone``, which runs *before* Claude starts and
    therefore outside the tool-permission model entirely."""

    @pytest.mark.parametrize(
        "hostile",
        [
            "ext::sh -c 'touch /tmp/pwned'",  # git's ext:: transport runs a command
            "EXT::sh -c id",  # …and it is case-insensitive
            "--upload-pack=touch /tmp/pwned",  # argv injection via a flag-shaped URL
            "-u./evil",
            "   ",
        ],
    )
    def test_rejects_repo_urls_that_execute_commands(self, hostile: str) -> None:
        from c_lord.database.channel_repo import validate_repo_url

        with pytest.raises(ValueError):
            validate_repo_url(hostile)

    @pytest.mark.parametrize(
        "ok",
        [
            "git@github.com:yousan/dotclaude.git",
            "https://github.com/yousan/dotclaude.git",
            "https://github.com/yousan/dotclaude/pull/12",
            "/home/yousan/repos/local-thing",
            "ssh://git@example.com/x/y.git",
        ],
    )
    def test_accepts_the_forms_people_actually_paste(self, ok: str) -> None:
        from c_lord.database.channel_repo import validate_repo_url

        assert validate_repo_url(ok)

    async def test_spawn_session_refuses_a_hostile_repo(self, channel_cog, thread_repo) -> None:
        cog = _make_cog(channel_cog)
        channel = _make_text_channel()
        cog._run_claude = AsyncMock()  # type: ignore[method-assign]
        respond = AsyncMock()

        await cog._clord_impl(
            channel=channel,
            channel_id_fallback=channel.id,
            user=MagicMock(),
            prompt="やること",
            repo="ext::sh -c id",
            respond=respond,
            ack=AsyncMock(),
        )

        assert await thread_repo.get(501) is None
        said = " ".join(str(c.args[0]) for c in respond.await_args_list if c.args)
        assert "リポジトリ" in said


class TestCloneArgvHardening:
    def test_clone_puts_a_double_dash_before_the_url(self, tmp_path) -> None:
        """Without ``--`` a flag-shaped source_repo is read by git as an option."""
        from c_lord.session_dir import SessionDirManager

        mgr = SessionDirManager(base_dir=str(tmp_path / "s"), source_repo="https://x/y.git")
        with patch("c_lord.session_dir._run") as run:
            run.return_value = MagicMock(returncode=0, stderr="")
            mgr.create_session_dir(4242)

        args = run.call_args.args[0]
        assert "--" in args
        assert args.index("--") < args.index("https://x/y.git")
