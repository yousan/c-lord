"""Claude Code runner that executes inside a tmux window.

Instead of spawning a subprocess with ``-p --output-format stream-json``,
this runner starts Claude in TUI mode inside a tmux pane and polls
``tmux capture-pane`` to extract text changes.

The trade-off is that structured stream events (tool use, thinking, etc.)
are not available — only plain text deltas.  However, users can
``tmux attach -t clord:wN`` to see the full Claude TUI in real time.
"""

from __future__ import annotations

import asyncio
import logging
import re
from collections.abc import AsyncGenerator
from pathlib import Path

from ..tmux import TmuxSessionManager, pane_command_is_dead
from ..transcript.resolver import derive_project_dir
from .context_usage import parse_context_total, parse_cost_from_pane
from .types import (
    FREE_TEXT_NONE,
    FREE_TEXT_NOTES,
    FREE_TEXT_ROW,
    AskOption,
    AskQuestion,
    FreeTextMode,
    MessageType,
    StreamEvent,
    UsageLimit,
)

logger = logging.getLogger(__name__)

# How often to poll capture-pane (seconds).
_POLL_INTERVAL = 0.5

# If extracted response text hasn't changed for this long AND the input prompt
# is visible (❯ bare), consider Claude done.
_RESPONSE_STABLE_TIMEOUT = 3.0

# Fallback: if response is stable for this long, break even without an input
# prompt (the prompt area may contain completion summaries or suggestions).
_RESPONSE_STABLE_FALLBACK = 30.0

# If no response appears at all for this long, give up (idle timeout).
# Generous because Claude can spend a while "thinking" before it emits anything
# visible — notably before an AskUserQuestion menu renders (#166), where
# extended thinking shows a static frame that defeats the raw-pane-activity
# guard.  The hard ``timeout_seconds`` (300s) is the real backstop; this only
# governs how long a genuinely silent turn waits before the fallback notice.
_IDLE_TIMEOUT = 60.0

# How long to wait for Claude to become ready (show input prompt).
_STARTUP_TIMEOUT = 30.0

# How long a turn is allowed to not-start-yet before the idle exit gives up on
# it (#562).  Distinct from ``_STARTUP_TIMEOUT``, which only covers a *cold*
# claude: a warm session used to get no grace at all, so 60s of pane silence on
# a busy host ended the turn and reported it finished.  Generous on purpose —
# a late "no response" notice is far cheaper than a false "finished", and the
# ``timeout_seconds`` backstop still bounds the wait.
_TURN_START_GRACE = 120.0

# Prefix of the RESULT error for "this turn never produced anything" (#562).
# Exported so callers can recognise the outcome without string-sniffing.
NO_RESPONSE_ERROR_PREFIX = "No response —"

# Prefix of the RESULT error for "Claude is rate limited" (#631).  A separate
# outcome from NO_RESPONSE on purpose: both produce an empty turn, but only
# one of them gets better if you send the message again.
USAGE_LIMIT_ERROR_PREFIX = "Usage limit —"

# How long an unknown interactive menu must be continuously visible before we
# alert Discord (seconds).  Guards against transient TUI redraws.
_UNKNOWN_ALERT_DELAY = 5.0

# How long an AskUserQuestion menu must be stable before bridging it to Discord
# buttons (seconds).  Shorter than the unknown delay — it is a definite,
# expected prompt — but still guards against a half-drawn menu frame (#166).
_ASK_ALERT_DELAY = 1.5

# /context probe: how many times to re-capture the pane while waiting for the
# (locally-rendered) /context output to settle, and the gap between captures.
_CONTEXT_PROBE_ATTEMPTS = 6
_CONTEXT_PROBE_INTERVAL = 0.5

# Delay between individual menu-navigation keystrokes (seconds).  Keys must be
# sent one at a time with a gap — batching `Down Down Enter` into one send-keys
# call is too fast and the TUI drops the navigations, selecting the wrong
# option (#171).
_MENU_NAV_DELAY = 0.25

# Delay after startup before polling begins (seconds).
_POST_STARTUP_DELAY = 1.0

# Startup grace after sending ``claude --continue`` before its outcome is
# judged at all (issue #123 Part 2).  The pane still runs zsh for a moment
# while claude execs, so a liveness probe taken any earlier reads every start
# as a failure.
_CONTINUE_CHECK_DELAY = 3.0

# How long a ``--continue`` with no folder-trust dialog on screen must stay
# alive before it counts as started (#657).  The grace above is not a verdict on
# its own: a session dir where claude has never run opens the trust dialog
# first, and a claude parked on that dialog is alive whatever ``--continue`` is
# going to do — staging measured the exit landing ~1.5s AFTER the dialog was
# answered.  So the clock only runs while the dialog is gone, and this is the
# margin over that observed exit.
_CONTINUE_SETTLE = 5.0

# Absolute ceiling on the undecided window (#657).  Only reachable when the
# trust dialog never closes — i.e. the keystrokes that answer it did not take —
# in which case "assume it started" is the same answer the old fixed-delay check
# gave, and the turn loop's own timeouts take it from there.
_CONTINUE_VERDICT_TIMEOUT = 15.0

#: How long :meth:`TmuxClaudeRunner.wake` waits for a restored pane to reach its
#: input box. Generous on purpose: a cold ``claude --continue`` re-renders the
#: whole conversation on a host that may already be running a dozen of them, and
#: the caller (``/tmux-screenshot``) has deferred its Discord response, which
#: buys 15 minutes. Reporting failure while the pane is in fact still coming up
#: is the worse error: it leaves a Claude nobody photographed.
_WAKE_TIMEOUT = 120.0


# #560: how many Enter presses send_input tries before giving up. Mirrors
# c_lord.tmux._SUBMIT_ATTEMPTS; kept as a literal here so the user-facing
# wording does not drag a tmux import into this module.
_SUBMIT_ATTEMPTS_HINT = 3


def _delivery_failure(action: str, prompt: str) -> str:
    """User-facing text for "the message never reached Claude" (#527).

    The old wording was ``Failed to send input to Claude in tmux`` — true, but
    it left the user with no idea what broke or what to do, which is how a
    19,852-byte attachment silently going nowhere looked from Discord.  Name
    the input size (the usual culprit) and the one command that fixes a dead
    pane.
    """
    size = len(prompt.encode("utf-8"))
    return (
        f"{action}に失敗しました — tmux のペインが入力を受け付けませんでした "
        f"(入力 {size:,} bytes)。ペインが落ちているか応答しない状態です。"
        "`/claude-restart` でセッションを立て直してから、もう一度送ってください。"
    )


def _missing_window(action: str, thread_id: int) -> str:
    """User-facing text for "there is no window to start Claude in" (#621).

    ``start_claude`` returns False for two unrelated reasons, and #527's wording
    only fits one of them.  When the window was never created there is no pane
    to be unresponsive, and ``/claude-restart`` — which restarts the process
    *inside* an existing pane — cannot conjure one.  Sending the reader there
    (as every scheduled run did) costs them the one attempt they had at fixing
    it themselves.  So name what is actually absent, and point at the two things
    that can actually be absent: the channel's repo binding, and tmux itself.
    """
    return (
        f"{action}に失敗しました — このスレッド用の tmux ウィンドウが作られていません "
        f"(thread={thread_id})。ペインが落ちているのではなく、Claude を動かす先の"
        "ウィンドウがそもそも存在しない状態です。"
        "チャンネルが `/clord-init` で repo に紐づいているか、"
        "ホストで tmux が使えるかを確認してください。"
    )


def _ambiguous_window(action: str, thread_id: int, session: str, names: list[str]) -> str:
    """User-facing text for "the tmux target does not identify one window" (#649).

    Two windows sharing a name make every ``session:NAME`` target ambiguous, so
    keystrokes land in whichever tmux matched first — another thread's checkout.
    The pane is alive and well, which is why the #527 wording ("ペインが落ちて
    いる", go run ``/claude-restart``) sent people to a command that could not
    possibly help: it restarts Claude *inside* a window, and the duplicate name
    is still there afterwards. Two threads sat dead for a day following that
    advice. Name the real problem and give the command that actually shows it.
    """
    listed = ", ".join(f"`{n}`" for n in names)
    return (
        f"{action}に失敗しました — このスレッドの tmux ウィンドウを一意に特定できません "
        f"(thread={thread_id})。同じ名前のウィンドウが複数あります: {listed}。"
        "ペインが落ちているのではなくターゲットが曖昧なので、"
        "`/claude-restart` では直りません。"
        f"ホストで `tmux list-windows -t {session} "
        "-F '#{window_id} #{window_name} #{@thread_id} #{pane_current_path}'` を確認し、"
        "重複したウィンドウを rename するか、不要な方を kill してください。"
    )


def _stuck_in_input_box(prompt: str) -> str:
    """User-facing text for "typed into the pane, but it would not submit" (#560).

    Deliberately does **not** suggest ``/claude-restart``: the message is sitting
    in the input box right now, and restarting the session would throw it away.
    """
    size = len(prompt.encode("utf-8"))
    return (
        f"メッセージの送信に失敗しました — 本文はペインの入力欄に入りましたが、"
        f"Enter を {_SUBMIT_ATTEMPTS_HINT} 回試しても送信されませんでした "
        f"(入力 {size:,} bytes)。本文はまだ入力欄に残っています。"
        "もう一度送り直すか、`/tmux-screenshot` で状態を確認してください "
        "(`/claude-restart` は入力欄の本文ごと破棄されるので、まず送り直しを試してください)。"
    )


# Patterns that indicate a trust/safety prompt that needs Enter to dismiss.
_TRUST_PROMPT_MARKERS = (
    "Yes, I trust this folder",
    "Enter to confirm",
)

# The trust dialog's distinctive *menu option line*.  Claude Code has shipped it
# in two shapes, and BOTH must be handled:
#
#   <= 2.1.247   "❯ 1. Yes, I trust this folder" / "  2. No, exit"  (numbered, Yes first)
#   >= 2.1.248   "❯ No, exit" / "  Yes, I trust this folder"        (unnumbered, No first)
#
# Detection keys on the anchored option line rather than a loose substring of the
# marker phrases anywhere in the pane: the runner captures ~500 lines of
# scrollback, so a session that merely *mentions* the dialog's wording (e.g. a
# chat about this very feature) must not trip a spurious Enter into the input.
# The unnumbered line is plainer prose than the numbered one, so detection also
# requires the dialog's confirm footer to be present.
# The numbered line is a structure prose does not reproduce, so it stands alone.
_TRUST_PROMPT_NUMBERED_RE = re.compile(
    r"^\s*❯?\s*\d+\.\s+Yes, I trust this folder\s*$", re.MULTILINE
)
# The unnumbered line is plain enough that quoted prose could reproduce it, so it
# is only trusted alongside the dialog's confirm footer.
_TRUST_PROMPT_RE = re.compile(r"^\s*❯?\s*Yes, I trust this folder\s*$", re.MULTILINE)

# One option line of the trust dialog, in either shape.  Used to work out how
# many Downs land the cursor on "Yes" — on the newer dialog the cursor starts on
# "No, exit", so a bare Enter DECLINES trust and Claude exits without ever
# starting the turn.
_TRUST_OPTION_RE = re.compile(
    r"^(?P<cursor>\s*❯)?\s*(?:\d+\.\s+)?(?P<label>Yes, I trust this folder|No, exit)\s*$"
)

# Footer the trust dialog always prints under its options.
_TRUST_CONFIRM_FOOTER = "Enter to confirm"

# Patterns from legacy AskUserQuestion TUI menus that must NOT be flagged as
# "unknown" interactive prompts (#153).
#
# Plan-approval markers ("Would you like to proceed?", "No, keep planning") were
# REMOVED here (#251): they are now actively bridged via _parse_plan_from_pane,
# and the run loop ``continue``s on a parsed plan menu before reaching the
# unknown-interactive check.  Keeping them in this whitelist would defeat the
# fallback — an unparsed plan-ish menu (e.g. a future format change the parser
# misses) must still trip the unknown-TUI notice rather than stall silently.
# AskUserQuestion's current variant is excluded via _is_ask_question(); these
# legacy "Other" markers remain only for the older AskUserQuestion layout.
# Sources: Claude Code TUI catalog (docs/tui-prompts.md).
_KNOWN_INTERACTIVE_MARKERS = (
    # AskUserQuestion (legacy) — adds "Other" as the last option (no number prefix)
    "\nOther",
    "\n  Other",
)

# Patterns that indicate a permission/approval prompt that needs "Yes".
# Sources: existing c-lord markers + Claude Code v2.1+ npm package strings.
_PERMISSION_PROMPT_MARKERS = (
    "Do you want to proceed?",
    "This command requires approval",
    "Do you want to continue?",
    "Do you want to allow Claude to fetch this content?",
    "Do you want to allow this connection?",
    "Continue anyway?",
)

# Fatal Claude-CLI startup errors that appear in the tmux pane when the
# ``claude`` process dies immediately instead of starting a session — e.g. a
# broken install whose platform-native binary was never downloaded
# (``Error: claude native binary not installed.``), or ``claude`` missing from
# PATH (``command not found``).  When the pane shows one of these and no
# response was ever produced, the runner surfaces it to Discord as an error
# instead of silently reporting a normal completion (#366).  Matched
# case-insensitively as substrings of a single pane line.
_STARTUP_ERROR_MARKERS = (
    "native binary not installed",
    "command not found: claude",
    "claude: command not found",
)


def _extract_startup_error(pane: str) -> str | None:
    """Return a fatal Claude-CLI startup-error line found in *pane*, else None.

    Used when the runner reaches completion without ever extracting a response:
    the pane then typically shows the CLI's own error output (e.g.
    ``Error: claude native binary not installed.``) above the shell prompt.
    Returns the first line containing a known fatal signature (trimmed and
    length-capped for a Discord embed), or None when no such signature is
    present.  Gating callers on "no response yet" keeps this from matching a
    marker phrase that merely appears inside Claude's own answer (#366).
    """
    if not pane:
        return None
    for line in pane.splitlines():
        stripped = line.strip()
        if any(marker in stripped.lower() for marker in _STARTUP_ERROR_MARKERS):
            return stripped[:300]
    return None


# -- Claude plan/usage limits (#631) --------------------------------------------
#
# When an account hits a plan limit the API answers 429 and Claude Code renders
# the refusal as the assistant's message, then goes idle.  c-lord saw a turn
# that produced nothing and told the user to send it again — advice that can
# never work, because the limit holds until it resets.  So the banner has to be
# recognised, and its reset time reported instead.
#
# The wording comes from the CLI's own builders (verified against the shipped
# 2.1.252 binary): ``"You've hit your ${label}${suffix}"`` where ``suffix`` is
# ``" · resets ${when}"`` and ``label`` is one of "weekly limit" (seven_day),
# "session limit" (five_hour), "Opus limit" / "Sonnet limit" (seven_day_opus /
# seven_day_sonnet), "usage limit" (overage), "org's monthly usage limit", or a
# bare "limit"; plus ``"You're out of usage credits${suffix}"``.  ``when`` is
# rendered as ``"Aug 29, 4pm (Asia/Tokyo)"`` more than a day out and as a bare
# ``"4pm (Asia/Tokyo)"`` within the day, and the whole clause is absent when the
# API sent no ``resetsAt``.
#
# What must NOT match is just as important: the same screen also carries the
# *warning* banners ("You've used 79% of your weekly limit · resets ...",
# "Approaching weekly limit · ...", "You're close to your usage limit"), which
# render while Claude is working perfectly well.  Treating one of those as a
# stop would strand a healthy session.

