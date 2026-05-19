"""Discord thread-name builder/parser for the redesigned naming scheme (#95).

Format::

    <status_emoji> <stable_topic>[ #<tmux_window_index>]

Examples::

    🟢 c-lord命名検討 #5
    🟠 絵本-イラスト発注 #3
    ⚪ 終わったプロジェクト

Rules:
* total length ≤ 30 chars (topic is truncated if needed to fit)
* state ``dead`` drops the ``#N`` suffix entirely
* the parser is the inverse: strips the leading emoji + optional space
  and the trailing `` #N``, returning only the topic body.  Used when a
  user manually renames a thread via the Discord UI.
"""

from __future__ import annotations

import re

STATUS_EMOJI: dict[str, str] = {
    "alive": "🟢",
    "pending": "🟠",
    "dead": "⚪",
}

_MAX_NAME_LEN = 30

# Matches an optional leading status emoji + space.
_LEADING_EMOJI_RE = re.compile(
    r"^(?:" + "|".join(re.escape(e) for e in STATUS_EMOJI.values()) + r")\s*"
)
# Matches a trailing " #<digits>" suffix.
_TRAILING_INDEX_RE = re.compile(r"\s*#\d+\s*$")


def build_name(
    topic: str,
    state: str,
    tmux_window_index: int | None,
) -> str:
    """Build a thread name from its parts, capped at 30 characters.

    Unknown ``state`` falls back to the ``alive`` emoji so a missing
    state value never breaks naming.  When the combination is too long,
    only the topic is truncated — the emoji and suffix are kept intact
    so the at-a-glance status remains readable.
    """
    emoji = STATUS_EMOJI.get(state, STATUS_EMOJI["alive"])
    suffix = ""
    if state != "dead" and tmux_window_index is not None:
        suffix = f" #{tmux_window_index}"

    topic_clean = (topic or "").strip()
    # Reserve space for the emoji + single space + suffix.
    fixed = f"{emoji} "
    budget = _MAX_NAME_LEN - len(fixed) - len(suffix)
    if budget < 1:
        # Pathological case (very long suffix); drop the suffix.
        budget = _MAX_NAME_LEN - len(fixed)
        suffix = ""
    if len(topic_clean) > budget:
        topic_clean = topic_clean[: max(budget, 0)]

    return f"{fixed}{topic_clean}{suffix}"[:_MAX_NAME_LEN]


def parse_topic_from_name(name: str) -> str:
    """Inverse of :func:`build_name` — extract the topic body.

    Strips the leading status emoji (if present) and the trailing
    ``#<digits>`` suffix (if present).  Whitespace around the result is
    trimmed.  Returns an empty string only when the input has no
    body at all.
    """
    body = _LEADING_EMOJI_RE.sub("", name or "")
    body = _TRAILING_INDEX_RE.sub("", body)
    return body.strip()
