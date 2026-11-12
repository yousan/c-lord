"""Structural interfaces that cogs can satisfy without inheriting from anything.

A cog that wants to be waited for before a restart only has to expose the
members named here; nothing in c-lord needs to know its class.
"""

from __future__ import annotations

from typing import Protocol, runtime_checkable


@runtime_checkable
class DrainAware(Protocol):
    """Something that can say how much work it still has in flight.

    ``AutoUpgradeCog`` holds the restart back until every cog that matches
    this protocol reports zero.
    """

    @property
    def active_count(self) -> int:
        """How many units of work are running right now."""
        ...
