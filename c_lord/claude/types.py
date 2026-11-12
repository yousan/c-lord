# <<REDACTED 7 lines>>
from typing import TYPE_CHECKING, Any, Literal, NamedTuple
# <<REDACTED 57 lines>>
#: How the open TUI menu lets a free-text answer be typed (#650).  The two
#: layouts Claude Code draws take *different* keystrokes, and sending the wrong
#: ones answers the tool with "(No answer provided)" — losing what the user
#: wrote.  Read off the pane, never assumed.
#:
#: - ``row``   — classic layout: a "Type something." row below the options.
#:               Type onto the highlighted row (``Down`` × N → text → Enter).
#: - ``notes`` — preview layout (any option carries a ``preview``): no such row;
#:               a ``Notes:`` field opened with ``n`` (``n`` → text → Enter).
#: - ``none``  — neither affordance is on screen: the menu takes no free text.
FREE_TEXT_ROW = "row"
FREE_TEXT_NOTES = "notes"
FREE_TEXT_NONE = "none"

FreeTextMode = Literal["row", "notes", "none"]


# <<REDACTED 16 lines>>
    # Whether to offer the free-text "✏️ Other" affordance in AskView.
    # True for AskUserQuestion (which has a "Type something." row to type onto).
    # Plan-approval menus (#251) set this False: their free-text option ("Tell
    # Claude what to change") uses a different keystroke flow, so a generic
    # Other modal would mis-send keys into the open TUI menu.
    allow_other: bool = True
    # #650: which keystrokes deliver free text on THIS menu — see FreeTextMode.
    # ``allow_other`` says whether to offer the ✏️ Other button at all; this says
    # how to type the answer once it arrives.  Defaults to the classic row so a
    # hand-built AskQuestion keeps the pre-#650 behaviour.
    free_text_mode: FreeTextMode = FREE_TEXT_ROW
    # #399: the assistant prose spoken directly above the menu (経緯・推し),
    # extracted from the pane. The CLI buffers the jsonl chunk containing the
    # menu until resolution, so without this the question reaches Discord with
    # zero decision context. Empty when no clean prose block sits above the
    # menu (tool blocks / chrome are never carried — see _extract_pane_context).
    context: str = ""
# <<REDACTED 2 lines>>
def ask_question_to_dict(question: AskQuestion) -> dict[str, Any]:
    """Serialise *question* for the ``pending_asks`` restart ledger (#671).

    Everything an answer needs to reach the right place is carried, not just
    what a menu needs to be *drawn*: the option order (answers are typed as
    ``Down × index``, so a reordered list selects the wrong thing),
    ``allow_other`` (whether the ✏️ Other affordance exists at all) and
    ``free_text_mode`` (which keystrokes deliver typed text — sending the wrong
    one answered "(No answer provided)" in #650).

    ``context`` is deliberately dropped: the 経緯 prose was already posted as its
    own message when the menu went up, and it can be long. The restored menu
    re-uses the message that is still on screen, so nothing needs re-rendering.
    """
    return {
        "question": question.question,
        "header": question.header,
        "multi_select": question.multi_select,
        "allow_other": question.allow_other,
        "free_text_mode": question.free_text_mode,
        "options": [{"label": o.label, "description": o.description} for o in question.options],
    }


def ask_question_from_dict(raw: dict[str, Any]) -> AskQuestion:
    """Rebuild an :class:`AskQuestion` from :func:`ask_question_to_dict`.

    Tolerant of rows written by older versions (which stored only
    question/header/multi_select/options): the missing keys fall back to the
    dataclass defaults rather than raising, because a row that cannot be parsed
    would silently become an un-restored — that is, dead — menu.
    """
    mode = raw.get("free_text_mode") or FREE_TEXT_ROW
    return AskQuestion(
        question=raw.get("question", ""),
        header=raw.get("header") or "",
        multi_select=bool(raw.get("multi_select", False)),
        options=[
            AskOption(label=o.get("label", ""), description=o.get("description") or "")
            for o in raw.get("options", [])
        ],
        allow_other=bool(raw.get("allow_other", True)),
        free_text_mode=mode if mode in ("row", "notes", "none") else FREE_TEXT_ROW,
    )


# <<REDACTED 69 lines>>
class UsageLimit(NamedTuple):
    """A plan limit Claude reported instead of answering (#631).

    ``scope`` is the limit's own name as the CLI printed it ("weekly limit",
    "session limit", "Opus limit", ...) and ``resets_at`` the recovery time it
    printed alongside, verbatim ("Aug 29, 4pm (Asia/Tokyo)"), or None when the
    banner carried none.  Kept verbatim on purpose: re-deriving a local time
    from a string the CLI already localised is a way to be confidently wrong
    about the one fact the user actually needs.
    """

    scope: str
    resets_at: str | None
    line: str


# <<REDACTED 14 lines>>
    # AskUserQuestion menu parsed from the tmux pane (jsonl/tmux mode, #166).
    # Distinct from ``ask_questions`` (SDK-mode resume-via-prompt): a pane_ask is
    # answered in-place by sending menu keystrokes back to the still-open TUI.
    pane_ask: AskQuestion | None = None
    # <<REDACTED 4 lines>>
    unknown_tui_prompt: str | None = None
    # <<REDACTED 5 lines>>
    # #631: the plan limit that ended this turn, when one did. Structured
    # rather than sniffed back out of ``error`` — the reset time is the one
    # fact the reader needs and it must survive to the embed intact.
    usage_limit: UsageLimit | None = None
    # <<REDACTED 11 lines>>
    """Parse AskUserQuestion tool input into a list of AskQuestion objects.

    The tool input is also where the menu's TUI *layout* is decided (#650):
    Claude Code draws the preview layout — no "Type something." row, free text
    typed into a ``Notes:`` field — exactly when the question is single-select
    and at least one option carries a ``preview``. Deriving
    ``free_text_mode`` here matters for the transcript-mirror bridge, which
    builds its menu from this input and never sees the pane; without it the
    answer keystrokes go to the wrong affordance and the user's typed sentence
    is answered as "(No answer provided)".
    """
    # <<REDACTED 3 lines>>
        raw_options = q.get("options", [])
        # <<REDACTED 5 lines>>
            for o in raw_options
            if o.get("label")
        ]
        multi_select = bool(q.get("multiSelect", False))
        has_preview = any(o.get("preview") is not None for o in raw_options)
        # <<REDACTED 4 lines>>
                multi_select=multi_select,
                options=options,
                free_text_mode=(
                    FREE_TEXT_NOTES if has_preview and not multi_select else FREE_TEXT_ROW
                ),
            # <<REDACTED 21 lines>>


# <<REDACTED 19 lines>>