# Leading TUI chrome allowed before the banner: indentation plus the gutter
# glyphs Claude Code draws beside message and tool-result lines.  Anchoring to
# a line that holds NOTHING but chrome + banner is what keeps the phrase from
# matching when it merely appears inside Claude's own prose — the same
# false-positive class as #156 / #184, and a live one here because c-lord
# threads discuss this very banner.
_LIMIT_GUTTER = r"^[^\S\n]*(?:[●⏺⎿╰│┃|>*•-]+[^\S\n]*)*"

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
# scraped from a pane whose contents Claude (and therefore any file or web page
# Claude echoed) can influence, so an ``@everyone`` smuggled through the banner
# would mass-ping the guild.  The CLI's label vocabulary is small and closed
# ("weekly limit", "session limit", "Opus limit", "Sonnet limit", "usage limit",
# "usage credit limit", "org's monthly usage limit", "monthly spend limit",
# "fast limit", bare "limit"), and its reset rendering is a localised date/time,
# so anything outside these shapes is not a banner worth acting on: reject it
# rather than sanitise it, which also keeps false positives down.
_LIMIT_SCOPE_RE = re.compile(r"^[A-Za-z][A-Za-z' ]{0,39}$")
_LIMIT_RESET_RE = re.compile(r"^[A-Za-z0-9][A-Za-z0-9:,()/_. +-]{0,49}$")


# The choice menu Claude Code opens under the banner (2.1.252):
#
#     What do you want to do?
#     ❯ 1. Stop and wait for limit to reset
#       2. Switch to usage credits
#       3. Switch to Team plan
#
# It has to be closed — a menu left open swallows the NEXT message in the thread
# instead of letting it reach the prompt.  But two of its three answers change
# the account's billing, so the option is located by NAME and c-lord presses
# nothing at all when it cannot find the one that spends nothing.  Matching on
# position would make a future reordering buy a Team plan.
_LIMIT_MENU_OPTION_RE = re.compile(
    r"^[^\S\n]*❯?[^\S\n]*(?P<index>\d+)\.[^\S\n]+(?P<label>\S.*?)[^\S\n]*$"
)

# The one answer that costs nothing and changes no setting.
_LIMIT_MENU_WAIT_MARKERS = ("stop and wait", "wait for limit")


# Header of the limit's choice menu, and the options only IT offers.  Both are
# required: "What do you want to do?" alone is too plain to key on, and the
# option labels alone could appear in prose about this very feature.
_LIMIT_MENU_HEADER = "What do you want to do?"
_LIMIT_MENU_SIGNATURE_MARKERS = (
    "stop and wait",
    "wait for limit",
    "switch to usage credits",
    "switch to team plan",
    "continue automatically",
)


def _usage_limit_menu_open(pane: str) -> bool:
    """True when the limit's choice menu is on screen and unanswered (#631).

    This is the strongest "the turn is blocked right now" signal there is: the
    menu closes as soon as it is answered, and while it is open the next message
    typed into the pane lands in the menu instead of the prompt.  It is what
    tells a fresh refusal apart from a re-rendered old banner — including the
    case the reporter actually hit, where the SAME request was sent three times
    and every refusal printed the identical line.
    """
    if not pane or _LIMIT_MENU_HEADER not in pane:
        return False
    for line in pane.splitlines():
        match = _LIMIT_MENU_OPTION_RE.match(line)
        if match is None:
            continue
        label = match.group("label").lower()
        if any(marker in label for marker in _LIMIT_MENU_SIGNATURE_MARKERS):
            return True
    return False


def _count_usage_limit(pane: str) -> int:
    """How many blocking limit banners are on *pane*.

    A rising count means this turn added one.  Comparing counts rather than the
    banner text is what makes a repeat of the *same* limit detectable — the text
    is identical every time.
    """
    if not pane:
        return 0
    return sum(1 for _ in _USAGE_LIMIT_RE.finditer(pane))


def _usage_limit_wait_option(pane: str) -> int | None:
    """0-based index of the limit menu's "keep waiting" option, else None.

    None means "press nothing": either there is no menu, or none of its options
    could be identified as the free one.  Silence is the safe failure here —
    a stuck menu costs a turn, a wrong keystroke costs money.
    """
    if not pane:
        return None
    options: list[str] = []
    for line in pane.splitlines():
        match = _LIMIT_MENU_OPTION_RE.match(line)
        if match is None:
            if options:
                break
            continue
        # Options are numbered from 1 and contiguous; anything else is not the
        # menu (a numbered list in Claude's prose, say).
        if int(match.group("index")) != len(options) + 1:
            options = []
            continue
        options.append(match.group("label").lower())
    for i, label in enumerate(options):
        if any(marker in label for marker in _LIMIT_MENU_WAIT_MARKERS):
            return i
    return None


def _extract_usage_limit(pane: str) -> UsageLimit | None:
    """Return the plan limit shown on *pane*, or None when there is none.

    Only the *blocking* banners count.  The percentage / "Approaching" warnings
    are deliberately excluded: they share the vocabulary but mean the opposite
    (Claude is still answering), and stopping a turn on one of those would be
    the same class of lie this function exists to remove.

    Callers gate this on "no response text yet this turn", the way
    :func:`_extract_startup_error` is gated — a banner Claude *quotes* inside a
    real answer must not end that answer's turn (#631).
    """
    if not pane:
        return None
    match = _USAGE_LIMIT_RE.search(pane)
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
    return UsageLimit(
        scope=scope,
        resets_at=reset,
        line=match.group(0).strip()[:300],
    )


# Regex matching [y/N] or [Y/n] inline yes/no prompts (Claude Code v2.1+).
# These need "y" + Enter instead of just Enter (Enter selects the default, N).
_YN_PROMPT_RE = re.compile(r"\[y/N\]|\[Y/n\]", re.IGNORECASE)

# Regex matching a numbered-menu cursor line: "❯ 1. ..." or y/N inline prompt.
# Used to detect interactive menus regardless of whether the question text is known.
_INTERACTIVE_MENU_RE = re.compile(r"^\s*❯\s+\d+\.", re.MULTILINE)

# Number of lines from the bottom of the pane to scan for interactive prompts.
# Real Claude Code prompts always appear right before the input area (❯) at the
# bottom of the terminal.  Conversation text higher up in the scrollback must
# not trigger auto-accept (#156).
_PERMISSION_SCAN_LINES = 15

# Number of lines from the bottom to scan for the live input box (#62).  Must be
# generous: the box sits above the bottom chrome (separator + user-configurable
# ccstatusline rows + ``-- INSERT --`` status bar + effort/tip footer), which
# is commonly 6–8 lines tall, so a smaller window misses the ``❯`` box line.
_INPUT_PROMPT_SCAN_LINES = 15


def _permission_zone(text: str) -> str:
    """Return the bottom N lines of the pane where real prompts appear.

    Only this zone is scanned for permission / y/N / unknown-interactive markers.
    Conversation text in the scrollback above is excluded, preventing false
    positives when Claude's own output contains marker phrases (#156).

    The window is anchored to the last line carrying *content*, not to the last
    line of the capture (#611).  A prompt that draws no input box under itself —
    an AskUserQuestion menu, or its "Review your answers" confirmation — leaves
    the rest of the pane as blank rows, and a flat ``lines[-N:]`` then returned
    nothing but that padding.  Every zone-based check went blind at the same
    moment: ``_is_ask_submit_screen`` stopped pressing Enter on an answered
    flow, *and* ``_has_unknown_interactive`` — the fail-safe whose whole job is
    to shout about a stuck menu — could not see it either, so the session
    stalled without emitting a single log line.  Skipping the padding only moves
    the window across rows that carry no signal, so the #156 guarantee is intact:
    conversation text above a pane that really does end in chrome stays excluded.
    """
    lines = text.splitlines()
    while lines and not lines[-1].strip():
        lines.pop()
    return "\n".join(lines[-_PERMISSION_SCAN_LINES:])


# Matches a numbered menu item line, with or without the leading ❯ cursor:
# "❯ 1. Production", "  2. Staging".  Used to build a stable signature of an
# unknown menu so duplicate alerts can be suppressed (#165).
_MENU_ITEM_RE = re.compile(r"^\s*❯?\s*(\d+\..*\S)\s*$", re.MULTILINE)


def _unknown_prompt_signature(text: str) -> str:
    """Stable identity of an unknown interactive prompt, ignoring volatile chrome.

    The pane's spinner, elapsed-seconds and cost rows change on every poll, so
    comparing raw captures would defeat dedup.  We key on the menu's option
    lines (cursor stripped) plus any inline [y/N] line — the parts that stay
    constant while the same menu lingers (#165).
    """
    zone = _permission_zone(text)
    sig_lines = [m.strip() for m in _MENU_ITEM_RE.findall(zone)]
    for line in zone.splitlines():
        if _YN_PROMPT_RE.search(line):
            sig_lines.append(line.strip())
    return "\n".join(sig_lines)


# Regex for separator lines (all box-drawing horizontal characters).
_SEPARATOR_RE = re.compile(r"^[─━═─\s]{10,}$")

# -- AskUserQuestion TUI menu parsing (#166) ----------------------------------
# The AskUserQuestion tool renders a numbered menu in the pane that c-lord
# bridges to Discord buttons.  Recent Claude Code (v2.1.150) appends two
# meta-affordances — "Type something." and "Chat about this" — instead of the
# older "Other".  "Chat about this" is the most stable signature of the menu.
_ASK_SIGNATURE = "Chat about this"
_ASK_META_LABELS = ("Type something.", "Chat about this")
_ASK_HEADER_RE = re.compile(r"^\s*☐\s+(.+?)\s*$")
# A numbered option line, with or without the leading ❯ cursor.
_ASK_OPTION_RE = re.compile(r"^\s*❯?\s*\d+\.\s+(.+?)\s*$", re.MULTILINE)
# #388: multiSelect menus render checkbox options ("❯ 1. [ ] ログ強化") under a
# tab row ("←  ☐ 機能  ✔ Submit  →").  The checkbox prefix is stripped from
# labels and its presence marks the question multi-select.
_ASK_CHECKBOX_RE = re.compile(r"^\[[ xX✓]\]\s*")
_ASK_TAB_HEADER_RE = re.compile(r"☐\s+(.+?)\s{2,}")
# #388: preview-equipped menus draw a preview pane to the RIGHT of the option
# list; any of these box-drawing characters on an option line marks where the
# pane starts, and everything from that column on must be ignored.
_ASK_PREVIEW_BOX_CHARS = ("┌", "│", "└", "╭", "╰", "┐", "┘")


def _join_wrapped(label: str, tail: str) -> str:
    """Re-join an option label the TUI wrapped onto the next line (#579).

    The wrap happens wherever the column runs out, so it can land mid-word:
    Japanese needs the halves glued (「…（推」+「奨）」), while a Latin wrap broke
    at a space that the capture then stripped, and gluing there would read as
    "somewordelse". Insert a space only between two word characters.
    """
    if not label:
        return tail
    if label[-1].isalnum() and label[-1].isascii() and tail[:1].isalnum() and tail[:1].isascii():
        return f"{label} {tail}"
    return f"{label}{tail}"


# #650: the two ways an open menu accepts free text.  The classic layout has a
# "Type something." row to type onto; the preview layout (drawn as soon as any
# option carries a ``preview``) replaces it with a ``Notes:`` field opened by
# ``n``.  Both footers advertise "n to add notes", so that substring matches the
# hint line and the "press n to add notes" placeholder alike.
_ASK_NOTES_AFFORDANCE = "n to add notes"


def _free_text_mode(menu_text: str) -> FreeTextMode:
    """Which keystrokes this menu takes for a typed answer (#650).

    Read off the pane rather than assumed, because guessing is not survivable:
    on a preview menu the classic sequence (``Down`` × option_count → type →
    ``Enter``) parks the cursor on "Chat about this", where printable keys are
    ignored and ``Enter`` returns "(No answer provided)" — the user's sentence
    is silently dropped (production incident, 2026-09-01).

    *menu_text* must cover the ACTIVE menu and the hint line below it, not the
    whole scrollback: an older menu's affordance would otherwise decide how the
    current one is answered.
    """
    if "Type something" in menu_text:
        return FREE_TEXT_ROW
    if _ASK_NOTES_AFFORDANCE in menu_text:
        return FREE_TEXT_NOTES
    return FREE_TEXT_NONE


def _is_ask_meta_label(label: str) -> bool:
    """True for TUI affordance rows that must not become Discord buttons.

    "Type something." lost its trailing period in the multiSelect layout, and
    that layout adds a "Submit" row (#388), so exact matching is not enough.
    """
    return label in ("Chat about this", "Submit") or label.startswith("Type something")


def _is_ask_question(text: str) -> bool:
    """True iff the pane shows an AskUserQuestion TUI menu (current variant)."""
    return _ASK_SIGNATURE in text and bool(_ASK_OPTION_RE.search(text))


# A multi-question AskUserQuestion ends on a "Review your answers" screen with a
# two-option menu — "Submit answers" / "Cancel" — and the cursor defaulting to
# "Submit answers".  It carries NO "Chat about this" marker, so _is_ask_question
# misses it; without explicit handling it falls through to the unknown-prompt
# alert.  Match the two option labels (cursor optional) anchored to the bottom
# zone, where real prompts live — the pairing is unique to this screen, so it
# can't be confused with a normal question menu or an unrelated numbered list.
_ASK_SUBMIT_RE = re.compile(r"^\s*❯?\s*\d+\.\s+Submit answers\s*$", re.MULTILINE)
_ASK_CANCEL_RE = re.compile(r"^\s*❯?\s*\d+\.\s+Cancel\s*$", re.MULTILINE)


def _is_ask_submit_screen(text: str) -> bool:
    """True iff the pane shows the AskUserQuestion Submit/Review confirmation.

    All questions were already answered via the Discord bridge; this final
    screen only needs a bare Enter (cursor starts on "Submit answers") to
    submit.  Only the bottom zone is scanned so a stale review screen left in
    the scrollback can't re-trigger a submit.
    """
    zone = _permission_zone(text)
    return bool(_ASK_SUBMIT_RE.search(zone)) and bool(_ASK_CANCEL_RE.search(zone))


