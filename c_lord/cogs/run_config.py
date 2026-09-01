"""Configuration dataclass for Claude Code execution.

Bundles all parameters needed to execute Claude Code CLI and stream results
to a Discord thread. Using a dataclass instead of a long positional argument
list makes call sites more readable and extension safer (new fields can be
added without changing every caller).
"""

from __future__ import annotations

from dataclasses import dataclass, field
from typing import TYPE_CHECKING

import discord

from ..claude.tmux_runner import TmuxClaudeRunner
from ..concurrency import SessionRegistry
from ..database.ask_repo import PendingAskRepository
from ..database.lounge_repo import LoungeRepository
from ..database.repository import SessionRepository
from ..database.settings_repo import SettingsRepository
from ..discord_ui.authorization import Authorizer
from ..discord_ui.status import StatusManager

if TYPE_CHECKING:
    from ..discord_ui.views import StopView
    from ..session_dir import SessionDirManager
    from ..tmux import TmuxSessionManager


@dataclass
class RunOutcome:
    """What a completed run needs to tell its caller (#562).

    ``no_response``: the turn ended without Claude ever producing anything. The
    caller needs it because the turn-end ping would otherwise announce "Claude
    has finished" for work that never started.

    ``error``: the failure text that was posted to the thread, or None if the
    run finished cleanly (#621). A caller with no human watching the thread —
    the scheduler, a webhook — otherwise cannot tell a successful run from one
    that posted a red embed and returned, and logs a clean-looking exit either
    way.
    """

    no_response: bool = False
    error: str | None = None


@dataclass
class RunConfig:
    """All parameters needed for a single Claude Code execution.

    Required fields:
        thread: Discord thread to post results to.
        runner: A TmuxClaudeRunner instance for this thread.
        prompt: The user's message or skill invocation.

    Optional fields:
        session_id: Session ID to resume. None for new sessions.
        repo: Session repository for persisting thread-session mappings.
              Pass None for automated workflows without session persistence.
        status: StatusManager for emoji reactions on the user's message.
        registry: SessionRegistry for concurrency awareness. When provided,
                  the session is registered during execution and a concurrency
                  notice is prepended to the prompt.
        ask_repo: Repository for persisting AskUserQuestion state across restarts.
        lounge_repo: Repository for AI Lounge context injection.
        settings_repo: KV store for the persisted per-model context-window total
                       (#370). When provided, a learned window survives restarts
                       and the /context probe stops re-running every session.
        stop_view: StopView instance to bump after each major message, keeping
                   the Stop button at the bottom of the thread.
        session_dir_manager: SessionDirManager for automatic session dir cleanup.
                            When provided, the directory for this thread is
                            removed (if clean) after the session ends.
        tmux_manager: TmuxSessionManager for tmux session lifecycle.
                      When provided, the tmux session for this thread is
                      killed after the session ends.
    """

    thread: discord.Thread
    runner: TmuxClaudeRunner
    prompt: str
    session_id: str | None = None
    repo: SessionRepository | None = None
    status: StatusManager | None = None
    registry: SessionRegistry | None = None
    ask_repo: PendingAskRepository | None = None
    lounge_repo: LoungeRepository | None = None
    # #370: persists the per-model context-window total so the /context probe
    # (which renders into the human-visible tmux pane) fires at most once per
    # model per host instead of once per session / bot-restart.
    settings_repo: SettingsRepository | None = None
    stop_view: StopView | None = None
    session_dir_manager: SessionDirManager | None = None
    tmux_manager: TmuxSessionManager | None = None
    # Paths to downloaded image tempfiles passed via --image flags.
    # Cleaned up in run_claude_with_config() finally block.
    image_paths: list[str] | None = None
    # Absolute path of the session's working directory. Persisted to the
    # sessions DB so TranscriptMirrorCog can restart mirrors after a bot
    # restart (CLORD_BRIDGE_MODE=jsonl). None when no session_dir_manager.
    working_dir: str | None = None
    # #466: allowlist predicate forwarded to interactive Views so button
    # clicks (permission / plan / elicitation / ask) are gated to the same
    # allowed users as messages. None ⇒ no allowlist ⇒ everyone may click.
    authorizer: Authorizer | None = None
    # #480: Discord user to @-mention when an interactive prompt (permission /
    # plan / elicitation / AskUserQuestion) pauses the turn awaiting input.
    # A question-mode pause is mid-turn, so it never reaches the turn-end
    # WAITING_INPUT mention — without a content mention here, users whose
    # thread notifications are "mentions only" get no push. Set to the turn's
    # poster (its author); automated/terminal-driven turns fall back to the
    # bot owner. None ⇒ no one to ping ⇒ posted without a mention (silent-safe).
    notify_user_id: int | None = None

    # #562: how the run turned out, written by the runner side and read by the
    # caller after it returns. RunConfig's *inputs* stay a value object; this is
    # a separate, explicitly mutable output channel rather than a field the
    # caller is expected to mutate.
    outcome: RunOutcome = field(default_factory=lambda: RunOutcome())

    # Prevent accidental field mutation — RunConfig is a value object.
    # Use dataclasses.replace() to create modified copies.
    def __post_init__(self) -> None:
        if not self.prompt:
            raise ValueError("RunConfig.prompt must not be empty")

    def with_prompt(self, prompt: str) -> RunConfig:
        """Return a new RunConfig with a different prompt (immutable copy)."""
        from dataclasses import replace

        return replace(self, prompt=prompt)
