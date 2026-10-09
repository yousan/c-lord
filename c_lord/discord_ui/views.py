"""Discord UI Views for interactive session controls."""

from __future__ import annotations

import contextlib
import logging
from collections.abc import Awaitable, Callable
from typing import Protocol, runtime_checkable

import discord

from ..session_stop import StopOutcome, StopResult, stop_reply
from .authorization import AuthorizedViewMixin, Authorizer
from .error_reporting import ErrorReportingViewMixin
from .slash_io import followup

logger = logging.getLogger(__name__)

# Protocol-compatible type: any object with an async interrupt() method.
# TmuxClaudeRunner satisfies this.


@runtime_checkable
class Interruptable(Protocol):
    # ``False`` means the C-c could not be sent (#878); ``None`` (older
    # implementations) counts as sent.
    async def interrupt(self) -> bool | None: ...


# Every message that carries a ⏹ Stop button starts with this. The startup sweep
# (:mod:`c_lord.stale_stop_buttons`, #634) finds a previous process's leftovers
# by it, so the text lives here and nowhere else — a copy that drifts becomes
# residue nobody cleans up.
STOP_MESSAGE_PREFIX = "-# ⏺ Session running"


class StopView(AuthorizedViewMixin, ErrorReportingViewMixin, discord.ui.View):
    """A ⏹ Stop button attached to the session status message.

    Clicking it runs ``on_stop`` — the same stop ``/stop`` and ``!stop`` run
    (``ClaudeChatCog._stop_thread_work``, #878): interrupt the turn, end the
    background commands, and keep a self-started turn from carrying on. The
    reply comes from :func:`c_lord.session_stop.stop_reply`, so the button
    never says "stopped" when the stop failed. Without ``on_stop`` (older
    callers) it falls back to ``runner.interrupt()``.

    After the session ends — either via the button or naturally — call
    ``disable()`` to deactivate the button on the status message.

    Call ``bump(thread)`` after each major Discord message to keep the Stop
    button at the bottom of the thread (most recently visible position).
    """

    def __init__(
        self,
        runner: Interruptable,
        authorizer: Authorizer | None = None,
        on_stop: Callable[[], Awaitable[StopResult]] | None = None,
    ) -> None:
        super().__init__(timeout=None)
        self._runner = runner
        self._on_stop = on_stop
        self._authorizer = authorizer
        self._stopped = False
        self._message: discord.Message | None = None

    def set_message(self, message: discord.Message) -> None:
        """Store the message this view is attached to."""
        self._message = message

    async def bump(self, thread: discord.Thread) -> None:
        """Re-post the Stop button as the latest message in the thread.

        Deletes the old stop message and sends a new one at the bottom so the
        button stays accessible as Claude sends new messages above it.
        No-op if the session has already been stopped.
        """
        if self._stopped:
            return

        old_message = self._message
        with contextlib.suppress(discord.HTTPException):
            new_message = await thread.send(STOP_MESSAGE_PREFIX, view=self)
            self._message = new_message

        if old_message:
            with contextlib.suppress(discord.HTTPException):
                await old_message.delete()

    @discord.ui.button(label="⏹ Stop", style=discord.ButtonStyle.danger)
    async def stop_button(
        self, interaction: discord.Interaction, button: discord.ui.Button
    ) -> None:
        """Stop the thread's Claude work — the same stop as /stop and !stop (#878)."""
        if self._stopped:
            await interaction.response.defer()
            return

        self._stopped = True
        button.disabled = True
        await interaction.response.edit_message(view=self)

        result = await self._run_stop()
        content, embed, ephemeral = stop_reply(result)
        if result.outcome is StopOutcome.STOPPED:
            self.stop()
        else:
            # Nothing was stopped: give the button back rather than leave a
            # dead "stopped" control over a turn that may still be running.
            self._stopped = False
            button.disabled = False
            with contextlib.suppress(Exception):
                await interaction.edit_original_response(view=self)

        with contextlib.suppress(Exception):
            if embed is not None:
                await interaction.followup.send(embed=embed)
            else:
                await followup(interaction, content, ephemeral=ephemeral)

    async def _run_stop(self) -> StopResult:
        if self._on_stop is not None:
            return await self._on_stop()
        sent = await self._runner.interrupt()
        return StopResult(StopOutcome.FAILED if sent is False else StopOutcome.STOPPED)

    async def disable(self, message: discord.Message | None = None) -> None:
        """Delete the stop-button message after the session ends naturally.

        Uses the stored message reference if ``message`` is not provided.
        No-op if the stop button was already clicked.
        """
        if self._stopped:
            return

        target = message or self._message
        self._stopped = True
        self.stop()

        if target:
            try:
                await target.delete()
            except discord.HTTPException:
                pass
            except RuntimeError as exc:
                # aiohttp session already closed during bot shutdown (#91)
                logger.warning("StopView.disable: could not delete message — %s", exc)


