"""Discord thread-name builder/parser for the redesigned naming scheme (#95, #119, #120, #414).

Format::

    <status_emoji> W<work_number> │ #<issue> <topic>   (with window + Issue/PR number)
    <status_emoji> W<work_number> │ <topic>            (with window, no number)
    <status_emoji> <topic>                             (dead, or no window number)

The ``#<issue>`` token (#414) is the Issue/PR number the thread is working on,
auto-detected from the session's git branch or its first message; it is omitted
when no number is known.

Examples::

    🟢 W3 │ #404 認証リファクタ      (running, working on #404)
    🟡 W2 │ 絵本-イラスト発注        (waiting for user input, no number)
    🔴 W1 │ エラーが発生             (error)
    ⚪ 終わったプロジェクト           (dead)

Lamp states (#120):
* ``running``  → 🟢 — Claude is actively executing
* ``waiting``  → 🟡 — waiting for user input (❯ prompt visible)
* ``error``    → 🔴 — error detected in pane
* ``dead``     → ⚪ — tmux window gone
* ``alive``    → 🟢 — legacy alias for ``running``
* ``pending``  → 🟠 — reserved for external setters

Rules:
* total length ≤ 30 chars (topic is truncated if needed to fit)
* state ``dead`` drops the ``W<N> │`` prefix entirely
* no window index also drops the prefix
* the parser is the inverse: strips the leading emoji + optional ``W<N> │``
  and the legacy trailing `` #N`` (backward-compat), returning only the topic body.
  Used when a user manually renames a thread via the Discord UI.
"""

from __future__ import annotations

import os
import re

STATUS_EMOJI: dict[str, str] = {
    "running": "🟢",  # Claude is executing
    "alive": "🟢",  # legacy alias for "running"
    "waiting": "🟡",  # waiting for user input (❯ prompt visible)
    "error": "🔴",  # error detected in pane
    "pending": "🟠",  # reserved for external setters
    "dead": "⚪",  # no tmux window
}

_MAX_NAME_LEN = 30

# Matches an optional leading status emoji + space.
# Use unique emoji values to avoid duplicate alternates in the regex.
_LEADING_EMOJI_RE = re.compile(
    r"^(?:" + "|".join(re.escape(e) for e in dict.fromkeys(STATUS_EMOJI.values())) + r")\s*"
)
# Matches a leading "W<digits> │ " prefix (new format).
_WORK_PREFIX_RE = re.compile(r"^W\d+\s*[│]\s*")
# Matches a trailing " #<digits>" suffix (legacy backward-compat).
_TRAILING_INDEX_RE = re.compile(r"\s*#\d+\s*$")
# Matches a leading "#<digits> " issue/PR token (new format, #414). Sits between
# the work prefix and the topic body, so it is stripped after the work prefix.
_LEADING_REF_RE = re.compile(r"^#\d+\s*")

# States that suppress the W<N> prefix (no window info shown).
_NO_PREFIX_STATES = frozenset({"dead"})


