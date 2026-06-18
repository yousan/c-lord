"""Tests for bridge_pane_ask — the in-pane AskUserQuestion → Discord bridge.

#359: a menu can be answered/cancelled directly in the tmux pane (the human
attaches and types).  When that happens the bridge must stop waiting for a
Discord click — otherwise the suspended ``run_claude`` holds the thread lock
for up to 24h and the whole thread freezes.
"""

from __future__ import annotations

import asyncio
from unittest.mock import AsyncMock, MagicMock

import pytest

from c_lord.claude.types import AskOption, AskQuestion
from c_lord.discord_ui import ask_handler
from c_lord.discord_ui.ask_bus import ask_bus
from c_lord.discord_ui.ask_handler import bridge_pane_ask


def _question() -> AskQuestion:
    return AskQuestion(
        question="repro?",
        header="テスト",
        options=[AskOption("A1", "one"), AskOption("A2", "two"), AskOption("A3", "three")],
    )


def _thread(thread_id: int) -> tuple[MagicMock, MagicMock]:
    thread = MagicMock()
    thread.id = thread_id
    msg = MagicMock()
    msg.edit = AsyncMock()
    thread.send = AsyncMock(return_value=msg)
    return thread, msg


@pytest.mark.asyncio
async def test_tui_resolution_unblocks_bridge(monkeypatch):
    """A menu resolved in the TUI (no Discord click) must NOT hang the bridge (#359).

    Before the fix bridge_pane_ask awaited the click for 24h, suspending
    run_claude and freezing the thread.  Now it watches the pane: once the menu
    is gone it returns, disables the stale buttons, and does NOT send keystrokes.
    """
    monkeypatch.setattr(ask_handler, "_PANE_RESOLVE_POLL", 0.01)
    monkeypatch.setattr(ask_handler, "_PANE_RESOLVE_MISSES", 2)
    thread, msg = _thread(359_0001)
    runner = MagicMock()
    # Menu is open on the first peek, then gone (answered/cancelled in the TUI).
    runner.peek_pending_ask = AsyncMock(side_effect=[_question(), None, None, None, None])
    runner.answer_menu = AsyncMock()
    runner.answer_menu_text = AsyncMock()
    runner.cancel_menu = AsyncMock()

    try:
        await asyncio.wait_for(bridge_pane_ask(thread, _question(), runner), timeout=3.0)
    except (asyncio.TimeoutError, TimeoutError):  # noqa: UP041
        pytest.fail("bridge_pane_ask hung — TUI resolution did not unblock it (#359)")

    # No keystrokes sent (the menu was already answered in the TUI).
    runner.answer_menu.assert_not_called()
    runner.answer_menu_text.assert_not_called()
    runner.cancel_menu.assert_not_called()
    # Stale buttons disabled.
    msg.edit.assert_awaited()
    # Bus is cleaned up so the next menu in this thread can register.
    assert not ask_bus.is_active(thread.id)


@pytest.mark.asyncio
async def test_discord_click_still_answers_menu(monkeypatch):
    """Regression: a real Discord click still drives the TUI menu via answer_menu."""
    monkeypatch.setattr(ask_handler, "_PANE_RESOLVE_POLL", 0.01)
    monkeypatch.setattr(ask_handler, "_PANE_RESOLVE_MISSES", 2)
    thread, _msg = _thread(359_0002)
    runner = MagicMock()
    # Menu stays open throughout — the click should win the race.
    runner.peek_pending_ask = AsyncMock(return_value=_question())
    runner.answer_menu = AsyncMock()
    runner.answer_menu_text = AsyncMock()
    runner.cancel_menu = AsyncMock()

    async def _click_soon():
        await asyncio.sleep(0.05)
        ask_bus.post_answer(thread.id, ["A2"])

    await asyncio.gather(
        asyncio.wait_for(bridge_pane_ask(thread, _question(), runner), timeout=3.0),
        _click_soon(),
    )
    # Option index 1 (A2) selected via keystrokes.
    runner.answer_menu.assert_awaited_once_with(1)
    runner.cancel_menu.assert_not_called()


# -- #399: prose context above the menu -------------------------------------


