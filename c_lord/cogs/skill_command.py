"""``/skill`` (and its text twin ``!skill``): run a Claude Code skill by name.

The skills are whatever lives under ``~/.claude/skills/``. The slash command
offers them with autocomplete and has Claude type ``/<name> <args>``:

    /skill name:goodmorning                  →  /goodmorning
    /skill name:todoist args:filter "today"  →  /todoist filter "today"

Used inside a thread of the Claude channel, the skill runs in that thread's
session; anywhere else, a new thread is opened for it. The list of skills is
re-read lazily, at most every ``SKILL_RELOAD_INTERVAL`` seconds, so a skill
added on disk shows up without restarting the bot.
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
from ..database.settings_repo import SettingsRepository
from ..discord_failures import DISCORD_CALL_FAILURES
from ..discord_ui.authorization import Authorizer
from ..discord_ui.slash_io import slash_io
from ..discord_ui.thread_dashboard import board_turn, dashboard_of
from ..model_scopes import TurnSettings, begin_turn_for, effort_for_launch
from ..thread_owner import foreign_owner_notice_for
from ..thread_settings import resolve_auto_archive_duration
from ..turn_events import StartedBy
from ..utils.logger import log_ctx
from ._run_helper import has_other_run_in_flight, run_claude_with_config
from .run_config import RunConfig

if TYPE_CHECKING:
    from ..session_dir import SessionDirManager
    from ..tmux import TmuxSessionManager

logger = logging.getLogger(__name__)
# Callbacks the shared skill core uses to stay agnostic of slash vs text entry.
Responder = Callable[..., Awaitable[None]]
Acknowledger = Callable[[], Awaitable[None]]

# Seconds between two readings of the skills directory.
SKILL_RELOAD_INTERVAL = 60.0

# Discord's ceilings: choices offered at once, and the length of one's label.
_MAX_CHOICES = 25
_CHOICE_LABEL_LIMIT = 100
# How much of a skill's description a choice shows before it is cut.
_CHOICE_DESCRIPTION_CHARS = 60
# Discord's ceiling on a thread name.
_THREAD_NAME_LIMIT = 100

# The opening and closing fence of a SKILL.md frontmatter block.
_FENCE = "---"
# One ``key: value`` entry of a frontmatter block. The key starts its line; the
# whitespace skipped after the colon may run over line ends, so a key with
# nothing after it takes the next non-blank line as its value.
_FRONTMATTER_ENTRY = re.compile(r"^(\w[\w-]*):\s*(.+)$", re.MULTILINE)
# What a skill name may look like. It is typed into Claude's prompt, so this
# check is a security boundary — keep it strict.
_SKILL_NAME = re.compile(r"^[\w-]+$")


def _frontmatter_block(text: str) -> str | None:
    """The text between the ``---`` fences opening *text*, or ``None``.

    The first line must be the fence itself (spaces or tabs may trail it), and
    the block runs up to the next line that begins with a fence.
    """
    opening, newline, rest = text.partition("\n")
    if not newline or not opening.startswith(_FENCE) or opening[len(_FENCE) :].strip(" \t"):
        return None
    block: list[str] = []
    for line in rest.split("\n"):
        if line.startswith(_FENCE):
            return "\n".join(block)
        block.append(line)
    return None


def _parse_skill_meta(skill_dir: Path) -> dict[str, str] | None:
    """``{"name", "description"}`` from *skill_dir*'s ``SKILL.md`` frontmatter.

    ``None`` when there is no ``SKILL.md``, no frontmatter in it, or it cannot
    be read. A missing ``name`` falls back to the directory's name.
    """
    skill_md = skill_dir / "SKILL.md"
    if not skill_md.exists():
        return None
    try:
        text = skill_md.read_text(encoding="utf-8")
    except OSError:
        logger.warning("Failed to read %s", skill_md)
        return None

    block = _frontmatter_block(text)
    if block is None:
        return None
    # dict() keeps the last of several entries with the same key.
    entries = dict(_FRONTMATTER_ENTRY.findall(block))
    return {
        "name": entries.get("name", skill_dir.name).strip(),
        "description": entries.get("description", "").strip(),
    }


def _load_skills(skills_dir: Path) -> list[dict[str, str]]:
    """Every skill found directly under *skills_dir*, in directory-name order."""
    if not skills_dir.is_dir():
        logger.warning("Skills directory not found: %s", skills_dir)
        return []
    candidates = (entry for entry in sorted(skills_dir.iterdir()) if entry.is_dir())
    skills = [meta for meta in map(_parse_skill_meta, candidates) if meta]
    logger.info("Loaded %d skills from %s", len(skills), skills_dir)
    return skills


def _choice_label(skill: dict[str, str]) -> str:
    """How *skill* reads in the autocomplete list: its name, then a short description."""
    name, description = skill["name"], skill["description"]
    if not description:
        return name
    shown = description[:_CHOICE_DESCRIPTION_CHARS]
    if len(description) > _CHOICE_DESCRIPTION_CHARS:
        shown += "…"
    return f"{name} — {shown}"


class SkillCommandCog(commands.Cog):
    """The ``/skill`` slash command, its autocomplete, and the ``!skill`` text twin."""

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
        settings_repo: SettingsRepository | None = None,
    ) -> None:
        self.bot = bot
        # #927: where /model's settings live. Optional (zero-config) — see
        # :meth:`_settings_repo_for_turn` for the fallback.
        self._settings_repo = settings_repo
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
        self._skills_dir = (
            Path.home() / ".claude" / "skills" if skills_dir is None else Path(skills_dir)
        )
        # Read once now; ``_maybe_reload_skills`` keeps it fresh afterwards.
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
        """Read the skills directory again once the last reading has aged out."""
        now = time.monotonic()
        if self._skills_fresh_at(now):
            return
        self._skills, self._last_loaded = _load_skills(self._skills_dir), now

    def _skills_fresh_at(self, now: float) -> bool:
        """Whether the list read at ``_last_loaded`` still counts at *now*."""
        return now - self._last_loaded < SKILL_RELOAD_INTERVAL

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
        self, interaction: discord.Interaction, current: str
    ) -> list[app_commands.Choice[str]]:
        """Choices for ``/skill name:`` — skills whose name or description contains *current*.

        Defined above ``run_skill``, whose decorator refers to it by name.
        """
        self._maybe_reload_skills()
        needle = current.lower()
        matching = [
            skill
            for skill in self._skills
            if needle in skill["name"].lower() or needle in skill["description"].lower()
        ]
        return [
            app_commands.Choice(
                name=_choice_label(skill)[:_CHOICE_LABEL_LIMIT],
                value=skill["name"],
            )
            for skill in matching[:_MAX_CHOICES]
        ]

    def _earlier_triggers_forgetter(
        self, thread_id: int, mirror_cog: object, prior: set[int | None]
    ) -> Callable[[], Awaitable[None]]:
        """Make the earlier turns' trigger messages stop counting — #1234 / #1262.

        A ``/skill`` turn has no trigger message, so an earlier turn's must not
        stay on record — after a restart its author would be pinged for this
        turn, and the answer would be posted as a reply to them. Nothing on
        record → the owner (#1212).

        Everything happens in the returned hook, which runs the moment the
        ``/skill`` prompt is really in the pane (``RunConfig.on_prompt_sent``) —
        the pane answers the input it got last, and a ``/skill`` whose send
        failed changes nothing. Forgotten then: what was on record (memory, row,
        pending) when this ``/skill`` started (*prior*), except a message turn
        still on its way to the pane (in flight — its prompt arrives after this
        one, so it is the newer turn); and the trigger of a message turn whose
        prompt got there first. A message recorded since that has not been sent
        keeps its trigger.
        """
        known = {mid for mid in prior if isinstance(mid, int)}

        async def on_prompt_sent() -> None:
            stale = {mid for mid in known if not self.repo.trigger_in_flight(thread_id, mid)}
            sent = self.repo.sent_trigger(thread_id)
            if isinstance(sent, int):
                stale.add(sent)
            # This /skill is now what the pane answers.
            self.repo.note_trigger_sent(thread_id, None)
            seen_memory = getattr(mirror_cog, "trigger_message", None)
            forget = getattr(mirror_cog, "forget_trigger_message", None)
            current = seen_memory(thread_id) if callable(seen_memory) else None
            if callable(forget) and current in stale:
                forget(thread_id, only=current)
            if not stale:
                return
            try:
                await self.repo.clear_trigger_message(thread_id, only=stale)
            except Exception:
                # The repository keeps the ids for the next session save.
                logger.warning(
                    "%s /skill could not clear the recorded trigger message now; "
                    "it is applied with the next session save (#1262)",
                    log_ctx(thread_id=thread_id),
                    exc_info=True,
                )

        return on_prompt_sent

    def _make_runner(
        self,
        tmux: TmuxSessionManager,
        thread_id: int,
        working_dir: str | None = None,
        *,
        turn: TurnSettings | None = None,
        try_continue: bool = False,
    ) -> TmuxClaudeRunner:
        """Create a TmuxClaudeRunner for this turn's model/effort (#927).

        *turn* is the pair :func:`begin_turn_for` fixed; ``None`` means the
        settings could not be read and the instance defaults apply (#918).
        """
        model = (turn.model if turn else None) or self.runner.model
        return TmuxClaudeRunner(
            tmux_manager=tmux,
            thread_id=thread_id,
            model=model,
            working_dir=working_dir or self.runner.working_dir,
            timeout_seconds=self.runner.timeout_seconds,
            dangerously_skip_permissions=True,
            effort=effort_for_launch(model, turn.effort if turn else self.runner.effort),
            # #762: a /skill prompt is ``/<name> …`` — a command, not a message.
            slash_command=True,
            try_continue=try_continue,
        )

    def _settings_repo_for_turn(self) -> SettingsRepository | None:
        """The settings repository /model writes to (#927).

        Right after a restart nothing has cached the model-scope service yet, so
        it has to be built from this. A consumer that constructs the cog without
        ``settings_repo`` still gets /model through the chat cog's repository —
        the same DB — rather than silently running on the instance defaults.
        """
        if self._settings_repo is not None:
            return self._settings_repo
        chat = self.bot.get_cog("ClaudeChatCog")
        repo = getattr(chat, "_settings_repo", None)
        if isinstance(repo, SettingsRepository):
            return repo
        repo = getattr(self.bot, "settings_repo", None)
        return repo if isinstance(repo, SettingsRepository) else None

    async def _fix_turn(
        self, thread: discord.Thread, channel_id: int | None, tmux: TmuxSessionManager
    ) -> tuple[TurnSettings | None, bool]:
        """Fix this turn's model/effort pair, as a chat turn does (#918/#927).

        Returns ``(turn, restarted)``. When the Claude already in the thread's
        pane was launched with a different pair, it is stopped here — before the
        window is (re)made — so the run starts it again with the new flags and
        ``--resume``, instead of typing ``/model`` into it (which would rewrite
        the host's user settings for every Claude).

        #976: not while another run (a chat turn) is using that Claude — killing
        it would kill that turn. The change then waits for a turn with nothing
        in flight.
        """
        turn = await begin_turn_for(
            self.bot,
            self._settings_repo_for_turn(),
            self.runner,
            thread.id,
            channel_id,
            keep_running=has_other_run_in_flight(thread.id),
        )
        if turn is None or not turn.needs_restart:
            return turn, False
        if not await asyncio.to_thread(tmux.is_claude_running, thread.id):
            return turn, False
        await asyncio.to_thread(tmux.kill_session, thread.id)
        logger.info(
            "%s restarting claude for new model/effort %s/%s (was %s) (#927)",
            log_ctx(thread_id=thread.id),
            turn.model,
            turn.effort,
            turn.previously_applied,
        )
        with contextlib.suppress(*DISCORD_CALL_FAILURES):
            await thread.send(
                "-# 🔄 モデル設定が変わったため、会話を保ったまま Claude を"
                f"立て直します（{turn.model} / {turn.effort or '—'}）。"
            )
        return turn, True

    def _is_claude_thread(self, channel: discord.abc.GuildChannel | discord.Thread) -> bool:
        """Whether *channel* is a thread of the Claude channel."""
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
                with contextlib.suppress(*DISCORD_CALL_FAILURES):
                    await thread.send("❌ この /skill の作業ディレクトリを用意できませんでした。")
                return False, None
            working_dir = made if isinstance(made, str) and made else None
        working_dir = working_dir or self.runner.working_dir

        window = await asyncio.to_thread(tmux.create_session, thread.id, working_dir or ".")
        # create_session also returns a name when tmux itself is unavailable,
        # so only an existing window proves Claude has somewhere to start.
        if not await asyncio.to_thread(tmux.session_exists, thread.id):
            logger.error("%s could not create a tmux window for /skill — aborting the run", ctx)
            with contextlib.suppress(*DISCORD_CALL_FAILURES):
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

        # ``match`` with ``$``, not ``fullmatch``: the long-standing check.
        if not _SKILL_NAME.match(name):
            await respond(f"Invalid skill name: `{name}`", ephemeral=True)
            return

        self._maybe_reload_skills()
        if not any(skill["name"] == name for skill in self._skills):
            await respond(
                f"Skill `{name}` not found. Use `/skill` with autocomplete.",
                ephemeral=True,
            )
            return
        prompt = f"/{name} {args}" if args else f"/{name}"

        await ack()

        # Inside a thread of the Claude channel: run in that thread's session.
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

            record = await self.repo.get(channel.id)
            session_id: str | None = record.session_id if record else None
            # #1234 (W57): what is on record *now*, before the workspace is
            # prepared — memory, row and pending (#1262: the memory too, since the
            # row may still be waiting for that message's write). These are
            # forgotten below; a message that arrives meanwhile keeps its trigger
            # unless its prompt reaches the pane before this one.
            mirror_cog = getattr(self.bot, "transcript_mirror_cog", None)
            seen_memory = getattr(mirror_cog, "trigger_message", None)
            prior = {
                seen_memory(channel.id) if callable(seen_memory) else None,
                record.trigger_message_id if record else None,
                self.repo.pending_trigger(channel.id),
            }

            display = f"`{prompt}`"
            await respond(f"Running {display} in this thread…")

            # #927: before the workspace — a restart for a new pair closes the
            # window, and _prepare_workspace is what opens it again.
            turn, restarted = await self._fix_turn(channel, parent_channel_id, tmux)
            ready, working_dir = await self._prepare_workspace(
                thread=channel,
                tmux=tmux,
                sdm=sdm,
                recorded_dir=record.working_dir if record else None,
                requester=user,
            )
            if not ready:
                return
            runner = self._make_runner(
                tmux,
                channel.id,
                working_dir,
                turn=turn,
                try_continue=restarted and session_id is not None,
            )
            # #1234 / #1262: the earlier turns' triggers stop counting.
            on_prompt_sent = self._earlier_triggers_forgetter(channel.id, mirror_cog, prior)
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
                        started_by=StartedBy("user", str(user.id)),
                        on_prompt_sent=on_prompt_sent,
                    )
                )
            return

        # Anywhere else: open a new thread in the Claude channel and run there.
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

        # The thread is named after what Claude is asked to type.
        thread_name = prompt[:_THREAD_NAME_LIMIT]
        archive_minutes = await resolve_auto_archive_duration(
            getattr(self.bot, "settings_repo", None)
        )
        thread = await claude_channel.create_thread(
            name=thread_name,
            type=discord.ChannelType.public_thread,
            auto_archive_duration=archive_minutes,
        )
        display = f"`{prompt}`"
        await respond(f"Running {display} → {thread.mention}")

        # #927: a new thread has no running Claude, so this never restarts.
        turn, _ = await self._fix_turn(thread, claude_channel.id, tmux)
        ready, working_dir = await self._prepare_workspace(
            thread=thread, tmux=tmux, sdm=sdm, recorded_dir=None, requester=user
        )
        if not ready:
            return
        runner = self._make_runner(tmux, thread.id, working_dir, turn=turn)
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
                    started_by=StartedBy("user", str(user.id)),
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
        """Slash entry point: hand the request to the shared skill core."""
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
