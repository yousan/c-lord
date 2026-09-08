"""The one place a new process clears up after the process before it.

Two jobs, one moment. Startup is the only time at which "everything the last
process left on screen is dead" is guaranteed true — no turn is in flight, so
nothing can be destroyed by mistake — and it is the only time both jobs are
safe. They were nonetheless implemented in two different ``on_ready`` handlers,
which is exactly how one of them ran for a month while the other never ran once:

- ``⏹ Stop`` buttons a shutdown could not delete (#634) — swept from
  ``ClaudeChatCog``;
- ask menus whose handlers died with the process (#671) — re-armed from
  ``ClaudeDiscordBot``, reading a table that (as it turned out) nothing ever
  wrote to.

Keeping them together is the point of this module: adding a third kind of dead
UI should mean one more line here, not a third startup hook nobody knows about.
"""

from __future__ import annotations

import contextlib
import logging
from typing import TYPE_CHECKING

from .ask_menu_recovery import recover_ask_menus
from .stale_stop_buttons import sweep_dead_stop_buttons

if TYPE_CHECKING:
    from discord.ext.commands import Bot

    from .database.ask_repo import PendingAskRepository
    from .database.repository import SessionRepository

logger = logging.getLogger(__name__)


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
        await sweep_dead_stop_buttons(bot, session_repo)
    with contextlib.suppress(Exception):
        await recover_ask_menus(bot, ask_repo)
