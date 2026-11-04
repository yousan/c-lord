"""``COORDINATION_CHANNEL_ID`` as read by the places that use it themselves (#1194).

``load_config`` refuses a bad value at startup (#1084), but ``ApiServer``,
``setup_bridge`` and ``ClaudeChatCog`` read the variable directly — and an
instance repo may call them without ``load_config``. One reading for all three:
surrounding spaces are dropped (``KEY="123 "`` started before #1084), and only
ASCII digits are an id — ``"²"`` is ``isdigit()`` and made ``int()`` raise.
"""

from __future__ import annotations

import os

ENV_NAME = "COORDINATION_CHANNEL_ID"


def coordination_channel_id_from_env() -> int | None:
    """The coordination channel id from the environment, or ``None``."""
    raw = os.getenv(ENV_NAME, "").strip()
    return int(raw) if raw.isascii() and raw.isdigit() else None
