# <<REDACTED 3 lines>>
- types: AskOption, AskQuestion, ToolCategory.ASK, _parse_ask_questions
# <<REDACTED 5 lines>>
import asyncio

# <<REDACTED 2 lines>>
from c_lord.claude.types import (
    FREE_TEXT_NOTES,
    FREE_TEXT_ROW,
    AskOption,
    AskQuestion,
    ToolCategory,
    _parse_ask_questions,
)
from c_lord.discord_ui.embeds import ask_embed
# <<REDACTED 23 lines>>
        from c_lord.claude.types import TOOL_CATEGORIES
# <<REDACTED 6 lines>>
# _parse_ask_questions
# <<REDACTED 3 lines>>
class TestParseAskQuestions:
    # <<REDACTED 35 lines>>
    def test_full_parse(self) -> None:
        tool_input = {
            # <<REDACTED 6 lines>>
                        {"label": "JWT tokens", "description": "Stateless, good for APIs"},
                        {"label": "Session cookies", "description": "Simple, stateful"},
                        {"label": "OAuth2", "description": "Federated identity"},
                    ],
                }
            ]
        }
        # <<REDACTED 2 lines>>
        q = questions[0]
        # <<REDACTED 3 lines>>
        assert len(q.options) == 3
        assert q.options[0].label == "JWT tokens"
        assert q.options[0].description == "Stateless, good for APIs"
        assert q.options[2].label == "OAuth2"