def _parse_ask_from_pane(text: str) -> AskQuestion | None:
    """Parse an AskUserQuestion TUI menu from the pane into an ``AskQuestion``.

    Returns ``None`` when the pane is not an AskUserQuestion menu (e.g. a Plan
    approval menu, an idle prompt, or an unrelated numbered list).

    The returned ``options`` list holds only the *real* choices in display
    order, with meta-affordances excluded.  Because the TUI numbers real
    options ``1..M`` contiguously before the meta-options, the 0-based index of
    an option in this list equals the number of ``Down`` presses needed to land
    the cursor on it from the top (see ``TmuxClaudeRunner.answer_menu``).
    """
    if _ASK_SIGNATURE not in text:
        return None

    lines = text.splitlines()

    # capture-pane returns up to 500 scrollback lines, which may contain OLD
    # AskUserQuestion menus.  Anchor to the *active* (bottom-most) menu: the last
    # "Chat about this" line marks its end, and the nearest ☐ header above it
    # marks its start (#166 staging fix — stale options caused duplicate Select
    # values → Discord 400).
    end_idx = max(i for i, line in enumerate(lines) if _ASK_SIGNATURE in line)

    header = ""
    header_idx = -1
    for i in range(end_idx, -1, -1):
        m = _ASK_HEADER_RE.match(lines[i])
        if m:
            header = m.group(1).strip()
            header_idx = i
            break
        # multiSelect tab row ("←  ☐ 機能  ✔ Submit  →") — the header sits
        # inside the row instead of owning the whole line (#388).
        if "☐" in lines[i]:
            m2 = _ASK_TAB_HEADER_RE.search(lines[i])
            if m2:
                header = m2.group(1).strip()
                header_idx = i
                break

    # Bound the scan: from the header (or, if it scrolled off, a small window
    # above the menu end) down to the menu end — never the whole buffer.
    scan_from = header_idx + 1 if header_idx >= 0 else max(0, end_idx - 20)

    # #388: preview-equipped menus draw a box to the RIGHT of the options.  The
    # pane start column is the leftmost box-drawing character on any numbered
    # option line; everything from that column on (on every menu line) is the
    # preview pane, not menu content.
    pane_col: int | None = None
    for i in range(scan_from, end_idx + 1):
        if _ASK_OPTION_RE.match(lines[i]):
            cols = [lines[i].find(ch) for ch in _ASK_PREVIEW_BOX_CHARS if ch in lines[i]]
            if cols:
                c = min(cols)
                pane_col = c if pane_col is None else min(pane_col, c)

    def _menu_text(line: str) -> str:
        """The menu-column part of *line*, with any preview-pane box cut off.

        ``pane_col`` is a *character* index taken from the numbered option lines,
        but the box is drawn at a fixed *display* column: a line whose text is
        double-width (Japanese) reaches that column in fewer characters, so the
        slice alone leaves the box character (and the preview text) behind. That
        leftover is what put "…並べる    │" into an option label (#579), so cut
        again at the first box character actually present in this line.
        """
        text = line[:pane_col] if pane_col is not None else line
        cuts = [text.find(ch) for ch in _ASK_PREVIEW_BOX_CHARS if ch in text]
        return text[: min(cuts)] if cuts else text

    # Indices of every numbered line (including meta-options) within the menu —
    # used to bound the region from which each real option's description is read.
    all_opt_indices = [
        i for i in range(scan_from, end_idx + 1) if _ASK_OPTION_RE.match(_menu_text(lines[i]))
    ]

    options: list[AskOption] = []
    first_opt_idx: int | None = None
    multi_select = False
    for pos, i in enumerate(all_opt_indices):
        label = _ASK_OPTION_RE.match(_menu_text(lines[i])).group(1).strip()  # type: ignore[union-attr]
        # multiSelect checkbox prefix ("[ ] ログ強化") → strip + flag (#388).
        if _ASK_CHECKBOX_RE.match(label):
            label = _ASK_CHECKBOX_RE.sub("", label, count=1).strip()
            multi_select = True
        if _is_ask_meta_label(label):
            continue
        if first_opt_idx is None:
            first_opt_idx = i
        # Description (#169): the first non-empty, non-separator line between this
        # option and the next numbered line — Claude renders it indented below
        # the option.  Empty when the option has no description.
        #
        # #579: in a preview-table menu (``pane_col`` set) the left column is
        # narrow and holds labels only — the explanation is in the box on the
        # right — so a non-numbered line there is the continuation of an option
        # whose text did not fit on one line. Two shapes, both real:
        #   "❯ 1. 具体的な場面から入る（推" / "    奨）"   → a label cut mid-word
        #   "  2." / "    欠けているものを先に並べる"      → the whole label wrapped
        # The second used to produce ``label=""``, which Discord rejects with 400
        # ("This field is required"), so the menu never posted at all. Dropping
        # such an option instead would be worse than an empty label: the TUI
        # still shows it and answers are delivered as ``Down × index``, so a
        # shorter list would select the wrong option.
        #
        # Indentation cannot be the test here: descriptions sit at column 5 in
        # a plain menu but at column 2 in a multiSelect one (the "[ ] " checkbox
        # shifts them), which overlaps the wrapped-tail indent.
        #
        # ``end_idx`` (exclusive) rather than ``end_idx + 1``: that line is the
        # "Chat about this" affordance, which was being read as the last
        # option's description.
        next_i = all_opt_indices[pos + 1] if pos + 1 < len(all_opt_indices) else end_idx
        description = ""
        for j in range(i + 1, next_i):
            raw = _menu_text(lines[j])
            text = raw.strip()
            if not text or _SEPARATOR_RE.match(raw):
                continue
            if pane_col is not None and not description:
                # Preview-table layout: the explanation lives in the box on the
                # right, so the narrow left column carries labels and nothing
                # else. A non-numbered line in it is therefore the tail of the
                # label above, never a description (#579).
                label = _join_wrapped(label, text)
                continue
            description = text
            break
        options.append(AskOption(label=label, description=description))

    if not options:
        return None

    # The question is the last non-empty, non-separator line between the header
    # and the first option (it sits closest to the options).
    question = ""
    end = first_opt_idx if first_opt_idx is not None else end_idx
    for line in lines[scan_from:end]:
        s = _menu_text(line).strip()
        if not s or _SEPARATOR_RE.match(_menu_text(line)):
            continue
        question = s

    # #399: carry the prose spoken directly above the menu (経緯・推し). Only
    # when the ☐ header anchored the menu — the scrolled-off fallback window
    # gives no reliable upper bound, so guessing there risks chrome leaks.
    context = _extract_pane_context(lines, header_idx) if header_idx >= 0 else ""

    # #650: the free-text affordance sits at the very bottom of the menu — the
    # "Type something." row just above "Chat about this", or the ``Notes:``
    # field plus the "n to add notes" hint printed *below* it.  Scan from the
    # menu's own start to the end of the capture so the hint line is included
    # and an older menu higher up in the scrollback is not.
    free_text_mode = _free_text_mode("\n".join(lines[scan_from:]))

    return AskQuestion(
        question=question,
        header=header,
        options=options,
        multi_select=multi_select,
        context=context,
        allow_other=free_text_mode != FREE_TEXT_NONE,
        free_text_mode=free_text_mode,
    )


# -- Plan approval (ExitPlanMode) TUI menu parsing (#251) ---------------------
# The ExitPlanMode menu ("Claude has written up a plan … Would you like to
# proceed?") is a numbered Yes/No menu c-lord bridges to Discord buttons via
# the same path as AskUserQuestion (#166).  Unlike AskUserQuestion it has no
# "Chat about this" / "Type something." rows, so it needs its own detector.
# Labels are parsed dynamically — the menu text changes between Claude Code
# versions (e.g. v2.1.156 dropped "No, keep planning" for "No, refine with
# Ultraplan …"), so hard-coding them would silently break on the next change.
# "Would you like to proceed?" is distinct from the permission prompt's "Do you
# want to proceed?" (_PERMISSION_PROMPT_MARKERS), so it uniquely marks a plan.
_PLAN_SIGNATURE = "Would you like to proceed?"
# The plan body is rendered between this header and the menu; we fold it into
# the question so reviewers see the plan on Discord before approving (#251).
_PLAN_BODY_START = "Here is Claude's plan:"
# A line that is purely box-drawing dashes (the plan body's ╌╌╌ / ─── rules).
_PLAN_RULE_RE = re.compile(r"^[╌─━═┄┈\-\s]+$")


def _parse_plan_from_pane(text: str) -> AskQuestion | None:
    """Parse an ExitPlanMode approval menu from the pane into an ``AskQuestion``.

    Returns ``None`` when the pane is not a plan-approval menu (an
    AskUserQuestion menu, a permission prompt, or any non-plan screen).  The
    returned options preserve display order; the 0-based index of each option
    equals the number of ``Down`` presses needed to land the cursor on it, so
    the result answers the open menu via :meth:`TmuxClaudeRunner.answer_menu`
    exactly like a ``pane_ask`` (#166).

    ``allow_other`` is ``False``: the plan menu's free-text option ("Tell Claude
    what to change") is a normal numbered choice, not AskUserQuestion's "Type
    something." row, so the generic Other modal must be suppressed.
    """
    if not text or _PLAN_SIGNATURE not in text:
        return None
    # AskUserQuestion menus are parsed by _parse_ask_from_pane; never here.
    if _is_ask_question(text):
        return None

    lines = text.splitlines()
    # Anchor to the active (bottom-most) menu — scrollback may hold old plans.
    sig_idx = max(i for i, line in enumerate(lines) if _PLAN_SIGNATURE in line)

    options: list[AskOption] = []
    started = False
    for line in lines[sig_idx + 1 :]:
        m = _ASK_OPTION_RE.match(line)
        if m:
            options.append(AskOption(label=m.group(1).strip()))
            started = True
        elif started:
            # First non-numbered line after the options ends the menu (e.g. the
            # "shift+tab to approve with this feedback" hint under option 4).
            break
        elif line.strip() == "":
            continue  # blank padding between the question and the first option
        else:
            break  # unexpected non-option content before any option — bail

    if len(options) < 2:
        return None

    question = lines[sig_idx].strip()
    body = _extract_plan_body(lines, sig_idx)
    if body:
        question = f"{body}\n\n{question}"
    # #399 (AC4): prose spoken before ExitPlanMode sits above the plan box —
    # same buffering gap as AskUserQuestion, same narrow extraction.
    body_starts = [i for i, line in enumerate(lines[:sig_idx]) if _PLAN_BODY_START in line]
    context = _extract_pane_context(lines, max(body_starts)) if body_starts else ""
    return AskQuestion(
        question=question,
        header="📋 Plan ready — approve?",
        options=options,
        multi_select=False,
        allow_other=False,
        context=context,
    )


def _extract_plan_body(lines: list[str], sig_idx: int) -> str:
    """Return the plan markdown shown above the menu, for the Discord embed.

    Reads the lines between the last ``Here is Claude's plan:`` header and the
    menu signature, dropping the box-drawing rule lines that frame it.  Returns
    ``""`` when no plan body is present (e.g. the legacy menu format), in which
    case only the one-line question is shown.
    """
    starts = [i for i, line in enumerate(lines[:sig_idx]) if _PLAN_BODY_START in line]
    if not starts:
        return ""
    out: list[str] = []
    for line in lines[max(starts) + 1 : sig_idx]:
        s = line.rstrip()
        # Drop box-drawing rule lines (╌╌╌ / ───) but keep blank lines so the
        # plan's paragraph structure survives in the embed.
        if s.strip() and _PLAN_RULE_RE.match(line):
            continue
        out.append(s)
    # Collapse the leading/trailing blank padding the TUI adds.
    return "\n".join(out).strip()


# -- #399: prose context above a menu ------------------------------------------
# When Claude talks (経緯・推し) and then opens an AskUserQuestion / plan menu,
# the CLI buffers the whole jsonl chunk — preceding text block included — until
# the menu resolves, so the transcript mirror structurally cannot deliver that
# prose while the menu is open (#359 S2). The pane is the only live source.
#
# To avoid reviving the #53 TUI-scrape path, extraction is deliberately narrow:
# ONLY the last column-0 "● " response block sitting directly above the menu
# frame is carried, and nothing at all when that block is a tool invocation
# ("● Bash(date)"), tool output ("⎿ …"), the echoed user prompt ("❯ …"), or
# known chrome. The blast radius of a future chrome change is thus confined to
# the single context message attached to the ask bridge.
#
# Tool invocations render as "● ToolName(args)" with an ASCII identifier —
# prose virtually never starts with one ("● 案A(楽観ロック)…" does not match
# because 案 is not [A-Za-z]). MCP tools render "● plugin:x:y - reply (MCP)(…)".
_TOOL_INVOCATION_RE = re.compile(r"^●\s+[A-Za-z][\w.:|-]*(?:\s+-\s+\S+)*\s*\(")
# Chrome painted between the response and a plan box.
_PRE_MENU_CHROME = ("Ready to code?",)
# Column-0 ● blocks that are TUI affordances, not assistant prose.
_CONTEXT_CHROME_BLOCKS = ("Updated plan", "User answered Claude's questions")
# Upper bound on the upward scan for the block start — a prose block before a
# menu is short; anything larger is a runaway and must not be carried.
_CONTEXT_SCAN_LIMIT = 120
# #633: a finished tool block folds, on every redraw, into ONE indented summary
# line — "Ran 2 shell commands", "Read 4 files", "Searched for 3 patterns, read
# 1 file, ran 6 shell commands".  It is indented exactly like a prose
# continuation, so the upward walk used to step straight over it and attach the
# ``●`` block from *before* the tool call to this menu.  That block is not the
# menu's 経緯: the CLI only buffers the jsonl chunk that carries the menu, so
# anything separated from the menu by a completed tool block has already been
# delivered by the transcript mirror.  Carrying it re-posted a two-day-old
# answer as "new" (thread 1508626302813601843: the same 1900-character message
# posted on 2026-08-26 and again on 2026-08-28).  Treat it as a block boundary.
#
# Shape, not vocabulary: a comma-joined list of "<verb> [for] <N> <noun…>"
# clauses and nothing else. Prose that happens to start with an English verb
# does not match, because the whole line has to be that list.
_FOLDED_CLAUSE = r"[A-Za-z][a-z]+(?:\s+for)?\s+\d+\s+[a-z][a-z ]*"
_FOLDED_TOOL_SUMMARY_RE = re.compile(rf"^{_FOLDED_CLAUSE}(?:,\s*{_FOLDED_CLAUSE})*$")

# Chrome that can be painted INSIDE the walked-up region during a mid-redraw
# ghost frame (#32 class). Any hit kills the whole extraction — fail closed.
_CONTEXT_INTERIOR_BAIL = (
    re.compile(r"\(esc to interrupt.*"),
    re.compile(r"Context left until auto-compact.*"),
    re.compile(r"[\u2800-\u28FF] .+"),  # braille spinner frames ("⠧ Worked for 5m 3s")
)


def _extract_pane_context(lines: list[str], boundary_idx: int) -> str:
    """Return the cleaned ``●`` prose block directly above ``boundary_idx``.

    ``boundary_idx`` is the menu's anchor line (the ``☐`` header for
    AskUserQuestion, the ``Here is Claude's plan:`` header for plans). Returns
    ``""`` whenever the block directly above is anything but clean assistant
    prose — missing, a tool block, the echoed user prompt, another menu, or
    chrome. Conservative by design (#53): no guess is ever bridged.
    """

    def _is_skippable(line: str) -> bool:
        s = line.strip()
        if not s:
            return True
        if _SEPARATOR_RE.match(s) or _PLAN_RULE_RE.match(s):
            return True
        if s in _PRE_MENU_CHROME:
            return True
        # Spinner / completion summaries ("✻ Baked for 37s").
        return bool(_GENERATION_STATUS_RE.match(s))

    # Skip the chrome padding between the menu frame and the content above it.
    i = boundary_idx - 1
    while i >= 0 and _is_skippable(lines[i]):
        i -= 1
    if i < 0:
        return ""

    # Walk up to the block start (a column-0 "● " line). Hitting anything that
    # marks a different kind of block first means there is no prose to carry.
    start = -1
    for j in range(i, max(-1, i - _CONTEXT_SCAN_LIMIT), -1):
        line = lines[j]
        s = line.strip()
        if line.startswith("● "):
            start = j
            break
        if s.startswith(("❯", "⎿", "●")) or "☐" in s or _ASK_SIGNATURE in s or _PLAN_SIGNATURE in s:
            return ""
        # A folded tool block ends this menu's own block: everything above it
        # has already reached Discord through the mirror (#633).
        if _FOLDED_TOOL_SUMMARY_RE.match(s):
            return ""
    if start < 0:
        return ""

    head = lines[start].strip()
    if _TOOL_INVOCATION_RE.match(head):
        return ""
    if head[2:].lstrip().startswith(_CONTEXT_CHROME_BLOCKS):
        return ""

    # Validate the block INTERIOR (review blocker 2): a mid-redraw ghost frame
    # can paint chrome between the ● head and the menu. Only space-indented
    # continuations and blanks are prose; any other column-0 line or known
    # status/hint line means the region is not a clean prose block → carry
    # nothing (fail closed), never "most of it".
    block = lines[start : i + 1]
    for line in block[1:]:
        s = line.strip()
        if not s:
            continue
        if any(p.fullmatch(s) for p in _CONTEXT_INTERIOR_BAIL):
            return ""
        if not line.startswith(" "):
            return ""
    return _clean_tui_lines(block)


