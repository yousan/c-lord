"""Claude's plan-limit banner: recognising it, and what c-lord says instead (#631).

When an account hits a plan limit the API answers 429 and Claude Code renders
the refusal *as the assistant's message* — on the pane and, identically, in the
jsonl transcript.  Both of c-lord's readers therefore meet the same sentence,
and both used to get it wrong in their own way:

* the **pane reader** (:mod:`c_lord.claude.tmux_runner`) saw a turn that
  produced nothing and told the user to send it again — advice that cannot work
  until the limit resets, which is how the reporter of #631 came to send the
  same request three times;
* the **transcript mirror** (:mod:`c_lord.transcript.mirror`) saw an ordinary
  assistant message and posted it verbatim, so the thread got a bare English
  line with no explanation and no recovery time — six copies of it in one
  thread on 2026-09-04, and in the two threads where the pane reader *did*
  detect the limit, the same sentence again in English one second after
  c-lord's own Japanese notice.

This module is the one place that knows the banner's vocabulary, so the two
readers cannot drift apart, plus the small registry that lets the mirror know
c-lord has already said it in Japanese.

The wording comes from the CLI's own builders (verified against the shipped
2.1.252 binary): ``"You've hit your ${label}${suffix}"`` where ``suffix`` is
``" · resets ${when}"`` and ``label`` is one of "weekly limit" (seven_day),
"session limit" (five_hour), "Opus limit" / "Sonnet limit" (seven_day_opus /
seven_day_sonnet), "usage limit" (overage), "org's monthly usage limit",
"monthly spend limit", or a bare "limit"; plus
``"You're out of usage credits${suffix}"``.  ``when`` is rendered as
``"Aug 29, 4pm (Asia/Tokyo)"`` more than a day out and as a bare
``"4pm (Asia/Tokyo)"`` within the day, and the whole clause is absent when the
API sent no ``resetsAt``.

What must NOT match is just as important: the same screen also carries the
*warning* banners ("You've used 79% of your weekly limit · resets ...",
"Approaching weekly limit · ...", "You're close to your usage limit"), which
render while Claude is working perfectly well.  Treating one of those as a stop
would strand a healthy session.
"""

from __future__ import annotations

import logging
import re
import time
from collections import OrderedDict

from .claude.types import UsageLimit

logger = logging.getLogger(__name__)


# -- Recognising the banner ----------------------------------------------------

# Leading TUI chrome allowed before the banner: indentation plus the gutter
# glyphs Claude Code draws beside message and tool-result lines.  Anchoring to
# a line that holds NOTHING but chrome + banner is what keeps the phrase from
# matching when it merely appears inside Claude's own prose — the same
# false-positive class as #156 / #184, and a live one here because c-lord
# threads discuss this very banner.
#
# ONE character at a time under ONE quantifier, deliberately.  The first version
# of this pattern nested them — ``(?:[glyphs]+[space]*)*`` — which is the classic
# catastrophic-backtracking shape: every way of splitting a run of dashes between
# the inner and outer repeat is tried before the match can fail.  A line of 28
# dashes (a markdown rule, a table border, an ASCII box — things Claude writes
# constantly) took **10 seconds**, and 40 would outlive the process.  That was
# reachable from the pane all along, and #631's fold made it reachable from every
# assistant message and every transcript rescue scan as well.
#
# Here the two branches are disjoint and each consumes exactly one character, so
# there is only ever one way to match a given run: linear, with no ambiguity to
# back-track through.  The whitespace branch stays ``[^\S\n]`` rather than a
# literal " \t" class because real panes are full of NBSP.
_LIMIT_GUTTER = r"^(?:[^\S\n]|[●⏺⎿╰│┃|>*•-])*"

# The blocking banners, and the reset clause that may follow them.
_USAGE_LIMIT_RE = re.compile(
    _LIMIT_GUTTER
    + r"(?:You've hit your (?P<scope>[^·\n]+?)"
    + r"|You(?:'|’)re out of (?P<credits>usage credits))"
    + r"[^\S\n]*"
    + r"(?:·[^\S\n]*resets[^\S\n]+(?P<reset>[^·\n]+?)[^\S\n]*)?"
    + r"(?:·[^\n]*)?$",
    re.MULTILINE,
)


