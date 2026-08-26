"""What the user sees after pressing a choice button (#536).

Pressing a button used to be a coin flip: on success the question vanished and
left a one-line grey ``-# ✅ Selected: X``; on failure the only feedback was an
*ephemeral* "the bot was restarted" — invisible to everyone else, gone on
refresh, wrong whenever the bot had not restarted, and the buttons stayed live.
yousan's report: 「選んだあとに決定されていない気がして y とメッセージを送っていた」.

These tests pin the after-the-click contract:
- the outcome always lands in the thread, never only in an ephemeral;
- a failure names the real cause and disables the buttons;
- a success keeps BOTH the question and the answer readable afterwards;
- other live copies of the same menu stop accepting clicks.
"""

from __future__ import annotations

import datetime as dt
from unittest.mock import AsyncMock, MagicMock

import pytest

from c_lord.claude.types import AskOption, AskQuestion
from c_lord.discord_ui import ask_view as ask_view_mod
from c_lord.discord_ui.ask_bus import AskAnswerBus
from c_lord.discord_ui.ask_view import AskView


def _question(header: str = "方針") -> AskQuestion:
    return AskQuestion(
        question="どの案にしますか?",
        header=header,
        options=[AskOption("A案", "その場で直す"), AskOption("B案", "新規ファイル")],
    )


def _interaction(created_at: dt.datetime | None = None) -> MagicMock:
    """A button interaction whose message is a normal (non-ephemeral) thread message."""
    interaction = MagicMock()
    interaction.response.edit_message = AsyncMock()
    interaction.response.send_message = AsyncMock()
    interaction.message = MagicMock()
    interaction.message.id = 777
    interaction.message.created_at = created_at or dt.datetime.now(dt.UTC)
    return interaction


def _edit_kwargs(interaction: MagicMock) -> dict:
    assert interaction.response.edit_message.await_args is not None, "the message was not edited"
    return interaction.response.edit_message.await_args.kwargs


def _edit_text(interaction: MagicMock) -> str:
    """All user-visible text of the edit (content + embed title/description)."""
    kwargs = _edit_kwargs(interaction)
    parts = [str(kwargs.get("content") or "")]
    embed = kwargs.get("embed")
    if embed is not None:
        parts += [str(embed.title or ""), str(embed.description or "")]
    return "\n".join(parts)


class TestUndeliveredAnswer:
    """AC1–AC3: the answer did not reach Claude — say so, correctly, in the thread."""

    @pytest.mark.asyncio
    async def test_reported_in_the_thread_not_only_ephemerally(self) -> None:
        """AC1: an ephemeral is invisible to the thread and gone on refresh."""
        bus = AskAnswerBus()  # nobody waiting → delivery fails
        view = AskView(_question(), thread_id=536_0001, q_idx=0, bus=bus)
        interaction = _interaction()

        await view._deliver(interaction, ["A案"])

        interaction.response.send_message.assert_not_awaited()
        interaction.response.edit_message.assert_awaited()

    @pytest.mark.asyncio
    async def test_disables_the_buttons(self) -> None:
        """AC3: buttons that can no longer deliver must stop inviting clicks."""
        bus = AskAnswerBus()
        view = AskView(_question(), thread_id=536_0002, q_idx=0, bus=bus)
        interaction = _interaction()

        await view._deliver(interaction, ["A案"])

        assert _edit_kwargs(interaction).get("view") is None

    @pytest.mark.asyncio
    async def test_says_resolved_elsewhere_not_restarted(self) -> None:
        """AC2: the bot did NOT restart — the menu was answered another way.

        This is the wording bug: every failure claimed a restart, so a user who
        had just answered in the terminal was told the bot fell over.
        """
        bus = AskAnswerBus()
        tid = 536_0003
        bus.register(tid)
        bus.unregister(tid)
        bus.note_closed(tid, "terminal")
        view = AskView(_question(), thread_id=tid, q_idx=0, bus=bus)
        interaction = _interaction()

        await view._deliver(interaction, ["A案"])

        text = _edit_text(interaction)
        assert "再起動" not in text, f"claimed a restart that never happened: {text!r}"
        assert "端末" in text, f"should name the real cause (answered in the pane): {text!r}"

    @pytest.mark.asyncio
    async def test_says_already_answered_when_a_click_resolved_it(self) -> None:
        """AC2: a stale copy clicked after the menu was already answered."""
        bus = AskAnswerBus()
        tid = 536_0004
        bus.note_closed(tid, "answered")
        view = AskView(_question(), thread_id=tid, q_idx=0, bus=bus)
        interaction = _interaction()

        await view._deliver(interaction, ["A案"])

        text = _edit_text(interaction)
        assert "回答済み" in text, text
        assert "再起動" not in text, text

    @pytest.mark.asyncio
    async def test_says_restarted_for_a_menu_older_than_this_process(self, monkeypatch) -> None:
        """AC2: the restart wording is still correct where it IS the cause."""
        monkeypatch.setattr(
            ask_view_mod,
            "_PROCESS_STARTED_AT",
            dt.datetime.now(dt.UTC) - dt.timedelta(minutes=5),
        )
        bus = AskAnswerBus()  # a fresh process knows nothing about this thread
        view = AskView(_question(), thread_id=536_0005, q_idx=0, bus=bus)
        interaction = _interaction(created_at=dt.datetime.now(dt.UTC) - dt.timedelta(hours=2))

        await view._deliver(interaction, ["A案"])

        assert "再起動" in _edit_text(interaction)

    @pytest.mark.asyncio
    async def test_keeps_the_question_and_what_was_clicked(self) -> None:
        """A failure the user cannot interpret is barely better than silence."""
        bus = AskAnswerBus()
        view = AskView(_question(), thread_id=536_0006, q_idx=0, bus=bus)
        interaction = _interaction()

        await view._deliver(interaction, ["A案"])

        text = _edit_text(interaction)
        assert "どの案にしますか?" in text
        assert "A案" in text