_CONTEXT = (
    "案Aと案Bを比較すると、案Aは実装が単純でデッドロックの心配がない一方、"
    "案Bは競合が頻繁な処理で安定します。私の推しは (A) です。理由は通常時の"
    "オーバーヘッドがほぼゼロだからです。"
)


def _resolved_runner() -> MagicMock:
    """Runner whose menu resolves in the TUI right away (fast test exit)."""
    runner = MagicMock()
    runner.peek_pending_ask = AsyncMock(return_value=None)
    runner.answer_menu = AsyncMock()
    runner.answer_menu_text = AsyncMock()
    runner.cancel_menu = AsyncMock()
    return runner


@pytest.fixture(autouse=True)
def _clear_bridged_context():
    from c_lord.discord_ui.bridged_context import bridged_context

    bridged_context.clear()
    yield
    bridged_context.clear()


@pytest.mark.asyncio
async def test_context_posted_as_silent_message_before_embed(monkeypatch):
    """#399: the prose spoken above the menu must reach Discord as its own
    silent message, posted BEFORE the menu embed, and survive menu resolution
    (the embed message is edited to nothing when the menu resolves)."""
    monkeypatch.setattr(ask_handler, "_PANE_RESOLVE_POLL", 0.01)
    monkeypatch.setattr(ask_handler, "_PANE_RESOLVE_MISSES", 2)
    thread, _msg = _thread(399_0001)
    q = _question()
    q.context = _CONTEXT

    await asyncio.wait_for(bridge_pane_ask(thread, q, _resolved_runner()), timeout=3.0)

    assert thread.send.await_count == 2
    first, second = thread.send.await_args_list
    # 1st send: the context, plain content, silent, no buttons.
    assert _CONTEXT in first.kwargs.get("content", "")
    assert first.kwargs.get("silent") is True
    assert "view" not in first.kwargs
    # 2nd send: the menu embed.
    assert "embed" in second.kwargs


@pytest.mark.asyncio
async def test_context_registered_for_mirror_dedup(monkeypatch):
    """#399 AC3: once posted, the context is registered so the transcript
    mirror can suppress the CLI's post-resolution flush of the same text."""
    from c_lord.discord_ui.bridged_context import bridged_context

    monkeypatch.setattr(ask_handler, "_PANE_RESOLVE_POLL", 0.01)
    monkeypatch.setattr(ask_handler, "_PANE_RESOLVE_MISSES", 2)
    thread, _msg = _thread(399_0002)
    q = _question()
    q.context = _CONTEXT

    await asyncio.wait_for(bridge_pane_ask(thread, q, _resolved_runner()), timeout=3.0)

    assert bridged_context.consume_match(thread.id, _CONTEXT) is True


@pytest.mark.asyncio
async def test_no_context_message_when_context_empty(monkeypatch):
    monkeypatch.setattr(ask_handler, "_PANE_RESOLVE_POLL", 0.01)
    monkeypatch.setattr(ask_handler, "_PANE_RESOLVE_MISSES", 2)
    thread, _msg = _thread(399_0003)

    await asyncio.wait_for(bridge_pane_ask(thread, _question(), _resolved_runner()), timeout=3.0)

    # Only the embed message — no extra context post.
    assert thread.send.await_count == 1


@pytest.mark.asyncio
async def test_long_context_fully_delivered_in_chunks(monkeypatch):
    """#399 review blocker 1: a context longer than one Discord message must be
    delivered IN FULL (sequential silent chunks) — clipping the head while
    registering the full text suppressed the flush and lost the head forever."""
    from c_lord.discord_ui.bridged_context import bridged_context

    monkeypatch.setattr(ask_handler, "_PANE_RESOLVE_POLL", 0.01)
    monkeypatch.setattr(ask_handler, "_PANE_RESOLVE_MISSES", 2)
    thread, _msg = _thread(399_0004)
    q = _question()
    q.context = ("経緯です。" * 500) + "私の推しは (A) です。"  # ~2510 chars

    await asyncio.wait_for(bridge_pane_ask(thread, q, _resolved_runner()), timeout=3.0)

    sends = thread.send.await_args_list
    context_sends = [s for s in sends if "content" in s.kwargs and "embed" not in s.kwargs]
    assert len(context_sends) == 2
    joined = "".join(s.kwargs["content"] for s in context_sends)
    assert joined == q.context  # nothing lost, nothing added
    assert all(len(s.kwargs["content"]) <= 2000 for s in context_sends)
    assert all(s.kwargs.get("silent") is True for s in context_sends)
    # Full text registered → the flush twin is suppressed.
    assert bridged_context.consume_match(thread.id, q.context) is True