# The banner's two variable parts end up in Discord — the reset time inside an
# embed, and the scope inside a PLAIN thread message, which pings.  Both are
# read out of content Claude (and therefore any file or web page Claude echoed)
# can influence, so an ``@everyone`` smuggled through the banner would mass-ping
# the guild.  The CLI's label vocabulary is small and closed ("weekly limit",
# "session limit", "Opus limit", "Sonnet limit", "usage limit", "usage credit
# limit", "org's monthly usage limit", "monthly spend limit", "fast limit", bare
# "limit"), and its reset rendering is a localised date/time, so anything
# outside these shapes is not a banner worth acting on: reject it rather than
# sanitise it, which also keeps false positives down.
_LIMIT_SCOPE_RE = re.compile(r"^[A-Za-z][A-Za-z' ]{0,39}$")
_LIMIT_RESET_RE = re.compile(r"^[A-Za-z0-9][A-Za-z0-9:,()/_. +-]{0,49}$")


# Cheap literal pre-filter.  Every banner contains one of these verbatim, so a
# text holding neither cannot match — and skipping the scan keeps the regex away
# from the long prose the mirror hands it on every single turn.
_LIMIT_MARKERS = ("hit your", "usage credits")


def extract_usage_limit(text: str) -> UsageLimit | None:
    """Return the plan limit shown anywhere in *text*, or None when there is none.

    Only the *blocking* banners count.  The percentage / "Approaching" warnings
    are deliberately excluded: they share the vocabulary but mean the opposite
    (Claude is still answering), and stopping a turn on one of those would be
    the same class of lie this module exists to remove.

    Pane callers gate this on "no response text yet this turn", the way
    ``_extract_startup_error`` is gated — a banner Claude *quotes* inside a real
    answer must not end that answer's turn (#631).
    """
    if not text or not any(marker in text for marker in _LIMIT_MARKERS):
        return None
    match = _USAGE_LIMIT_RE.search(text)
    if match is None:
        return None
    scope = (match.group("scope") or match.group("credits") or "").strip()
    if not _LIMIT_SCOPE_RE.match(scope):
        return None
    reset = match.group("reset")
    reset = reset.strip() if reset else None
    if reset is not None and not _LIMIT_RESET_RE.match(reset):
        # A banner whose reset clause is not a plain localised time is not one
        # we will quote at a user.  Keep the limit (it is real) but drop the
        # part we cannot vouch for, rather than passing it through.
        reset = None
    return UsageLimit(scope=scope, resets_at=reset, line=match.group(0).strip())


def count_usage_limit(text: str) -> int:
    """How many blocking limit banners are in *text*.

    A rising count means this turn added one.  Comparing counts rather than the
    banner text is what makes a repeat of the *same* limit detectable — the text
    is identical every time.
    """
    if not text or not any(marker in text for marker in _LIMIT_MARKERS):
        return 0
    return sum(1 for _ in _USAGE_LIMIT_RE.finditer(text))


# A refusal is one short line.  The marked-envelope path folds without reading
# the wording at all, so this cap is what stops it from being able to destroy a
# real answer: whatever else a rate-limit envelope might one day carry, anything
# longer than a banner is posted as-is rather than replaced by a line that would
# not describe it.  Every capture of the real thing is under 70 characters.
_MAX_REFUSAL_CHARS = 200


def is_refusal_shaped(text: str) -> bool:
    """True when *text* is short enough to be the CLI's one-line refusal."""
    stripped = text.strip()
    return bool(stripped) and "\n" not in stripped and len(stripped) <= _MAX_REFUSAL_CHARS


def banner_only(text: str) -> UsageLimit | None:
    """Return the limit when *text* is **nothing but** the banner, else None.

    This is the transcript-side question, and it is deliberately stricter than
    :func:`extract_usage_limit`: the mirror is about to *replace* this message,
    so anything Claude wrote around the banner would be destroyed by folding it.
    A quoted banner (this repo's threads are full of them) keeps its prose and
    therefore fails here, which is the whole point.
    """
    if not text:
        return None
    stripped = text.strip()
    # "Only the banner" implies the refusal's own shape, and asking that first
    # caps what the scan ever sees at one short line — the mirror calls this on
    # every assistant message, including answers megabytes long.
    if not is_refusal_shaped(stripped):
        return None
    limit = extract_usage_limit(stripped)
    if limit is None:
        return None
    # ``line`` is the matched span with its gutter chrome; the message counts as
    # "only the banner" when nothing else is left once that span is removed.
    if stripped != limit.line:
        return None
    return limit


# -- Recognising it from the transcript's own markers --------------------------

