"""The webhooks c-lord speaks through for sub-agents — and must never obey (#971).

With ``CLORD_SUBAGENT_VOICES=1`` a sub-agent's messages are posted through a
webhook whose username is the agent's name. c-lord *runs* webhook messages in
its threads: that is how one Claude dispatches work to another (2026-10-08), and
possession of a webhook is authorization (:func:`command_gate.is_message_authorized`).
Without this registry every message an agent said would come straight back as a
new instruction to the parent — a loop that talks to itself.

The ids are kept on disk, under ``~/.c-lord/``, because the danger outlives the
process: after a restart the pick-up of missed messages (#745/#776) reads the
thread back, and the voices posted before the restart are in it. They are
shared by every instance on the host on purpose — no instance should obey
another one's voices either.
"""

from __future__ import annotations

import json
import logging
import threading
from pathlib import Path

from .clord_home import clord_home

logger = logging.getLogger(__name__)

#: What c-lord names the webhook it creates (one per parent channel).
VOICE_WEBHOOK_NAME = "c-lord subagents"

_lock = threading.Lock()
_ids: set[int] | None = None


def registry_path() -> Path:
    return clord_home() / "voice-webhooks.json"


def _load() -> set[int]:
    global _ids
    if _ids is None:
        ids: set[int] = set()
        try:
            raw = json.loads(registry_path().read_text(encoding="utf-8"))
            if isinstance(raw, list):
                ids = {int(x) for x in raw if isinstance(x, int | str) and str(x).isdigit()}
        except FileNotFoundError:
            pass
        except (OSError, ValueError):
            logger.warning("voice webhooks: could not read %s", registry_path(), exc_info=True)
        _ids = ids
    return _ids


def register(webhook_id: int) -> None:
    """Remember *webhook_id* as one of ours, on disk, before anything is sent through it."""
    with _lock:
        ids = _load()
        if webhook_id in ids:
            return
        ids.add(webhook_id)
        path = registry_path()
        try:
            path.parent.mkdir(parents=True, exist_ok=True)
            tmp = path.with_suffix(".tmp")
            tmp.write_text(json.dumps(sorted(ids)), encoding="utf-8")
            tmp.replace(path)
        except OSError:
            # Still known for this process; only a restart would forget it.
            logger.warning("voice webhooks: could not write %s", path, exc_info=True)


def is_voice_webhook(webhook_id: int | None) -> bool:
    if not webhook_id:
        return False
    with _lock:
        return webhook_id in _load()


def is_voice_message(message: object) -> bool:
    """Was *message* posted by c-lord itself, speaking for a sub-agent?"""
    return is_voice_webhook(getattr(message, "webhook_id", None))


def _reset_for_tests() -> None:
    global _ids
    with _lock:
        _ids = None
