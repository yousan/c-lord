"""c-lord — Discord frontend for Claude Code CLI.

Written end to end by Claude Code itself; README.md tells the story.

Everything an instance needs is importable from the package root::

    from c_lord import ClaudeChatCog, ClaudeConfig, SessionRepository
"""

from .claude.config import ClaudeConfig
from .claude.tmux_runner import TmuxClaudeRunner
from .claude.types import MessageType, StreamEvent, ToolCategory, ToolUseEvent
from .cogs.auto_upgrade import AutoUpgradeCog, UpgradeConfig
from .cogs.channel_repo import ChannelRepoCog
from .cogs.claude_chat import ClaudeChatCog
from .cogs.event_processor import EventProcessor
from .cogs.run_config import RunConfig
from .cogs.scheduler import SchedulerCog
from .cogs.session_cleanup import SessionCleanupCog
from .cogs.session_manage import SessionManageCog
from .cogs.skill_command import SkillCommandCog
from .cogs.version_cmd import VersionCog
from .cogs.webhook_trigger import WebhookTrigger, WebhookTriggerCog
from .concurrency import ActiveSession, SessionRegistry
from .database.channel_repo import ChannelRepository
from .database.notification_repo import NotificationRepository
from .database.repository import SessionRepository
from .database.settings_repo import SettingsRepository
from .database.task_repo import TaskRepository as ScheduledTaskRepository
from .database.thread_repo import ThreadRepository
from .discord_ui.embeds import error_embed, session_start_embed
from .discord_ui.status import StatusManager
from .protocols import DrainAware
from .session_dir import CleanupResult, SessionDirInfo, SessionDirManager
from .setup import BridgeComponents, setup_bridge
from .tmux import TmuxSessionManager
from .version import resolve_version, runtime_version


def _resolve_dist_version() -> str:
    """Best-effort installed-distribution version for ``__version__``.

    Falls back to "0.0.0" when the package isn't installed (e.g. running from a
    source tree before the hatch-vcs build hook has run). The richer
    article-format string is available via ``resolve_version()``.
    """
    try:
        from importlib.metadata import PackageNotFoundError, version

        return version("c-lord")
    except PackageNotFoundError:
        return "0.0.0"
    except Exception:
        return "0.0.0"


__version__ = _resolve_dist_version()

# The order below is what ``from c_lord import *`` and editor completion show;
# new names go at the end of their group.
__all__ = [
    # which build this is
    "__version__",
    "resolve_version",
    "runtime_version",
    # running Claude Code
    "ClaudeConfig",
    "TmuxClaudeRunner",
    # chat, and the channel/thread → repository bindings
    "ClaudeChatCog",
    "ChannelRepoCog",
    "ChannelRepository",
    "ThreadRepository",
    # one run and the sessions alive right now
    "RunConfig",
    "EventProcessor",
    "ActiveSession",
    "SessionRegistry",
    # commands
    "SessionCleanupCog",
    "SessionManageCog",
    "SkillCommandCog",
    "VersionCog",
    # session storage
    "SessionRepository",
    "SettingsRepository",
    # automation: webhooks, self-upgrade, schedules, notifications
    "WebhookTriggerCog",
    "WebhookTrigger",
    "AutoUpgradeCog",
    "UpgradeConfig",
    "SchedulerCog",
    "ScheduledTaskRepository",
    "DrainAware",
    "NotificationRepository",
    # what a runner yields
    "MessageType",
    "StreamEvent",
    "ToolCategory",
    "ToolUseEvent",
    # Session Management
    "SessionDirManager",
    "SessionDirInfo",
    "CleanupResult",
    "TmuxSessionManager",
    # wiring it all up in one call
    "setup_bridge",
    "BridgeComponents",
    # Discord presentation
    "StatusManager",
    "error_embed",
    "session_start_embed",
]