@pytest.mark.asyncio
async def test_oversized_context_keeps_tail_and_skips_registration(monkeypatch):
    """Beyond the chunk budget the tail is posted with a 前略 marker and the
    text is NOT registered: the later jsonl flush then delivers the full text
    (a late duplicate) — degraded mode is duplication, never loss."""
    from c_lord.discord_ui.bridged_context import bridged_context

    monkeypatch.setattr(ask_handler, "_PANE_RESOLVE_POLL", 0.01)
    monkeypatch.setattr(ask_handler, "_PANE_RESOLVE_MISSES", 2)
    thread, _msg = _thread(399_0006)
    q = _question()
    q.context = "あ" * 6500  # > 3 * 1900

    await asyncio.wait_for(bridge_pane_ask(thread, q, _resolved_runner()), timeout=3.0)

    sends = thread.send.await_args_list
    context_sends = [s for s in sends if "content" in s.kwargs and "embed" not in s.kwargs]
    assert 1 <= len(context_sends) <= 3
    assert context_sends[0].kwargs["content"].startswith("…")
    assert all(len(s.kwargs["content"]) <= 2000 for s in context_sends)
    # NOT registered — the flush must not be suppressed (it carries the head).
    assert bridged_context.consume_match(thread.id, q.context) is False


@pytest.mark.asyncio
async def test_context_send_failure_does_not_break_bridge(monkeypatch):
    """An HTTP failure posting the context must not kill the menu bridge, and
    the undelivered text must NOT be registered (suppressing it later would
    swallow it entirely)."""
    import discord

    from c_lord.discord_ui.bridged_context import bridged_context

    monkeypatch.setattr(ask_handler, "_PANE_RESOLVE_POLL", 0.01)
    monkeypatch.setattr(ask_handler, "_PANE_RESOLVE_MISSES", 2)
    thread, msg = _thread(399_0005)
    q = _question()
    q.context = _CONTEXT

    calls = {"n": 0}

    async def _send(*args, **kwargs):
        calls["n"] += 1
        if calls["n"] == 1:
            raise discord.HTTPException(MagicMock(status=500), "boom")
        return msg

    thread.send = AsyncMock(side_effect=_send)

    await asyncio.wait_for(bridge_pane_ask(thread, q, _resolved_runner()), timeout=3.0)

    assert calls["n"] == 2  # embed still sent
    assert bridged_context.consume_match(thread.id, _CONTEXT) is False


@pytest.mark.asyncio
async def test_pane_skips_context_when_mirror_already_posted(monkeypatch):
    """#399 plan order: if the mirror already delivered the prose (registered
    source='mirror'), bridge_pane_ask must NOT post a duplicate context message
    — but it must still post the menu embed."""
    from c_lord.discord_ui.bridged_context import bridged_context

    monkeypatch.setattr(ask_handler, "_PANE_RESOLVE_POLL", 0.01)
    monkeypatch.setattr(ask_handler, "_PANE_RESOLVE_MISSES", 2)
    thread, _msg = _thread(399_0007)
    q = _question()
    q.context = _CONTEXT
    # Mirror got there first (plan-style early flush).
    bridged_context.register(thread.id, _CONTEXT, source="mirror")

    await asyncio.wait_for(bridge_pane_ask(thread, q, _resolved_runner()), timeout=3.0)

    sends = thread.send.await_args_list
    context_sends = [s for s in sends if "content" in s.kwargs and "embed" not in s.kwargs]
    assert context_sends == []  # no duplicate context post
    embed_sends = [s for s in sends if "embed" in s.kwargs]
    assert len(embed_sends) == 1  # menu still shown
