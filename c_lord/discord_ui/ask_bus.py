# <<REDACTED 16 lines>>
import time
# <<REDACTED 3 lines>>
# Why a thread's menu stopped accepting answers (#536).  A click that arrives
# after the fact needs the real reason: "the bot restarted" was told to every
# late clicker, including the one who had just answered in the terminal.
CLOSE_ANSWERED = "answered"  # a Discord click was delivered
CLOSE_TERMINAL = "terminal"  # answered/cancelled directly in the tmux pane
CLOSE_TIMEOUT = "timeout"  # nobody answered within ASK_ANSWER_TIMEOUT
CLOSE_INTERRUPTED = "interrupted"  # a new instruction pre-empted the turn (#315)
CLOSE_STOPPED = "stopped"  # the turn was stopped (workspace stop, ⏹ Stop) (#977)

# What became of an answer the bus routed to the TUI (#804).  The bus itself
# only ever queues an answer, so "post_answer returned True" means "someone is
# listening", never "Claude got it" — announcing the second from the first is
# how a typed answer came to be reported as 送りました while Claude recorded it
# as a refusal.  The bridge that actually types the keystrokes reports one of
# these, and whoever needs to speak to the user waits for it.
DELIVERY_DELIVERED = "delivered"  # keystrokes landed AND Claude recorded the answer
DELIVERY_UNDELIVERED = "undelivered"  # the keystrokes reached no window at all (#600)
DELIVERY_NOT_ANSWERED = "not_answered"  # keys landed, Claude recorded "no answer" (#650)
DELIVERY_UNCONFIRMED = "unconfirmed"  # keys landed, the outcome could not be read (#651)

# Closure notes are only useful while a stale copy of the menu can still be
# clicked; the ask-menu answer timeout (24h) is that horizon.
_CLOSE_NOTE_TTL = 86_400.0
_MAX_CLOSE_NOTES = 512

# <<REDACTED 1 lines>>
class ChosenOption(str):
    """An answer that IS option ``option_index`` of its menu (#674).

    An answer on the bus is a list of strings, and a string used to be matched
    back to its option by comparing it with the labels. That broke whenever the
    string was not the label verbatim: a Select value cut to Discord's
    80-character display limit matched nothing, so the pick was typed into the
    pane as free text — "chose it, got a different answer". A choice made on a
    button or in a Select therefore carries the index it was made at, and only
    text with no index (✏️ Other, a typed sentence) is matched by label.

    It is still the option's full label as a string, so everything that only
    *shows* an answer — the embeds, the logs, the non-tmux answer prompt —
    reads it unchanged. (``option_index``, not ``index``: that would shadow
    ``str.index``.)
    """

    option_index: int

    def __new__(cls, label: str, option_index: int) -> ChosenOption:
        choice = super().__new__(cls, label)
        choice.option_index = option_index
        return choice