class ReopenSessionView(AuthorizedViewMixin, ErrorReportingViewMixin, discord.ui.View):
    """A ▶️ 再開する button attached to the "this workspace is stopped" notice (#512).

    A message sent to a workspace the user stopped with ``/workspace-stop`` is
    **held**, not run — c-lord posts :func:`c_lord.session_close.closed_notice_embed`
    with this view instead. Clicking the button reopens the workspace and then runs
    the message that was held, so the user does not have to retype it.

    **Only a stop the user asked for gets this button (#700).** The 7-day idle
    stop is c-lord's own housekeeping, so a message there simply reopens and runs;
    a button would be charging the reader for a decision they never made. What
    survives here is the opposite case, and the reason it survives is
    ``docs/specs/session-close.md``: 「自分で終わらせたなら、勝手に動き出さないで
    ほしい」.

    ``on_reopen`` is an async callable taking the button's
    :class:`discord.Interaction`; it does the actual reopen (clear ``closed_at``,
    restore the thread name, dispatch the held message). Keeping it injected
    leaves this class free of cog/database imports.

    ``timeout=None`` on purpose: the notice may sit unread for days, and a
    timed-out button that silently stops working would strand the only visible
    way back into the session.
    """

    def __init__(
        self,
        on_reopen: Callable[[discord.Interaction], Awaitable[None]],
        authorizer: Authorizer | None = None,
    ) -> None:
        super().__init__(timeout=None)
        self._on_reopen = on_reopen
        self._authorizer = authorizer
        self._reopened = False

    @discord.ui.button(label="再開する", emoji="▶️", style=discord.ButtonStyle.primary)
    async def reopen_button(
        self, interaction: discord.Interaction, button: discord.ui.Button
    ) -> None:
        """Reopen the session and run the held message."""
        # Guard against a double click: reopening twice would dispatch the held
        # message twice, i.e. run the user's instruction two times.
        if self._reopened:
            with contextlib.suppress(discord.HTTPException):
                await interaction.response.send_message(
                    "▶️ このスレッドは再開済みです。", ephemeral=True
                )
            return
        self._reopened = True

        button.disabled = True
        button.label = "再開しました"
        with contextlib.suppress(discord.HTTPException):
            await interaction.response.defer()
        # ``interaction.message`` is None for interactions that did not originate
        # from a message (never the case for a button, but the type allows it).
        if interaction.message is not None:
            with contextlib.suppress(discord.HTTPException):
                await interaction.message.edit(view=self)

        await self._on_reopen(interaction)
        self.stop()


class TextAnsweredMenuView(AuthorizedViewMixin, ErrorReportingViewMixin, discord.ui.View):
    """The undo for "your sentence was used as the menu's answer" (#536 AC7).

    A sentence typed while a menu is open is delivered as that menu's answer
    rather than interrupting the turn — because typing is what users do when the
    buttons feel unresponsive, and dropping the question they were answering is
    the worse failure.  When the guess is wrong, this button turns the sentence
    back into a new instruction: exactly the old behaviour, one click away.

    ``on_rerun`` re-dispatches the original message as an instruction (it
    pre-empts the running turn, as an ordinary reply does).  Injecting it keeps
    this class free of cog imports.

    ``timeout=None``: the notice can sit for a while before someone notices the
    answer went to the wrong place, and a button that silently stopped working
    would strand the only way back.
    """

    def __init__(
        self,
        on_rerun: Callable[[discord.Interaction], Awaitable[None]],
        authorizer: Authorizer | None = None,
    ) -> None:
        super().__init__(timeout=None)
        self._on_rerun = on_rerun
        self._authorizer = authorizer
        self._rerun = False

    @discord.ui.button(
        label="これは新しい指示でした",
        emoji="⚡",
        style=discord.ButtonStyle.secondary,
    )
    async def rerun_button(
        self, interaction: discord.Interaction, button: discord.ui.Button
    ) -> None:
        """Re-run the message as a new instruction instead of a menu answer."""
        # A double click would dispatch the instruction twice.
        if self._rerun:
            with contextlib.suppress(discord.HTTPException):
                await interaction.response.send_message(
                    "⚡ すでに新しい指示として実行しています。", ephemeral=True
                )
            return
        self._rerun = True

        button.disabled = True
        button.label = "新しい指示として実行しました"
        with contextlib.suppress(discord.HTTPException):
            await interaction.response.defer()
        if interaction.message is not None:
            with contextlib.suppress(discord.HTTPException):
                await interaction.message.edit(view=self)

        await self._on_rerun(interaction)
        self.stop()
