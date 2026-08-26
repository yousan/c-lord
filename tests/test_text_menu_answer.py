"""Typing an answer while a menu is open (#536 AC7, yousan の裁定: A案).

Pressing a button was the ONLY way to answer a menu. Typing — the thing a user
does when the buttons feel unresponsive — silently threw the question away and
ran the text as a brand-new instruction (``⚡ Interrupted``). yousan hit exactly
that: 「選んだあとに決定されていない気がして `y` とメッセージを送っていた」.

Decision (recorded in the issue body): the text IS the answer. A wrong guess is
recoverable with one button, so the cost of guessing "answer" is far below the
cost of dropping a question the user was trying to answer.

The exception is a menu that has no free-text row (plan approval): typing into
one would mis-send keystrokes, so those keep the interrupt behaviour.
"""

from __future__ import annotations

from unittest.mock import AsyncMock, MagicMock

import discord
import pytest

from c_lord.cogs.claude_chat import ClaudeChatCog
from c_lord.discord_ui.ask_bus import ask_bus


def _cog() -> ClaudeChatCog:
    """A cog with just enough wiring for _maybe_answer_open_menu."""
    cog = ClaudeChatCog.__new__(ClaudeChatCog)
    cog.bot = MagicMock()
    cog._authorizer = None
    return cog


def _message(thread_id: int, content: str, attachments: list | None = None) -> MagicMock:
    message = MagicMock(spec=discord.Message)
    message.content = content
    message.attachments = attachments or []
    thread = MagicMock(spec=discord.Thread)
    thread.id = thread_id
    thread.send = AsyncMock(return_value=MagicMock())
    message.channel = thread
    return message, thread


@pytest.mark.asyncio
async def test_text_is_delivered_as_the_menu_answer() -> None:
    """The whole point: `y` reaches Claude as the answer, not as a new order."""
    tid = 536_7001
    queue = ask_bus.register(tid, allow_free_text=True)
    assert queue is not None
    try:
        message, thread = _message(tid, "y")
        handled = await _cog()._maybe_answer_open_menu(message, thread)

        assert handled is True, "the text should have been routed to the open menu"
        assert queue.get_nowait() == ["y"]
    finally:
        ask_bus.unregister(tid)


@pytest.mark.asyncio
async def test_the_thread_says_the_text_was_used_as_an_answer() -> None:
    """Routing silently would trade one invisible behaviour for another."""
    tid = 536_7002
    ask_bus.register(tid, allow_free_text=True)
    try:
        message, thread = _message(tid, "おぷーのままで")
        await _cog()._maybe_answer_open_menu(message, thread)

        thread.send.assert_awaited()
        kwargs = thread.send.await_args.kwargs
        text = str(kwargs.get("content") or "")
        assert "おぷーのままで" in text
        assert kwargs.get("view") is not None, "no way to say 'that was a new instruction'"
    finally:
        ask_bus.unregister(tid)


@pytest.mark.asyncio
async def test_no_open_menu_means_business_as_usual() -> None:
    """With no menu open, nothing changes — the message is a new instruction."""
    tid = 536_7003
    message, thread = _message(tid, "普通の指示")
    assert await _cog()._maybe_answer_open_menu(message, thread) is False
    thread.send.assert_not_awaited()


@pytest.mark.asyncio
async def test_a_menu_without_a_free_text_row_is_not_answered_by_text() -> None:
    """Plan-approval menus have no `Type something.` row (#251).

    Typing into one would send the keystrokes into a menu that cannot take them,
    so those keep the interrupt path.
    """
    tid = 536_7004
    queue = ask_bus.register(tid, allow_free_text=False)
    assert queue is not None
    try:
        message, thread = _message(tid, "やっぱりBで")
        assert await _cog()._maybe_answer_open_menu(message, thread) is False
        assert queue.empty()
    finally:
        ask_bus.unregister(tid)


@pytest.mark.asyncio
async def test_an_attachment_is_a_new_instruction_not_an_answer() -> None:
    """A file makes no sense as a menu choice — that is a fresh request."""
    tid = 536_7005
    ask_bus.register(tid, allow_free_text=True)
    try:
        message, thread = _message(tid, "これ見て", attachments=[MagicMock()])
        assert await _cog()._maybe_answer_open_menu(message, thread) is False
    finally:
        ask_bus.unregister(tid)


@pytest.mark.asyncio
async def test_empty_text_is_not_an_answer() -> None:
    tid = 536_7006
    ask_bus.register(tid, allow_free_text=True)
    try:
        message, thread = _message(tid, "   ")
        assert await _cog()._maybe_answer_open_menu(message, thread) is False
    finally:
        ask_bus.unregister(tid)


@pytest.mark.asyncio
async def test_register_defaults_to_no_free_text() -> None:
    """Free text must be opted into by the bridge that knows the menu's shape."""
    from c_lord.discord_ui.ask_bus import AskAnswerBus

    bus = AskAnswerBus()
    bus.register(1)
    assert bus.accepts_free_text(1) is False
    assert bus.accepts_free_text(2) is False  # unknown thread


class TestUndo:
    """One button turns a mis-read answer back into a new instruction."""

    @pytest.mark.asyncio
    async def test_undo_button_reruns_the_message_as_an_instruction(self) -> None:
        from c_lord.discord_ui.views import TextAnsweredMenuView

        rerun = AsyncMock()
        view = TextAnsweredMenuView(rerun)
        interaction = MagicMock()
        interaction.response.defer = AsyncMock()
        interaction.message = MagicMock()
        interaction.message.edit = AsyncMock()

        await view.rerun_button.callback(interaction)

        rerun.assert_awaited_once()
        assert view.rerun_button.disabled is True


@pytest.mark.asyncio
async def test_a_long_message_is_an_instruction_not_a_menu_answer() -> None:
    """Long prose is a new request, not a pick from a 3-option menu.

    The ✏️ Other modal caps free text at 500 characters, and a menu answer that
    exceeds it would also be typed character-by-character onto a TUI row. Both
    say the same thing: past that length this is an instruction.
    """
    tid = 536_7007
    queue = ask_bus.register(tid, allow_free_text=True)
    assert queue is not None
    try:
        message, thread = _message(tid, "あ" * 501)
        assert await _cog()._maybe_answer_open_menu(message, thread) is False
        assert queue.empty()
    finally:
        ask_bus.unregister(tid)


@pytest.mark.asyncio
async def test_a_message_at_the_limit_is_still_an_answer() -> None:
    tid = 536_7008
    queue = ask_bus.register(tid, allow_free_text=True)
    assert queue is not None
    try:
        message, thread = _message(tid, "あ" * 500)
        assert await _cog()._maybe_answer_open_menu(message, thread) is True
        assert queue.get_nowait() == ["あ" * 500]
    finally:
        ask_bus.unregister(tid)
