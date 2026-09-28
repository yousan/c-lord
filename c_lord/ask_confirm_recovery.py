"""Pick up the ⏳ 確認中 watches a restart killed (#786).

#746: an answer Claude's transcript has not recorded within the 12-second
confirm window leaves its menu at ⏳ 確認中, and a watcher task keeps reading the
transcript to turn it ✅ / ⚠️ when the result lands. One AskUserQuestion with
several questions records its result only after the last answer (+53 min
measured), so that watch can easily outlive the process. When the process
went, the watcher went with it, and the menu said 確認中 forever.

Nothing is stored for this. Everything the watch needs is already somewhere
that survives a restart:

* the **menu message** in Discord — its ⏳ embed carries the question and the
  answer that was sent (:func:`~c_lord.discord_ui.embeds.parse_confirming_embed`);
* the thread's **session row** — its ``working_dir`` names the transcript dir;
* the **transcript** — the ask carrying that question, and its result
  (:func:`~c_lord.transcript.ask_result.find_ask_for_question`).

So on startup the new process looks for its own ⏳ menus in the threads that
were active within the watch's lifetime and starts the same watcher again. A
result already written corrects the menu at once; one written later corrects it
when it lands. A second restart simply finds the menu again.

The behaviour is written down in ``docs/askuserquestion-bridge.md``.
"""

from __future__ import annotations

import asyncio
import contextlib
import datetime as dt
import logging
from pathlib import Path
from typing import TYPE_CHECKING, Any

import discord

from .claude.types import AskOption, AskQuestion
from .discord_ui import ask_handler
from .discord_ui.ask_view import _PROCESS_STARTED_AT
from .discord_ui.embeds import ConfirmingMenu, parse_confirming_embed
from .transcript.ask_result import FoundAsk, find_ask_for_question
from .transcript.resolver import derive_project_dir
from .utils.logger import log_ctx

if TYPE_CHECKING:
    from discord.ext.commands import Bot

    from .database.repository import SessionRepository

logger = logging.getLogger(__name__)

# Messages read back per thread, newest first. A ⏳ menu is at most one watch
# lifetime old; a thread that posted more than this since is logged, not paged.
_HISTORY_LIMIT = 200


def _transcript_stamp(moment: dt.datetime) -> str:
    """*moment* in the transcript's own timestamp format (``…T14:10:00.000Z``)."""
    utc = moment.astimezone(dt.timezone.utc)  # noqa: UP017 — dt.UTC is 3.11+
    return utc.strftime("%Y-%m-%dT%H:%M:%S.") + f"{utc.microsecond // 1000:03d}Z"


def _question_of(menu: ConfirmingMenu, found: FoundAsk | None) -> AskQuestion:
    """The menu's question, with the options the ask offered when it is known.

    The options only matter for ⚠️, which shows what could be picked (#804);
    the ⏳ embed does not carry them, the ask in the transcript does.
    """
    options: list[AskOption] = []
    for raw in (found.question.get("options") if found else None) or []:
        if isinstance(raw, dict) and raw.get("label"):
            options.append(AskOption(str(raw["label"]), str(raw.get("description") or "")))
    return AskQuestion(question=menu.question, header=menu.header, options=options)


async def resume_confirming_menus(
    bot: Bot,
    repo: SessionRepository,
    *,
    projects_root: Path | None = None,
    started_at: dt.datetime | None = None,
) -> int:
    """Restart the watch of every ⏳ menu a previous process left. Never raises.

    Returns how many menus are being watched again.
    """
    me = getattr(bot, "user", None)
    if me is None:
        return 0
    started = started_at or _PROCESS_STARTED_AT
    oldest = started - dt.timedelta(seconds=ask_handler._LATE_CONFIRM_TIMEOUT)
    before = discord.Object(id=discord.utils.time_snowflake(started))
    resumed = 0
    for guild in list(getattr(bot, "guilds", None) or []):
        try:
            threads = await guild.active_threads()
        except Exception as exc:
            logger.info(
                "confirming-menu recovery: could not list active threads in guild=%s (%s) — "
                "its ⏳ menus stay until the next start (#786)",
                getattr(guild, "id", "?"),
                exc,
            )
            continue
        for thread in threads:
            last = getattr(thread, "last_message_id", None)
            if not last or discord.utils.snowflake_time(last) < oldest:
                continue
            try:
                resumed += await _resume_thread(thread, repo, me.id, before, oldest, projects_root)
            except Exception:
                logger.info(
                    "%s confirming-menu recovery: could not read this thread — its ⏳ menus "
                    "stay until the next start (#786)",
                    log_ctx(thread_id=thread.id),
                    exc_info=True,
                )
    logger.info(
        "confirming-menu recovery: watching %d ⏳ menu(s) left by the previous process "
        "again (#786)",
        resumed,
    )
    return resumed


async def _resume_thread(
    thread: Any,
    repo: SessionRepository,
    my_id: int,
    before: discord.Object,
    oldest: dt.datetime,
    projects_root: Path | None,
) -> int:
    record = await repo.get(thread.id)
    working_dir = getattr(record, "working_dir", None)
    if not working_dir:
        return 0
    project_dir = derive_project_dir(working_dir, projects_root=projects_root)
    resumed = 0
    async for message in thread.history(limit=_HISTORY_LIMIT, before=before):
        if message.created_at < oldest:
            break
        if getattr(message.author, "id", None) != my_id:
            continue
        menu = next(
            (m for e in message.embeds or () if (m := parse_confirming_embed(e)) is not None),
            None,
        )
        if menu is None:
            continue
        await _resume_one(message, menu, project_dir, thread.id)
        resumed += 1
    return resumed


async def _resume_one(
    message: discord.Message, menu: ConfirmingMenu, project_dir: Path, thread_id: int
) -> None:
    menu_at = _transcript_stamp(message.created_at)
    found: FoundAsk | None = None
    with contextlib.suppress(Exception):
        found = await asyncio.to_thread(find_ask_for_question, project_dir, menu.question, menu_at)
    ref = ask_handler._MenuRef(
        project_dir,
        ask=(found.tool_use_id, found.session_path) if found else None,
        after=menu_at,
        question=menu.question,
    )
    logger.info(
        "%s confirming-menu recovery: watching menu %s again for %s (#786)",
        log_ctx(thread_id=thread_id),
        message.id,
        ref.describe(),
    )
    task = asyncio.create_task(
        ask_handler._confirm_late(message, _question_of(menu, found), menu.selected, ref, thread_id)
    )
    ask_handler._late_confirmations.add(task)
    task.add_done_callback(ask_handler._late_confirmations.discard)