# <<REDACTED 5 lines>>
    post the chosen options (as :class:`ChosenOption`) into that Queue.
    # <<REDACTED 4 lines>>
        # thread_id -> (noted_at_monotonic, reason) — see note_closed (#536)
        self._closed: dict[int, tuple[float, str]] = {}
        # thread_id -> may a typed sentence be delivered as this menu's answer?
        # (#536 AC7) False for plan-approval menus, which have no free-text row.
        self._free_text: dict[int, bool] = {}
        # thread_id -> where to report what became of this thread's answer (#804)
        self._delivery: dict[int, asyncio.Queue[str]] = {}

    def register(
        self, thread_id: int, *, allow_free_text: bool = False
    ) -> asyncio.Queue[list[str]] | None:
        """Claim the menu for *thread_id* and return its Queue, or None (#535).
# <<REDACTED 1 lines>>
        *allow_free_text* says whether the claimed menu can accept a typed
        sentence as its answer (#536 AC7).  Only the bridge knows: an
        AskUserQuestion has a ``Type something.`` row, a plan-approval menu does
        not, and typing into the latter mis-sends keystrokes.  It defaults to
        False so a caller that has not thought about it cannot opt a menu in by
        accident.

        Registration **is** the ownership acquisition: the first caller gets a
        Queue, every later caller gets ``None`` ("already owned") until the
        owner calls :meth:`unregister`.  The caller should await
        ``queue.get()`` (ideally with a timeout) and call :meth:`unregister`
        when done regardless of success/timeout.

        Why refuse instead of overwrite: several independent paths can spot the
        same TUI menu (the run_claude poll loop, the transcript mirror, the
        #359 watchdog).  Overwriting handed the second one the queue and left
        the first awaiting a queue nobody would ever post to — a 24h hang
        holding the thread, with two live copies of the buttons in Discord
        (#535).  Checking ``is_active`` first cannot fix that: the check and
        the register are two steps and both bridges can pass the check.  This
        method is the single atomic step, so exactly one bridge wins.
        # <<REDACTED 1 lines>>
        if thread_id in self._waiters:
            logger.debug(
                "AskAnswerBus: thread %d already has an owner — refusing register", thread_id
            )
            return None
        # <<REDACTED 2 lines>>
        self._free_text[thread_id] = allow_free_text
        # <<REDACTED 3 lines>>
    def is_active(self, thread_id: int) -> bool:
        """Return True if a bridge is currently waiting for an answer on *thread_id*.

        Used to dedup between the run_claude poll-loop bridge and the always-on
        transcript-mirror bridge (#232): whichever registers first owns the
        menu; the other must not show a second set of buttons.
        """
        return thread_id in self._waiters

    # <<REDACTED 14 lines>>
    def accepts_free_text(self, thread_id: int) -> bool:
        """True if a typed sentence may be delivered as this menu's answer (#536).

        False when no menu is open, and False for a menu whose TUI has no
        free-text row (plan approval) — there the sentence must stay a new
        instruction rather than becoming stray keystrokes.
        """
        return bool(self._free_text.get(thread_id, False)) and thread_id in self._waiters

    def watch_delivery(self, thread_id: int) -> asyncio.Queue[str]:
        """Arm a channel for the verdict on *thread_id*'s next answer (#804).

        Armed by whoever is about to :meth:`post_answer` and needs to tell the
        user what happened — today that is the typed-sentence path in
        ``ClaudeChatCog``.  A click does not arm it: the button already has its
        own feedback (the menu message is rewritten with the verified outcome by
        the bridge), and arming from there would change a path #651 settled.

        Always call :meth:`unwatch_delivery` when done, or a verdict meant for
        the next answer lands in a queue nobody reads.
        """
        q: asyncio.Queue[str] = asyncio.Queue()
        self._delivery[thread_id] = q
        return q

    def note_delivery(self, thread_id: int, verdict: str) -> None:
        """Report what became of *thread_id*'s answer — one of ``DELIVERY_*`` (#804).

        A no-op when nobody armed a channel, which is the normal case (clicks).
        Reporting is therefore always safe to do, and the bridge does it on
        every exit that consumed an answer.
        """
        q = self._delivery.get(thread_id)
        if q is None:
            return
        q.put_nowait(verdict)
        logger.debug("AskAnswerBus: delivery verdict %s for thread %d", verdict, thread_id)

    def unwatch_delivery(self, thread_id: int) -> None:
        """Stop listening for *thread_id*'s delivery verdict (#804)."""
        self._delivery.pop(thread_id, None)

    # <<REDACTED 3 lines>>
        self._free_text.pop(thread_id, None)
        # <<REDACTED 2 lines>>
    def note_closed(self, thread_id: int, reason: str) -> None:
        """Record WHY *thread_id*'s menu stopped accepting answers (#536).

        A button click that arrives after the menu closed finds no waiter, and
        without this note the view could only guess — it used to guess "the bot
        was restarted" every time, which is wrong for every menu that was
        answered in the terminal, timed out, or was pre-empted. Use one of the
        ``CLOSE_*`` constants.
        """
        now = time.monotonic()
        self._closed[thread_id] = (now, reason)
        if len(self._closed) > _MAX_CLOSE_NOTES:
            for tid, (noted_at, _) in list(self._closed.items()):
                if now - noted_at >= _CLOSE_NOTE_TTL:
                    del self._closed[tid]
        while len(self._closed) > _MAX_CLOSE_NOTES:
            self._closed.pop(next(iter(self._closed)))  # oldest insertion first

    def closed_reason(self, thread_id: int) -> str | None:
        """Return the recorded ``CLOSE_*`` reason for *thread_id*, or None.

        None means "this process has no idea" — which, for a menu posted before
        the process started, is itself the evidence of a restart.
        """
        note = self._closed.get(thread_id)
        if note is None:
            return None
        noted_at, reason = note
        if time.monotonic() - noted_at >= _CLOSE_NOTE_TTL:
            del self._closed[thread_id]
            return None
        return reason

# <<REDACTED 3 lines>>
