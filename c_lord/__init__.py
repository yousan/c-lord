"""c-lord — Discord frontend for Claude Code CLI.

Built entirely by Claude Code itself. See README.md for details.

Quick start::

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
from .discord_ui.embeds import (
    error_embed,
    session_complete_embed,
    session_start_embed,
    tool_use_embed,
)
from .discord_ui.status import StatusManager
from .protocols import DrainAware
from .session_dir import CleanupResult, SessionDirInfo, SessionDirManager
from .setup import BridgeComponents, setup_bridge
from .tmux import TmuxSessionManager
from .version import resolve_version


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

__all__ = [
    "__version__",
    "resolve_version",
    # Core
    "ClaudeConfig",
    "TmuxClaudeRunner",
    "ClaudeChatCog",
    "ChannelRepoCog",
    "ChannelRepository",
    "ThreadRepository",
    "RunConfig",
    "EventProcessor",
    # Concurrency
    "ActiveSession",
    "SessionRegistry",
    "SessionCleanupCog",
    "SessionManageCog",
    "SkillCommandCog",
    "VersionCog",
    "SessionRepository",
    "SettingsRepository",
    # Webhook & Automation
    "WebhookTriggerCog",
    "WebhookTrigger",
    "AutoUpgradeCog",
    "UpgradeConfig",
    # Scheduling
    "SchedulerCog",
    "ScheduledTaskRepository",
    "DrainAware",
    "NotificationRepository",
    # Types
    "MessageType",
    "StreamEvent",
    "ToolCategory",
    "ToolUseEvent",
    # Session Management
    "SessionDirManager",
    "SessionDirInfo",
    "CleanupResult",
    "TmuxSessionManager",
    # Setup
    "setup_bridge",
    "BridgeComponents",
    # UI
    "StatusManager",
    "error_embed",
    "session_complete_embed",
    "session_start_embed",
    "tool_use_embed",
]
