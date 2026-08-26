"""Tests for AskAnswerBus ownership semantics (#535).

The bus is what makes "one menu → one set of buttons" enforceable: whichever
bridge registers first OWNS the menu for that thread, and every later bridge is
told "already owned" so it returns instead of posting a second copy.

Before #535 ``register()`` silently overwrote the existing waiter, so the first
bridge was left awaiting a queue nobody would ever post to — a 24h hang holding
the thread — while two identical menus sat in Discord.
"""

from __future__ import annotations

import asyncio

import pytest

from c_lord.discord_ui.ask_bus import AskAnswerBus


def test_register_returns_none_when_already_owned() -> None:
    """AC2: the second register is refused, not silently granted."""
    bus = AskAnswerBus()
    first = bus.register(1)
    second = bus.register(1)
    assert first is not None
    assert second is None


def test_first_owner_keeps_its_queue() -> None:
    """AC2: a refused second register must not replace the owner's queue."""
    bus = AskAnswerBus()
    first = bus.register(2)
    bus.register(2)
    assert first is not None
    assert bus.post_answer(2, ["A"]) is True
    assert first.get_nowait() == ["A"]


def test_unregister_releases_ownership() -> None:
    """Ownership is a lease, not a permanent claim."""
    bus = AskAnswerBus()
    bus.register(3)
    bus.unregister(3)
    assert bus.register(3) is not None


def test_register_is_independent_per_thread() -> None:
    bus = AskAnswerBus()
    assert bus.register(4) is not None
    assert bus.register(5) is not None


@pytest.mark.asyncio
async def test_concurrent_register_yields_exactly_one_owner() -> None:
    """AC3: register itself IS the ownership acquisition — no check→register gap.

    Ten bridges racing on the same thread must produce exactly one queue; the
    other nine get None and bail.
    """
    bus = AskAnswerBus()

    async def _try() -> object:
        return bus.register(6)

    results = await asyncio.gather(*(_try() for _ in range(10)))
    owners = [q for q in results if q is not None]
    assert len(owners) == 1
    assert bus.post_answer(6, ["only"]) is True
    assert owners[0].get_nowait() == ["only"]  # type: ignore[union-attr]
