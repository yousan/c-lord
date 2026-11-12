# <<REDACTED 12 lines>>
AskView callbacks call ``ask_bus.post_answer(thread_id, answers)``; if the
# <<REDACTED 7 lines>>

Options are identified by **index**, never by label (#674): a Select option's
``value`` is ``option:{index}``, a button is bound to its index, and what goes
on the bus is a :class:`~.ask_bus.ChosenOption` — the full label, carrying the
index it was chosen at.
# <<REDACTED 4 lines>>
import contextlib
import datetime as dt
# <<REDACTED 5 lines>>
from .ask_bus import (
    CLOSE_ANSWERED,
    CLOSE_INTERRUPTED,
    CLOSE_STOPPED,
    CLOSE_TERMINAL,
    CLOSE_TIMEOUT,
    AskAnswerBus,
    ChosenOption,
)
# <<REDACTED 1 lines>>
from .ask_menus import ask_menus, disable_stale_copies
from .authorization import AuthorizedViewMixin, Authorizer
from .embeds import ask_sending_embed, ask_undelivered_embed
from .error_reporting import ErrorReportingViewMixin
# <<REDACTED 2 lines>>
    from collections.abc import Awaitable, Callable, Iterable

    # <<REDACTED 3 lines>>
    # #671: ``(selected, menu message) -> (delivered, reason)``. Implemented by
    # ``ask_menu_recovery.PaneMenuAnswerer``; typed structurally so the view does
    # not import the recovery module (which imports the view).
    RecoveryDeliver = Callable[[list[str], object | None], Awaitable[tuple[bool, str]]]

# <<REDACTED 2 lines>>
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
    CLOSE_STOPPED: "停止したため、この質問は取り消されました",
}
_CLOSED_UNKNOWN = "この質問はすでに閉じられています"
_CLOSED_RESTARTED = "この質問のあとに bot が再起動したため、セッションが失われました"

# Left on the other copies of a menu once one of them is resolved (#536).
_STALE_COPY_NOTE = "-# 🔁 この質問は別のメッセージで解決済みです（このボタンは無効です）"
# <<REDACTED 2 lines>>
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


# #674: a Select option's ``value`` names the option by its index. It used to be
# the display label — cut to 80 characters, which then matched none of the real
# labels, so the pick was typed into the pane as free text. The prefix is what
# tells it apart from a menu posted before #674, whose values WERE the labels: a
# label can well be "2"; it is never "option:2".
_OPTION_VALUE_PREFIX = "option:"


def _option_value(index: int) -> str:
    """The Select ``value`` for the option at *index* (#674)."""
    return f"{_OPTION_VALUE_PREFIX}{index}"


