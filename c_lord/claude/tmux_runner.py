"""Claude Code runner that executes inside a tmux window.

Instead of spawning a subprocess with ``-p --output-format stream-json``,
this runner starts Claude in TUI mode inside a tmux pane and polls
``tmux capture-pane`` to extract text changes.

The trade-off is that structured stream events (tool use, thinking, etc.)
are not available — only plain text deltas.  However, users can
``tmux attach -t clord:workN`` to see the full Claude TUI in real time.
"""

from __future__ import annotations

import asyncio
import logging
import re
from collections.abc import AsyncGenerator

from ..tmux import TmuxSessionManager
from .types import AskOption, AskQuestion, MessageType, StreamEvent

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

# How long an unknown interactive menu must be continuously visible before we
# alert Discord (seconds).  Guards against transient TUI redraws.
_UNKNOWN_ALERT_DELAY = 5.0

# How long an AskUserQuestion menu must be stable before bridging it to Discord
# buttons (seconds).  Shorter than the unknown delay — it is a definite,
# expected prompt — but still guards against a half-drawn menu frame (#166).
_ASK_ALERT_DELAY = 1.5

# Delay between individual menu-navigation keystrokes (seconds).  Keys must be
# sent one at a time with a gap — batching `Down Down Enter` into one send-keys
# call is too fast and the TUI drops the navigations, selecting the wrong
# option (#171).
_MENU_NAV_DELAY = 0.25

# Delay after startup before polling begins (seconds).
_POST_STARTUP_DELAY = 1.0

# How long to wait after sending ``claude --continue`` before checking whether
# Claude actually started (issue #123 Part 2).  If Claude is still not running
# after this delay, ``--continue`` failed (no session to resume) and we fall
# back to a plain fresh start.
_CONTINUE_CHECK_DELAY = 3.0

# Patterns that indicate a trust/safety prompt that needs Enter to dismiss.
_TRUST_PROMPT_MARKERS = (
    "Yes, I trust this folder",
    "Enter to confirm",
)

