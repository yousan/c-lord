"""Discord thread-name builder/parser for the redesigned naming scheme (#95, #119, #120, #414).

Format::

    <status_emoji> W<work_number> │ #<issue> <topic>   (with window + Issue/PR number)
    <status_emoji> W<work_number> │ <topic>            (with window, no number)
    <status_emoji> <topic>                             (dead, or no window number)
    [終了] #<issue> <topic>                             (closed — /close-workspace'd)

The ``#<issue>`` token (#414) is the Issue/PR number the thread is working on,
auto-detected from the session's git branch or its first message; it is omitted
when no number is known.

Examples::

    🟢 W3 │ #404 認証リファクタ      (running, working on #404)
    🟡 W2 │ 絵本-イラスト発注        (waiting for user input, no number)
    🔴 W1 │ エラーが発生             (error)
    ⚪ 終わったプロジェクト           (dead)
    [終了] #404 認証リファクタ        (closed, #512)

The ``[終了]`` marker (#512) means the session was **intentionally** closed with
``/close-workspace`` — as opposed to ``dead``/⚪, which only means "no tmux window
right now" (a crash, a bot restart, a dead tmux server). The distinction matters:
a ``dead`` thread silently resumes on the next message (#270), a ``[終了]`` one
holds the message and asks the user to reopen.

Lamp states (#120):
* ``running``  → 🟢 — Claude is actively executing
* ``waiting``  → 🟡 — waiting for user input (❯ prompt visible)
* ``error``    → 🔴 — error detected in pane
* ``dead``     → ⚪ — tmux window gone
* ``alive``    → 🟢 — legacy alias for ``running``
* ``pending``  → 🟠 — reserved for external setters

Rules:
* total length ≤ 30 chars (topic is truncated if needed to fit)
* ``closed`` replaces the lamp emoji and the ``W<N> │`` prefix with ``[終了] ``
  (the window it named no longer exists), and wins over ``state``
* state ``dead`` drops the ``W<N> │`` prefix entirely
* no window index also drops the prefix
* the parser is the inverse: strips the leading emoji, the optional ``W<N> │``
  prefix, the ``[終了] `` closed marker, and the legacy trailing `` #N``
  (backward-compat), returning only the topic body.
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

# #512: prefix marking a workspace that was stopped on purpose.
# Deliberately plain text rather than an emoji: it must stay legible where emoji
# don't render, and it reads as a state word rather than as one more lamp colour.
#
# #574 renamed it from 「終了」. The operation is about to fire automatically
# after 7 days, and 「終了」 says *it is over* while nothing is — the working
# directory, the conversation and the DB volume all survive, and
# ``/workspace-start`` brings it straight back. Telling someone their work
# "ended" when they did not ask for anything is a false alarm; 「停止」 is simply
# true.
CLOSED_MARK = "[停止]"

#: Marks written by earlier versions. Threads still carry them, so they must keep
#: parsing — otherwise every one of them silently gains the old marker as part of
#: its topic the next time it is renamed.
LEGACY_CLOSED_MARKS = ("[終了]",)

_CLOSED_PREFIX = f"{CLOSED_MARK} "

# Matches an optional leading status emoji + space.
# Use unique emoji values to avoid duplicate alternates in the regex.
_LEADING_EMOJI_RE = re.compile(
    r"^(?:" + "|".join(re.escape(e) for e in dict.fromkeys(STATUS_EMOJI.values())) + r")\s*"
)
# Matches the leading "[終了] " closed marker (#512).
_CLOSED_PREFIX_RE = re.compile(
    r"^(?:"
    + "|".join(re.escape(m) for m in (CLOSED_MARK, *LEGACY_CLOSED_MARKS))
    + r")\s*"
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
    closed: bool = False,
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

    ``closed=True`` (#512) means the user closed this session on purpose with
    ``/close-workspace``. It overrides both the lamp emoji and the ``W<N> │``
    prefix with ``[終了] ``: the lamp describes a *live* pane and the window
    number names a tmux window, and after a close neither exists. Because the
    marker replaces those two parts rather than adding to them, it costs the
    topic ~2 characters of budget, not 6.
    """
    emoji = STATUS_EMOJI.get(state, STATUS_EMOJI["alive"])
    prefix_emoji = "" if closed else (f"{emoji} " if lamp else "")

    ref = (issue_ref or "").lstrip("#").strip()
    ref_token = f"#{ref} " if ref else ""

    if not closed and state not in _NO_PREFIX_STATES and window_number is not None:
        work = f"W{window_number} │ "
    else:
        work = ""

    mark = _CLOSED_PREFIX if closed else ""

    topic_clean = (topic or "").strip()
    # Shed name parts in priority order when the budget is too tight: the topic
    # is most valuable, so drop the work prefix first, then the issue number.
    for fixed in (
        f"{mark}{prefix_emoji}{work}{ref_token}",
        f"{mark}{prefix_emoji}{ref_token}",
        f"{mark}{prefix_emoji}",
    ):
        budget = _MAX_NAME_LEN - len(fixed)
        if budget >= 1:
            break
    else:
        fixed = f"{mark}{prefix_emoji}"
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

    Strips the leading status emoji (if present), the ``[終了]`` closed marker
    (#512, if present), the ``W<N> │`` work prefix (if present), the leading
    ``#<digits>`` issue/PR token (#414, if present), and the legacy trailing
    `` #<digits>`` suffix (if present, for backward-compat with the old format).
    Whitespace around the result is trimmed.
    Returns an empty string only when the input has no body at all.

    Stripping the leading ``#<digits>`` (and the ``[終了]`` marker) keeps
    manual-rename detection clean: when a user edits a thread title that already
    carries them, they are not absorbed into the stored topic — which would
    otherwise double them on the next rebuild (``[終了] [終了] …``).
    """
    body = _LEADING_EMOJI_RE.sub("", name or "")
    body = _CLOSED_PREFIX_RE.sub("", body)
    body = _WORK_PREFIX_RE.sub("", body)
    body = _LEADING_REF_RE.sub("", body)
    body = _TRAILING_INDEX_RE.sub("", body)
    return body.strip()
