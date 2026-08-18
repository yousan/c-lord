"""Skill command Cog.

Provides a /skill slash command with autocomplete that lists all available
Claude Code skills from ~/.claude/skills/ and executes the selected one.

Usage:
    /skill [name: goodmorning]                → runs /goodmorning in Claude Code
    /skill [name: todoist] [args: filter "today"]  → runs /todoist filter "today"

When used inside an existing thread (under the claude channel), the skill
resumes the thread's session instead of creating a new thread.

Skills are lazily reloaded every 60 seconds so new skills appear without restart.
"""

from __future__ import annotations

import logging
import re
import time
from collections.abc import Awaitable, Callable
from pathlib import Path
from typing import TYPE_CHECKING

import discord
from discord import app_commands
from discord.ext import commands

from ..claude.config import ClaudeConfig
from ..claude.tmux_runner import TmuxClaudeRunner
from ..concurrency import SessionRegistry
from ..database.repository import SessionRepository
from ..thread_settings import resolve_auto_archive_duration
from ._run_helper import run_claude_with_config
from .run_config import RunConfig

if TYPE_CHECKING:
    from ..session_dir import SessionDirManager
    from ..tmux import TmuxSessionManager

logger = logging.getLogger(__name__)

# Callbacks the shared skill core uses to stay agnostic of slash vs text entry.
Responder = Callable[..., Awaitable[None]]
Acknowledger = Callable[[], Awaitable[None]]

# YAML frontmatter pattern to extract name/description from SKILL.md
_FRONTMATTER_RE = re.compile(r"^---[ \t]*\n(?P<body>.*?)^---", re.DOTALL | re.MULTILINE)
_FIELD_RE = re.compile(r"^(?P<key>\w[\w-]*):\s*(?P<value>.+)$", re.MULTILINE)

# How often to re-scan the skills directory (seconds)
SKILL_RELOAD_INTERVAL = 60.0


def _parse_skill_meta(skill_dir: Path) -> dict[str, str] | None:
    """Read SKILL.md frontmatter and return {name, description} or None."""
    skill_md = skill_dir / "SKILL.md"
    if not skill_md.exists():
        return None
    try:
        text = skill_md.read_text(encoding="utf-8")
        m = _FRONTMATTER_RE.match(text)
        if not m:
            return None
        fields = dict(_FIELD_RE.findall(m.group("body")))
        name = fields.get("name", skill_dir.name).strip()
        description = fields.get("description", "").strip()
        return {"name": name, "description": description}
    except OSError:
        logger.warning("Failed to read %s", skill_md)
        return None


def _load_skills(skills_dir: Path) -> list[dict[str, str]]:
    """Scan skills_dir and return sorted list of {name, description}."""
    skills: list[dict[str, str]] = []
    if not skills_dir.is_dir():
        logger.warning("Skills directory not found: %s", skills_dir)
        return skills

    for entry in sorted(skills_dir.iterdir()):
        if not entry.is_dir():
            continue
        meta = _parse_skill_meta(entry)
        if meta:
            skills.append(meta)

    logger.info("Loaded %d skills from %s", len(skills), skills_dir)
    return skills