# <<REDACTED 2 lines>>
        tool_input = {
            # <<REDACTED 5 lines>>
                    "options": [{"label": "Auth"}, {"label": "Logging"}],
                # <<REDACTED 4 lines>>
        assert questions[0].multi_select is True

    def test_preview_options_mean_the_notes_layout(self) -> None:
        """#650: ``preview`` is what makes Claude Code draw the other TUI menu.

        The menu the mirror bridges is never read off the pane, so the tool
        input is the only place the layout can be known here — and getting it
        wrong loses the user's typed answer (the answer keystrokes land on
        "Chat about this", which answers "(No answer provided)").
        """
        tool_input = {
            "questions": [
                {
                    "question": "どの配色にしますか？",
                    "header": "配色案",
                    "options": [
                        {"label": "案A ダーク", "preview": "#121212 …"},
                        {"label": "案B ライト", "preview": "#ffffff …"},
                    ],
                }
            ]
        }
        assert _parse_ask_questions(tool_input)[0].free_text_mode == FREE_TEXT_NOTES

    def test_one_preview_is_enough_to_switch_the_layout(self) -> None:
        """The CLI switches on ``options.some(o => o.preview !== undefined)``."""
        tool_input = {
            "questions": [
                {
                    "question": "Choose?",
                    "options": [{"label": "A"}, {"label": "B", "preview": "…"}],
                }
            ]
        }
        assert _parse_ask_questions(tool_input)[0].free_text_mode == FREE_TEXT_NOTES

    def test_no_preview_keeps_the_classic_row(self) -> None:
        tool_input = {
            "questions": [{"question": "Choose?", "options": [{"label": "A"}, {"label": "B"}]}]
        }
        assert _parse_ask_questions(tool_input)[0].free_text_mode == FREE_TEXT_ROW

    def test_multi_select_keeps_the_row_even_with_previews(self) -> None:
        """A multiSelect menu never gets the preview layout, previews or not."""
        tool_input = {
            "questions": [
                {
                    "question": "Which?",
                    "multiSelect": True,
                    "options": [{"label": "A", "preview": "…"}, {"label": "B", "preview": "…"}],
                }
            ]
        }
        assert _parse_ask_questions(tool_input)[0].free_text_mode == FREE_TEXT_ROW

# <<REDACTED 31 lines>>
# ---------------------------------------------------------------------------
# AskView — free-text ✏️ Other gating (#251)
# ---------------------------------------------------------------------------


class TestAskViewOtherGating:
    """#251: plan-approval menus reuse AskView but must suppress the free-text
    ✏️ Other affordance — plan's "Tell Claude what to change" is a normal
    numbered option, not AskUserQuestion's "Type something." row, so a generic
    Other modal would mis-send keystrokes into the open TUI menu.
    """

    @staticmethod
    def _other_button_ids(q: AskQuestion) -> list[str]:
        from c_lord.discord_ui.ask_view import AskView

        view = AskView(q, thread_id=123, q_idx=0)
        return [
            cid
            for c in view.children
            if (cid := getattr(c, "custom_id", "")) and cid.endswith("_other")
        ]

    @pytest.mark.asyncio
    async def test_other_button_present_by_default(self) -> None:
        q = AskQuestion(
            question="Pick one",
            options=[AskOption(label="A"), AskOption(label="B")],
        )
        assert q.allow_other is True
        assert self._other_button_ids(q)  # AskUserQuestion keeps ✏️ Other

    @pytest.mark.asyncio
    async def test_other_button_suppressed_for_plan(self) -> None:
        q = AskQuestion(
            question="Would you like to proceed?",
            options=[AskOption(label="Yes"), AskOption(label="No")],
            allow_other=False,
        )
        assert self._other_button_ids(q) == []


class TestAskViewMultiSelectConfirm:
    """#418: a multiSelect question needs an explicit ✅ confirm button — the
    Select records the choice, the button submits it (single-select stays
    immediate, with no confirm button)."""

    @staticmethod
    def _confirm_button_ids(q: AskQuestion) -> list[str]:
        from c_lord.discord_ui.ask_view import AskView

        view = AskView(q, thread_id=123, q_idx=0)
        return [
            cid
            for c in view.children
            if (cid := getattr(c, "custom_id", "")) and cid.endswith("_confirm")
        ]

    @pytest.mark.asyncio
    async def test_confirm_button_present_for_multi_select(self) -> None:
        q = AskQuestion(
            question="pick many",
            options=[AskOption(label="A"), AskOption(label="B"), AskOption(label="C")],
            multi_select=True,
        )
        assert self._confirm_button_ids(q), "multiSelect must expose a ✅ confirm button"

    @pytest.mark.asyncio
    async def test_no_confirm_button_for_single_select(self) -> None:
        q = AskQuestion(
            question="pick one",
            # <<REDACTED 3 lines>>
        assert self._confirm_button_ids(q) == []

    @pytest.mark.asyncio
    async def test_select_records_only_confirm_delivers_all(self) -> None:
        """Selecting in the dropdown records (does NOT deliver); pressing ✅
        confirm delivers every recorded value to the bus."""
        from unittest.mock import AsyncMock, MagicMock

        from c_lord.discord_ui.ask_bus import ask_bus
        from c_lord.discord_ui.ask_view import AskView

        tid = 418_0010
        q = AskQuestion(
            question="pick",
            options=[AskOption(label="A"), AskOption(label="B"), AskOption(label="C")],
            multi_select=True,
        )
        view = AskView(q, thread_id=tid, q_idx=0)
        queue = ask_bus.register(tid)
        try:
            # 1) Select fires — must record only, not deliver.
            sel = MagicMock()
            # The values the rendered options carry — options are identified
            # by index, not label (#674).
            select = next(c for c in view.children if hasattr(c, "options"))
            sel.data = {"values": [o.value for o in select.options if o.label in ("A", "C")]}
            sel.response.edit_message = AsyncMock()
            await view._multi_select_record(sel)
            assert queue.empty(), "selection must not deliver before confirm"

            # 2) Confirm button delivers all recorded values.
            conf = MagicMock()
            conf.response.edit_message = AsyncMock()
            await view._confirm_callback(conf)
            delivered = await asyncio.wait_for(queue.get(), timeout=1.0)
            assert delivered == ["A", "C"]
        finally:
            ask_bus.unregister(tid)

    @pytest.mark.asyncio
    async def test_confirm_without_selection_errors_and_does_not_deliver(self) -> None:
        from unittest.mock import AsyncMock, MagicMock

        from c_lord.discord_ui.ask_bus import ask_bus
        from c_lord.discord_ui.ask_view import AskView

        tid = 418_0011
        q = AskQuestion(
            question="pick",
            # <<REDACTED 1 lines>>
            multi_select=True,
        )
        view = AskView(q, thread_id=tid, q_idx=0)
        queue = ask_bus.register(tid)
        try:
            conf = MagicMock()
            conf.response.send_message = AsyncMock()
            conf.response.edit_message = AsyncMock()
            await view._confirm_callback(conf)
            conf.response.send_message.assert_awaited()  # ephemeral "select first"
            assert queue.empty()
        finally:
            ask_bus.unregister(tid)