# TUI status bar patterns at the very bottom.
# Use prefix-only ("-- INSERT") because the TUI sometimes omits the closing "--"
# e.g. "-- INSERT ⏵⏵ bypass permissions on (shift…"
_STATUS_BAR_MARKERS = ("-- INSERT", "-- NORMAL", "--", "⏵⏵", "⏸⏸")

# TUI generation status indicators (shown during Claude's response generation).
# These appear between two separator lines at the bottom of the pane.
# Claude uses various Unicode dingbats (✻ ✽ ✹ ✦ etc.) as thinking animations
# (e.g. "✻ Envisioning…") and completion summaries (e.g. "✻ Cooked for 56s").
# tmux capture-pane sometimes converts dingbats to plain ASCII (e.g. ✻ → *),
# so we match both the Dingbats Unicode block and common fallback characters.
_GENERATION_STATUS_RE = re.compile(r"^(?!❯)[\u2700-\u27BF*·] .+$")
# Additional explicit markers.
_GENERATION_STATUS_MARKERS = ("Tip:", "·")

# #365 (follow-up): the live working spinner shows a "(<elapsed> · …)" timer that
# only exists while Claude is actively generating/executing, e.g.
#   ✻ Running… (12s · ↑ 4.2k tokens · esc to interrupt)
# While a tool runs, its result preview + the input box + footer push that timer
# line 10-20 lines off the bottom, so a bottom-6-only scan misses it and the turn
# is finalized early (premature mention before the real answer). We scan the
# timer across a WIDE bottom window — the same heuristic the #190 lamp detector
# (`thread_state_sync._pane_lamp_state`) already uses. Matching the timer (not the
# bare glyph) avoids false-positives on stale completed spinners in scrollback.
_RUNNING_PROBE_LINES = 30
_RUNNING_SPINNER_RE = re.compile(r"\((?:\d+h\s*)?(?:\d+m\s*)?\d+s\s*·")

# Markers that indicate Claude has actually started producing output:
#   ● — assistant response paragraph
#   ⎿ — tool result
# Thinking/generation indicators (✻ ✶ ✽ ✦ ✹) are intentionally NOT included:
# anchoring on them caused TUI animations like "✶ Calling plugin:discord:discord…"
# to leak as response text and stall the turn (issue #39).
_RESPONSE_MARKERS: tuple[str, ...] = ("●", "⎿")

# Lines to strip from the response (TUI hints, not useful on Discord).
_STRIP_PATTERNS = (
    re.compile(r"● Recalled \d+ memor(?:y|ies).*"),
    re.compile(r"\(ctrl\+o to expand\)"),
    # Thinking animations: "✻ Forming…", "* Moseying…", "· Thinking…",
    # and multi-word variants like "✶ Calling plugin:discord:discord…" (#39).
    re.compile(r"[^\w\s●⎿❯>] [^\n]+…"),
    # Completion summaries: "✻ Cooked for 56s", "* Worked for 3s"
    re.compile(r"[^\w\s●⎿❯>] \w+ for \d+s?"),
    # TUI noise — interactive prompts and greetings
    re.compile(r"Press Ctrl-C again to exit"),
    re.compile(r"Claude Code has switched.*"),
    # TUI tool activity indicators (e.g. "Reading 1 file…", "Recalling 2 memories…",
    # "Searching for 1 pattern…"). The `(?:\s+\w+)*` allows optional words between
    # the verb and the digit (e.g. "Searching for N …", "Checking against N …").
    re.compile(r"(?:Reading|Recalling|Writing|Searching|Running|Checking)(?:\s+\w+)*\s+\d+\s+.+…"),
    # Vim-style status bar lines that leak into the response area
    re.compile(r"--\s*INSERT\s.*"),
    re.compile(r"--\s*NORMAL\s.*"),
    # ASCII hyphen separator lines (5+ hyphens)
    re.compile(r"^-{5,}$"),
    # Box-drawing separator lines (safety net if bottom-chrome stripping misses them)
    re.compile(r"^[─━═\s]{10,}$"),
    # Issue #32: defense-in-depth against TUI chrome leaking into Discord
    # when capture-pane catches a mid-redraw frame. Step 1 walks bottom-up
    # and bails on unrecognised lines, so a "ghost" copy of chrome above the
    # real chrome is not stripped. These patterns catch the chrome content
    # itself in _clean_tui_lines.
    re.compile(r"❯"),  # bare input prompt char
    re.compile(r"Model:\s.+\sStyle:.+"),  # ccstatusline row 1 ("Model: ... Style: ...")
    re.compile(r"Cost:\s\$.+\sSession:.+"),  # ccstatusline row 2 ("Cost: $... Session: ...")
    re.compile(r"⎇\s.+\scwd:.+"),  # ccstatusline row 3 ("⎇ branch ... cwd: ...")
    re.compile(r"\d+\s+skill descriptions?\s+dropped.*"),
    re.compile(r"Tip:\s.+"),  # TUI hint line ("Tip: Use Plan Mode ...")
    # Issue #50: effort/model footer indicator ("◐ medium · /effort", "○ low · /effort").
    # Geometric Shapes block (U+25A0–U+25FF) is not covered by _GENERATION_STATUS_RE
    # which only matches Dingbats (U+2700–U+27BF).
    re.compile(r"^[■-◿]\s+\w+\s+·\s+/\w+"),
)


# OSC 8 hyperlink: ESC ] 8 ; <params> ; <URL> ESC \ <TEXT> ESC ] 8 ; ; ESC \
# Captured by ``tmux capture-pane -e``. Issue #47: without unpacking these,
# the URL portion is lost and Discord users only see the visible text.
_OSC8_RE = re.compile(
    r"\x1b\]8;[^;]*;(?P<url>[^\x1b\x07]*)(?:\x1b\\|\x07)"
    r"(?P<text>.*?)"
    r"\x1b\]8;;(?:\x1b\\|\x07)",
    re.DOTALL,
)

# ANSI CSI sequences (colors, cursor control). After OSC 8 rewrite we strip
# everything else so the existing line-based chrome filters see plain text.
_ANSI_CSI_RE = re.compile(r"\x1b\[[\d;?]*[A-Za-z]")
_ANSI_OSC_REMNANT_RE = re.compile(r"\x1b\][^\x1b\x07]*(?:\x1b\\|\x07)?")


def _normalize_capture(text: str) -> str:
    """Rewrite OSC 8 hyperlinks to plain text and strip ANSI escapes.

    OSC 8 ``\\x1b]8;;URL\\x1b\\TEXT\\x1b]8;;\\x1b\\`` becomes:
      - ``TEXT (URL)`` for http(s) URLs
      - ``URL`` alone when visible text already equals the URL
      - ``TEXT`` for file:// (URL is local to bot, useless on Discord)
    """

    def _replace(match: re.Match[str]) -> str:
        url = match.group("url")
        visible = _ANSI_CSI_RE.sub("", match.group("text"))
        if url.startswith("file://"):
            return visible
        if visible == url:
            return url
        if not url:
            return visible
        return f"{visible} ({url})"

    text = _OSC8_RE.sub(_replace, text)
    text = _ANSI_CSI_RE.sub("", text)
    text = _ANSI_OSC_REMNANT_RE.sub("", text)
    return text


