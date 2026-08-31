"""Discord thread-name builder/parser for the redesigned naming scheme (#95, #119, #120, #414).

Format::

    <status_emoji> W<work_number> │ #<origin> <topic> →#<current>
    <status_emoji> W<work_number> │ #<issue> <topic>   (with window + Issue/PR number)
    <status_emoji> W<work_number> │ <topic>            (with window, no number)
    <status_emoji> <topic>                             (dead, or no window number)
    [終了] #<issue> <topic>                             (closed — /close-workspace'd)

The leading ``#<issue>`` token (#414) is the Issue/PR number that **identifies**
the thread — the one it was opened for. It is written once and never moves, so a
thread stays findable in the sidebar. When the thread has since moved on to a
different number (a spun-off Issue, a PR), that one is appended as ``→#<current>``
(#593). Both are omitted when no number is known.

Examples::

    🟢 W3 │ #404 認証リファクタ      (running, working on #404)
    W131 │ #540 メモリ →#588          (opened for #540, now working on #588)
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
* total length ≤ 45 chars (topic is truncated if needed to fit)
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

#: Hard cap on the rendered thread name.
#:
#: Discord itself allows 100; c-lord self-limits so the sidebar stays scannable.
#: #593 raised it from 30 after measuring the live server: 46 of 195 ``W…``
#: threads were **already** truncated at 30 (median 26). Appending the
#: ``→#<current>`` token there would have eaten the topic rather than slack —
#: "keep the original number" must not cost "what is this thread about".
MAX_NAME_LEN = 45

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
    r"^(?:" + "|".join(re.escape(m) for m in (CLOSED_MARK, *LEGACY_CLOSED_MARKS)) + r")\s*"
)
# Matches a leading "W<digits> │ " prefix (new format).
_WORK_PREFIX_RE = re.compile(r"^W\d+\s*[│]\s*")
# Matches a trailing " #<digits>" suffix (legacy backward-compat).
_TRAILING_INDEX_RE = re.compile(r"\s*#\d+\s*$")
# Matches a leading "#<digits> " issue/PR token (new format, #414). Sits between
# the work prefix and the topic body, so it is stripped after the work prefix.
_LEADING_REF_RE = re.compile(r"^#\d+\s*")
# Matches the trailing " →#<digits>" current-work token (#593). Stripped so a
# manual rename of a name carrying it does not fold it into the stored topic —
# which would double it (``… →#588 →#588``) on the next rebuild.
_TRAILING_CUR_REF_RE = re.compile(r"\s*→\s*#\d+\s*$")

# States that suppress the W<N> prefix (no window info shown).
_NO_PREFIX_STATES = frozenset({"dead"})


def build_name(
    topic: str,
    state: str,
    window_number: int | None,
    *,
    lamp: bool = True,
    issue_ref: str | None = None,
    origin_issue_ref: str | None = None,
    closed: bool = False,
) -> str:
    """Build a thread name from its parts, capped at :data:`MAX_NAME_LEN` characters.

    ``window_number`` is the ``N`` from the tmux ``w{N}`` window name (a
    stable identifier), not tmux's volatile ``#{window_index}``.

    Format: ``<emoji> W<N> │ #<issue> <topic>`` when running/waiting/error with
    a window number and an ``issue_ref``. The ``#<issue>`` token (#414) is the
    Issue/PR number the thread is working on; it is dropped when ``issue_ref`` is
    ``None``. Drops the ``W<N> │`` work prefix for dead state or when the window
    number is unknown. Unknown ``state`` falls back to the ``alive``/🟢 emoji.
    When the combination is too long, the ``→#<current>`` token is dropped first
    (#593 — the identity must outlive the running commentary), then only the
    topic is truncated (the number is kept); under extreme length pressure the
    work prefix, then the number, are dropped before the topic disappears.

    ``origin_issue_ref`` (#593) is the number the thread was **opened for** — its
    identity. It leads the name; ``issue_ref`` (what the thread is working on
    *now*) is appended as ``→#<current>`` only when the two differ. When no
    origin is recorded (rows predating the column) the current number leads
    instead, so those threads render exactly as they did before.

    Both refs may be passed with or without a leading ``#`` — they are
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

    current = (issue_ref or "").lstrip("#").strip()
    # No origin recorded (a row written before #593) → the current number *is*
    # the thread's identity, which is exactly the pre-#593 rendering.
    origin = (origin_issue_ref or "").lstrip("#").strip() or current
    ref_token = f"#{origin} " if origin else ""
    cur_token = f" →#{current}" if current and current != origin else ""

    # #607: the number survives the stop. #512 dropped it because the tmux window
    # it names is exactly what was just killed — correct for "where do I look
    # now", but that is not the only thing the name is used for. yousan reads it
    # months later to find which workspace a piece of work lived in, and the
    # issue number alone is not enough. ``[停止]`` already says the window is
    # gone, so a stopped thread cannot be mistaken for a live one.
    #
    # A live thread elsewhere may hold the same number — numbers restart per
    # repository, and the production server already shows w1 four times over. The
    # collision is pre-existing and visual only: window lookup goes through the
    # pane's own ``@thread_id``, never through this name.
    if window_number is not None and (closed or state not in _NO_PREFIX_STATES):
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
        budget = MAX_NAME_LEN - len(fixed)
        if budget >= 1:
            break
    else:
        fixed = f"{mark}{prefix_emoji}"
        budget = MAX_NAME_LEN - len(fixed)

    if len(topic_clean) > budget:
        topic_clean = topic_clean[: max(budget, 0)]

    name = f"{fixed}{topic_clean}"[:MAX_NAME_LEN]
    # The ``→#<current>`` token is strictly additive: it is appended only when it
    # fits in the slack the topic left over, never by shortening the topic. The
    # thread's identity (``#<origin>``) and what it is about must both survive
    # first — this is the shed order AC4 asks for. It is also pointless without
    # the origin token, which extreme length pressure may already have dropped.
    if cur_token and ref_token in fixed and len(name) + len(cur_token) <= MAX_NAME_LEN:
        name += cur_token
    return name


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
    ``#<digits>`` issue/PR token (#414, if present), the trailing ``→#<digits>``
    current-work token (#593, if present), and the legacy trailing
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
    body = _TRAILING_CUR_REF_RE.sub("", body)
    body = _TRAILING_INDEX_RE.sub("", body)
    return body.strip()
