"""Discord UI components for AskUserQuestion interactive prompts.

Design
------
AskView is a **persistent** view (timeout=None).  Persistent views survive bot
restarts: on_ready re-registers them via ``bot.add_view()``, so buttons in old
messages keep working rather than showing Discord's generic "Interaction Failed".

Answer routing uses :mod:`ask_bus` (an in-process asyncio.Queue per thread).
The waiting side (``_collect_ask_answers`` in _run_helper.py) calls
``ask_bus.register(thread_id)`` and awaits ``queue.get()`` with a 24-hour
timeout instead of the old 5-minute hard limit.
AskView callbacks call ``ask_bus.post_answer(thread_id, labels)``; if the
session is gone after a restart, post_answer returns False and the view shows
a clear "session ended" message instead of silently failing.

custom_id format:  ``ask_{thread_id}_{q_idx}_{slot}``
  - slot = 0..3 for regular buttons
  - slot = ``select`` for the Select menu
  - slot = ``other`` for the free-text button
"""

from __future__ import annotations

import contextlib
import datetime as dt
import logging
from typing import TYPE_CHECKING

import discord

from .ask_bus import (
    CLOSE_ANSWERED,
    CLOSE_INTERRUPTED,
    CLOSE_TERMINAL,
    CLOSE_TIMEOUT,
    AskAnswerBus,
)
from .ask_bus import ask_bus as _default_ask_bus
from .ask_menus import ask_menus, disable_stale_copies
from .authorization import AuthorizedViewMixin, Authorizer
from .embeds import ask_sending_embed, ask_undelivered_embed
from .error_reporting import ErrorReportingViewMixin

if TYPE_CHECKING:
    from ..claude.types import AskQuestion
    from ..database.ask_repo import PendingAskRepository

logger = logging.getLogger(__name__)

# When this process started.  A menu message older than this can only have come
# from a previous process — which is the one case where "the bot restarted" is
# the true explanation for an undeliverable answer (#536).
_PROCESS_STARTED_AT = dt.datetime.now(dt.timezone.utc)  # noqa: UP017 — dt.UTC is 3.11+, we support 3.10

# Cause → what to tell the user.  The old code told everyone the bot had been
# restarted, which was false for every menu that was answered in the terminal,
# timed out, or was pre-empted — and reading "the bot was restarted" when it had
# not is worse than no message at all (#536).
_CLOSE_REASON_TEXT = {
    CLOSE_ANSWERED: "この質問はすでに回答済みです（別のメッセージで回答されました）",
    CLOSE_TERMINAL: "この質問はすでに端末（tmux ペイン）側で回答済みです",
    CLOSE_TIMEOUT: "この質問は時間切れで締め切られました",
    CLOSE_INTERRUPTED: "新しい指示が届いたため、この質問は取り消されました",
}
_CLOSED_UNKNOWN = "この質問はすでに閉じられています"
_CLOSED_RESTARTED = "この質問のあとに bot が再起動したため、セッションが失われました"

# Left on the other copies of a menu once one of them is resolved (#536).
_STALE_COPY_NOTE = "-# 🔁 この質問は別のメッセージで解決済みです（このボタンは無効です）"


def _button_label(label: str, index: int) -> str:
    """A label Discord will accept, for the option at *index* (#579).

    Discord rejects an empty button label — and rejects the **whole message**
    with it, so one option the pane parser could not read silenced the entire
    menu (400 ``In components.0.components.1.label: This field is required``),
    which the watchdog then retried every 30–60s forever.

    Substituting a placeholder rather than dropping the option is deliberate:
    the option still exists in the TUI, and answers are delivered as
    ``Down × index``, so a shorter list would select the wrong one. The number
    shown is the TUI's own, which is what the user sees in the pane.
    """
    text = (label or "").strip()
    return text[:80] if text else f"{index + 1}."


