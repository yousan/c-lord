"""#473: a 30s stall shows the ⚠️ lamp and nothing else.

The hard-stall condition used to be announced twice: once as the ⚠️ reaction on
the trigger message, and once as a prose line posted into the thread
("No activity for 30s — could be extended thinking or context compression").
The prose duplicated information the lamp already carries, dressed a normal
situation (extended thinking / compaction) up as a warning, and could interrupt
the conversation several times in one turn. Only the reaction survives.
"""

from __future__ import annotations

import asyncio
import inspect
from pathlib import Path
from unittest.mock import AsyncMock, MagicMock

import pytest

from c_lord.discord_ui.status import EMOJI_STALL_HARD, STALL_HARD_SECONDS, StatusManager

_PACKAGE = Path(__file__).resolve().parent.parent / "c_lord"


def _make_message() -> MagicMock:
    msg = MagicMock()
    msg.add_reaction = AsyncMock()
    msg.remove_reaction = AsyncMock()
    msg.guild = MagicMock()
    msg.guild.me = MagicMock()
    return msg


class TestNoStallProse:
    """AC1 / AC3 — the thread never gets the "No activity for 30s" line again."""

    @pytest.mark.parametrize("needle", ["No activity for 30s", "on_hard_stall", "_notify_stall"])
    def test_the_package_no_longer_mentions_it(self, needle: str) -> None:
        hits = [
            f"{path.relative_to(_PACKAGE.parent)}"
            for path in _PACKAGE.rglob("*.py")
            if needle in path.read_text(encoding="utf-8")
        ]
        assert hits == [], f"{needle!r} still present in: {hits}"

    def test_status_manager_takes_no_stall_callback(self) -> None:
        params = inspect.signature(StatusManager.__init__).parameters
        assert "on_hard_stall" not in params


class TestHardStallStillLamps:
    """AC2 — the ⚠️ lamp is the one thing that must NOT change."""

    @pytest.mark.asyncio
    async def test_thirty_seconds_of_silence_paints_the_warning_reaction(self) -> None:
        msg = _make_message()
        sm = StatusManager(msg)
        await sm.set_running()
        sm._last_activity = asyncio.get_running_loop().time() - STALL_HARD_SECONDS - 1

        await asyncio.sleep(2.5)

        assert sm._current_emoji == EMOJI_STALL_HARD
        assert msg.add_reaction.await_args_list[-1].args[0] == EMOJI_STALL_HARD
        await sm.cleanup()