class TmuxClaudeRunner:
    """Runs Claude Code inside a tmux window and streams output via capture-pane.

    This is the sole execution backend for c-lord.  It provides ``run``,
    ``interrupt``, and ``kill`` methods.
    """

    def __init__(
        self,
        tmux_manager: TmuxSessionManager,
        thread_id: int,
        model: str = "sonnet",
        working_dir: str | None = None,
        timeout_seconds: int = 300,
        permission_mode: str = "acceptEdits",
        dangerously_skip_permissions: bool = False,
        try_continue: bool = False,
        effort: str | None = None,
    ) -> None:
        self._tmux = tmux_manager
        self._thread_id = thread_id
        self.model = model
        self.working_dir = working_dir
        self.timeout_seconds = timeout_seconds
        self._permission_mode = permission_mode
        self._dangerously_skip_permissions = dangerously_skip_permissions
        self._effort = effort
        # True only for the restart-resume path (on_ready → pending_resumes).
        # /clear and normal new threads must remain False to prevent --continue
        # from recovering cleared conversation history (issue #123 Part 2 fix).
        self._try_continue = try_continue
        self._stopped = False
        self._silent_stop = False
        self._last_capture: str = ""

    async def _duplicate_window_names(self) -> list[str]:
        """Ambiguous window names in this thread's session, or ``[]`` (#649).

        Undecidable reads as "no duplicates": this only ever *replaces* a
        broader message with a sharper one, so a failed probe must fall back to
        the general wording rather than assert a cause we did not verify.
        """
        try:
            return await asyncio.to_thread(self._tmux.duplicate_window_names)
        except Exception:
            logger.warning(
                "Could not check thread %d's session for duplicate window names",
                self._thread_id,
            )
            return []

    async def _start_failure_reason(self, prompt: str) -> str:
        """Explain a failed ``start_claude`` by asking tmux, not by guessing (#621).

        The call returns a bare ``False`` for failures that need opposite
        advice: a pane that will not take the keystrokes, a window that was
        never created (which is what every scheduled run hit — see #621), and a
        window name that identifies two windows (#649).  Same shape as the #560
        probe on the send path: read the actual state before telling the user
        what to do about it.
        """
        try:
            has_window = bool(await asyncio.to_thread(self._tmux.session_exists, self._thread_id))
        except Exception:
            # Undecidable — keep the broader #527 wording rather than assert a
            # cause we did not verify.
            logger.warning("Could not check whether thread %d has a tmux window", self._thread_id)
            has_window = True
        if has_window:
            # #649: a duplicate name means the keystrokes went somewhere — just
            # not here. Check before blaming a pane that is running fine.
            if dupes := await self._duplicate_window_names():
                logger.error(
                    "start_claude failed for thread %d while %d window name(s) are "
                    "duplicated in session %s (%s) — the target is ambiguous (#649)",
                    self._thread_id,
                    len(dupes),
                    self._tmux.session_name,
                    ", ".join(dupes),
                )
                return _ambiguous_window(
                    "Claude の起動", self._thread_id, self._tmux.session_name, dupes
                )
            return _delivery_failure("Claude の起動", prompt)
        logger.error(
            "start_claude failed with no tmux window for thread %d — "
            "the window was never created (#621)",
            self._thread_id,
        )
        return _missing_window("Claude の起動", self._thread_id)

    async def run(
        self,
        prompt: str,
        session_id: str | None = None,
    ) -> AsyncGenerator[StreamEvent, None]:
        """Start Claude in tmux and yield extracted response events.

        1. Check if Claude is already running; if not, start it.
        2. Handle trust/safety prompts automatically.
        3. Send the user's prompt.
        4. Poll ``capture-pane`` and extract Claude's response text.
        5. Yield response text as ASSISTANT events (cumulative).
        6. Detect completion (input prompt reappears or idle timeout).
        7. Yield a final RESULT event with ``is_complete=True``.
        """
        self._stopped = False

        # Emit a synthetic SYSTEM event so EventProcessor._on_system() saves
        # the session_id to the DB.  Without this, thread replies are ignored
        # because repo.get(thread_id) returns None.
        synthetic_session_id = f"tmux-{self._thread_id}"
        yield StreamEvent(
            raw={},
            message_type=MessageType.SYSTEM,
            session_id=synthetic_session_id,
        )

        # Start Claude or send a new prompt to an already-running instance.
        claude_running = await asyncio.to_thread(self._tmux.is_claude_running, self._thread_id)

        if claude_running:
            # If Claude is blocked on an open AskUserQuestion/plan menu, a bare
            # send_input would type the message + Enter straight into the menu —
            # the trailing Enter selects the highlighted *first* option and the
            # typed text is discarded (#358).  The user sending a normal message
            # instead of clicking a button means "I'm not answering that menu, do
            # this instead", so dismiss the menu (Esc) first and let the TUI
            # settle back to the input prompt before delivering the message.
            if await self.peek_pending_ask() is not None:
                logger.info(
                    "Open menu detected before delivering new input; dismissing it "
                    "(Esc) so the message is not consumed as a menu selection (thread=%d)",
                    self._thread_id,
                )
                await self.cancel_menu()
                await asyncio.sleep(_MENU_NAV_DELAY)
            ok = await asyncio.to_thread(self._tmux.send_input, self._thread_id, prompt)
            if not ok:
                # #560: two very different failures reach this branch. Either the
                # pane never took the input (#527), or the text is typed in and
                # simply will not submit. Telling the second case to
                # ``/claude-restart`` would discard the message the user just
                # wrote, so ask the pane which one it is before advising.
                stuck = await asyncio.to_thread(self._tmux.input_box_holds, self._thread_id, prompt)
                if stuck:
                    reason = _stuck_in_input_box(prompt)
                elif dupes := await self._duplicate_window_names():
                    # #649: not stuck and not dead — the keystrokes were typed
                    # into a window this thread does not own.
                    logger.error(
                        "send_input failed for thread %d while %d window name(s) are "
                        "duplicated in session %s (%s) — the target is ambiguous (#649)",
                        self._thread_id,
                        len(dupes),
                        self._tmux.session_name,
                        ", ".join(dupes),
                    )
                    reason = _ambiguous_window(
                        "メッセージの送信", self._thread_id, self._tmux.session_name, dupes
                    )
                else:
                    reason = _delivery_failure("メッセージの送信", prompt)
                yield StreamEvent(
                    raw={},
                    message_type=MessageType.RESULT,
                    is_complete=True,
                    error=reason,
                )
                return
        else:
            if self._try_continue:
                # Restart-resume path only (on_ready → pending_resumes).
                # Try --continue first; if Claude exits immediately (no session),
                # fall back to a fresh start (issue #123 Part 2).
                ok = await asyncio.to_thread(
                    self._tmux.start_claude,
                    self._thread_id,
                    prompt,
                    self.model,
                    permission_mode=self._permission_mode,
                    dangerously_skip_permissions=self._dangerously_skip_permissions,
                    try_continue=True,
                    effort=self._effort,
                )
                if not ok:
                    yield StreamEvent(
                        raw={},
                        message_type=MessageType.RESULT,
                        is_complete=True,
                        error=await self._start_failure_reason(prompt),
                    )
                    return

                if not await self._continue_came_up():
                    logger.info(
                        "start_claude --continue found no session for thread %d; "
                        "falling back to fresh start",
                        self._thread_id,
                    )
                    ok = await asyncio.to_thread(
                        self._tmux.start_claude,
                        self._thread_id,
                        prompt,
                        self.model,
                        permission_mode=self._permission_mode,
                        dangerously_skip_permissions=self._dangerously_skip_permissions,
                        try_continue=False,
                        effort=self._effort,
                    )
                    if not ok:
                        yield StreamEvent(
                            raw={},
                            message_type=MessageType.RESULT,
                            is_complete=True,
                            error=await self._start_failure_reason(prompt),
                        )
                        return
            else:
                # Normal cold start: /clear, new thread, any non-resume path.
                # Always fresh — never --continue (would recover cleared history).
                ok = await asyncio.to_thread(
                    self._tmux.start_claude,
                    self._thread_id,
                    prompt,
                    self.model,
                    permission_mode=self._permission_mode,
                    dangerously_skip_permissions=self._dangerously_skip_permissions,
                    try_continue=False,
                    effort=self._effort,
                )
                if not ok:
                    yield StreamEvent(
                        raw={},
                        message_type=MessageType.RESULT,
                        is_complete=True,
                        error=await self._start_failure_reason(prompt),
                    )
                    return

            # Handle trust prompt if it appears.
            await self._handle_startup_prompts()

        # Wait a moment then snapshot.
        await asyncio.sleep(_POST_STARTUP_DELAY)

        # Poll capture-pane and extract response text.
        # Completion is detected by the response text stabilising (not changing
        # for _RESPONSE_STABLE_TIMEOUT seconds).  This is more reliable than
        # looking for bare "❯" prompts, because the Claude TUI always shows
        # the input area ❯ at the bottom, even during generation.
        elapsed = 0.0
        stable_seconds = 0.0
        last_response = ""
        # Previous capture's extracted response — used to debounce non-prefix
        # changes so that transient TUI redraw artifacts (e.g. mid-frame cursor
        # rewrites that produce text like "claude_chat.pypy") are not yielded.
        prev_capture_response = ""
        # Seconds the unknown-interactive pattern has been continuously visible.
        # We require it to be stable for this long before alerting Discord,
        # to avoid false positives from transient TUI redraws.
        unknown_interactive_stable = 0.0
        # Signature of the last menu we already alerted on.  While the same menu
        # lingers we must NOT re-alert every poll (#165); we re-alert only when
        # the menu changes or after it clears (reset to None below).
        last_alerted_unknown: str | None = None
        # AskUserQuestion (#166): seconds the menu has been stable, and the
        # signature of the menu we already bridged to Discord (dedup).
        ask_stable = 0.0
        last_bridged_ask: str | None = None
        # AskUserQuestion Submit/Review screen: seconds it has been stable, and
        # the signature of the screen we already submitted (dedup so a lingering
        # screen isn't Enter-ed every poll).
        submit_stable = 0.0
        last_submitted: str | None = None
        # Raw pane activity (#166): while Claude works the pane changes every
        # poll (spinner frame, elapsed-seconds tick), even when no response text
        # is extracted yet.  We only treat the session as idle once the raw pane
        # has been frozen for the idle window — otherwise a long thinking phase
        # before an AskUserQuestion menu trips the idle timeout and we stop
        # polling before the menu appears.
        last_raw = ""
        raw_static_seconds = 0.0
        # Last normalised pane capture — initialised so the final-event error
        # logic can scan it even if the poll loop body never ran (#366).
        current = ""
        # Fatal Claude-CLI startup error scraped from the pane (#366).  Set when
        # the ``claude`` process dies immediately (e.g. native binary missing);
        # surfaced as the RESULT error so Discord shows the cause instead of a
        # silent "done".
        startup_error: str | None = None
        # Plan limit scraped from the pane (#631).  Set when Claude refuses the
        # turn because the account is rate limited; surfaced as the RESULT
        # outcome so Discord reports the reset time instead of telling the user
        # to send the same message again (which can only fail again).
        usage_limit: UsageLimit | None = None
        # How many limit banners were ALREADY on the pane when this turn started
        # (#631).  Claude Code re-renders the conversation tail on ``--resume``,
        # so a thread that hit the limit yesterday opens today's turn with
        # yesterday's banner on screen.  Treating that as today's outcome would
        # refuse a turn that is about to work — the opposite mistake, and the
        # worse one, because it leaves the session with no way forward.
        #
        # Counting rather than comparing the text is deliberate: a repeat of the
        # same limit prints the IDENTICAL line, so a text comparison misses every
        # attempt after the first — which is precisely what the reporter did,
        # three times.  An open choice menu says the same thing more directly and
        # is checked alongside it.
        baseline_limit_count = 0
        baseline_limit_captured = False

        # #365: Gate completion on the NEW turn actually having started. When a
        # follow-up message is delivered to an already-running Claude
        # (``claude_running`` above), the pane still shows the PREVIOUS turn's
        # (stable, non-empty) response with the input prompt visible until Claude
        # picks up the prompt and starts generating — a gap of several seconds on
        # a --resume / large-context turn. Without this gate the completion
        # detector below reads that residual response as "done" and finalizes the
        # turn early, firing the "Claude has finished — your reply is needed"
        # owner mention BEFORE the real answer is produced (it arrives later via
        # the transcript / discord-reply path). We only accept completion once
        # either: a generation spinner was observed at least once this turn, OR
        # the extracted response changed from the baseline captured at the start
        # of polling. The baseline is empty on a fresh start, so the first real
        # response trivially differs and cold starts are unaffected.
        saw_generation = False
        baseline_response: str | None = None
        # #562: the #365 gate, hoisted out of the loop body. The idle exit and
        # the final verdict both need it, and it must be defined even if the
        # loop body never runs.
        new_turn_started = False

        # The hard ``timeout_seconds`` backstop is INACTIVITY-based, not total
        # wall-clock (#94).  A heavy turn — Explore subagent + extended thinking
        # — easily runs past 300s while the pane keeps changing every poll
        # (spinner frame, elapsed-seconds tick).  Gating on ``raw_static_seconds``
        # means an actively-working turn is never killed mid-flight; the timeout
        # fires only once the pane has been frozen (Claude truly hung) for the
        # whole window.  ``elapsed`` is kept for logging and the startup grace.
        while not self._stopped and raw_static_seconds < self.timeout_seconds:
            await asyncio.sleep(_POLL_INTERVAL)
            elapsed += _POLL_INTERVAL

            raw_current = await asyncio.to_thread(self._tmux.capture_pane, self._thread_id)

            # Raw-pane activity is tracked on the un-normalised capture so that
            # spinner-frame / elapsed-counter changes count as "still working".
            if raw_current != last_raw:
                raw_static_seconds = 0.0
                last_raw = raw_current
            else:
                raw_static_seconds += _POLL_INTERVAL

            # All prompt detection runs on the NORMALISED text.  The Claude TUI
            # renders menus with per-token ANSI colour codes that split "❯" from
            # "1." — leaving the escapes in place makes the menu regexes (and the
            # AskUserQuestion parser) silently miss real menus (#166).
            current = _normalize_capture(raw_current)

            # Fast-fail on a fatal Claude-CLI startup error (#366).  When the
            # ``claude`` process dies immediately (e.g. native binary missing,
            # command not found) the pane shows the CLI's error instead of a
            # session and will never produce a response — don't wait out the idle
            # timeout, break now so the final event surfaces the cause.  Gated on
            # ``not last_response`` so it can only fire before any answer text
            # exists, never mid-conversation (a marker phrase inside Claude's own
            # answer must not abort a live turn).
            if not last_response:
                startup_error = _extract_startup_error(current)
                if startup_error is not None:
                    logger.warning(
                        "Claude startup error detected, aborting poll (thread=%d): %s",
                        self._thread_id,
                        startup_error,
                    )
                    break

                # #631: the account is rate limited.  Claude printed the banner
                # instead of an answer and is now idle, so there is nothing left
                # to wait for — waiting only delays the notice by the whole
                # _TURN_START_GRACE window.  Gated on ``not last_response`` for
                # the same reason as the startup error above: the phrase can
                # legitimately appear inside Claude's own answer.
                found_limit = _extract_usage_limit(current)
                limit_count = _count_usage_limit(current)
                if not baseline_limit_captured:
                    baseline_limit_captured = True
                    baseline_limit_count = limit_count
                if found_limit is not None and (
                    _usage_limit_menu_open(current) or limit_count > baseline_limit_count
                ):
                    usage_limit = found_limit
                    logger.warning(
                        "Usage limit detected, aborting poll (thread=%d): %s",
                        self._thread_id,
                        usage_limit.line,
                    )
                    await self._dismiss_usage_limit_menu(current)
                    break

            # Auto-accept the folder-trust dialog ("Quick safety check…").  Every
            # thread runs in a freshly-cloned session dir with no trusted ancestor,
            # so this dialog blocks on first launch — and --dangerously-skip-permissions
            # does NOT bypass it.  Unlike permission prompts it is a TOP-anchored
            # full-screen prompt (bottom rows blank), so the zone-based checks below
            # never see it; it must be matched against the full pane here.  The
            # cold-start handler (_handle_startup_prompts) races the dialog and often
            # bails before it renders, so the main loop is the reliable backstop.
            # c-lord already runs these dirs with --dangerously-skip-permissions, so
            # trusting the dir it just cloned is consistent with that threat model.
            if self._has_trust_prompt(current):
                await self._accept_trust_prompt(current)
                continue

            # Auto-accept permission prompts so the bot doesn't stall.
            if self._has_permission_prompt(current):
                unknown_interactive_stable = 0.0
                last_alerted_unknown = None
                ask_stable = 0.0
                last_bridged_ask = None
                submit_stable = 0.0
                last_submitted = None
                await self._accept_permission_prompt(current)
                continue

            # AskUserQuestion menu → bridge to Discord buttons (#166).
            # Yielding pane_ask suspends this generator while the EventProcessor
            # shows the buttons and waits for a click; it then sends the menu
            # keystrokes back via answer_menu(), after which polling resumes and
            # the (now-closed) menu no longer matches.
            # Plan approval (ExitPlanMode) menus are bridged through the SAME
            # pane_ask path as AskUserQuestion (#251): both are numbered TUI
            # menus answered by sending Down×index + Enter back to the pane, so
            # the only difference is detection.  ``_parse_plan_from_pane``
            # returns an AskQuestion with ``allow_other=False`` (no free-text
            # row), which AskView renders without the ✏️ Other button.
            ask_q = _parse_ask_from_pane(current) or _parse_plan_from_pane(current)
            if ask_q is not None:
                ask_stable += _POLL_INTERVAL
                ask_sig = "\n".join(o.label for o in ask_q.options)
                if ask_stable >= _ASK_ALERT_DELAY and ask_sig != last_bridged_ask:
                    last_bridged_ask = ask_sig
                    # #468: long pre-menu prose (経緯・推し) scrolls off the
                    # alternate screen and is lost to the normal capture
                    # (context_chars=0), so the question would reach Discord with
                    # no decision context. Claude redraws its whole conversation
                    # on SIGWINCH, so re-capture at a taller height to recover
                    # the prose, then the window is restored. Only when context
                    # is empty (the failing case) — avoids a resize round-trip on
                    # every menu.
                    if not ask_q.context and hasattr(self._tmux, "capture_pane_tall"):
                        tall = await asyncio.to_thread(
                            self._tmux.capture_pane_tall, self._thread_id
                        )
                        if isinstance(tall, str) and tall:
                            norm_tall = _normalize_capture(tall)
                            recovered = _parse_ask_from_pane(norm_tall) or _parse_plan_from_pane(
                                norm_tall
                            )
                            if recovered is not None and recovered.context:
                                ask_q = recovered
                                logger.info(
                                    "Recovered pre-menu context via tall capture "
                                    "(thread=%d, context_chars=%d)",
                                    self._thread_id,
                                    len(recovered.context),
                                )
                    logger.info(
                        "Interactive menu detected, bridging to Discord "
                        "(thread=%d, context_chars=%d)",
                        self._thread_id,
                        len(ask_q.context),
                    )
                    yield StreamEvent(
                        raw={},
                        message_type=MessageType.SYSTEM,
                        pane_ask=ask_q,
                    )
                continue
            else:
                ask_stable = 0.0
                last_bridged_ask = None

            # AskUserQuestion multi-question Submit/Review screen (post-bridge).
            # Once every question was answered via the Discord buttons, the TUI
            # shows a "Review your answers" confirmation with the cursor on
            # "Submit answers".  Auto-confirm with Enter so the answered flow
            # completes instead of stalling as an "unknown" prompt.  A short
            # dwell guards against acting on a half-drawn frame, and the
            # signature dedup prevents re-pressing Enter while it lingers.
            if _is_ask_submit_screen(current):
                submit_stable += _POLL_INTERVAL
                if submit_stable >= _ASK_ALERT_DELAY:
                    sig = _unknown_prompt_signature(current)
                    if sig != last_submitted:
                        last_submitted = sig
                        await self._submit_ask_screen()
                    submit_stable = 0.0
                continue
            else:
                submit_stable = 0.0
                last_submitted = None

            # Detect unknown TUI interactive menus (not covered by known markers).
            # Alert Discord so the session doesn't stall silently.
            if self._has_unknown_interactive(current):
                unknown_interactive_stable += _POLL_INTERVAL
                if unknown_interactive_stable >= _UNKNOWN_ALERT_DELAY:
                    signature = _unknown_prompt_signature(current)
                    if signature != last_alerted_unknown:
                        logger.warning(
                            "Unknown TUI interactive prompt detected (thread=%d)",
                            self._thread_id,
                        )
                        yield StreamEvent(
                            raw={},
                            message_type=MessageType.SYSTEM,
                            unknown_tui_prompt=current[-800:],
                        )
                        last_alerted_unknown = signature
                    # Reset the stability timer either way so we re-evaluate the
                    # signature on the next dwell window rather than every poll.
                    unknown_interactive_stable = 0.0
                continue
            else:
                unknown_interactive_stable = 0.0
                last_alerted_unknown = None

            # Extract the clean response from the TUI pane.
            response = self._extract_response(current)
            has_prompt = self._has_input_prompt(current)

            # #365: snapshot the residual (previous-turn) response on the first
            # poll so we can tell when the NEW turn produces output of its own.
            if baseline_response is None:
                baseline_response = response

            if elapsed % 10 < _POLL_INTERVAL:  # Log every ~10 seconds
                logger.debug(
                    "poll: elapsed=%.0fs stable=%.1fs resp_len=%d has_prompt=%s (thread=%d)",
                    elapsed,
                    stable_seconds,
                    len(response),
                    has_prompt,
                    self._thread_id,
                )

            # Issue #53: text events are no longer yielded — Claude posts its
            # own final answer via the discord-reply skill. We still track
            # response stability here purely as a completion signal.
            if response == last_response:
                stable_seconds += _POLL_INTERVAL
            elif response and response == prev_capture_response:
                stable_seconds = 0.0
                last_response = response
            else:
                stable_seconds = 0.0

            prev_capture_response = response

            # Done: non-empty response has been stable long enough AND Claude is
            # not actively generating.  Two tiers, both gated on ``not is_gen``:
            #  - Quick exit (3s): response stable AND input prompt visible.
            #    Without the is_gen check, tool execution pauses (where the
            #    pane is stable for several seconds) would trigger false
            #    completion, posting raw tool-call text instead of Claude's
            #    final formatted response.
            #  - Fallback exit (30s): response stable but no input prompt
            #    (Claude may have finished but prompt detection failed,
            #    e.g. completion summary text in the prompt area).
            # The ``not is_gen`` guard on BOTH tiers is what prevents premature
            # completion during a long thinking phase: an intermediate response
            # can sit stable for >30s while Claude keeps working toward (say) an
            # AskUserQuestion menu.  Finalizing then stops the poll loop, so the
            # menu that renders later is never bridged and the session stalls
            # (#179).  While the generation indicator is visible the turn stays
            # open; the inactivity ``timeout_seconds`` backstop still applies.
            is_gen = self._is_generating(current)
            if is_gen:
                saw_generation = True

            # #365: only finalize once the new turn demonstrably started —
            # either we saw it generate, or its output differs from the residual
            # previous-turn response captured as the baseline. Otherwise a
            # follow-up delivered to a still-loading Claude would finalize off
            # the old answer and mention the owner prematurely.
            new_turn_started = saw_generation or (
                bool(last_response) and last_response != baseline_response
            )

            if (
                last_response
                and stable_seconds >= _RESPONSE_STABLE_TIMEOUT
                and not is_gen
                and new_turn_started
                and (has_prompt or stable_seconds >= _RESPONSE_STABLE_FALLBACK)
            ):
                break

            # Idle timeout: no response and the pane has been completely frozen
            # for the idle window.  Requiring raw-pane staleness (not just empty
            # response text) means a long "thinking" phase before an
            # AskUserQuestion menu — where the spinner/elapsed counter keeps the
            # pane changing — does not trip the timeout and stop polling before
            # the menu appears (#166).  ``timeout_seconds`` (300s) is the backstop.
            if (
                not last_response
                and stable_seconds >= _IDLE_TIMEOUT
                and raw_static_seconds >= _IDLE_TIMEOUT
            ):
                # #562: a turn that never started is not a turn that finished.
                # #365 gates the *completion* detector on the new turn actually
                # having begun; this exit ignored that gate, so a warm session
                # that simply had not started drawing yet was declared done —
                # and the verdict below then read it as a normal completion,
                # firing "🟡 Claude has finished" at a user with no answer.
                # The old grace was `not claude_running and elapsed <
                # _STARTUP_TIMEOUT`, i.e. cold starts only; a warm session got
                # none. Wait out `_TURN_START_GRACE` either way.
                if not new_turn_started and elapsed < _TURN_START_GRACE:
                    continue
                logger.info(
                    "Idle timeout (%.1fs) — no response (thread=%d)",
                    stable_seconds,
                    self._thread_id,
                )
                break

        # Yield final complete event.  ``error`` is computed first so the single
        # yield below carries the right outcome (#366: a failed/empty run must
        # surface an error, not a silent "done").
        timed_out = raw_static_seconds >= self.timeout_seconds
        if self._stopped:
            error = None if self._silent_stop else "Stopped by user"
        elif timed_out or not last_response:
            # Reached completion without a usable response — either the hard
            # inactivity backstop fired (``timed_out``) or we never extracted
            # any response text.  Neither is automatically a failure, so run the
            # #366 liveness ladder before blaming the session:
            #   1. A fatal startup error is on the pane → report it verbatim.
            #   2. No marker, but ``claude`` is no longer running → it exited
            #      without answering (crash / unrecognised fatal error).
            #   3. ``claude`` is alive but NOT idle at its prompt → it really is
            #      wedged mid-turn; a frozen pane for the whole timeout window is
            #      a genuine hang, so report the timeout.
            #   4. ``claude`` is alive and idle at its prompt → the turn is over
            #      and the answer went out through the jsonl mirror / reply skill
            #      (#541).  Stay silent rather than posting a false error embed.
            #
            # #541: step 3/4 is what the backstop was missing.  In jsonl mode the
            # pane freezes completely once a turn ends, and the idle pane yields
            # no scrapable response, so EVERY normal turn hit the backstop ~312s
            # after answering and posted "⏱️ Session timed out" (33 of 35 observed
            # timeouts were this false alarm).  The ladder has to run first —
            # which also means an ordinary timeout no longer masks a crashed
            # ``claude`` (step 2 now reports the crash instead).
            # The pane scrape stays gated on "no response was ever produced":
            # a marker phrase like ``command not found: claude`` can legitimately
            # appear inside Claude's own answer, and a timed-out turn that DID
            # produce text must not be re-labelled a startup failure (#366).
            pane_error = startup_error
            if pane_error is None and not last_response:
                pane_error = _extract_startup_error(current)
            if usage_limit is None and not last_response:
                # Same baseline rule as the poll loop: a banner that was already
                # there when the turn started belongs to an earlier turn.
                found_limit = _extract_usage_limit(current)
                if found_limit is not None and (
                    _usage_limit_menu_open(current)
                    or _count_usage_limit(current) > baseline_limit_count
                ):
                    usage_limit = found_limit
            if pane_error is not None:
                error = f"Claude failed to start: {pane_error}"
            elif usage_limit is not None:
                # #631 sits ABOVE the "never started" rung deliberately. Both
                # describe an empty turn, but only this one knows why — and the
                # other one's advice ("send the message again") is advice that
                # cannot work until the limit resets, which is what made the
                # reporter send the same request three times.
                resets = (
                    f" Resets {usage_limit.resets_at}."
                    if usage_limit.resets_at
                    else " No reset time was reported."
                )
                error = (
                    f"{USAGE_LIMIT_ERROR_PREFIX} Claude hit your "
                    f"{usage_limit.scope} and did not run this turn.{resets}"
                )
            elif not await asyncio.to_thread(self._tmux.is_claude_running, self._thread_id):
                error = (
                    "Claude exited without producing a response "
                    "(possible startup failure or crash) — check the tmux pane."
                )
            elif not new_turn_started:
                # #562: claude is alive but this turn never produced anything —
                # no generation was ever seen and the pane still shows only the
                # previous turn's residue. Reporting "done" here is what made
                # c-lord ping people about work that had not started. Note this
                # is checked AFTER the #541 rungs: a turn that DID generate has
                # ``new_turn_started`` set, so an answered-then-silent pane
                # (the #541 case) never lands here.
                #
                # ``new_turn_started`` is the whole test, deliberately: an
                # earlier revision also required ``not last_response`` (matching
                # the journal line in the report, ``Idle timeout … no
                # response``). Reproducing the bug on staging showed that scoping
                # left the other half in place — when the frozen pane still had
                # the PREVIOUS turn's output on it the scrape succeeded, the run
                # fell through to the inactivity backstop, and c-lord announced
                # "finished" for a turn Claude never received. A turn that really
                # produced something always moves the extracted text away from
                # the baseline captured right after the prompt was sent, so the
                # gate alone is both sufficient and safe.
                error = (
                    f"{NO_RESPONSE_ERROR_PREFIX} Claude never started this turn "
                    f"(pane unchanged for {raw_static_seconds:.0f}s). "
                    "Send the message again, or check the tmux pane."
                )
            elif timed_out and not self._is_idle_at_prompt(current):
                error = f"Timed out after {self.timeout_seconds} seconds"
            else:
                error = None
        else:
            # Normal completion — emit RESULT only. The text is intentionally
            # dropped (#53): Claude posts its own final answer via the
            # discord-reply skill, not through the runner stream.
            error = None

        yield StreamEvent(
            raw={},
            message_type=MessageType.RESULT,
            is_complete=True,
            error=error,
            usage_limit=usage_limit,
        )

    async def wake(self, *, timeout: float = _WAKE_TIMEOUT) -> bool:
        """Bring a stopped workspace back up **without running a turn** (#642).

        Same startup as :meth:`run` — ``--continue`` first so the conversation
        comes back, a fresh start when there is nothing to continue (#123 Part 2)
        — but with no prompt, so Claude opens its TUI and waits. That matters
        twice: the pane can be photographed (``/tmux-screenshot``), and the
        process left behind is the one the user's *next* message talks to, so
        nothing spawns twice.

        Readiness is "the input box is on screen **and** the process is alive".
        The pane text alone cannot decide it: a zsh theme renders the very same
        ``❯`` glyph, so a claude that exited on startup would otherwise read as
        a ready prompt.

        Returns True only when the pane really came back.
        """
        if await asyncio.to_thread(self._tmux.is_claude_running, self._thread_id):
            return True  # already awake — restarting would kill a live turn

        started = await asyncio.to_thread(
            self._tmux.start_claude,
            self._thread_id,
            None,
            self.model,
            permission_mode=self._permission_mode,
            dangerously_skip_permissions=self._dangerously_skip_permissions,
            try_continue=True,
            effort=self._effort,
        )
        if not started:
            logger.warning("wake: start_claude --continue failed (thread=%d)", self._thread_id)
            return False

        if not await self._continue_came_up():
            logger.info(
                "wake: --continue found no conversation for thread %d; starting fresh",
                self._thread_id,
            )
            started = await asyncio.to_thread(
                self._tmux.start_claude,
                self._thread_id,
                None,
                self.model,
                permission_mode=self._permission_mode,
                dangerously_skip_permissions=self._dangerously_skip_permissions,
                try_continue=False,
                effort=self._effort,
            )
            if not started:
                logger.warning("wake: fresh start_claude failed (thread=%d)", self._thread_id)
                return False

        # One loop for both jobs: a recreated window can land on the folder-trust
        # dialog, and an unanswered dialog never reaches an input box — so the
        # wait for readiness has to be able to answer it.
        elapsed = 0.0
        trust_handled = False
        while elapsed < timeout:
            await asyncio.sleep(_POLL_INTERVAL)
            elapsed += _POLL_INTERVAL
            # ``capture_pane`` keeps the escape sequences (``-e``), so the input
            # box arrives as ``\x1b[39m❯\xa0`` and every text check below would
            # miss it. The turn loop normalises before it looks; staging proved
            # what happens when this one does not — a restored, idle pane read as
            # "never came up" and the wake reported a failure (#642).
            pane = _normalize_capture(
                await asyncio.to_thread(self._tmux.capture_pane, self._thread_id)
            )
            if not trust_handled and self._has_trust_prompt(pane):
                await self._accept_trust_prompt(pane)
                trust_handled = True
                continue
            if not self._is_idle_at_prompt(pane):
                continue
            if await asyncio.to_thread(self._tmux.is_claude_running, self._thread_id):
                logger.info(
                    "wake: workspace restored in %.1fs (thread=%d)", elapsed, self._thread_id
                )
                return True

        logger.warning(
            "wake: pane never reached an idle prompt in %.0fs (thread=%d)", timeout, self._thread_id
        )
        return False

    @property
    def effort(self) -> str | None:
        """Reasoning effort passed to the CLI (``--effort``), or ``None`` for default."""
        return self._effort

    @property
    def stopped(self) -> bool:
        """True once :meth:`interrupt` or :meth:`kill` has been called.

        Lets callers (e.g. ``_run_helper``'s post-turn menu recovery) tell a turn
        that was deliberately pre-empted from one that ended on its own, so a
        pre-empted turn is not re-bridged into a fresh menu it would re-park on
        (#315).
        """
        return self._stopped

    async def interrupt(self, *, silent: bool = False) -> None:
        """Send C-c to the tmux pane (graceful interrupt).

        Args:
            silent: When True, the RESULT event will have ``error=None``
                instead of ``"Stopped by user"``.  Used when a new message
                automatically interrupts the previous run — users should
                not see a scary error embed they didn't cause.
        """
        self._stopped = True
        self._silent_stop = silent
        await asyncio.to_thread(self._tmux.send_interrupt, self._thread_id)

    async def kill(self) -> None:
        """Kill the tmux window entirely."""
        self._stopped = True
        await asyncio.to_thread(self._tmux.kill_session, self._thread_id)

    async def probe_context_window(self) -> int | None:
        """Learn the real context-window total by scraping ``/context``.

        ``/context`` renders locally and consumes no model turn (verified: it
        writes no ``usage``-bearing assistant entry to the transcript), so it is
        safe to fire between turns.  It must only be called when Claude is idle
        at the prompt — the caller is responsible for that ordering.

        Returns the window total in absolute tokens (e.g. ``1_000_000`` for a
        1M tier), or ``None`` if Claude is not running or the pane could not be
        parsed (the caller then falls back to a per-model default).
        """
        if not await asyncio.to_thread(self._tmux.is_claude_running, self._thread_id):
            return None

        # ``send_literal`` (not ``send_input``) so the jsonl-bridge ZWSP marker
        # is not prepended — a leading invisible char would stop Claude from
        # recognising ``/context`` as a slash command.
        if not await asyncio.to_thread(self._tmux.send_literal, self._thread_id, "/context"):
            return None
        await asyncio.to_thread(self._tmux.send_keys, self._thread_id, "Enter")

        for _ in range(_CONTEXT_PROBE_ATTEMPTS):
            await asyncio.sleep(_CONTEXT_PROBE_INTERVAL)
            pane = await asyncio.to_thread(self._tmux.capture_pane, self._thread_id)
            total = parse_context_total(_normalize_capture(pane))
            if total is not None:
                logger.info("probe_context_window: total=%d (thread=%d)", total, self._thread_id)
                return total

        logger.info("probe_context_window: could not parse /context (thread=%d)", self._thread_id)
        return None

    async def get_cost_from_pane(self) -> float | None:
        """Extract the per-turn cost from the ccstatusline ``Cost: $X.XXXX`` row.

        Captures the current pane without sending any command, so it is safe to
        call between turns.  Returns ``None`` when Claude is not running or the
        cost row is absent.
        """
        if not await asyncio.to_thread(self._tmux.is_claude_running, self._thread_id):
            return None
        pane = await asyncio.to_thread(self._tmux.capture_pane, self._thread_id)
        return parse_cost_from_pane(_normalize_capture(pane))

    # ------------------------------------------------------------------
    # Private helpers
    # ------------------------------------------------------------------

    async def _continue_came_up(self) -> bool:
        """Decide whether ``claude --continue`` actually came up (#657).

        ``--continue`` gives us no exit status to read — the pane is all there
        is — so "did it work?" is answered by watching the process.  The obvious
        way to do that (wait a fixed few seconds, then ask whether claude is
        running) is wrong for the case that hurts most: a session dir where
        claude has never run opens the **folder-trust dialog** first, and a
        claude parked on that dialog is alive no matter what ``--continue`` is
        going to do.  In production (2026-09-01 13:49) the dialog was still up
        at the 3s mark, the check read "started fine", the fresh-start fallback
        never fired, and the exit six seconds later — ``No conversation found to
        continue`` — took the user's first message with it.

        So the verdict is *deferred* while the dialog is up (and the dialog is
        answered here, which is what lets the real outcome happen at all), then
        settled on something that separates the two outcomes:

        * claude is gone                    → nothing to continue → False
        * claude is up at its input box     → the conversation loaded → True
        * claude simply outlives the window in which a failed ``--continue``
          exits → it started

        Returns True when the caller should keep this process, False when it
        must fall back to a fresh start.
        """
        # Startup grace: the pane runs zsh for a moment while claude execs.
        await asyncio.sleep(_CONTINUE_CHECK_DELAY)

        elapsed = 0.0
        settled = 0.0
        trust_handled = False
        while elapsed < _CONTINUE_VERDICT_TIMEOUT and not self._stopped:
            # ``capture_pane`` keeps the escape sequences (``-e``), so the input
            # box arrives as ``\x1b[39m❯\xa0`` and would never be recognised
            # unnormalised — the same trap #642 hit in ``wake``.
            pane = _normalize_capture(
                await asyncio.to_thread(self._tmux.capture_pane, self._thread_id)
            )
            if self._has_trust_prompt(pane):
                # Not a verdict: answer it, and hold the settle clock at zero —
                # nothing about ``--continue`` is decided while this is up.
                if not trust_handled:
                    await self._accept_trust_prompt(pane)
                    trust_handled = True
                settled = 0.0
            elif not await asyncio.to_thread(self._tmux.is_claude_running, self._thread_id):
                return False
            elif self._has_input_prompt(pane) or self._is_generating(pane):
                # Liveness is checked first on purpose: a zsh theme renders the
                # very same ``❯``, so a claude that just exited must not read as
                # a ready input box.
                return True
            elif settled >= _CONTINUE_SETTLE:
                # Still alive, dialog gone, but the TUI has painted nothing we
                # recognise. It outlived the window in which a failed
                # ``--continue`` exits, so it started.
                return True
            else:
                settled += _POLL_INTERVAL
            await asyncio.sleep(_POLL_INTERVAL)
            elapsed += _POLL_INTERVAL

        if not self._stopped:
            logger.warning(
                "--continue neither reached its input box nor exited within %.0fs "
                "(thread=%d); treating it as started",
                _CONTINUE_VERDICT_TIMEOUT,
                self._thread_id,
            )
        return True

    async def _handle_startup_prompts(self) -> None:
        """Handle any interactive prompts during Claude startup."""
        elapsed = 0.0
        trust_handled = False

        while elapsed < _STARTUP_TIMEOUT and not self._stopped:
            await asyncio.sleep(_POLL_INTERVAL)
            elapsed += _POLL_INTERVAL

            pane = await asyncio.to_thread(self._tmux.capture_pane, self._thread_id)

            if not trust_handled and self._has_trust_prompt(pane):
                await self._accept_trust_prompt(pane)
                trust_handled = True
                await asyncio.sleep(1.0)
                elapsed += 1.0
                continue

            if trust_handled or elapsed > 3.0:
                return

        logger.info(
            "Startup prompts handled in %.1fs (thread=%d)",
            elapsed,
            self._thread_id,
        )

    @staticmethod
    def _has_trust_prompt(text: str) -> bool:
        """Check if the pane shows the folder-trust dialog.

        The dialog is top-anchored (its content is near the top of the pane,
        bottom rows blank), so this scans the WHOLE pane rather than the bottom
        permission zone.  To stay robust against the ~500 lines of scrollback the
        runner captures, it keys on the *menu option line* "Yes, I trust this
        folder" — anchored to its own line — instead of a loose substring of the
        marker phrases.  The pre-2.1.248 line carries a "1." that prose does not
        reproduce; the current unnumbered line is plainer, so it additionally
        requires the dialog's "Enter to confirm" footer.
        """
        if _TRUST_PROMPT_NUMBERED_RE.search(text):
            return True
        if _TRUST_CONFIRM_FOOTER not in text:
            return False
        return bool(_TRUST_PROMPT_RE.search(text))

    @staticmethod
    def _trust_option_offset(text: str) -> int:
        """Downs needed to move the cursor onto "Yes, I trust this folder".

        Returns 0 for the pre-2.1.248 dialog (the cursor already starts on
        "1. Yes, I trust this folder") and 1 for the current one, whose options
        are unnumbered with "❯ No, exit" selected by default.

        Only the LAST contiguous run of option lines counts: the runner captures
        ~500 lines of scrollback, which may hold an older copy of the dialog
        above the live one.
        """
        block: list[tuple[bool, str]] = []
        run: list[tuple[bool, str]] = []
        for line in text.splitlines():
            match = _TRUST_OPTION_RE.match(line)
            if match:
                run.append((bool(match.group("cursor")), match.group("label")))
            elif line.strip():
                # A non-blank, non-option line ends the run (blank lines do not,
                # so a spacer between options does not split the menu).  The
                # footer under the options ends it too, which is why a finished
                # run is banked here and not only after the loop.
                if run:
                    block = run
                run = []
        if run:
            block = run
        if not block:
            return 0
        cursor_idx = next((i for i, (sel, _) in enumerate(block) if sel), 0)
        yes_idx = next(
            (i for i, (_, label) in enumerate(block) if label.startswith("Yes")),
            cursor_idx,
        )
        return max(0, yes_idx - cursor_idx)

    @staticmethod
    def _has_permission_prompt(text: str) -> bool:
        """Check if the pane shows a permission/approval prompt.

        Only scans the bottom N lines (_PERMISSION_SCAN_LINES) to avoid
        false positives from conversation text containing marker phrases (#156).
        """
        zone = _permission_zone(text)
        return any(marker in zone for marker in _PERMISSION_PROMPT_MARKERS)

    @staticmethod
    def _is_yn_prompt(text: str) -> bool:
        """Return True if the pane contains a [y/N] or [Y/n] inline prompt.

        Only scans the bottom N lines to avoid matching [y/N] in conversation (#156).
        """
        zone = _permission_zone(text)
        return bool(_YN_PROMPT_RE.search(zone))

    @staticmethod
    def _has_unknown_interactive(text: str) -> bool:
        """Return True if the pane shows an interactive menu not covered by known markers.

        Detects numbered-menu cursors (❯ 1. ...) and [y/N] prompts that do NOT
        match any known trust or permission marker. Used to surface unknown prompts
        to Discord rather than letting the session stall silently.

        Only scans the bottom N lines (_PERMISSION_SCAN_LINES) to avoid false
        positives from conversation text (#156).
        """
        if not text:
            return False
        zone = _permission_zone(text)
        has_menu = bool(_INTERACTIVE_MENU_RE.search(zone)) or bool(_YN_PROMPT_RE.search(zone))
        if not has_menu:
            return False
        # Exclude already-handled prompts so they don't double-fire.
        if any(marker in zone for marker in _TRUST_PROMPT_MARKERS):
            return False
        if any(marker in zone for marker in _PERMISSION_PROMPT_MARKERS):
            return False
        # Exclude AskUserQuestion menus (#166): the current Claude Code variant
        # uses "Type something." / "Chat about this" instead of the older
        # "Other", so the #153 markers below no longer match it.  These menus are
        # bridged to Discord buttons; flagging them as "unknown" would duplicate
        # the real UI with a spurious warning.
        if _is_ask_question(zone):
            return False
        # Exclude the AskUserQuestion multi-question Submit/Review screen: it is a
        # known, auto-submitted prompt (handled in the poll loop), not an unknown
        # one — flagging it would surface a spurious warning over an answered flow.
        if _is_ask_submit_screen(zone):
            return False
        # Exclude Plan approval (ExitPlanMode) and (legacy) AskUserQuestion menus (#153).
        # These are handled via Discord buttons; surfacing them as "unknown" would
        # confuse users with a duplicate warning alongside the real Discord UI.
        return not any(marker in zone for marker in _KNOWN_INTERACTIVE_MARKERS)

    async def _accept_trust_prompt(self, pane_text: str = "") -> None:
        """Accept the folder-trust dialog by selecting "Yes, I trust this folder".

        Which keys do that depends on the Claude Code version: the older dialog
        starts on "1. Yes, I trust this folder" (bare Enter), the current one
        starts on "No, exit" (Down, then Enter).  Passing the pane text lets the
        offset be read off the live dialog instead of assumed — confirming the
        wrong default makes Claude exit without ever starting the turn.
        """
        offset = self._trust_option_offset(pane_text)
        logger.info(
            "Trust prompt detected, accepting (down=%d, thread=%d)", offset, self._thread_id
        )
        await self._navigate_menu(offset)

    async def _dismiss_usage_limit_menu(self, pane_text: str) -> None:
        """Close the limit's "What do you want to do?" menu, if it is open (#631).

        Only ever selects the option that keeps waiting.  The other two switch
        the account to usage credits or a Team plan — real money, and not a
        decision c-lord gets to make on someone's behalf.  When that option
        cannot be identified the menu is left alone and reported as-is; a stuck
        menu is recoverable by a human, an unasked-for plan change is not.
        """
        index = _usage_limit_wait_option(pane_text)
        if index is None:
            logger.info(
                "Usage-limit menu: no 'keep waiting' option found, leaving it "
                "untouched (thread=%d)",
                self._thread_id,
            )
            return
        logger.info(
            "Usage-limit menu: selecting the wait option (index=%d, thread=%d)",
            index,
            self._thread_id,
        )
        await self._navigate_menu(index)

    async def _accept_permission_prompt(self, pane_text: str = "") -> None:
        """Auto-accept a permission prompt.

        Numbered-menu prompts (❯ 1. Yes / 2. No) accept with Enter (selects highlighted).
        Inline [y/N] prompts send "y" explicitly, because Enter selects the default N.
        """
        logger.info(
            "Permission prompt detected, auto-accepting (thread=%d)",
            self._thread_id,
        )
        from ..tmux import _run

        window = self._tmux._find_window_for_thread(self._thread_id)
        if window:
            # #649: address the unique window_id. Accepting a permission prompt
            # is a keystroke that changes what Claude is allowed to do — the one
            # place an ambiguous ``session:name`` target must never send it to
            # whichever window happened to be first.
            target = self._tmux._target(window)
            key = "y" if self._is_yn_prompt(pane_text) else "Enter"
            _run(["tmux", "send-keys", "-t", target, key])

    async def _submit_ask_screen(self) -> None:
        """Confirm a multi-question AskUserQuestion Submit/Review screen.

        Every question was already answered via the Discord bridge, so this
        final screen only needs confirming.  Its cursor starts on "Submit
        answers", so a bare Enter submits the recorded answers and lets Claude
        proceed (mirrors the auto-accept used for trust/permission prompts).
        """
        logger.info(
            "AskUserQuestion Submit screen detected, submitting (thread=%d)", self._thread_id
        )
        await asyncio.to_thread(self._tmux.send_keys, self._thread_id, "Enter")

    async def _navigate_menu(self, index: int) -> bool:
        """Move the menu cursor down *index* times then confirm with Enter.

        Each key is sent as a SEPARATE ``send-keys`` call with a delay between
        them (#171): batching them into one ``send-keys Down Down Enter`` is too
        fast — the TUI drops the Down navigations and Enter selects the wrong
        (first) option.
        """
        delivered = True
        for _ in range(max(0, index)):
            if not await asyncio.to_thread(self._tmux.send_keys, self._thread_id, "Down"):
                delivered = False
            await asyncio.sleep(_MENU_NAV_DELAY)
        if not await asyncio.to_thread(self._tmux.send_keys, self._thread_id, "Enter"):
            delivered = False
        if not delivered:
            # #600: send_keys returns False when the thread has no tmux window —
            # the answer went nowhere. Reporting it is what stops the menu from
            # sitting open and being re-posted on every restart.
            logger.warning(
                "answer keystrokes were not delivered — no tmux window for thread %d (#600)",
                self._thread_id,
            )
        return delivered

    async def answer_menu(self, index: int) -> bool:
        """Select option *index* (0-based) of an open AskUserQuestion menu (#166).

        The cursor always starts on the first option, so landing on option
        ``index`` takes ``index`` Down presses, then Enter to confirm.  ``index``
        equals the option's position in the list returned by
        :func:`_parse_ask_from_pane`.
        """
        logger.info(
            "Answering AskUserQuestion menu: option index=%d (thread=%d)",
            index,
            self._thread_id,
        )
        return await self._navigate_menu(index)

    async def answer_menu_multi(self, indices: list[int], option_count: int) -> bool:
        """Answer a multiSelect AskUserQuestion: toggle each option then Submit (#418).

        ``answer_menu`` (Down×index + Enter) is single-select only, so the bridge
        used to drop every checkbox but the first.  The multiSelect TUI, verified
        on a live Claude Code TUI, works differently:

        - the cursor starts on option 0;
        - **Space** toggles the checkbox under the cursor (``[ ]`` ⇄ ``[✔]``);
        - a **"Submit"** row sits just past the real options and the
          "Type something" affordance, i.e. at index ``option_count + 1``
          (mirroring the ``option_count`` index :meth:`answer_menu_text` uses for
          the "Type something" row); ``Down`` to it and ``Enter`` opens the
          "Submit answers" review screen whose cursor defaults to submit, so a
          final ``Enter`` records every toggled value.

        Keys are sent one-per-call with ``_MENU_NAV_DELAY`` spacing — batching is
        dropped by the TUI (#171).
        """
        # #600: every keystroke reports whether it reached a window; an
        # undelivered answer must not read as an answered menu.
        _delivered = True

        def _ok(sent: object) -> None:
            nonlocal _delivered
            if not sent:
                _delivered = False

        logger.info(
            "Answering multiSelect AskUserQuestion menu: indices=%s (thread=%d)",
            indices,
            self._thread_id,
        )
        cursor = 0
        for idx in sorted({i for i in indices if i >= 0}):
            for _ in range(idx - cursor):
                _ok(await asyncio.to_thread(self._tmux.send_keys, self._thread_id, "Down"))
                await asyncio.sleep(_MENU_NAV_DELAY)
            cursor = idx
            _ok(await asyncio.to_thread(self._tmux.send_keys, self._thread_id, "Space"))
            await asyncio.sleep(_MENU_NAV_DELAY)
        # Navigate to the "Submit" row (one past "Type something") and open the
        # "Submit answers" review screen.
        for _ in range(max(0, (option_count + 1) - cursor)):
            _ok(await asyncio.to_thread(self._tmux.send_keys, self._thread_id, "Down"))
            await asyncio.sleep(_MENU_NAV_DELAY)
        _ok(await asyncio.to_thread(self._tmux.send_keys, self._thread_id, "Enter"))
        await asyncio.sleep(_MENU_NAV_DELAY)
        # Confirm the review screen (cursor defaults to "Submit answers").
        _ok(await asyncio.to_thread(self._tmux.send_keys, self._thread_id, "Enter"))
        if not _delivered:
            logger.warning(
                "answer keystrokes were not delivered — no tmux window for thread %d (#600)",
                self._thread_id,
            )
        return _delivered

    async def answer_menu_text(
        self,
        text_option_index: int,
        text: str,
        *,
        mode: FreeTextMode = FREE_TEXT_ROW,
    ) -> bool:
        """Answer with free text, the way *this* menu's layout accepts it (#172, #650).

        The two layouts Claude Code draws take different keystrokes, and the
        wrong ones do not fail loudly — they answer the tool with "(No answer
        provided)" and throw the typed sentence away.  *mode* therefore comes
        from the pane (:func:`_free_text_mode`), never from an assumption.

        ``row`` — classic layout, verified on a live Claude Code v2.1.150 TUI:

        1. Navigate to the "Type something." row with ``Down`` × *text_option_index*
           — **without** pressing Enter.  (Pressing Enter on that row registers a
           *decline* and closes the menu; no input field opens — this was the #172
           bug.)
        2. Type *text* **literally onto the highlighted row**, which replaces the
           "Type something." label with the typed text.  This must NOT go through
           :meth:`send_input`, which would append Enter and post the text as a
           separate message instead of the answer.
        3. Press ``Enter`` once to record the typed text as the menu answer.

        ``notes`` — preview layout (any option carries a ``preview``), verified
        on a live Claude Code v2.1.252 TUI in an isolated tmux (2026-09-01):

        1. Press ``n`` from the option list.  There is no "Type something." row
           here; ``n`` opens the ``Notes:`` field beside the preview box.
           *text_option_index* is unused — walking down that far would land on
           "Chat about this", which ignores typed characters and answers
           "(No answer provided)" on Enter (#650).
        2. Type *text* literally into the field.
        3. Press ``Enter`` once — with no option selected the notes become the
           answer (``"…"=(no option selected) notes: <text>``).

        Keystrokes are spaced by ``_MENU_NAV_DELAY`` for the same reason as
        :meth:`answer_menu` (#171): the TUI drops keys sent too fast.
        """
        # #600: every keystroke reports whether it reached a window; an
        # undelivered answer must not read as an answered menu.
        _delivered = True

        def _ok(sent: object) -> None:
            nonlocal _delivered
            if not sent:
                _delivered = False

        logger.info(
            "Answering AskUserQuestion with free text (thread=%d, mode=%s)",
            self._thread_id,
            mode,
        )
        if mode == FREE_TEXT_NOTES:
            # Open the Notes field — the preview layout's only free-text input.
            _ok(await asyncio.to_thread(self._tmux.send_keys, self._thread_id, "n"))
            await asyncio.sleep(_MENU_NAV_DELAY)
        else:
            for _ in range(max(0, text_option_index)):
                _ok(await asyncio.to_thread(self._tmux.send_keys, self._thread_id, "Down"))
                await asyncio.sleep(_MENU_NAV_DELAY)
        # Type the free text onto the highlighted row / into the notes field.
        _ok(await asyncio.to_thread(self._tmux.send_literal, self._thread_id, text))
        await asyncio.sleep(_MENU_NAV_DELAY)
        # Confirm — records the typed text as the AskUserQuestion answer.
        _ok(await asyncio.to_thread(self._tmux.send_keys, self._thread_id, "Enter"))
        if not _delivered:
            logger.warning(
                "answer keystrokes were not delivered — no tmux window for thread %d (#600)",
                self._thread_id,
            )
        return _delivered

    async def transcript_project_dir(self) -> Path | None:
        """Where Claude Code writes this pane's transcript, or None (#651).

        The transcript is how c-lord confirms that an answer actually reached
        Claude rather than merely reaching the terminal — see
        :func:`c_lord.discord_ui.ask_handler._verify_answer_reached_claude`.
        None whenever the pane's cwd cannot be read; the caller then falls back
        to the weaker "did the menu close" evidence.
        """
        getter = getattr(self._tmux, "pane_working_dir", None)
        if not callable(getter):
            return None
        cwd = await asyncio.to_thread(getter, self._thread_id)
        if not isinstance(cwd, str) or not cwd:
            return None
        return derive_project_dir(cwd)

    async def cancel_menu(self) -> bool:
        """Dismiss an open AskUserQuestion menu with Esc (e.g. on timeout) (#166)."""
        return bool(await asyncio.to_thread(self._tmux.send_keys, self._thread_id, "Escape"))

    async def peek_pending_ask(self) -> AskQuestion | None:
        """Re-capture the pane and return an open AskUserQuestion/plan menu (#219).

        Post-turn safety net: the ``run`` poll loop only bridges a menu while it
        is active, so a turn that finalizes just before the menu renders (e.g.
        the 30s stable-response fallback firing during a silent pre-menu thinking
        phase) leaves Claude blocked on a TUI menu that was never bridged.
        ``_run_helper`` calls this after the stream ends to recover such a menu
        and bridge it instead of posting the misleading 'no discord-reply' notice.

        Recovers both AskUserQuestion and plan-approval (#251) menus, which share
        the pane_ask bridge.
        """
        if await self._pane_is_dead():
            return None
        raw = await asyncio.to_thread(self._tmux.capture_pane, self._thread_id)
        pane = _normalize_capture(raw)
        return _parse_ask_from_pane(pane) or _parse_plan_from_pane(pane)

    async def _pane_is_dead(self) -> bool:
        """True when the pane's foreground process is positively not claude (#510).

        A menu drawn by a claude that has since exited stays on screen forever —
        tmux-resurrect even restores it verbatim after a reboot. Text alone
        cannot tell that apart from a live question, so every menu peek asks the
        process table first. Unreadable ⇒ False (unknown, not dead).
        """
        getter = getattr(self._tmux, "pane_foreground_command", None)
        if not callable(getter):  # pragma: no cover - legacy/stub managers
            return False
        return pane_command_is_dead(await asyncio.to_thread(getter, self._thread_id))

    async def peek_menu_state(self) -> tuple[AskQuestion | None, bool]:
        """Return ``(open menu or None, capture_ok)`` (#485).

        ``capture_ok`` is False when the pane capture came back **empty** — a
        tmux/window hiccup (e.g. the window mapping momentarily unresolved), NOT
        evidence the menu closed. The bridge's resolve-watcher must treat that as
        "unknown, keep waiting" rather than "menu gone"; treating an empty
        capture as resolution is what let a still-open menu be marked answered,
        then get selected by the next reply. Distinct from
        :meth:`peek_pending_ask` (kept as-is for the post-turn recovery caller).

        #510: a pane whose claude has exited reports ``(None, True)`` — a
        healthy read with no live menu — so an in-flight bridge winds down in
        seconds instead of sitting on the 24h answer timeout and then re-posting
        the same dead question every day.
        """
        if await self._pane_is_dead():
            return None, True
        raw = await asyncio.to_thread(self._tmux.capture_pane, self._thread_id)
        capture_ok = bool(raw.strip())
        pane = _normalize_capture(raw)
        menu = _parse_ask_from_pane(pane) or _parse_plan_from_pane(pane)
        return menu, capture_ok

    @staticmethod
    def _extract_response(pane_text: str) -> str:
        """Extract Claude's latest response from the TUI pane text.

        Parses the Claude TUI structure to find the response content
        after the last user prompt, stripping TUI chrome (banner,
        shell noise, separators, status bar, prompt markers).

        The TUI layout (bottom to top):
        - Status bar: ``-- INSERT -- ⏵⏵ bypass permissions on ...``
        - Separator: ``────────────...``
        - Input prompt: ``❯`` (bare, waiting for input)
        - Separator: ``────────────...``
        - Response content (``●`` markers, ``⎿`` tool results)
        - User prompt: ``❯ <user message>``
        - (previous exchanges, banner, shell noise above)
        """
        # Issue #47: capture-pane is invoked with ``-e`` so terminal hyperlinks
        # (OSC 8) survive. Rewrite them to plain text + bare URL and strip
        # remaining ANSI before the line-based chrome filters run.
        pane_text = _normalize_capture(pane_text)
        lines = pane_text.splitlines()

        # Step 1: Strip bottom TUI chrome (status bar, separators, input
        # area, and generation status indicators).
        #
        # The bottom of the Claude TUI has this structure:
        #   ──────────── (separator 1)
        #   ❯ <input or hint>  (or "· Thinking…", "Tip: ...")
        #   ──────────── (separator 2)
        #   <ccstatusline lines, optional, variable count>
        #   -- INSERT -- (status bar)
        #
        # We track separator_count so that ❯ lines are only stripped while
        # inside the input area (between or below the two separators).
        # Once we've passed both separators, a ❯ line is a user prompt
        # and should NOT be stripped.
        #
        # `in_status_bar_zone` is True after we've consumed the vim status
        # bar (-- INSERT) and before we've crossed the bottom separator.
        # In this zone, unrecognised lines are user-configured ccstatusline
        # output (Model:, Cost:, ⎇ branch, etc.) and must be stripped.
        end = len(lines)
        separator_count = 0
        in_status_bar_zone = False
        while end > 0:
            stripped = lines[end - 1].strip()
            if not stripped or any(stripped.startswith(m) for m in _STATUS_BAR_MARKERS):
                if any(stripped.startswith(m) for m in _STATUS_BAR_MARKERS):
                    in_status_bar_zone = True
                end -= 1
            elif _SEPARATOR_RE.match(stripped):
                separator_count += 1
                # Crossing the bottom separator exits the ccstatusline zone.
                in_status_bar_zone = False
                end -= 1
            elif stripped.startswith("❯") or stripped.startswith(">"):
                if separator_count < 2:
                    # Still in the input area (between/below separators).
                    end -= 1
                else:
                    # Above the input area — this is a user prompt; stop.
                    break
            elif any(
                stripped.startswith(m) for m in _GENERATION_STATUS_MARKERS
            ) or _GENERATION_STATUS_RE.match(stripped):
                end -= 1
            elif in_status_bar_zone:
                # Unrecognised line between vim status bar and bottom separator —
                # this is ccstatusline output (user-configurable, so we cannot
                # match it with explicit patterns).
                end -= 1
            else:
                break

        if end == 0:
            return ""

        # Step 2: Find the last user prompt (❯ followed by actual text).
        # Now that bottom chrome is stripped, search backwards from end.
        # The user prompt uses a regular space after ❯ (not \xa0).
        prompt_idx = -1
        for i in range(end - 1, -1, -1):
            stripped = lines[i].strip()
            if stripped.startswith("❯ ") and len(stripped) > 2:
                prompt_idx = i
                break

        if prompt_idx == -1:
            # Fallback: the user prompt has scrolled off-screen (long response).
            # Only activate when TUI chrome is present (both separators found)
            # to avoid false positives on non-TUI text.
            if separator_count < 2:
                return ""
            # Strip top-of-pane noise (banner, shell lines) and use the rest.
            banner_chars = ("▐", "▝", "▘")
            start = 0
            for i in range(end):
                stripped = lines[i].strip()
                # Skip empty lines, shell prompts, and the Claude TUI banner
                is_noise = (
                    not stripped
                    or stripped.startswith("$")
                    or stripped.startswith("yousan")
                    or "Claude Code" in stripped
                    or any(stripped.startswith(c) for c in banner_chars)
                )
                if is_noise:
                    start = i + 1
                else:
                    break
            response_lines = lines[start:end]
        else:
            # Step 3: Extract response lines between prompt and end.
            raw_response_lines = lines[prompt_idx + 1 : end]
            # Anchor on the first Claude response marker (●/⎿/✻ etc.) so that
            # continuation lines of a multi-line user prompt — which sit
            # between the ❯ line and Claude's first marker — are not treated
            # as response text and echoed back to Discord (issue #30).
            first_marker = -1
            for i, line in enumerate(raw_response_lines):
                if line.lstrip().startswith(_RESPONSE_MARKERS):
                    first_marker = i
                    break
            if first_marker == -1:
                return ""
            response_lines = raw_response_lines[first_marker:]

        # Step 4: Clean up the response.
        return _clean_tui_lines(response_lines)

    @staticmethod
    def _is_generating(text: str) -> bool:
        """Check if Claude is actively generating (thinking/tool indicators visible).

        Looks at the bottom 6 lines (which contain TUI chrome) for a generation
        status indicator that contains ``…`` (U+2026).  Active indicators carry
        the ellipsis — sometimes at the end (``✻ Running…``) and sometimes
        followed by an elapsed/token suffix (``✽ Generating… (7m 45s · ↑ 23.2k
        tokens)``) — while completion summaries like ``✻ Cooked for 56s`` have
        no ellipsis at all.  Matching ``…`` anywhere in the line (not only at
        the end) is what catches the long-thinking case where the suffix pushed
        the ellipsis off the end and the turn was wrongly finalized early (#179).
        """
        lines = text.rstrip().splitlines()
        # #365 follow-up: scan the live-spinner "(Ns ·" timer across a WIDE bottom
        # window. While a tool runs, its result preview + input box + footer push
        # the timer line 10-20 lines off the bottom; a bottom-6-only scan misses
        # it and the turn is finalized early. The timer only exists while actively
        # working, so a wide scan does not false-positive on idle panes.
        for line in lines[-_RUNNING_PROBE_LINES:]:
            if _RUNNING_SPINNER_RE.search(line):
                return True
        # Fallback: an ellipsis-bearing status glyph in the bottom few lines —
        # catches a spinner that has no timer line yet (just "✻ Running…"). Kept
        # narrow so a stale in-progress spinner up in scrollback is not matched.
        for line in lines[-6:]:
            stripped = line.strip()
            if _GENERATION_STATUS_RE.match(stripped) and "…" in stripped:
                return True
        return False

    @staticmethod
    def _has_input_prompt(text: str) -> bool:
        """Check if the pane text contains a Claude input prompt near the bottom.

        The TUI shows a status bar (``-- INSERT --``, separator lines) below
        the ``❯`` prompt, so we cannot simply check ``endswith``.  Instead,
        look at the last few lines for the input box.

        A bare ``❯`` means an empty input box.  But Claude Code also renders
        ghost/placeholder text — and any unsent text the user typed — right
        after the prompt glyph in the live input box, e.g.
        ``❯\\xa0Try "create a util ..."`` or ``❯\\xa0A で 3回試して`` (#62).  That
        still means Claude is idle and waiting, so it counts as a ready prompt;
        without this the input box is misread as "still busy" until the 30s
        fallback fires.

        The discriminator (verified against real ``capture-pane -e`` output):
        the **live input box** puts a non-breaking space (``\\xa0``) after the
        glyph, while a **sent/confirmed user message** in the scrollback uses a
        regular space (``❯ 2+2 は？``).  A bare ``❯`` and the ``❯\\xa0`` form are
        therefore unique to the live box, so an old sent message scrolled near
        the bottom is never mistaken for the live prompt.  A numbered-menu
        cursor (``❯ 1. ...``) is excluded — it is an interactive menu, not a
        ready prompt, and treating it as one would complete the turn before the
        menu is answered.

        We scan a generous bottom window rather than just the last few lines:
        the box sits above the bottom chrome (separator + the user-configurable
        ccstatusline rows + ``-- INSERT --`` + effort/tip footer), which can be
        ~6–8 lines tall, so a 6-line window misses the box entirely (#62).
        """
        lines = text.rstrip().splitlines()
        for line in lines[-_INPUT_PROMPT_SCAN_LINES:]:
            stripped_line = line.strip()
            if stripped_line in ("❯", ">"):
                return True
            if (
                stripped_line.startswith("❯\xa0") or stripped_line.startswith(">\xa0")
            ) and not _INTERACTIVE_MENU_RE.match(stripped_line):
                return True
        return False

    @classmethod
    def _is_idle_at_prompt(cls, text: str) -> bool:
        """Return True when the pane shows ``claude`` parked at an idle prompt.

        Used by the inactivity backstop to tell "the turn is over" from "the
        turn is wedged" (#541).  ``_has_input_prompt`` alone is not enough: the
        TUI keeps the input box on screen *while generating too*, so a session
        frozen mid-tool would look idle.  Requiring the generation indicator to
        be absent as well makes the pair a real idle check —
        no spinner **and** a ready input box means Claude finished and is
        waiting for the next message.

        The caller checks process liveness separately, which is what keeps a
        dead pane's leftover ``❯`` in the scrollback from reading as idle.
        """
        return cls._has_input_prompt(text) and not cls._is_generating(text)

    # Keep for backward compatibility / testing.
    @staticmethod
    def _compute_delta(old: str, new: str) -> str:
        """Compute the text that was added between two pane captures."""
        old_stripped = old.rstrip()
        new_stripped = new.rstrip()

        if new_stripped.startswith(old_stripped):
            return new_stripped[len(old_stripped) :]

        old_lines = old_stripped.splitlines()
        new_lines = new_stripped.splitlines()

        overlap = 0
        for i in range(min(len(old_lines), len(new_lines)), 0, -1):
            if old_lines[-i:] == new_lines[:i]:
                overlap = i
                break

        if overlap > 0:
            added_lines = new_lines[overlap:]
            return "\n".join(added_lines)

        return new_stripped