def build_name(
    topic: str,
    state: str,
    window_number: int | None,
    *,
    lamp: bool = True,
    issue_ref: str | None = None,
) -> str:
    """Build a thread name from its parts, capped at 30 characters.

    ``window_number`` is the ``N`` from the tmux ``w{N}`` window name (a
    stable identifier), not tmux's volatile ``#{window_index}``.

    Format: ``<emoji> W<N> │ #<issue> <topic>`` when running/waiting/error with
    a window number and an ``issue_ref``. The ``#<issue>`` token (#414) is the
    Issue/PR number the thread is working on; it is dropped when ``issue_ref`` is
    ``None``. Drops the ``W<N> │`` work prefix for dead state or when the window
    number is unknown. Unknown ``state`` falls back to the ``alive``/🟢 emoji.
    When the combination is too long, only the topic is truncated (the number is
    kept); under extreme length pressure the work prefix, then the number, are
    dropped before the topic disappears.

    ``issue_ref`` may be passed with or without a leading ``#`` — it is
    normalised to a single ``#``.

    ``lamp=False`` (the thread-lamp opt-out, #329) drops the leading status
    emoji entirely — the name keeps only ``W<N> │ #<issue> <topic>`` (or a
    subset). The point is that the name then no longer depends on ``state``, so a
    state change never produces a different name and never triggers a Discord
    rename (which is rate-limited to ~2 per 10 min per thread).
    """
    emoji = STATUS_EMOJI.get(state, STATUS_EMOJI["alive"])
    prefix_emoji = f"{emoji} " if lamp else ""

    ref = (issue_ref or "").lstrip("#").strip()
    ref_token = f"#{ref} " if ref else ""

    if state not in _NO_PREFIX_STATES and window_number is not None:
        work = f"W{window_number} │ "
    else:
        work = ""

    topic_clean = (topic or "").strip()
    # Shed name parts in priority order when the budget is too tight: the topic
    # is most valuable, so drop the work prefix first, then the issue number.
    for fixed in (f"{prefix_emoji}{work}{ref_token}", f"{prefix_emoji}{ref_token}", prefix_emoji):
        budget = _MAX_NAME_LEN - len(fixed)
        if budget >= 1:
            break
    else:
        fixed = prefix_emoji
        budget = _MAX_NAME_LEN - len(fixed)

    if len(topic_clean) > budget:
        topic_clean = topic_clean[: max(budget, 0)]

    return f"{fixed}{topic_clean}"[:_MAX_NAME_LEN]


_LAMP_TRUTHY = frozenset({"1", "true", "yes", "on"})


def thread_lamp_enabled(explicit: bool | None = None) -> bool:
    """Whether the thread-name status lamp (🟢🟡🔴⚪) is active.

    **Off by default (#329).** Repainting the Discord thread name on every state
    change calls the thread-rename API, which Discord rate-limits to ~2 changes
    per 10 minutes per thread; in a busy server the lamp saturates that limit
    (429s). Opt back in with ``CLORD_THREAD_LAMP=1`` (or ``true``/``yes``/``on``)
    or the ``thread_lamp`` constructor argument of :class:`ClaudeChatCog`.

    ``explicit`` (a constructor override) wins over the environment when not
    ``None``; otherwise the ``CLORD_THREAD_LAMP`` env var decides.
    """
    if explicit is not None:
        return explicit
    return os.getenv("CLORD_THREAD_LAMP", "").strip().lower() in _LAMP_TRUTHY


def thread_retitle_enabled(explicit: bool | None = None) -> bool:
    """Whether automatic mid-conversation topic re-titling is active (#414).

    **Off by default (#414).** The re-titling pass (``topic.maybe_retitle``)
    asks an LLM whether a later message means the thread's work changed and, if
    so, rewrites the topic. In practice this fired too eagerly and renamed
    threads users did not want renamed, so it is now opt-in. The initial topic
    (generated from the first message) is unaffected — only the later rewrite is
    gated. Opt back in with ``CLORD_THREAD_RETITLE=1`` (or ``true``/``yes``/
    ``on``) or the ``thread_retitle`` constructor argument of
    :class:`ClaudeChatCog`.

    ``explicit`` (a constructor override) wins over the environment when not
    ``None``; otherwise the ``CLORD_THREAD_RETITLE`` env var decides.
    """
    if explicit is not None:
        return explicit
    return os.getenv("CLORD_THREAD_RETITLE", "").strip().lower() in _LAMP_TRUTHY


def parse_topic_from_name(name: str) -> str:
    """Inverse of :func:`build_name` — extract the topic body.

    Strips the leading status emoji (if present), the ``W<N> │`` work prefix
    (if present), the leading ``#<digits>`` issue/PR token (#414, if present),
    and the legacy trailing `` #<digits>`` suffix (if present, for backward-compat
    with the old format).
    Whitespace around the result is trimmed.
    Returns an empty string only when the input has no body at all.

    Stripping the leading ``#<digits>`` keeps manual-rename detection clean: when
    a user edits a thread title that already carries the number, the number is
    not absorbed into the stored topic (which would otherwise double it on the
    next rebuild).
    """
    body = _LEADING_EMOJI_RE.sub("", name or "")
    body = _WORK_PREFIX_RE.sub("", body)
    body = _LEADING_REF_RE.sub("", body)
    body = _TRAILING_INDEX_RE.sub("", body)
    return body.strip()