class TestDeliveredAnswer:
    """AC4: after a successful click the thread still shows what was asked."""

    @pytest.mark.asyncio
    async def test_keeps_both_the_question_and_the_answer(self) -> None:
        bus = AskAnswerBus()
        tid = 536_0010
        bus.register(tid)
        view = AskView(_question(), thread_id=tid, q_idx=0, bus=bus)
        interaction = _interaction()

        await view._deliver(interaction, ["A案"])

        text = _edit_text(interaction)
        assert "どの案にしますか?" in text, f"the question was wiped: {text!r}"
        assert "方針" in text, f"the header was wiped: {text!r}"
        assert "A案" in text, f"the answer is missing: {text!r}"

    @pytest.mark.asyncio
    async def test_the_answer_is_not_hidden_in_small_text(self) -> None:
        """``-#`` is Discord's small grey style — the answer must not whisper."""
        bus = AskAnswerBus()
        tid = 536_0011
        bus.register(tid)
        view = AskView(_question(), thread_id=tid, q_idx=0, bus=bus)
        interaction = _interaction()

        await view._deliver(interaction, ["A案"])

        content = _edit_kwargs(interaction).get("content") or ""
        assert not content.startswith("-#"), content


class TestOtherCopies:
    """AC5: when one copy is answered, the others must stop accepting clicks."""

    @pytest.mark.asyncio
    async def test_other_copies_are_disabled_when_one_is_answered(self) -> None:
        from c_lord.discord_ui.ask_menus import ask_menus

        bus = AskAnswerBus()
        tid = 536_0020
        bus.register(tid)

        stale = MagicMock()
        stale.id = 999
        stale.edit = AsyncMock()
        clicked = MagicMock()
        clicked.id = 777
        clicked.edit = AsyncMock()
        ask_menus.register(tid, stale)
        ask_menus.register(tid, clicked)
        try:
            view = AskView(_question(), thread_id=tid, q_idx=0, bus=bus)
            await view._deliver(_interaction(), ["A案"])
            stale.edit.assert_awaited()
            assert stale.edit.await_args.kwargs.get("view") is None
            clicked.edit.assert_not_awaited()  # the clicked one is edited via the interaction
        finally:
            ask_menus.clear(tid)


class TestMultiSelectPending:
    """AC6: "chosen but not submitted" must be unmistakable, not grey small text."""

    @pytest.mark.asyncio
    async def test_pending_selection_is_not_small_text(self) -> None:
        q = AskQuestion(
            question="どれ?",
            header="env",
            options=[AskOption("A"), AskOption("B"), AskOption("C")],
            multi_select=True,
        )
        view = AskView(q, thread_id=536_0030, q_idx=0)
        interaction = _interaction()
        interaction.data = {"values": ["A", "C"]}

        await view._multi_select_record(interaction)

        content = _edit_kwargs(interaction).get("content") or ""
        assert not content.startswith("-#"), f"pending state still whispers: {content!r}"
        assert "確定" in content
        assert "A" in content and "C" in content


class TestEmptyLabelGuard:
    """#579 AC3: a label the parser could not read must never reach Discord.

    Discord rejects a button with an empty label — 400 ``In
    components.0.components.1.label: This field is required`` — and rejects the
    WHOLE message, so one unreadable option silences the entire menu. The parser
    fix removes the known cause; this is the backstop for the next rendering
    nobody has seen yet.
    """

    def _labels(self, question: AskQuestion) -> list[str]:
        view = AskView(question, thread_id=579_001, q_idx=0, bus=AskAnswerBus())
        out = []
        for child in view.children:
            label = getattr(child, "label", None)
            if label is not None:
                out.append(label)
            for opt in getattr(child, "options", []) or []:
                out.append(opt.label)
        return out

    @pytest.mark.asyncio
    async def test_no_button_is_built_with_an_empty_label(self) -> None:
        q = AskQuestion(
            question="どれ?",
            header="h",
            options=[AskOption("A案", "one"), AskOption("", "wrapped away"), AskOption("C案", "")],
        )
        assert all(self._labels(q)), self._labels(q)

    @pytest.mark.asyncio
    async def test_the_unreadable_option_still_occupies_its_slot(self) -> None:
        """Answers are delivered as ``Down × index`` — dropping an option would
        shift every option after it and select the wrong one."""
        q = AskQuestion(
            question="どれ?",
            header="h",
            options=[AskOption("A案", ""), AskOption("", ""), AskOption("C案", "")],
        )
        labels = [
            child.label
            for child in AskView(q, thread_id=579_002, q_idx=0, bus=AskAnswerBus()).children
            if getattr(child, "custom_id", "").rsplit("_", 1)[-1].isdigit()
        ]
        assert len(labels) == 3, labels
        assert labels[0] == "A案" and labels[2] == "C案", labels

    @pytest.mark.asyncio
    async def test_select_menu_options_are_never_empty(self) -> None:
        """≥5 options render as a Select, whose option labels have the same rule."""
        q = AskQuestion(
            question="どれ?",
            header="h",
            options=[AskOption(f"opt{i}", "") for i in range(4)] + [AskOption("", "")],
        )
        assert all(self._labels(q)), self._labels(q)
