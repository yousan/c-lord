"""Discord thread-name builder/parser for the redesigned naming scheme (#95, #119).

Format::

    <status_emoji> W<window_index> │ <topic>   (alive/pending, with window)
    <status_emoji> <topic>                      (dead, or no window index)

Examples::

    🟢 W3 │ 認証まわりのリファクタ
    🟠 W1 │ 絵本-イラスト発注
    ⚪ 終わったプロジェクト

Rules:
* total length ≤ 30 chars (topic is truncated if needed to fit)
* state ``dead`` drops the ``W<N> │`` prefix entirely
* no window index also drops the prefix
* the parser is the inverse: strips the leading emoji + optional ``W<N> │``
  and the legacy trailing `` #N`` (backward-compat), returning only the topic body.
  Used when a user manually renames a thread via the Discord UI.
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
# Matches a leading "W<digits> │ " prefix (new format).
_WORK_PREFIX_RE = re.compile(r"^W\d+\s*[│]\s*")
# Matches a trailing " #<digits>" suffix (legacy backward-compat).
_TRAILING_INDEX_RE = re.compile(r"\s*#\d+\s*$")


def build_name(
    topic: str,
    state: str,
    tmux_window_index: int | None,
) -> str:
    """Build a thread name from its parts, capped at 30 characters.

    Format: ``<emoji> W<N> │ <topic>`` when alive/pending with a window index.
    Drops the work prefix for dead state or when window index is unknown.
    Unknown ``state`` falls back to the ``alive`` emoji.
    When the combination is too long, only the topic is truncated.
    """
    emoji = STATUS_EMOJI.get(state, STATUS_EMOJI["alive"])

    if state != "dead" and tmux_window_index is not None:
        fixed = f"{emoji} W{tmux_window_index} │ "
    else:
        fixed = f"{emoji} "

    topic_clean = (topic or "").strip()
    budget = _MAX_NAME_LEN - len(fixed)
    if budget < 1:
        # Pathological case (very long prefix); drop work prefix.
        fixed = f"{emoji} "
        budget = _MAX_NAME_LEN - len(fixed)
    if len(topic_clean) > budget:
        topic_clean = topic_clean[: max(budget, 0)]

    return f"{fixed}{topic_clean}"[:_MAX_NAME_LEN]


def parse_topic_from_name(name: str) -> str:
    """Inverse of :func:`build_name` — extract the topic body.

    Strips the leading status emoji (if present), the new ``W<N> │`` work
    prefix (if present), and the legacy trailing `` #<digits>`` suffix (if
    present, for backward-compat with the old format).
    Whitespace around the result is trimmed.
    Returns an empty string only when the input has no body at all.
    """
    body = _LEADING_EMOJI_RE.sub("", name or "")
    body = _WORK_PREFIX_RE.sub("", body)
    body = _TRAILING_INDEX_RE.sub("", body)
    return body.strip()
