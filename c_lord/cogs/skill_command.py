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
from pathlib import Path
from typing import TYPE_CHECKING

import discord
from discord import app_commands
from discord.ext import commands

from ..claude.runner import ClaudeRunner
from ..concurrency import SessionRegistry
from ..database.repository import SessionRepository
from ._run_helper import run_claude_with_config
from .run_config import RunConfig

if TYPE_CHECKING:
    from ..session_dir import SessionDirManager
    from ..tmux import TmuxSessionManager

logger = logging.getLogger(__name__)

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
        runner: ClaudeRunner,
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

    async def _resolve_session_dir_manager(self, channel_id: int) -> SessionDirManager | None:
        """Resolve a SessionDirManager for the given channel via ChannelRepoCog."""
        from .channel_repo import ChannelRepoCog

        channel_cog = self.bot.get_cog("ChannelRepoCog")
        if channel_cog is not None and isinstance(channel_cog, ChannelRepoCog):
            return await channel_cog.resolve_manager(channel_id)
        return None

    async def _resolve_tmux_manager(self, channel_id: int) -> TmuxSessionManager | None:
        """Resolve a TmuxSessionManager for the given channel via ChannelRepoCog."""
        from .channel_repo import ChannelRepoCog

        channel_cog = self.bot.get_cog("ChannelRepoCog")
        if channel_cog is not None and isinstance(channel_cog, ChannelRepoCog):
            return await channel_cog.resolve_tmux_manager(channel_id)
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

    def _is_claude_thread(self, channel: discord.abc.GuildChannel | discord.Thread) -> bool:
        """Check if the channel is a thread under the configured claude channel."""
        return isinstance(channel, discord.Thread) and channel.parent_id == self.claude_channel_id

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
        if not self._is_authorized(interaction.user):
            await interaction.response.send_message(
                "You don't have permission to use this command.", ephemeral=True
            )
            return

        # Validate skill name — only alphanumeric, hyphens, underscores
        if not re.match(r"^[\w-]+$", name):
            await interaction.response.send_message(f"Invalid skill name: `{name}`", ephemeral=True)
            return

        # Lazy reload before matching
        self._maybe_reload_skills()

        matched = next((s for s in self._skills if s["name"] == name), None)
        if not matched:
            await interaction.response.send_message(
                f"Skill `{name}` not found. Use `/skill` with autocomplete.",
                ephemeral=True,
            )
            return

        # Build the prompt: /name [args]
        prompt = f"/{name}"
        if args:
            prompt = f"/{name} {args}"

        await interaction.response.defer()

        # In-thread mode: if invoked inside a thread under the claude channel, resume it
        channel = interaction.channel
        if isinstance(channel, discord.Thread) and self._is_claude_thread(channel):
            parent_channel_id = channel.parent_id or self.claude_channel_id
            sdm = await self._resolve_session_dir_manager(parent_channel_id)
            tmux = await self._resolve_tmux_manager(parent_channel_id)

            session_id = None
            record = await self.repo.get(channel.id)
            if record:
                session_id = record.session_id

            display = f"`/{name} {args}`" if args else f"`/{name}`"
            await interaction.followup.send(f"Running {display} in this thread…")

            runner = self.runner.clone()
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
                )
            )
            return

        # New-thread mode: create a thread in the claude channel
        channel = self.bot.get_channel(self.claude_channel_id)
        if not isinstance(channel, discord.TextChannel):
            await interaction.followup.send("Claude channel not found.", ephemeral=True)
            return

        # Resolve per-channel managers
        sdm = await self._resolve_session_dir_manager(channel.id)
        tmux = await self._resolve_tmux_manager(channel.id)

        # Unbound channel check
        if sdm is None and tmux is None:
            await interaction.followup.send(
                "⚠️ このチャンネルにはリポジトリが紐づけられていません。\n"
                "先に `/clord-init repo:<URL> branch:<branch>` で設定してください。",
                ephemeral=True,
            )
            return

        thread_name = f"/{name} {args}" if args else f"/{name}"
        # Discord thread names are max 100 chars
        thread = await channel.create_thread(
            name=thread_name[:100],
            type=discord.ChannelType.public_thread,
        )

        display = f"`/{name} {args}`" if args else f"`/{name}`"
        await interaction.followup.send(f"Running {display} → {thread.mention}")

        runner = self.runner.clone()
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
            )
        )