class AskView(AuthorizedViewMixin, ErrorReportingViewMixin, discord.ui.View):
    """Renders buttons or a select menu for a single AskUserQuestion prompt.

    This is a **persistent** view — ``timeout=None``.  Register it with the
    bot via ``bot.add_view(view)`` so that button clicks still work after a
    bot restart (they'll receive a graceful "session ended" message).

    Answers are routed via :data:`ask_bus` rather than an internal Future,
    so the waiting coroutine (``_collect_ask_answers``) can use any timeout
    and ``view.stop()`` is always called on interaction.

    A click leaves the message in the **interim** ``⏳ 送信中`` state, never ✅
    (#651): at this point the answer has only reached an in-process queue. The
    bridge that actually types it into the pane replaces this with the outcome
    it verified against Claude's transcript.

    Usage::

        view = AskView(question, thread_id=thread.id, q_idx=0)
        bot.add_view(view)                 # register for restart recovery
        await thread.send(embed=..., view=view)
        # answer arrives via ask_bus.register(thread_id) → queue.get()
    """

    def __init__(
        self,
        question: AskQuestion,
        thread_id: int,
        q_idx: int,
        bus: AskAnswerBus | None = None,
        ask_repo: PendingAskRepository | None = None,
        authorizer: Authorizer | None = None,
    ) -> None:
        super().__init__(timeout=None)  # persistent — survives bot restarts
        self._authorizer = authorizer
        self._thread_id = thread_id
        # Kept so the resolved message can still show WHAT was asked (#536):
        # the old code wiped the embed and left a bare "Selected: X".
        self._question = question
        self._bus = bus if bus is not None else _default_ask_bus
        self._ask_repo = ask_repo
        # multiSelect records the choice in the Select and submits via the
        # ✅ confirm button (#418); single-select delivers immediately.
        self._multi_select = question.multi_select
        self._selected_values: list[str] = []

        options = question.options
        use_select = question.multi_select or len(options) > 4

        if use_select and options:
            max_vals = len(options) if question.multi_select else 1
            select = discord.ui.Select(
                placeholder=question.header or "Choose an option…",
                min_values=1,
                max_values=min(max_vals, 25),
                options=[
                    discord.SelectOption(
                        label=_button_label(opt.label, i)[:100],
                        description=opt.description[:100] if opt.description else None,
                        value=_button_label(opt.label, i)[:100],
                    )
                    for i, opt in enumerate(options[:25])
                ],
                custom_id=f"ask_{thread_id}_{q_idx}_select",
            )
            select.callback = (
                self._multi_select_record if question.multi_select else self._select_callback
            )
            self.add_item(select)
        elif options:
            for i, opt in enumerate(options[:4]):
                btn = discord.ui.Button(
                    label=_button_label(opt.label, i),
                    style=discord.ButtonStyle.primary,
                    custom_id=f"ask_{thread_id}_{q_idx}_{i}",
                    row=0,
                )
                btn.callback = _make_button_callback(self, opt.label)
                self.add_item(btn)

        # multiSelect needs an explicit submit affordance — the Select only
        # records the choice, so without this button the user has no way to
        # confirm (Discord's dismiss-to-submit is undiscoverable) (#418).
        if question.multi_select and options:
            confirm_btn = discord.ui.Button(
                label="✅ 確定",
                style=discord.ButtonStyle.success,
                custom_id=f"ask_{thread_id}_{q_idx}_confirm",
                row=1,
            )
            confirm_btn.callback = self._confirm_callback
            self.add_item(confirm_btn)

        # Plan-approval menus (#251) suppress the free-text affordance: their
        # "Tell Claude what to change" option is selected like any other (and
        # opens its own TUI feedback field), so a generic Other modal would
        # mis-send keystrokes into the open menu.
        if question.allow_other:
            other_btn = discord.ui.Button(
                label="✏️ Other",
                style=discord.ButtonStyle.secondary,
                custom_id=f"ask_{thread_id}_{q_idx}_other",
                row=1,
            )
            other_btn.callback = self._other_callback
            self.add_item(other_btn)

    # ------------------------------------------------------------------
    # Internal helpers
    # ------------------------------------------------------------------

    def _undeliverable_reason(self, interaction: discord.Interaction) -> str:
        """Explain, in the user's words, why *this* click could not be delivered.

        Order matters: a recorded closure reason (#536) is first-hand knowledge
        and always beats inference.  Only when this process knows nothing about
        the thread do we fall back to the message's age — a menu posted before
        this process started really did lose its session to a restart.
        """
        reason = self._bus.closed_reason(self._thread_id)
        if reason is not None:
            return _CLOSE_REASON_TEXT.get(reason, _CLOSED_UNKNOWN)
        created_at = getattr(getattr(interaction, "message", None), "created_at", None)
        if isinstance(created_at, dt.datetime) and created_at < _PROCESS_STARTED_AT:
            return _CLOSED_RESTARTED
        return _CLOSED_UNKNOWN

    async def _deliver(self, interaction: discord.Interaction, values: list[str]) -> None:
        """Deliver *values* via the bus and show the outcome IN THE THREAD (#536).

        Both outcomes replace the menu with an embed that still carries the
        question, so the thread reads as "asked X, answered Y" long after the
        buttons are gone — and so a failure is visible to everyone in the thread
        rather than only to the person who clicked.

        Whatever happens, the buttons stop inviting clicks: on the clicked
        message via this edit, on every other live copy via
        :func:`disable_stale_copies`.
        """
        delivered = self._bus.post_answer(self._thread_id, values)
        question = self._question
        if delivered:
            # #651: the bus accepted the answer — that is all that is known
            # right now. The keystrokes have not been sent, and "sent" is not
            # "received" either (#650). The bridge replaces this with the
            # verified outcome once Claude's transcript says what happened.
            embed = ask_sending_embed(question.question, question.header, values)
        else:
            if self._ask_repo is not None:
                await self._ask_repo.delete(self._thread_id)
            reason = self._undeliverable_reason(interaction)
            logger.info(
                "AskView: answer %r could not be delivered for thread %d (%s)",
                values,
                self._thread_id,
                reason,
            )
            embed = ask_undelivered_embed(question.question, question.header, values, reason)

        await interaction.response.edit_message(content=None, embed=embed, view=None)

        message_id = getattr(getattr(interaction, "message", None), "id", None)
        ask_menus.forget(self._thread_id, message_id)
        with contextlib.suppress(Exception):
            await disable_stale_copies(self._thread_id, message_id, _STALE_COPY_NOTE)
        self.stop()

    async def _select_callback(self, interaction: discord.Interaction) -> None:
        values: list[str] = interaction.data.get("values", [])  # type: ignore[union-attr]
        await self._deliver(interaction, values)

    async def _multi_select_record(self, interaction: discord.Interaction) -> None:
        """Record a multiSelect choice WITHOUT delivering — the user submits via
        the ✅ confirm button (#418).  Echo the running selection so it is clear
        what will be sent."""
        self._selected_values = interaction.data.get("values", [])  # type: ignore[union-attr]
        chosen = ", ".join(self._selected_values) or "（未選択）"
        # Full-size, not Discord's grey ``-#`` small text (#536): "picked but not
        # submitted" is the state users mistook for "submitted and ignored", so
        # it has to be the loudest thing on the message, not the quietest.
        await interaction.response.edit_message(
            content=(
                f"🔲 **選択中:** {chosen}\n"
                "**まだ送信されていません** — 下の「✅ 確定」を押すと Claude に届きます。"
            ),
        )

    async def _confirm_callback(self, interaction: discord.Interaction) -> None:
        """Deliver the recorded multiSelect choice when ✅ 確定 is pressed (#418)."""
        if not self._selected_values:
            await interaction.response.send_message(
                "1つ以上選択してから「✅ 確定」を押してください。", ephemeral=True
            )
            return
        await self._deliver(interaction, self._selected_values)

    async def _other_callback(self, interaction: discord.Interaction) -> None:
        modal = AskModal(title="Your answer")
        await interaction.response.send_modal(modal)
        timed_out = await modal.wait()
        if not timed_out and modal.answer:
            delivered = self._bus.post_answer(self._thread_id, [modal.answer])
            message = getattr(interaction, "message", None)
            question = self._question
            if delivered:
                # #651: interim — the bridge confirms and rewrites this.
                embed = ask_sending_embed(question.question, question.header, [modal.answer])
            else:
                if self._ask_repo is not None:
                    await self._ask_repo.delete(self._thread_id)
                reason = self._undeliverable_reason(interaction)
                # The free text is whatever the user typed — length only, never
                # the content (same convention as the prompt logging in
                # _run_helper: a modal is exactly where a secret would be pasted).
                logger.warning(
                    "AskView._other_callback: free text (%d chars) undeliverable "
                    "for thread %d (%s)",
                    len(modal.answer),
                    self._thread_id,
                    reason,
                )
                embed = ask_undelivered_embed(
                    question.question, question.header, [modal.answer], reason
                )
            # The modal already consumed the interaction response, so the menu
            # message is edited directly rather than through the interaction.
            if message is not None:
                with contextlib.suppress(discord.HTTPException, Exception):
                    await message.edit(content=None, embed=embed, view=None)
            message_id = getattr(message, "id", None)
            ask_menus.forget(self._thread_id, message_id)
            with contextlib.suppress(Exception):
                await disable_stale_copies(self._thread_id, message_id, _STALE_COPY_NOTE)
            self.stop()


class AskModal(discord.ui.Modal):
    """Modal for free-text input when the user selects 'Other'."""

    def __init__(self, title: str) -> None:
        super().__init__(title=title[:45])
        self.answer: str = ""
        self.text_input = discord.ui.TextInput(
            label="Your answer",
            placeholder="Type your answer here…",
            style=discord.TextStyle.paragraph,
            required=True,
            max_length=500,
        )
        self.add_item(self.text_input)

    async def on_submit(self, interaction: discord.Interaction) -> None:
        self.answer = self.text_input.value
        await interaction.response.defer()
        self.stop()


def _make_button_callback(view: AskView, label: str):
    """Factory that creates a button callback with *view* and *label* bound.

    Passing *view* explicitly (instead of capturing a Future) means:
    - ``view._deliver()`` is called, which routes via ask_bus and calls
      ``view.stop()`` — fixing the 300-second hang of the old design.
    - The restart-recovery path is handled uniformly with select/other.
    """

    async def callback(interaction: discord.Interaction) -> None:
        await view._deliver(interaction, [label])

    return callback
