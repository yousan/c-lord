# <<REDACTED 5 lines>>
- run_claude_in_thread no longer reads the lounge per turn (#766)
# <<REDACTED 11 lines>>
from c_lord.database.lounge_repo import LoungeMessage, LoungeRepository
from c_lord.database.models import init_db
from c_lord.database.notification_repo import NotificationRepository
from c_lord.ext.api_server import ApiServer
# <<REDACTED 141 lines>>
        from c_lord.database.lounge_repo import _MAX_STORED_MESSAGES
# <<REDACTED 97 lines>>
# run_claude_in_thread lounge tests
# <<REDACTED 4 lines>>
    """The lounge is never injected (#758), and #766 stopped reading it per turn."""
# <<REDACTED 1 lines>>
    async def test_lounge_repo_ignored(self) -> None:
        """A lounge_repo is still accepted but not read: the context it fed was
        never delivered (tmux TUI has no --append-system-prompt, #758), so #766
        stopped building it. The user prompt must be passed unchanged.
        # <<REDACTED 1 lines>>
        from c_lord.cogs._run_helper import run_claude_in_thread
# <<REDACTED 12 lines>>
            from c_lord.claude.types import MessageType, StreamEvent
# <<REDACTED 27 lines>>
        # #766: the lounge is not read — nothing is built from it any more.
        lounge_repo_mock.get_recent.assert_not_called()

        # clone() is NOT called — tmux TUI mode doesn't support --append-system-prompt.
        # <<REDACTED 4 lines>>
        from c_lord.cogs._run_helper import run_claude_in_thread
# <<REDACTED 5 lines>>
            from c_lord.claude.types import MessageType, StreamEvent
# <<REDACTED 28 lines>>
