"""The one place a new process clears up after the process before it.

Two jobs, one moment. Startup is the only time at which "everything the last
process left on screen is dead" is guaranteed true — no turn is in flight, so
nothing can be destroyed by mistake — and it is the only time both jobs are
safe. They were nonetheless implemented in two different ``on_ready`` handlers,
which is exactly how one of them ran for a month while the other never ran once:

- ask menus whose handlers died with the process (#671) — re-armed from
  ``ClaudeDiscordBot``, reading a table that (as it turned out) nothing ever
  wrote to;
- dead buttons a shutdown could not remove — ``⏹ Stop`` (#634), swept from
  ``ClaudeChatCog``, and since #752 every ❓ menu that re-arming did not bring
  back.

Keeping them together is the point of this module: adding a third kind of dead
UI should mean one more line here, not a third startup hook nobody knows about.

**Order matters (#752).** Re-arming runs first, and the sweep is told which menus
it brought back: a menu is dead exactly when nothing re-armed it, so sweeping
first would strip the very buttons #671 exists to keep working.
"""

from __future__ import annotations

import contextlib
import logging
from typing import TYPE_CHECKING

from .ask_menu_recovery import recover_ask_menus
from .database.sweep_cursor_repo import SweepCursorRepository
from .stale_stop_buttons import KeepMenu, sweep_dead_buttons

if TYPE_CHECKING:
    from discord.ext.commands import Bot

    from .database.ask_repo import PendingAskRepository
    from .database.repository import SessionRepository

logger = logging.getLogger(__name__)


def _keep_nothing(_thread_id: int, _message_id: int) -> bool:
    return False


def _keep_everything(_thread_id: int, _message_id: int) -> bool:
    return True


async def _rearmed_menus(ask_repo: PendingAskRepository | None) -> KeepMenu:
    """Which menus restart recovery left live — read AFTER it ran.

    Recovery deletes the rows of the menus it retired, so the rows left are
    exactly the re-armed ones. A row without a message id (written before #671)
    re-arms by custom id alone, i.e. for any menu message in its thread — so
    that whole thread's menus are left alone.

    If the rows cannot be read, nothing is known to be dead: keep every menu.
    """
    if ask_repo is None:
        return _keep_nothing
    try:
        records = await ask_repo.list_all()
    except Exception:
        logger.warning("startup recovery: could not read re-armed menus — keeping all menus")
        return _keep_everything
    bound = {r.message_id for r in records if r.message_id is not None}
    unbound_threads = {r.thread_id for r in records if r.message_id is None}

    def keep(thread_id: int, message_id: int) -> bool:
        return message_id in bound or thread_id in unbound_threads

    return keep


def _cursor_repo(session_repo: SessionRepository) -> SweepCursorRepository | None:
    db_path = getattr(session_repo, "db_path", None)
    return SweepCursorRepository(db_path) if isinstance(db_path, str) else None


async def run_startup_recovery(
    bot: Bot,
    session_repo: SessionRepository,
    ask_repo: PendingAskRepository | None,
) -> None:
    """Retire the previous process's dead UI. Never raises.

    Each job is isolated: a sweep that fails (an unreadable thread, a permission
    change) must not stop the other from running, because a half-recovered
    startup is how a dead button survives into a live process.
    """
    with contextlib.suppress(Exception):
        await recover_ask_menus(bot, ask_repo)
    keep_menu = await _rearmed_menus(ask_repo)
    with contextlib.suppress(Exception):
        await sweep_dead_buttons(
            bot, session_repo, keep_menu=keep_menu, cursors=_cursor_repo(session_repo)
        )
