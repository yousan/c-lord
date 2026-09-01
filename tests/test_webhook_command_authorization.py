"""Webhook-invokable text commands must not be gated by the human allowlist (#508).

#507 fixed this for ``!clord`` / ``!attach``.  The same defect stayed in the
three commands whose shared implementation gates on ``_is_allowed(ctx.author)``
— the webhook pseudo-user can never match ``allowed_user_ids={owner}``, so
setting ``DISCORD_OWNER_ID`` locked webhooks out of ``!skill``,
``!clord-init`` and ``!clord-thread-init`` while their docstrings still
promised "webhook-invokable for E2E (#209)".

Slash commands keep the human allowlist: they carry no message and can never be
webhook-driven.
"""

from __future__ import annotations

from typing import Any
from unittest.mock import AsyncMock, MagicMock

import discord
import pytest

from c_lord.cogs.channel_repo import ChannelRepoCog
from c_lord.cogs.skill_command import SkillCommandCog
from c_lord.database.channel_repo import ChannelRepository
from c_lord.database.repository import SessionRecord
from c_lord.database.thread_repo import ThreadRepository

OWNER = 499163459418587176
STRANGER = 12345
WEBHOOK_AUTHOR = 987654321


def _webhook_message() -> MagicMock:
    """A webhook message: bot-authored, ``webhook_id`` set, id in no allowlist."""
    msg = MagicMock(spec=discord.Message)
    msg.webhook_id = 555
    msg.author = MagicMock()
    msg.author.bot = True
    msg.author.id = WEBHOOK_AUTHOR
    return msg


def _human_message(user_id: int) -> MagicMock:
    msg = MagicMock(spec=discord.Message)
    msg.webhook_id = None
    msg.author = MagicMock(spec=discord.Member)
    msg.author.bot = False
    msg.author.id = user_id
    msg.author.roles = []
    return msg


def _recorder() -> tuple[list[str], Any]:
    said: list[str] = []

    async def respond(content: str | None = None, **_kwargs: object) -> None:
        said.append(content or "")

    return said, respond


def _session_record() -> SessionRecord:
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


def _bot() -> MagicMock:
    bot = MagicMock()
    bot.loop = MagicMock()
    bot.get_cog = MagicMock(return_value=None)
    bot.session_repo = MagicMock()
    bot.session_repo.get = AsyncMock(return_value=_session_record())
    bot.settings_repo = None
    return bot


# ── !clord-init / !clord-thread-init ──────────────────────────────────────


@pytest.fixture
async def channel_cog(tmp_path) -> ChannelRepoCog:
    repo = ChannelRepository(str(tmp_path / "channel.db"))
    await repo.init_db()
    thread_repo = ThreadRepository(str(tmp_path / "thread.db"))
    await thread_repo.init_db()
    return ChannelRepoCog(
        _bot(),
        repo=repo,
        thread_repo=thread_repo,
        allowed_user_ids={OWNER},  # DISCORD_OWNER_ID configured — the #508 setup
        session_dir_base=str(tmp_path / "sessions"),
    )


class TestClordInit:
    @pytest.mark.asyncio
    async def test_webhook_is_not_rejected(self, channel_cog: ChannelRepoCog) -> None:
        """#508 AC1."""
        said, respond = _recorder()
        await channel_cog._clord_init_impl(
            channel_id=42,
            user=MagicMock(),
            repo="https://github.com/yousan/c-lord",
            remove=False,
            respond=respond,
            message=_webhook_message(),
        )
        assert not any("not authorized" in s for s in said), said
        assert await channel_cog._repo.get(42) is not None

    @pytest.mark.asyncio
    async def test_non_owner_human_is_still_rejected(self, channel_cog: ChannelRepoCog) -> None:
        """#508 AC5 — the human rule is untouched."""
        said, respond = _recorder()
        await channel_cog._clord_init_impl(
            channel_id=42,
            user=MagicMock(id=STRANGER),
            repo="https://github.com/yousan/c-lord",
            remove=False,
            respond=respond,
            message=_human_message(STRANGER),
        )
        assert any("not authorized" in s for s in said), said
        assert await channel_cog._repo.get(42) is None

    @pytest.mark.asyncio
    async def test_slash_still_uses_the_human_allowlist(self, channel_cog: ChannelRepoCog) -> None:
        """#508 AC5 — no message means slash, which never comes from a webhook."""
        said, respond = _recorder()
        await channel_cog._clord_init_impl(
            channel_id=42,
            user=MagicMock(id=STRANGER),
            repo="https://github.com/yousan/c-lord",
            remove=False,
            respond=respond,
        )
        assert any("not authorized" in s for s in said), said


