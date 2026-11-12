"""c-lord — Discord frontend for Claude Code CLI.
# <<REDACTED 5 lines>>
    from c_lord import ClaudeChatCog, ClaudeConfig, SessionRepository
# <<REDACTED 3 lines>>
from .claude.config import ClaudeConfig
from .claude.tmux_runner import TmuxClaudeRunner
# <<REDACTED 2 lines>>
from .cogs.channel_repo import ChannelRepoCog
# <<REDACTED 4 lines>>
from .cogs.session_cleanup import SessionCleanupCog
# <<REDACTED 2 lines>>
from .cogs.version_cmd import VersionCog
# <<REDACTED 2 lines>>
from .database.channel_repo import ChannelRepository
# <<REDACTED 4 lines>>
from .database.thread_repo import ThreadRepository
from .discord_ui.embeds import error_embed, session_start_embed
# <<REDACTED 2 lines>>
from .session_dir import CleanupResult, SessionDirInfo, SessionDirManager
# <<REDACTED 1 lines>>
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
# <<REDACTED 2 lines>>
    "__version__",
    "resolve_version",
    "runtime_version",
    # <<REDACTED 1 lines>>
    "ClaudeConfig",
    "TmuxClaudeRunner",
    # <<REDACTED 1 lines>>
    "ChannelRepoCog",
    "ChannelRepository",
    "ThreadRepository",
    # <<REDACTED 5 lines>>
    "SessionCleanupCog",
    # <<REDACTED 2 lines>>
    "VersionCog",
    # <<REDACTED 17 lines>>
    # Session Management
    "SessionDirManager",
    "SessionDirInfo",
    "CleanupResult",
    "TmuxSessionManager",
    # <<REDACTED 8 lines>>