# Patterns from Plan approval (ExitPlanMode) and AskUserQuestion TUI menus.
# These are KNOWN prompt types handled via Discord buttons; they must NOT be
# flagged as "unknown" interactive prompts (#153).
# Sources: Claude Code TUI catalog (docs/tui-prompts.md).
_KNOWN_INTERACTIVE_MARKERS = (
    # ExitPlanMode (Plan approval) — docs/tui-prompts.md §2-2
    "Would you like to proceed?",
    "No, keep planning",
    # AskUserQuestion — always adds "Other" as the last option (no number prefix)
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


def _permission_zone(text: str) -> str:
    """Return the bottom N lines of the pane where real prompts appear.

    Only this zone is scanned for permission / y/N / unknown-interactive markers.
    Conversation text in the scrollback above is excluded, preventing false
    positives when Claude's own output contains marker phrases (#156).
    """
    lines = text.splitlines()
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


def _is_ask_question(text: str) -> bool:
    """True iff the pane shows an AskUserQuestion TUI menu (current variant)."""
    return _ASK_SIGNATURE in text and bool(_ASK_OPTION_RE.search(text))


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

    # Bound the scan: from the header (or, if it scrolled off, a small window
    # above the menu end) down to the menu end — never the whole buffer.
    scan_from = header_idx + 1 if header_idx >= 0 else max(0, end_idx - 20)

    # Indices of every numbered line (including meta-options) within the menu —
    # used to bound the region from which each real option's description is read.
    all_opt_indices = [i for i in range(scan_from, end_idx + 1) if _ASK_OPTION_RE.match(lines[i])]

    options: list[AskOption] = []
    first_opt_idx: int | None = None
    for pos, i in enumerate(all_opt_indices):
        label = _ASK_OPTION_RE.match(lines[i]).group(1).strip()  # type: ignore[union-attr]
        if label in _ASK_META_LABELS:
            continue
        if first_opt_idx is None:
            first_opt_idx = i
        # Description (#169): the first non-empty, non-separator line between this
        # option and the next numbered line — Claude renders it indented below
        # the option.  Empty when the option has no description.
        next_i = all_opt_indices[pos + 1] if pos + 1 < len(all_opt_indices) else end_idx + 1
        description = ""
        for j in range(i + 1, next_i):
            s = lines[j].strip()
            if not s or _SEPARATOR_RE.match(lines[j]):
                continue
            description = s
            break
        options.append(AskOption(label=label, description=description))

    if not options:
        return None

    # The question is the last non-empty, non-separator line between the header
    # and the first option (it sits closest to the options).
    question = ""
    end = first_opt_idx if first_opt_idx is not None else end_idx
    for line in lines[scan_from:end]:
        s = line.strip()
        if not s or _SEPARATOR_RE.match(line):
            continue
        question = s

    return AskQuestion(question=question, header=header, options=options)


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
    ) -> None:
        self._tmux = tmux_manager
        self._thread_id = thread_id
        self.model = model
        self.working_dir = working_dir
        self.timeout_seconds = timeout_seconds
        self._permission_mode = permission_mode
        self._dangerously_skip_permissions = dangerously_skip_permissions
        # True only for the restart-resume path (on_ready → pending_resumes).
        # /clear and normal new threads must remain False to prevent --continue
        # from recovering cleared conversation history (issue #123 Part 2 fix).
        self._try_continue = try_continue
        self._stopped = False
        self._silent_stop = False
        self._last_capture: str = ""

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
            ok = await asyncio.to_thread(self._tmux.send_input, self._thread_id, prompt)
            if not ok:
                yield StreamEvent(
                    raw={},
                    message_type=MessageType.RESULT,
                    is_complete=True,
                    error="Failed to send input to Claude in tmux",
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
                )
                if not ok:
                    yield StreamEvent(
                        raw={},
                        message_type=MessageType.RESULT,
                        is_complete=True,
                        error="Failed to start Claude in tmux",
                    )
                    return

                # Wait briefly; if Claude still not running, --continue failed.
                await asyncio.sleep(_CONTINUE_CHECK_DELAY)
                still_running = await asyncio.to_thread(
                    self._tmux.is_claude_running, self._thread_id
                )
                if not still_running:
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
                    )
                    if not ok:
                        yield StreamEvent(
                            raw={},
                            message_type=MessageType.RESULT,
                            is_complete=True,
                            error="Failed to start Claude in tmux",
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
                )
                if not ok:
                    yield StreamEvent(
                        raw={},
                        message_type=MessageType.RESULT,
                        is_complete=True,
                        error="Failed to start Claude in tmux",
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
        # Raw pane activity (#166): while Claude works the pane changes every
        # poll (spinner frame, elapsed-seconds tick), even when no response text
        # is extracted yet.  We only treat the session as idle once the raw pane
        # has been frozen for the idle window — otherwise a long thinking phase
        # before an AskUserQuestion menu trips the idle timeout and we stop
        # polling before the menu appears.
        last_raw = ""
        raw_static_seconds = 0.0

        while not self._stopped and elapsed < self.timeout_seconds:
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

            # Auto-accept permission prompts so the bot doesn't stall.
            if self._has_permission_prompt(current):
                unknown_interactive_stable = 0.0
                last_alerted_unknown = None
                ask_stable = 0.0
                last_bridged_ask = None
                await self._accept_permission_prompt(current)
                continue

            # AskUserQuestion menu → bridge to Discord buttons (#166).
            # Yielding pane_ask suspends this generator while the EventProcessor
            # shows the buttons and waits for a click; it then sends the menu
            # keystrokes back via answer_menu(), after which polling resumes and
            # the (now-closed) menu no longer matches.
            ask_q = _parse_ask_from_pane(current)
            if ask_q is not None:
                ask_stable += _POLL_INTERVAL
                ask_sig = "\n".join(o.label for o in ask_q.options)
                if ask_stable >= _ASK_ALERT_DELAY and ask_sig != last_bridged_ask:
                    last_bridged_ask = ask_sig
                    logger.info(
                        "AskUserQuestion menu detected, bridging to Discord (thread=%d)",
                        self._thread_id,
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

            # Done: non-empty response has been stable long enough.
            # Two tiers:
            #  - Quick exit (3s): response stable AND input prompt visible
            #    AND not actively generating (no ✻ Running… etc.).
            #    Without the is_gen check, tool execution pauses (where the
            #    pane is stable for several seconds) would trigger false
            #    completion, posting raw tool-call text instead of Claude's
            #    final formatted response.
            #  - Fallback exit (30s): response stable but no input prompt
            #    (Claude may have finished but prompt detection failed,
            #    e.g. completion summary text in the prompt area).
            is_gen = self._is_generating(current)
            if (
                last_response
                and stable_seconds >= _RESPONSE_STABLE_TIMEOUT
                and ((has_prompt and not is_gen) or stable_seconds >= _RESPONSE_STABLE_FALLBACK)
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
                # During a fresh start, allow extra time for Claude to load.
                if not claude_running and elapsed < _STARTUP_TIMEOUT:
                    continue
                logger.info(
                    "Idle timeout (%.1fs) — no response (thread=%d)",
                    stable_seconds,
                    self._thread_id,
                )
                break

        # Yield final complete event.
        if self._stopped:
            yield StreamEvent(
                raw={},
                message_type=MessageType.RESULT,
                is_complete=True,
                error=None if self._silent_stop else "Stopped by user",
            )
        elif elapsed >= self.timeout_seconds:
            yield StreamEvent(
                raw={},
                message_type=MessageType.RESULT,
                is_complete=True,
                error=f"Timed out after {self.timeout_seconds} seconds",
            )
        else:
            # Normal completion — emit RESULT only. The text is intentionally
            # dropped (#53): Claude posts its own final answer via the
            # discord-reply skill, not through the runner stream.
            yield StreamEvent(
                raw={},
                message_type=MessageType.RESULT,
                is_complete=True,
            )

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

    # ------------------------------------------------------------------
    # Private helpers
    # ------------------------------------------------------------------

    async def _handle_startup_prompts(self) -> None:
        """Handle any interactive prompts during Claude startup."""
        elapsed = 0.0
        trust_handled = False

        while elapsed < _STARTUP_TIMEOUT and not self._stopped:
            await asyncio.sleep(_POLL_INTERVAL)
            elapsed += _POLL_INTERVAL

            pane = await asyncio.to_thread(self._tmux.capture_pane, self._thread_id)

            if not trust_handled and self._has_trust_prompt(pane):
                logger.info(
                    "Trust prompt detected, sending Enter to accept (thread=%d)",
                    self._thread_id,
                )
                from ..tmux import _run

                window = self._tmux._find_window_for_thread(self._thread_id)
                if window:
                    target = f"{self._tmux.session_name}:{window}"
                    _run(["tmux", "send-keys", "-t", target, "Enter"])
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
        """Check if the pane shows a trust/safety confirmation prompt."""
        return any(marker in text for marker in _TRUST_PROMPT_MARKERS)

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
        # Exclude Plan approval (ExitPlanMode) and (legacy) AskUserQuestion menus (#153).
        # These are handled via Discord buttons; surfacing them as "unknown" would
        # confuse users with a duplicate warning alongside the real Discord UI.
        return not any(marker in zone for marker in _KNOWN_INTERACTIVE_MARKERS)

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
            target = f"{self._tmux.session_name}:{window}"
            key = "y" if self._is_yn_prompt(pane_text) else "Enter"
            _run(["tmux", "send-keys", "-t", target, key])

    async def _navigate_menu(self, index: int) -> None:
        """Move the menu cursor down *index* times then confirm with Enter.

        Each key is sent as a SEPARATE ``send-keys`` call with a delay between
        them (#171): batching them into one ``send-keys Down Down Enter`` is too
        fast — the TUI drops the Down navigations and Enter selects the wrong
        (first) option.
        """
        for _ in range(max(0, index)):
            await asyncio.to_thread(self._tmux.send_keys, self._thread_id, "Down")
            await asyncio.sleep(_MENU_NAV_DELAY)
        await asyncio.to_thread(self._tmux.send_keys, self._thread_id, "Enter")

    async def answer_menu(self, index: int) -> None:
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
        await self._navigate_menu(index)

    async def answer_menu_text(self, text_option_index: int, text: str) -> None:
        """Answer via the free-text ("Type something.") affordance (#172).

        Verified on a live Claude Code v2.1.150 TUI, the correct interaction is:

        1. Navigate to the "Type something." row with ``Down`` × *text_option_index*
           — **without** pressing Enter.  (Pressing Enter on that row registers a
           *decline* and closes the menu; no input field opens — this was the #172
           bug.)
        2. Type *text* **literally onto the highlighted row**, which replaces the
           "Type something." label with the typed text.  This must NOT go through
           :meth:`send_input`, which would append Enter and post the text as a
           separate message instead of the answer.
        3. Press ``Enter`` once to record the typed text as the menu answer.

        Keystrokes are spaced by ``_MENU_NAV_DELAY`` for the same reason as
        :meth:`answer_menu` (#171): the TUI drops keys sent too fast.
        """
        logger.info(
            "Answering AskUserQuestion with free text (thread=%d)",
            self._thread_id,
        )
        for _ in range(max(0, text_option_index)):
            await asyncio.to_thread(self._tmux.send_keys, self._thread_id, "Down")
            await asyncio.sleep(_MENU_NAV_DELAY)
        # Type the free text directly onto the highlighted "Type something." row.
        await asyncio.to_thread(self._tmux.send_literal, self._thread_id, text)
        await asyncio.sleep(_MENU_NAV_DELAY)
        # Confirm — records the typed text as the AskUserQuestion answer.
        await asyncio.to_thread(self._tmux.send_keys, self._thread_id, "Enter")

    async def cancel_menu(self) -> None:
        """Dismiss an open AskUserQuestion menu with Esc (e.g. on timeout) (#166)."""
        await asyncio.to_thread(self._tmux.send_keys, self._thread_id, "Escape")

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
        status indicator that ends with ``…`` (U+2026).  Active indicators
        like ``✻ Running…`` end with ellipsis; completion summaries like
        ``✻ Cooked for 56s`` do not.
        """
        lines = text.rstrip().splitlines()
        for line in lines[-6:]:
            stripped = line.strip()
            if _GENERATION_STATUS_RE.match(stripped) and stripped.endswith("…"):
                return True
        return False

    @staticmethod
    def _has_input_prompt(text: str) -> bool:
        """Check if the pane text contains a Claude input prompt near the bottom.

        The TUI shows a status bar (``-- INSERT --``, separator lines) below
        the ``❯`` prompt, so we cannot simply check ``endswith``.  Instead,
        look at the last few lines for a line that is *only* the prompt
        character (with optional whitespace).
        """
        lines = text.rstrip().splitlines()
        for line in lines[-6:]:
            stripped_line = line.strip()
            if stripped_line in ("❯", ">"):
                return True
        return False

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
