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

===========  ==============  =============  =============
value        turn-end 🟡      parked turn    broken turn
             ``completion``  ``blocked``    ``failure``
===========  ==============  =============  =============
``all``      owner           owner          owner
``blocked``  — (silent)      owner          owner          ← default
``off``      — (silent)      — (silent)     — (silent)
===========  ==============  =============  =============

``failure`` is its own rung (#681). Before it existed, a turn that never ran
was reported as a ``completion``, so the default mode — the one chosen to mute
routine "Claude has finished" pings — muted the one outcome nobody else would
notice. #677 and #678 were each dispatched, died at startup, and sat unread for
two days under exactly this setting. "Quiet when it works, loud when it breaks"
was not expressible in the old two-kind table; it is the default now.

``off`` still means off, failures included: it is the bottom rung of a ladder
and the only setting that promises silence, so a failure ping that ignored it
would leave no way to turn the fallback off at all. A deployment that set
``off`` to escape the 🟡 flood wants ``blocked`` instead.

This governs the **fallback only**. A turn a person actually asked for always
mentions that person, in every mode (#481).
"""

from __future__ import annotations

import os
from typing import Literal

#: Kinds of mention this policy governs. ``completion`` is the turn-end 🟡
#: message; ``blocked`` is a permission / plan / elicitation / AskUserQuestion
#: prompt that has the turn parked until someone answers; ``failure`` is a turn
#: that ended in an error — it never started, it crashed, it timed out (#681).
Kind = Literal["completion", "blocked", "failure"]

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
    # ``blocked``: everything that needs a human *now* — a parked turn and a
    # broken one. Only the routine turn-end 🟡 is dropped.
    return kind in ("blocked", "failure")


def owner_notify_id(bot: object, *, kind: Kind) -> int | None:
    """The owner to ``@`` for a turn with no human requester, or ``None``.

    ``None`` means "post without a mention" — every caller already treats a
    missing notify id that way, so an unset ``DISCORD_OWNER_ID`` and a policy
    that forbids the ping land in the same, already-tested branch.
    """
    if not owner_fallback_allowed(kind):
        return None
    return getattr(bot, "owner_id", None)
