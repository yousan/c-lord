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

import asyncio
import contextlib
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
from ..command_gate import is_message_authorized
from ..concurrency import SessionRegistry
from ..database.repository import SessionRepository
from ..discord_ui.authorization import Authorizer
from ..discord_ui.slash_io import slash_io
from ..discord_ui.thread_dashboard import board_turn, dashboard_of
from ..thread_owner import foreign_owner_notice_for
from ..thread_settings import resolve_auto_archive_duration
from ..utils.logger import log_ctx
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
        authorizer: Authorizer | None = None,
    ) -> None:
        self.bot = bot
        self.repo = repo
        self.runner = runner
        self.claude_channel_id = claude_channel_id
        self._allowed_user_ids = allowed_user_ids
        self._allowed_role_name = allowed_role_name
        # #713: the one allowlist rule, shared with every other gate (#466's
        # DRY, finished). ``setup_bridge`` passes the instance ClaudeChatCog
        # uses, so the owner resolved at startup applies to /skill too.
        self._authorizer = authorizer or Authorizer(allowed_user_ids, allowed_role_name)
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
        self, channel_id: int, *, thread_id: int | None
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
        Delegates to :class:`Authorizer` (#713): ``/skill`` runs arbitrary
        skills, so it must not answer this question differently from the gate
        on plain messages — a second copy of the rule is a second default to
        get wrong, which is what left ``/skill`` open to everyone.
        """
        if isinstance(member, int):
            return self._authorizer.is_allowed_user_id(member)
        return self._authorizer.is_allowed(member)

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

    def _make_runner(
        self, tmux: TmuxSessionManager, thread_id: int, working_dir: str | None = None
    ) -> TmuxClaudeRunner:
        """Create a TmuxClaudeRunner from the stored config and resolved tmux manager."""
        return TmuxClaudeRunner(
            tmux_manager=tmux,
            thread_id=thread_id,
            model=self.runner.model,
            working_dir=working_dir or self.runner.working_dir,
            timeout_seconds=self.runner.timeout_seconds,
            dangerously_skip_permissions=True,
            effort=self.runner.effort,
            # #762: a /skill prompt is ``/<name> …`` — a command, not a message.
            slash_command=True,
        )

    def _is_claude_thread(self, channel: discord.abc.GuildChannel | discord.Thread) -> bool:
        """Check if the channel is a thread under the configured claude channel."""
        return isinstance(channel, discord.Thread) and channel.parent_id == self.claude_channel_id

    async def _prepare_workspace(
        self,
        *,
        thread: discord.Thread,
        tmux: TmuxSessionManager,
        sdm: SessionDirManager | None,
        recorded_dir: str | None,
        requester: discord.Member | discord.User,
    ) -> tuple[bool, str | None]:
        """Checkout + tmux window + transcript mirror for a /skill run (#762).

        ``run_claude_with_config`` types into a window it does not create, so a
        caller that skips this starts Claude at nothing — #621 (scheduler) and
        #629 (webhook) each forgot, and so did /skill: every run ended in a
        thread holding a single ❌. Same order as a chat turn: the thread's own
        checkout (the one a reply will continue in), the window in it, then the
        mirror — the only path Claude's answer takes back to Discord (#712).

        Returns ``(ready, working_dir)``. ``ready`` is False when there is no
        window to start Claude in — the caller must then stop; this has already
        said so in the log and the thread. ``working_dir`` may be ``None`` (no
        binding and no configured default), which is not a failure.
        """
        working_dir = recorded_dir if isinstance(recorded_dir, str) and recorded_dir else None
        ctx = log_ctx(thread_id=thread.id)
        if working_dir is None and sdm is not None:
            try:
                made = await asyncio.to_thread(sdm.create_session_dir, thread.id, requester)
            except Exception:
                # #477: a failed clone must end the run visibly, not escape it.
                logger.exception("%s could not prepare the checkout for /skill", ctx)
                with contextlib.suppress(discord.HTTPException):
                    await thread.send("❌ この /skill の作業ディレクトリを用意できませんでした。")
                return False, None
            working_dir = made if isinstance(made, str) and made else None
        working_dir = working_dir or self.runner.working_dir

        window = await asyncio.to_thread(tmux.create_session, thread.id, working_dir or ".")
        # create_session also returns a name when tmux itself is unavailable,
        # so only an existing window proves Claude has somewhere to start.
        if not await asyncio.to_thread(tmux.session_exists, thread.id):
            logger.error("%s could not create a tmux window for /skill — aborting the run", ctx)
            with contextlib.suppress(discord.HTTPException):
                await thread.send(
                    "❌ この /skill を動かす tmux ウィンドウを作れませんでした。"
                    "ホストで tmux が使えるか、チャンネルが `/clord-init` で repo に"
                    "紐づいているかを確認してください。"
                )
            return False, working_dir
        logger.info("%s tmux window for /skill: %s (dir=%s)", ctx, window, working_dir)

        mirror_cog = getattr(self.bot, "transcript_mirror_cog", None)
        if mirror_cog is not None and working_dir:
            try:
                mirror_cog.start_for(thread.id, working_dir)
            except Exception:
                logger.warning(
                    "%s could not start the transcript mirror (dir=%s)",
                    ctx,
                    working_dir,
                    exc_info=True,
                )
        return True, working_dir

    async def _run_skill_impl(
        self,
        *,
        channel: object,
        user: discord.Member | discord.User,
        name: str,
        args: str | None,
        respond: Responder,
        ack: Acknowledger,
        message: discord.Message | None = None,
    ) -> None:
        """Shared core for the ``/skill`` slash command and the ``!skill`` text twin.

        ``respond`` posts a message the way the caller needs (interaction
        response/followup vs ``ctx.send``); ``ack`` acknowledges a long-running
        operation (defer for the slash command, no-op for text).  Keeping the
        logic here means the slash and text entry points stay behaviourally
        identical without copy-paste (see #209).

        ``message`` is the invoking message for the text twin, ``None`` for
        slash.  When present, authorization uses the shared message-backed rule
        so a configured ``DISCORD_OWNER_ID`` does not lock out the webhooks this
        command advertises itself as supporting (#508).
        """
        authorized = (
            is_message_authorized(message, self._is_authorized)
            if message is not None
            else self._is_authorized(user)
        )
        if not authorized:
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
            # #811: another c-lord's thread — running here would take it over.
            foreign = await foreign_owner_notice_for(self.bot, self.repo, channel)
            if foreign is not None:
                await respond(foreign, ephemeral=True)
                return
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

            ready, working_dir = await self._prepare_workspace(
                thread=channel,
                tmux=tmux,
                sdm=sdm,
                recorded_dir=record.working_dir if record else None,
                requester=user,
            )
            if not ready:
                return
            runner = self._make_runner(tmux, channel.id, working_dir)
            # #754: a /skill run is on 📊 Session Status while it runs.
            async with board_turn(dashboard_of(self.bot), channel.id, prompt):
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
                        working_dir=working_dir,
                        # #739: the views this run posts (ask menu / permission /
                        # stop) are gated by this; without it they cannot see the
                        # allowlist and fall back to the process-wide one.
                        authorizer=self._authorizer,
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
        # New-thread mode: no thread exists yet (#600 audit).
        tmux = await self._resolve_tmux_manager(claude_channel.id, thread_id=None)

        # Unbound channel check
        if tmux is None:
            await respond(
                "⚠️ このチャンネルにはリポジトリが紐づけられていません。\n"
                "先に `/clord-init repo:<URL>` で設定してください。",
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

        ready, working_dir = await self._prepare_workspace(
            thread=thread, tmux=tmux, sdm=sdm, recorded_dir=None, requester=user
        )
        if not ready:
            return
        runner = self._make_runner(tmux, thread.id, working_dir)
        # #754: see above.
        async with board_turn(dashboard_of(self.bot), thread.id, prompt):
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
                    working_dir=working_dir,
                    # #739: see above — the run's buttons are gated by this.
                    authorizer=self._authorizer,
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
        # Before defer, validation errors go on the initial response (instant,
        # ephemeral). After defer, everything goes through followup — and an
        # ephemeral one stays ephemeral (#748, see slash_io).
        respond, ack = slash_io(interaction)

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
            message=ctx.message,
        )