class SkillCommandCog(commands.Cog):
    """Cog that exposes Claude Code skills as a /skill slash command."""

    def __init__(
        self,
        bot: commands.Bot,
        repo: SessionRepository,
        runner: ClaudeConfig,
        claude_channel_id: int,
        skills_dir: Path | str | None = None,
        allowed_user_ids: set[int] | None = None,
        registry: SessionRegistry | None = None,
        allowed_role_name: str | None = None,
    ) -> None:
        self.bot = bot
        self.repo = repo
        self.runner = runner
        self.claude_channel_id = claude_channel_id
        self._allowed_user_ids = allowed_user_ids
        self._allowed_role_name = allowed_role_name
        self._registry = registry or getattr(bot, "session_registry", None)

        # Default to ~/.claude/skills/
        if skills_dir is None:
            skills_dir = Path.home() / ".claude" / "skills"
        self._skills_dir = Path(skills_dir)
        self._skills = _load_skills(self._skills_dir)
        self._last_loaded: float = time.monotonic()

    async def _resolve_session_dir_manager(
        self, channel_id: int, thread_id: int | None = None
    ) -> SessionDirManager | None:
        """Resolve a SessionDirManager for the given channel via ChannelRepoCog."""
        from .channel_repo import ChannelRepoCog

        channel_cog = self.bot.get_cog("ChannelRepoCog")
        if channel_cog is not None and isinstance(channel_cog, ChannelRepoCog):
            return await channel_cog.resolve_manager(channel_id, thread_id=thread_id)
        return None

    async def _resolve_tmux_manager(
        self, channel_id: int, thread_id: int | None = None
    ) -> TmuxSessionManager | None:
        """Resolve a TmuxSessionManager for the given channel via ChannelRepoCog.

        #427: pass ``thread_id`` whenever a thread is in scope so a
        ``/clord-thread-init`` thread lands in its own repo's tmux session
        rather than the parent channel's.
        """
        from .channel_repo import ChannelRepoCog

        channel_cog = self.bot.get_cog("ChannelRepoCog")
        if channel_cog is not None and isinstance(channel_cog, ChannelRepoCog):
            return await channel_cog.resolve_tmux_manager(channel_id, thread_id=thread_id)
        return None

    def _maybe_reload_skills(self) -> None:
        """Reload skills from disk if SKILL_RELOAD_INTERVAL has elapsed."""
        now = time.monotonic()
        if now - self._last_loaded >= SKILL_RELOAD_INTERVAL:
            self._skills = _load_skills(self._skills_dir)
            self._last_loaded = now

    def _is_authorized(self, member: discord.Member | discord.User | int) -> bool:
        """Check if a member/user is authorized.

        Accepts a Member, User, or bare int (user ID) for backward compatibility.
        """
        if isinstance(member, int):
            # Legacy call-site: bare user ID — cannot check roles
            user_id = member
            if self._allowed_user_ids is not None and user_id in self._allowed_user_ids:
                return True
            # Cannot check roles with a bare int
            return self._allowed_user_ids is None and self._allowed_role_name is None

        if self._allowed_user_ids is not None and member.id in self._allowed_user_ids:
            return True
        if self._allowed_role_name is not None:
            if isinstance(member, discord.Member):
                return any(r.name == self._allowed_role_name for r in member.roles)
            return False  # DM — no role info
        return self._allowed_user_ids is None

    async def _skill_name_autocomplete(
        self,
        interaction: discord.Interaction,
        current: str,
    ) -> list[app_commands.Choice[str]]:
        """Return up to 25 matching skill names for autocomplete."""
        self._maybe_reload_skills()

        current_lower = current.lower()
        matches = [
            s
            for s in self._skills
            if current_lower in s["name"].lower() or current_lower in s["description"].lower()
        ]
        choices = []
        for s in matches[:25]:
            label = s["name"]
            if s["description"]:
                short_desc = s["description"][:60]
                if len(s["description"]) > 60:
                    short_desc += "…"
                label = f"{s['name']} — {short_desc}"
            choices.append(app_commands.Choice(name=label[:100], value=s["name"]))
        return choices

    def _make_runner(self, tmux: TmuxSessionManager, thread_id: int) -> TmuxClaudeRunner:
        """Create a TmuxClaudeRunner from the stored config and resolved tmux manager."""
        return TmuxClaudeRunner(
            tmux_manager=tmux,
            thread_id=thread_id,
            model=self.runner.model,
            working_dir=self.runner.working_dir,
            timeout_seconds=self.runner.timeout_seconds,
            dangerously_skip_permissions=True,
            effort=self.runner.effort,
        )

    def _is_claude_thread(self, channel: discord.abc.GuildChannel | discord.Thread) -> bool:
        """Check if the channel is a thread under the configured claude channel."""
        return isinstance(channel, discord.Thread) and channel.parent_id == self.claude_channel_id

    async def _run_skill_impl(
        self,
        *,
        channel: object,
        user: discord.Member | discord.User,
        name: str,
        args: str | None,
        respond: Responder,
        ack: Acknowledger,
    ) -> None:
        """Shared core for the ``/skill`` slash command and the ``!skill`` text twin.

        ``respond`` posts a message the way the caller needs (interaction
        response/followup vs ``ctx.send``); ``ack`` acknowledges a long-running
        operation (defer for the slash command, no-op for text).  Keeping the
        logic here means the slash and text entry points stay behaviourally
        identical without copy-paste (see #209).
        """
        if not self._is_authorized(user):
            await respond("You don't have permission to use this command.", ephemeral=True)
            return

        # Validate skill name — only alphanumeric, hyphens, underscores
        if not re.match(r"^[\w-]+$", name):
            await respond(f"Invalid skill name: `{name}`", ephemeral=True)
            return

        # Lazy reload before matching
        self._maybe_reload_skills()

        matched = next((s for s in self._skills if s["name"] == name), None)
        if not matched:
            await respond(
                f"Skill `{name}` not found. Use `/skill` with autocomplete.",
                ephemeral=True,
            )
            return

        # Build the prompt: /name [args]
        prompt = f"/{name} {args}" if args else f"/{name}"

        await ack()

        # In-thread mode: if invoked inside a thread under the claude channel, resume it
        if isinstance(channel, discord.Thread) and self._is_claude_thread(channel):
            parent_channel_id = channel.parent_id or self.claude_channel_id
            sdm = await self._resolve_session_dir_manager(parent_channel_id, thread_id=channel.id)
            tmux = await self._resolve_tmux_manager(parent_channel_id, thread_id=channel.id)

            if tmux is None:
                await respond("⚠️ tmux is not configured for this channel.", ephemeral=True)
                return

            session_id = None
            record = await self.repo.get(channel.id)
            if record:
                session_id = record.session_id

            display = f"`/{name} {args}`" if args else f"`/{name}`"
            await respond(f"Running {display} in this thread…")

            runner = self._make_runner(tmux, channel.id)
            await run_claude_with_config(
                RunConfig(
                    thread=channel,
                    runner=runner,
                    repo=self.repo,
                    prompt=prompt,
                    session_id=session_id,
                    registry=self._registry,
                    session_dir_manager=sdm,
                    tmux_manager=tmux,
                    # #480: ping the invoking user if a question-mode pause blocks the skill.
                    notify_user_id=user.id,
                )
            )
            return

        # New-thread mode: create a thread in the claude channel
        claude_channel = self.bot.get_channel(self.claude_channel_id)
        if not isinstance(claude_channel, discord.TextChannel):
            await respond("Claude channel not found.", ephemeral=True)
            return

        # Resolve per-channel managers
        sdm = await self._resolve_session_dir_manager(claude_channel.id)
        tmux = await self._resolve_tmux_manager(claude_channel.id)

        # Unbound channel check
        if tmux is None:
            await respond(
                "⚠️ このチャンネルにはリポジトリが紐づけられていません。\n"
                "先に `/clord-init repo:<URL> branch:<branch>` で設定してください。",
                ephemeral=True,
            )
            return

        thread_name = f"/{name} {args}" if args else f"/{name}"
        # Discord thread names are max 100 chars
        archive_minutes = await resolve_auto_archive_duration(
            getattr(self.bot, "settings_repo", None)
        )
        thread = await claude_channel.create_thread(
            name=thread_name[:100],
            type=discord.ChannelType.public_thread,
            auto_archive_duration=archive_minutes,
        )

        display = f"`/{name} {args}`" if args else f"`/{name}`"
        await respond(f"Running {display} → {thread.mention}")

        runner = self._make_runner(tmux, thread.id)
        await run_claude_with_config(
            RunConfig(
                thread=thread,
                runner=runner,
                repo=self.repo,
                prompt=prompt,
                session_id=None,
                registry=self._registry,
                session_dir_manager=sdm,
                tmux_manager=tmux,
                # #480: ping the invoking user if a question-mode pause blocks the skill.
                notify_user_id=user.id,
            )
        )

    @app_commands.command(name="skill", description="Run a Claude Code skill")
    @app_commands.describe(
        name="Skill name (type to filter)",
        args="Optional arguments to pass to the skill",
    )
    @app_commands.autocomplete(name=_skill_name_autocomplete)
    async def run_skill(
        self,
        interaction: discord.Interaction,
        name: str,
        args: str | None = None,
    ) -> None:
        """Run a Claude Code skill by name, optionally with arguments."""
        state = {"acked": False}

        async def ack() -> None:
            state["acked"] = True
            await interaction.response.defer()

        async def respond(
            content: str | None = None,
            *,
            embed: discord.Embed | None = None,
            ephemeral: bool = False,
        ) -> None:
            # Before defer, validation errors go on the initial response (instant,
            # ephemeral).  After defer, everything goes through followup.
            if state["acked"]:
                if embed is not None:
                    await interaction.followup.send(content or "", embed=embed, ephemeral=ephemeral)
                else:
                    await interaction.followup.send(content or "", ephemeral=ephemeral)
            elif embed is not None:
                await interaction.response.send_message(content, embed=embed, ephemeral=ephemeral)
            else:
                await interaction.response.send_message(content, ephemeral=ephemeral)

        await self._run_skill_impl(
            channel=interaction.channel,
            user=interaction.user,
            name=name,
            args=args,
            respond=respond,
            ack=ack,
        )

    @commands.command(name="skill")
    async def run_skill_text(
        self,
        ctx: commands.Context,
        name: str | None = None,
        *,
        args: str | None = None,
    ) -> None:
        """Text/mention twin of ``/skill`` — invokable from webhooks for E2E (#209).

        Usage: ``!skill <name> [args]`` or ``@bot skill <name> [args]``.
        """
        if not name:
            await ctx.send("Usage: `!skill <name> [args]`")
            return

        async def ack() -> None:
            return None

        async def respond(
            content: str | None = None,
            *,
            embed: discord.Embed | None = None,
            ephemeral: bool = False,
        ) -> None:
            # Text channels/threads can't be ephemeral — ``ephemeral`` is ignored.
            if embed is not None:
                await ctx.send(content or "", embed=embed)
            else:
                await ctx.send(content or "")

        await self._run_skill_impl(
            channel=ctx.channel,
            user=ctx.author,
            name=name,
            args=args,
            respond=respond,
            ack=ack,
        )