class AskView(AuthorizedViewMixin, ErrorReportingViewMixin, discord.ui.View):
    # <<REDACTED 10 lines>>
    A click leaves the message in the **interim** ``⏳ 送信中`` state, never ✅
    (#651): at this point the answer has only reached an in-process queue. The
    bridge that actually types it into the pane replaces this with the outcome
    it verified against Claude's transcript.

    # <<REDACTED 15 lines>>
        authorizer: Authorizer | None = None,
        recovery: RecoveryDeliver | None = None,
    # <<REDACTED 2 lines>>
        self._authorizer = authorizer
        # <<REDACTED 1 lines>>
        # #671: what to do when the ask bus has no waiter. Set only on views
        # rebuilt at startup, where that is the NORMAL state (the coroutine that
        # would have received the answer died with the previous process) and the
        # TUI menu is still open in the pane, waiting to be typed into.
        self._recovery = recovery
        # Kept so the resolved message can still show WHAT was asked (#536):
        # the old code wiped the embed and left a bare "Selected: X".
        self._question = question
        # <<REDACTED 2 lines>>
        # multiSelect records the choice in the Select and submits via the
        # ✅ confirm button (#418); single-select delivers immediately.
        self._multi_select = question.multi_select
        # Option indices, not labels (#674).
        self._selected_indices: list[int] = []
        # #672: the Select is kept so a recorded choice can be written back into
        # it as ``default=True`` — see _multi_select_record.
        self._select: discord.ui.Select | None = None
# <<REDACTED 12 lines>>
                        label=_button_label(opt.label, i)[:100],
                        # <<REDACTED 1 lines>>
                        value=_option_value(i),
                    # <<REDACTED 1 lines>>
                    for i, opt in enumerate(options[:25])
                # <<REDACTED 3 lines>>
            select.callback = (
                self._multi_select_record if question.multi_select else self._select_callback
            )
            self._select = select
            # <<REDACTED 4 lines>>
                    label=_button_label(opt.label, i),
                    # <<REDACTED 4 lines>>
                btn.callback = _make_button_callback(self, i)
                # <<REDACTED 2 lines>>
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
            # <<REDACTED 13 lines>>
    def _index_of(self, value: str) -> int | None:
        """The option a Select *value* names, or None when it names none (#674).

        Also reads a menu posted before #674, whose values were the display
        labels themselves: the deploy that ships this restarts the bot, and #671
        re-arms every menu still on screen — a press on one of those must still
        pick the option it shows.
        """
        options = self._question.options[:25]
        if value.startswith(_OPTION_VALUE_PREFIX):
            number = value[len(_OPTION_VALUE_PREFIX) :]
            if number.isdecimal() and int(number) < len(options):
                return int(number)
            return None
        for i, opt in enumerate(options):
            if _button_label(opt.label, i)[:100] == value:
                return i
        return None

    def _indices_of(self, values: Iterable[str]) -> list[int]:
        """The options *values* name, in order, each once.

        Once: two old-style values can name the same option, and a multiSelect
        toggles — choosing an option twice would un-choose it.
        """
        indices: list[int] = []
        for value in values:
            index = self._index_of(value)
            if index is not None and index not in indices:
                indices.append(index)
        return indices

    def _answers(self, indices: list[int]) -> list[str]:
        """What goes on the bus for *indices*: each option's full label, tagged
        with its index — the index is what the pane is answered with (#674)."""
        options = self._question.options
        return [ChosenOption(options[i].label, i) for i in indices]

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

    # <<REDACTED 1 lines>>
        """Deliver *values* via the bus and show the outcome IN THE THREAD (#536).
# <<REDACTED 1 lines>>
        Both outcomes replace the menu with an embed that still carries the
        question, so the thread reads as "asked X, answered Y" long after the
        buttons are gone — and so a failure is visible to everyone in the thread
        rather than only to the person who clicked.
# <<REDACTED 1 lines>>
        Whatever happens, the buttons stop inviting clicks: on the clicked
        message via this edit, on every other live copy via
        :func:`disable_stale_copies`.
        # <<REDACTED 2 lines>>
        question = self._question
        message = getattr(interaction, "message", None)
        recovering = False
        # <<REDACTED 1 lines>>
            # #651: the bus accepted the answer — that is all that is known
            # right now. The keystrokes have not been sent, and "sent" is not
            # "received" either (#650). The bridge replaces this with the
            # verified outcome once Claude's transcript says what happened.
            embed = ask_sending_embed(question.question, question.header, values)
        elif self._recovery is not None:
            # #671: no waiter is the EXPECTED state after a restart, not a dead
            # end — the TUI menu is still open in the pane with Claude blocked
            # on it. ACK with the same interim ⏳ the live path shows (Discord
            # gives us 3 seconds; typing and confirming can take twelve), then
            # let the answerer type into the pane and rewrite this message with
            # what it verified.
            logger.info(
                "AskView: thread %d has no live session — answering the "
                "still-open pane instead (#671)",
                self._thread_id,
            )
            embed = ask_sending_embed(question.question, question.header, values)
            recovering = True
        # <<REDACTED 3 lines>>
            reason = self._undeliverable_reason(interaction)
            logger.info(
                "AskView: answer %r could not be delivered for thread %d (%s)",
                values,
                self._thread_id,
                reason,
            )
            embed = ask_undelivered_embed(question.question, question.header, values, reason)

        await interaction.response.edit_message(content=None, embed=embed, view=None)

        if recovering:
            await self._recover_via_pane(values, message)

        message_id = getattr(message, "id", None)
        ask_menus.forget(self._thread_id, message_id)
        with contextlib.suppress(Exception):
            await disable_stale_copies(self._thread_id, message_id, _STALE_COPY_NOTE)
        # <<REDACTED 2 lines>>
    async def _recover_via_pane(self, values: list[str], message: object | None) -> None:
        """Type *values* into the still-open TUI menu after a restart (#671).

        The answerer rewrites *message* with the verified outcome when it
        succeeds. A refusal is written here instead — with the reason it gave,
        never a generic "the session was lost": that sentence was shown to
        people whose menu had merely been answered in the terminal (#536).
        """
        if self._recovery is None:
            return
        try:
            ok, reason = await self._recovery(values, message)
        except Exception:
            logger.exception("AskView: pane recovery raised for thread %d (#671)", self._thread_id)
            ok, reason = False, _CLOSED_UNKNOWN
        # Either way this menu is finished, so the restart ledger must let go of
        # it. Found on staging: a delivered answer left its row behind, and the
        # NEXT boot re-armed a menu that no longer existed and then retired it —
        # overwriting the message that recorded the answer. The bridge gets this
        # from ``_close``; this path has no bridge, so it does it here.
        # <<REDACTED 1 lines>>
            with contextlib.suppress(Exception):
                # <<REDACTED 1 lines>>
        if ok:
            return
        # Never the values themselves: on the ✏️ Other path they are whatever the
        # user typed (same convention as _other_callback, which logs a length).
        logger.info(
            "AskView: pane recovery refused %d value(s) for thread %d (%s) (#671)",
            len(values),
            self._thread_id,
            reason,
        )
        if message is not None:
            question = self._question
            with contextlib.suppress(Exception):
                await message.edit(  # type: ignore[attr-defined]
                    content=None,
                    embed=ask_undelivered_embed(question.question, question.header, values, reason),
                    view=None,
                )

    # <<REDACTED 2 lines>>
        indices = self._indices_of(values)
        if not indices:
            # Never deliver an empty answer: that is the #315 pre-emption signal,
            # and the bridge would Esc the menu away. The values are the bot's
            # own (option keys or labels), so they are safe to log.
            logger.warning(
                "AskView: Select value(s) %r name no option of this menu for thread %d "
                "— nothing delivered (#674)",
                values,
                self._thread_id,
            )
            await interaction.response.send_message(
                "選んだ選択肢がこの質問の中に見つからなかったため、送信しませんでした。"
                "もう一度選び直してください。",
                ephemeral=True,
            )
            return
        await self._deliver(interaction, self._answers(indices))
# <<REDACTED 1 lines>>
    async def _multi_select_record(self, interaction: discord.Interaction) -> None:
        """Record a multiSelect choice WITHOUT delivering — the user submits via
        the ✅ confirm button (#418).

        The choice is written **into the message** as ``default=True`` on the
        chosen options, not just into ``self._selected_indices`` (#672).  An
        instance attribute is reachable only from the one View object in the one
        process that rendered the menu, and three ordinary routes reach ✅ 確定
        without it: a dropdown whose interaction never fired (a mobile sheet
        dismissed without submitting), a confirm processed before the record,
        and a *different* View instance entirely (the restart-restored view of
        #671, a watchdog re-bridge of #633).  All three produced the same
        silence — press ✅ 確定, nothing happens, not one line in the log.

        Marking the options also fixes what the user sees: Discord re-renders
        the Select on every edit, so a recorded choice used to disappear from
        the dropdown and read as "my selection did not take".
        """
        values = interaction.data.get("values", [])  # type: ignore[union-attr]
        self._selected_indices = self._indices_of(values)
        self._mark_selected(self._selected_indices)
        options = self._question.options
        chosen = (
            ", ".join(_button_label(options[i].label, i) for i in self._selected_indices)
            or "（未選択）"
        )
        logger.info(
            "AskView: recorded multiSelect choice (options %r) for thread %d (#672)",
            self._selected_indices,
            self._thread_id,
        )
        # Full-size, not Discord's grey ``-#`` small text (#536): "picked but not
        # submitted" is the state users mistook for "submitted and ignored", so
        # it has to be the loudest thing on the message, not the quietest.
        #
        # ``view=self`` is load-bearing: without it the default marks above are
        # never sent to Discord and the selection stays process-local (#672).
        # <<REDACTED 1 lines>>
            content=(
                f"🔲 **選択中:** {chosen}\n"
                "**まだ送信されていません** — 下の「✅ 確定」を押すと Claude に届きます。"
            ),
            view=self,
        )

    def _mark_selected(self, indices: list[int]) -> None:
        """Mark exactly the options at *indices* as ``default`` on this View's
        Select (#672) — under the same value :meth:`_recover_selection` reads."""
        if self._select is None:
            return
        chosen = {_option_value(i) for i in indices}
        for option in self._select.options:
            option.default = option.value in chosen

    def _recover_selection(self, interaction: discord.Interaction) -> list[int]:
        """The choice recorded on the message, when this View does not hold it (#672).

        Discord echoes a message's components back on every interaction it
        carries, so the ``default=True`` marks written by
        :meth:`_multi_select_record` are readable here even when the recording
        happened in another View instance — or another process.
        """
        if self._select is None:
            return []
        message = getattr(interaction, "message", None)
        # Only ever deliver something this question actually offers.  The marks
        # are bot-authored (a user cannot edit a bot message's components), but
        # the answer they feed becomes keystrokes in a live pane, so the option
        # set stays the authority rather than whatever the message happens to
        # carry — ``_indices_of`` drops any value that names none of it.
        for row in getattr(message, "components", None) or ():
            for child in getattr(row, "children", None) or ():
                if getattr(child, "custom_id", None) != self._select.custom_id:
                    continue
                return self._indices_of(
                    option.value
                    for option in getattr(child, "options", None) or ()
                    if getattr(option, "default", False)
                )
        return []

    async def _confirm_callback(self, interaction: discord.Interaction) -> None:
        """Deliver the recorded multiSelect choice when ✅ 確定 is pressed (#418).

        Falls back to the selection stored on the message when this View has
        none of its own (#672) — see :meth:`_recover_selection`.  Every outcome
        is logged: the silence of this path is what hid the incident, because
        the only trace was the *absence* of a keystroke log downstream (#585).
        """
        indices = self._selected_indices or self._recover_selection(interaction)
        if not indices:
            logger.info(
                "AskView: ✅ confirm pressed with nothing selected for thread %d "
                "— nothing delivered (#672)",
                self._thread_id,
            )
            await interaction.response.send_message(
                "1つ以上選択してから「✅ 確定」を押してください。"
                "（プルダウンで選び、選択が「🔲 選択中:」に出てから押してください）",
                ephemeral=True,
            )
            return
        recovered = not self._selected_indices
        values = self._answers(indices)
        logger.info(
            "AskView: ✅ confirm pressed for thread %d — delivering %r%s (#672)",
            self._thread_id,
            values,
            " (recovered from the message)" if recovered else "",
        )
        # <<REDACTED 1 lines>>

    # <<REDACTED 6 lines>>
            message = getattr(interaction, "message", None)
            question = self._question
            recovering = False
            if delivered:
                # #651: interim — the bridge confirms and rewrites this.
                embed = ask_sending_embed(question.question, question.header, [modal.answer])
            elif self._recovery is not None:
                # #671: same fallback as the buttons — the pane still has the
                # free-text affordance open, so a typed answer can still land.
                embed = ask_sending_embed(question.question, question.header, [modal.answer])
                recovering = True
            else:
                # <<REDACTED 2 lines>>
                reason = self._undeliverable_reason(interaction)
                # The free text is whatever the user typed — length only, never
                # the content (same convention as the prompt logging in
                # _run_helper: a modal is exactly where a secret would be pasted).
                # <<REDACTED 1 lines>>
                    "AskView._other_callback: free text (%d chars) undeliverable "
                    "for thread %d (%s)",
                    len(modal.answer),
                    # <<REDACTED 1 lines>>
                    reason,
                )
                embed = ask_undelivered_embed(
                    question.question, question.header, [modal.answer], reason
                # <<REDACTED 1 lines>>
            # The modal already consumed the interaction response, so the menu
            # message is edited directly rather than through the interaction.
            if message is not None:
                with contextlib.suppress(discord.HTTPException, Exception):
                    await message.edit(content=None, embed=embed, view=None)
            if recovering:
                await self._recover_via_pane([modal.answer], message)
            message_id = getattr(message, "id", None)
            ask_menus.forget(self._thread_id, message_id)
            with contextlib.suppress(Exception):
                await disable_stale_copies(self._thread_id, message_id, _STALE_COPY_NOTE)
            # <<REDACTED 24 lines>>
def _make_button_callback(view: AskView, index: int):
    """Factory that creates a button callback with *view* and option *index* bound.
# <<REDACTED 5 lines>>

    Bound to the index, not the label (#674): two options the pane parser could
    not read both have the label ``""`` (#579), and matching by label always
    answered with the first of them.
    # <<REDACTED 3 lines>>
        await view._deliver(interaction, view._answers([index]))
# <<REDACTED 2 lines>>