class TestClordThreadInit:
    @pytest.mark.asyncio
    async def test_webhook_is_not_rejected(self, channel_cog: ChannelRepoCog) -> None:
        """#508 AC2."""
        said, respond = _recorder()
        thread = MagicMock(spec=discord.Thread)
        thread.id = 77
        thread.parent_id = 42
        await channel_cog._clord_thread_init_impl(
            thread_id=77,
            channel=thread,
            client=MagicMock(),
            user=MagicMock(),
            repo="https://github.com/yousan/c-lord",
            remove=False,
            respond=respond,
            message=_webhook_message(),
        )
        assert not any("not authorized" in s for s in said), said

    @pytest.mark.asyncio
    async def test_non_owner_human_is_still_rejected(self, channel_cog: ChannelRepoCog) -> None:
        said, respond = _recorder()
        thread = MagicMock(spec=discord.Thread)
        thread.id = 77
        thread.parent_id = 42
        await channel_cog._clord_thread_init_impl(
            thread_id=77,
            channel=thread,
            client=MagicMock(),
            user=MagicMock(id=STRANGER),
            repo="https://github.com/yousan/c-lord",
            remove=False,
            respond=respond,
            message=_human_message(STRANGER),
        )
        assert any("not authorized" in s for s in said), said


# ── !skill ────────────────────────────────────────────────────────────────


def _skill_cog() -> SkillCommandCog:
    repo = MagicMock()
    repo.get = AsyncMock(return_value=None)
    repo.save = AsyncMock()
    runner = MagicMock()
    runner.model = "sonnet"
    runner.working_dir = None
    runner.timeout_seconds = 300
    return SkillCommandCog(
        bot=_bot(),
        repo=repo,
        runner=runner,
        claude_channel_id=999,
        skills_dir="/nonexistent/skills",
        allowed_user_ids={OWNER},
    )


class TestSkill:
    @pytest.mark.asyncio
    async def test_webhook_is_not_rejected(self) -> None:
        """#508 AC3 — it gets past authorization; "not found" is the next step."""
        said, respond = _recorder()
        await _skill_cog()._run_skill_impl(
            channel=MagicMock(),
            user=MagicMock(),
            name="whatever",
            args=None,
            respond=respond,
            ack=AsyncMock(),
            message=_webhook_message(),
        )
        assert not any("permission" in s for s in said), said

    @pytest.mark.asyncio
    async def test_non_owner_human_is_still_rejected(self) -> None:
        said, respond = _recorder()
        await _skill_cog()._run_skill_impl(
            channel=MagicMock(),
            user=MagicMock(id=STRANGER),
            name="whatever",
            args=None,
            respond=respond,
            ack=AsyncMock(),
            message=_human_message(STRANGER),
        )
        assert any("permission" in s for s in said), said

    @pytest.mark.asyncio
    async def test_slash_still_uses_the_human_allowlist(self) -> None:
        said, respond = _recorder()
        await _skill_cog()._run_skill_impl(
            channel=MagicMock(),
            user=MagicMock(id=STRANGER),
            name="whatever",
            args=None,
            respond=respond,
            ack=AsyncMock(),
        )
        assert any("permission" in s for s in said), said


class TestUntrustedBot:
    """#508 AC4 — a bot that is not on the trusted list stays out."""

    @staticmethod
    def _bot_message() -> MagicMock:
        msg = MagicMock(spec=discord.Message)
        msg.webhook_id = None
        msg.author = MagicMock()
        msg.author.bot = True
        msg.author.id = 424242
        return msg

    @pytest.mark.asyncio
    async def test_clord_init_rejects_it(self, channel_cog: ChannelRepoCog) -> None:
        said, respond = _recorder()
        await channel_cog._clord_init_impl(
            channel_id=42,
            user=MagicMock(),
            repo="https://github.com/yousan/c-lord",
            remove=False,
            respond=respond,
            message=self._bot_message(),
        )
        assert any("not authorized" in s for s in said), said

    @pytest.mark.asyncio
    async def test_skill_rejects_it(self) -> None:
        said, respond = _recorder()
        await _skill_cog()._run_skill_impl(
            channel=MagicMock(),
            user=MagicMock(),
            name="whatever",
            args=None,
            respond=respond,
            ack=AsyncMock(),
            message=self._bot_message(),
        )
        assert any("permission" in s for s in said), said
