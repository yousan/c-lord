"""Startup notices for env vars that no longer select anything (#712).

c-lord used to have two delivery paths: the JSONL transcript mirror (#71/#216)
and the legacy skill push (#53), chosen with ``CLORD_BRIDGE_MODE`` /
``USE_SKILL_REPLY``. #712 removed the skill push, so those vars are inert.

An operator who upgrades the package with ``CLORD_BRIDGE_MODE=skill`` still in
their ``.env`` must not silently get different behaviour — the Zero-Config
Principle says an update alone must not surprise them. So we say it out loud
once at startup, and keep running on the only path there is.
"""

from __future__ import annotations

import logging
import os
from collections.abc import Mapping

logger = logging.getLogger(__name__)

#: Env vars that used to pick a delivery path and now do nothing.
REMOVED_DELIVERY_ENV: tuple[str, ...] = ("CLORD_BRIDGE_MODE", "USE_SKILL_REPLY")

_TRUTHY = {"1", "true", "yes", "on"}


def _requests_skill_push(name: str, value: str) -> bool:
    """Return True if *value* asked for the removed skill-push path.

    ``CLORD_BRIDGE_MODE=jsonl`` and ``USE_SKILL_REPLY=0`` asked for what they
    still get — those are leftovers to delete, not a changed behaviour.
    """
    if name == "CLORD_BRIDGE_MODE":
        return value.lower() != "jsonl"
    return value.lower() in _TRUTHY


def warn_removed_delivery_env(env: Mapping[str, str] | None = None) -> list[str]:
    """Log a notice for each removed delivery-path env var that is still set.

    Args:
        env: Environment mapping to inspect. Defaults to ``os.environ``.

    Returns:
        Names of the vars that asked for the removed skill-push path (i.e. the
        ones that got a WARNING). Leftovers that merely restated the current
        behaviour get an INFO and are not listed.
    """
    source = os.environ if env is None else env
    warned: list[str] = []

    for name in REMOVED_DELIVERY_ENV:
        raw = (source.get(name) or "").strip()
        if not raw:
            continue
        if _requests_skill_push(name, raw):
            warned.append(name)
            logger.warning(
                "%s=%s is set, but the skill-push delivery path was removed (#712). "
                "c-lord now always delivers through the JSONL transcript mirror. "
                "Remove the line from your .env — it does nothing.",
                name,
                raw,
            )
        else:
            logger.info(
                "%s=%s is set but no longer read (#712) — the JSONL transcript mirror "
                "is the only delivery path. Safe to delete from your .env.",
                name,
                raw,
            )

    return warned
