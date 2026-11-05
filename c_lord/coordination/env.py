"""``COORDINATION_CHANNEL_ID`` as read by the places that use it themselves (#1194).

``load_config`` refuses a bad value at startup (#1084), but ``ApiServer``,
``setup_bridge`` and ``ClaudeChatCog`` read the variable directly — and an
instance repo may call them without ``load_config``. One reading for all three:
surrounding spaces are dropped (``KEY="123 "`` started before #1084), and only
ASCII digits are an id — ``"²"`` is ``isdigit()`` and made ``int()`` raise —
and no more of them than an id can have (#1256).
"""

from __future__ import annotations

import os

ENV_NAME = "COORDINATION_CHANNEL_ID"
#: A Discord id never has more digits (``CLORD_TRUSTED_BOT_IDS`` uses the same
#: bound). Longer is not an id — and ``int()`` of 4300+ digits raises (#1256).
MAX_ID_DIGITS = 20


def coordination_channel_id_from_env() -> int | None:
    """The coordination channel id from the environment, or ``None``."""
    raw = os.getenv(ENV_NAME, "").strip()
    if raw.isascii() and raw.isdigit() and len(raw) <= MAX_ID_DIGITS:
        return int(raw)
    return None