def _clean_tui_lines(lines: list[str]) -> str:
    """Clean Claude TUI response lines for Discord display.

    - Strips leading/trailing empty lines
    - Removes ``●`` response markers
    - Cleans ``⎿`` tool result markers
    - Removes TUI hints (``ctrl+o to expand``, ``Recalled N memory``)
    """
    # Skip leading empty lines.
    start = 0
    while start < len(lines) and not lines[start].strip():
        start += 1

    cleaned: list[str] = []
    for line in lines[start:]:
        stripped = line.strip()

        # Determine the content text after removing TUI markers (● / ⎿).
        if stripped.startswith("● "):
            content = stripped[2:]
        elif stripped == "●":
            content = ""
        elif stripped.startswith("⎿"):
            content = stripped[1:].lstrip()
        else:
            content = stripped

        # Skip lines matching strip patterns — check both the raw line
        # and the marker-stripped content so patterns like "Hi! How can I
        # help you.*" match even when the line has a "● " prefix.
        skip = False
        for pat in _STRIP_PATTERNS:
            if pat.fullmatch(stripped) or (content != stripped and pat.fullmatch(content)):
                skip = True
                break
        if skip:
            continue

        # Remove (ctrl+o to expand) hints inline.
        line = re.sub(r"\s*\(ctrl\+o to expand\)", "", line)
        stripped = line.strip()

        # Strip ● marker from response/tool lines.
        if stripped.startswith("● "):
            cleaned.append(stripped[2:])
        elif stripped == "●":
            cleaned.append("")
        # Clean ⎿ tool result marker (keep content, indent slightly).
        elif stripped.startswith("⎿"):
            content = stripped[1:].lstrip()
            if content:
                cleaned.append(f"  {content}")
            else:
                cleaned.append("")
        else:
            # Keep other lines (indented continuation, etc.) with original indent.
            cleaned.append(line.rstrip())

    # Remove leading empty lines (may appear after filtered lines are removed).
    while cleaned and not cleaned[0].strip():
        cleaned.pop(0)

    # Remove trailing empty lines.
    while cleaned and not cleaned[-1].strip():
        cleaned.pop()

    return "\n".join(cleaned)
