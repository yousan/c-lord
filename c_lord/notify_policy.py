"""How loudly c-lord falls back to the owner when nobody human asked (#525).

A turn started by a webhook, CI, the scheduler or ``POST /api/spawn`` has no
person behind it, so there is nobody to ``@`` — c-lord falls back to
``DISCORD_OWNER_ID`` instead. Whether that fallback is welcome depends on the
deployment, not on the code:

- a server that runs a handful of sessions wants to hear about a stuck turn;
- a server running many automated threads (PR reviews, scheduled sweeps) gets
  its owner buried under "Claude has finished" pings for threads they never
  opened — the notification stops meaning anything.

``CLORD_OWNER_FALLBACK`` picks the policy:

===========  ==========================  ==============================
value        turn-end 🟡 (``completion``)  interactive pause (``blocked``)
===========  ==========================  ==============================
``all``      owner                       owner
``blocked``  — (silent)                  owner            ← default
``off``      — (silent)                  — (silent)
===========  ==========================  ==============================

This governs the **fallback only**. A turn a person actually asked for always
mentions that person, in every mode (#481).
"""

from __future__ import annotations

import os
from typing import Literal

#: Kinds of mention this policy governs. ``completion`` is the turn-end 🟡
#: message; ``blocked`` is a permission / plan / elicitation / AskUserQuestion
#: prompt that has the turn parked until someone answers.
Kind = Literal["completion", "blocked"]

ENV_VAR = "CLORD_OWNER_FALLBACK"
DEFAULT_MODE = "blocked"
MODES = ("all", "blocked", "off")


def owner_fallback_mode() -> str:
    """Return the configured mode, defaulting to ``blocked``.

    An unrecognised value falls back to the default rather than raising: a typo
    in ``.env`` must not take the bot down, and "ping me when it is stuck" is
    the safe reading of an ambiguous setting.
    """
    raw = os.getenv(ENV_VAR, DEFAULT_MODE).strip().lower()
    return raw if raw in MODES else DEFAULT_MODE


def owner_fallback_allowed(kind: Kind) -> bool:
    """Whether an owner ``@`` is allowed for this kind of mention."""
    mode = owner_fallback_mode()
    if mode == "off":
        return False
    if mode == "all":
        return True
    return kind == "blocked"


def owner_notify_id(bot: object, *, kind: Kind) -> int | None:
    """The owner to ``@`` for a turn with no human requester, or ``None``.

    ``None`` means "post without a mention" — every caller already treats a
    missing notify id that way, so an unset ``DISCORD_OWNER_ID`` and a policy
    that forbids the ping land in the same, already-tested branch.
    """
    if not owner_fallback_allowed(kind):
        return None
    return getattr(bot, "owner_id", None)
