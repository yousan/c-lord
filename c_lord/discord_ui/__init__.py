"""Discord-facing pieces of c-lord: views, embeds, the status lamp, the board.

Submodules are imported by name (``from c_lord.discord_ui import status``);
only the ask-menu timeout is re-exported at package level.
"""

from __future__ import annotations

from .ask_handler import ASK_ANSWER_TIMEOUT

__all__ = ["ASK_ANSWER_TIMEOUT"]
