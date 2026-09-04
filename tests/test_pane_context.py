"""Tests for c_lord.discord_ui.pane_context — #686 markdown replacement.

While an AskUserQuestion menu is open the CLI writes nothing to the jsonl, so the
prose above the menu can only be read from the **pane**: box-drawn tables, hard
wraps at the terminal width, markdown stripped. That is what Discord shows today,
and it is unreadable there (Discord is not monospaced). The CLI's own markdown
does arrive — after the menu resolves — but the mirror threw it away as an
already-delivered duplicate.

So keep the pane copy's immediacy (#399/#549) and swap its *text* for the
markdown when it lands: edit the messages already in the thread, never post new
ones. Every failure path must leave the pane copy standing — a lost 経緯 is worse
than an ugly one.
"""

from __future__ import annotations

from unittest.mock import AsyncMock, MagicMock

import discord
import pytest

from c_lord.discord_ui.pane_context import replace_pane_context

_PANE = (
    "  比較しました。\n"
    "  ┌─────────┬──────────┐\n"
    "  │  タグ   │ バージョン │\n"
    "  ├─────────┼──────────┤\n"
    "  │ latest  │ 2026.8.1 │\n"
    "  └─────────┴──────────┘\n"
    "  私の推しは latest です。"
)
_MARKDOWN = (
    "比較しました。\n\n"
    "| タグ | バージョン |\n|---|---|\n| latest | **2026.8.1** |\n\n"
    "私の推しは latest です。"
)


def _msg() -> MagicMock:
    m = MagicMock(spec=discord.Message)
    m.edit = AsyncMock()
    m.delete = AsyncMock()
    m.content = _PANE
    return m


@pytest.mark.asyncio
async def test_the_pane_copy_is_edited_into_the_markdown():
    """#686 AC1: the same message becomes the markdown — no new message."""
    m = _msg()

    assert await replace_pane_context([m], _MARKDOWN) is True

    m.edit.assert_awaited_once()
    assert m.edit.await_args.kwargs["content"] == _MARKDOWN
    m.delete.assert_not_awaited()


@pytest.mark.asyncio
async def test_each_chunk_lands_in_its_own_existing_message():
    """A prose that took two messages is replaced in two messages, in order."""
    a, b = _msg(), _msg()
    markdown = ("段落です。" * 500) + "\n\n私の推しは (A) です。"  # > 2000 chars

    assert await replace_pane_context([a, b], markdown) is True

    joined = a.edit.await_args.kwargs["content"] + b.edit.await_args.kwargs["content"]
    assert joined.replace("\n", "") == markdown.replace("\n", "")


@pytest.mark.asyncio
async def test_surplus_messages_are_removed_only_after_every_edit_lands():
    """Markdown is usually shorter than the pane's hard-wrapped rendering, so a
    3-message prose can collapse into 1 — the leftovers must not stay behind
    showing the old text."""
    a, b, c = _msg(), _msg(), _msg()

    assert await replace_pane_context([a, b, c], _MARKDOWN) is True

    assert a.edit.await_args.kwargs["content"] == _MARKDOWN
    b.delete.assert_awaited_once()
    c.delete.assert_awaited_once()
    a.delete.assert_not_awaited()


@pytest.mark.asyncio
async def test_markdown_that_needs_more_messages_is_declined():
    """Nothing may be posted on top (#680), so if it cannot fit in the messages
    we already have, the pane copy stays as it is."""
    a = _msg()
    huge = "あ" * 5000  # 3 chunks into 1 message: impossible

    assert await replace_pane_context([a], huge) is False

    a.edit.assert_not_awaited()
    a.delete.assert_not_awaited()


@pytest.mark.asyncio
async def test_a_failed_edit_never_deletes_anything():
    """#686 AC5: a failed replacement must not cost the reader the text."""
    a, b = _msg(), _msg()
    a.edit = AsyncMock(side_effect=discord.HTTPException(MagicMock(status=500), "boom"))

    assert await replace_pane_context([a, b], _MARKDOWN) is False

    a.delete.assert_not_awaited()
    b.delete.assert_not_awaited()


@pytest.mark.asyncio
async def test_nothing_to_replace_is_not_a_failure():
    assert await replace_pane_context([], _MARKDOWN) is False


@pytest.mark.asyncio
async def test_table_images_ride_on_the_last_replaced_message(monkeypatch):
    """#686 AC2: the table only becomes a picture once the markdown is there —
    the box-drawn pane copy has no table for the renderer to find."""
    monkeypatch.setattr(
        "c_lord.discord_ui.pane_context.get_table_images",
        lambda text: [("table_1.png", b"PNG")],
    )
    a, b = _msg(), _msg()
    markdown = ("段落です。" * 500) + "\n\n| タグ | 版 |\n|---|---|\n| latest | 1 |"

    assert await replace_pane_context([a, b], markdown) is True

    assert "attachments" not in a.edit.await_args.kwargs
    files = b.edit.await_args.kwargs["attachments"]
    assert [f.filename for f in files] == ["table_1.png"]


@pytest.mark.asyncio
async def test_no_tables_means_no_attachments_argument(monkeypatch):
    """Passing attachments=[] would WIPE any attachment the message has."""
    monkeypatch.setattr("c_lord.discord_ui.pane_context.get_table_images", lambda text: [])
    a = _msg()

    assert await replace_pane_context([a], _MARKDOWN) is True

    assert "attachments" not in a.edit.await_args.kwargs