# Claude Code stamps a rate-limited turn far more explicitly than its wording
# does (verified on captures from 2.1.159 / .246 / .247 / .252): the message is
# authored by ``<synthetic>``, and the envelope carries ``isApiErrorMessage``
# with ``error: "rate_limit"`` and HTTP 429.  Keying on these first means a
# banner whose wording the CLI changes tomorrow is still recognised — and,
# unlike the text, they cannot be forged by anything Claude merely *says*.
_RATE_LIMIT_ERROR = "rate_limit"


def is_rate_limit_event(event: object) -> bool:
    """True when a raw transcript event is the CLI's own rate-limit refusal.

    Runs on every line the tail yields, so it never assumes a shape: an
    exception here would kill the tail task and with it the thread's mirror.
    """
    if not isinstance(event, dict):
        return False
    if event.get("error") != _RATE_LIMIT_ERROR:
        return False
    return bool(event.get("isApiErrorMessage")) or event.get("apiErrorStatus") == 429


# -- What c-lord says instead --------------------------------------------------


def folded_notice(limit: UsageLimit | None) -> str:
    """The one line that replaces the banner in the thread (#631 AC7).

    Deliberately *not* the wording of :func:`c_lord.discord_ui.embeds.usage_limit_embed`
    ("このターンは実行されていません"): six of the eight threads on 2026-09-04 hit
    the limit and then ran anyway, because the CLI retried internally.  Claiming
    the turn did not run would be a new lie in place of the old one, so this line
    only says what is certainly true — Claude is waiting on a limit — and hands
    over the recovery time.

    ``limit`` is None when the event is a rate-limit refusal whose wording did
    not parse.  There is still something worth saying (the thread is stalled),
    just nothing quotable, so the line goes out without the unvouched-for parts.
    """
    if limit is None:
        return "⏳ 一時的に Claude の利用上限に当たって待っています。"
    when = (
        f"{limit.resets_at} に回復します。"
        if limit.resets_at
        else "回復時刻は報告されませんでした。"
    )
    return f"⏳ 一時的に Claude の{limit.scope}（利用上限）に当たって待っています — {when}"


# -- "c-lord already said this in Japanese" ------------------------------------

# Backstop only.  An entry is normally dropped at the turn boundary by the
# mirror, because AC8 asks whether *this* turn has already been told; the TTL
# just keeps a thread whose boundary never arrived (a restart mid-turn) from
# carrying the entry forever.  It is deliberately short: a stale entry does not
# cause a duplicate, it causes SILENCE — the following turn's banner is folded
# away with nothing in its place — and under a live limit the reader is sending
# again right now.  Staging showed exactly that at a 300 s TTL on 2026-09-14
# (announced 12:53:16, wrongly suppressed the 12:54:25 turn).
_NOTICE_TTL_SECONDS = 60.0
# Production mirrors hundreds of threads; the map is bounded and evicted
# oldest-first.  A dropped entry costs one duplicated line, never correctness.
_MAX_THREADS = 2048


class UsageLimitNotices:
    """Which threads c-lord has just told about a limit, in its own words.

    The mirror asks before folding: when c-lord's ⏳ embed has already gone out
    for this thread, the mirror stays quiet entirely rather than saying the same
    thing again in different words (#631 AC8).

    One-directional on purpose.  The embed is the richer message (it names the
    scope, the reset time and what the reader can actually do), so it is the one
    that survives; the mirror's line exists for the case the pane reader never
    saw the limit at all — six of eight threads on 2026-09-04.  In the observed
    ordering the embed lands first (14:42:57 vs 14:42:58) because the pane
    reader breaks its poll loop the moment it sees the banner while the mirror
    holds text until the turn boundary.  If that ever inverts, the degraded mode
    is two messages saying the same thing, never a silent thread.
    """

    def __init__(self) -> None:
        self._at: OrderedDict[int, float] = OrderedDict()

    def note(self, thread_id: int, *, at: float | None = None) -> None:
        """Record that c-lord posted its own usage-limit notice for *thread_id*."""
        self._at[thread_id] = time.monotonic() if at is None else at
        self._at.move_to_end(thread_id)
        while len(self._at) > _MAX_THREADS:
            self._at.popitem(last=False)

    def announced(self, thread_id: int, *, now: float | None = None) -> bool:
        """True when c-lord's own notice for *thread_id* is recent enough to stand."""
        at = self._at.get(thread_id)
        if at is None:
            return False
        moment = time.monotonic() if now is None else now
        if moment - at > _NOTICE_TTL_SECONDS:
            del self._at[thread_id]
            return False
        return True

    def clear_thread(self, thread_id: int) -> None:
        self._at.pop(thread_id, None)

    def clear_all(self) -> None:
        self._at.clear()


usage_limit_notices = UsageLimitNotices()
