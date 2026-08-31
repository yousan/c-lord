"""Issue #628: the harness's compaction scaffolding must not reach Discord.

When the context window fills, Claude Code compacts the conversation and
re-primes the session by injecting the summary as a ``user``-role string event.
On 2026-08-29 03:38 JST that was **14 988 characters** of English, which the
reply chunker split into **9 Discord messages** — every one of them wearing a
👤, i.e. claiming the user had written it.

Same class as ``<task-notification>`` (#380) and the ``<bash-*>`` markers
(#487): harness bookkeeping stored in the ``user`` role.  #380 closed one door
and left this one open.

The fixture ``tests/fixtures/transcripts/i628_compact_continuation.json`` is the
real event from the incident transcript, with identifiers scrubbed and the
summary body replaced by filler of the same order of size.  Its flags, type and
opening sentence are verbatim.
"""

from __future__ import annotations

import json
from pathlib import Path

import pytest

from c_lord.discord_ui.reply_chunker import chunk_discord_content
from c_lord.transcript.formatter import render_event

FIXTURE = (
    Path(__file__).resolve().parents[1]
    / "fixtures"
    / "transcripts"
    / "i628_compact_continuation.json"
)


def _compact_event() -> dict:
    return json.loads(FIXTURE.read_text(encoding="utf-8"))


def _user_str(content: str, **extra) -> dict:
    return {
        "type": "user",
        "uuid": "u1",
        "sessionId": "s",
        "message": {"role": "user", "content": content},
        **extra,
    }


# ── The fixture really is the thing that broke ───────────────────────────


def test_fixture_is_the_real_compact_continuation_event() -> None:
    """Guard the fixture: if it stops looking like the incident, it proves nothing."""
    event = _compact_event()
    assert event["type"] == "user"
    assert event["isCompactSummary"] is True
    assert event["isVisibleInTranscriptOnly"] is True
    body = event["message"]["content"]
    assert body.startswith(
        "This session is being continued from a previous conversation that ran out of context"
    )
    assert body.rstrip().endswith(
        "Pick up the last task as if the break never happened."
    )


def test_the_fixture_would_have_been_nine_discord_messages() -> None:
    """The user saw 9 messages; that is a property of this event's size."""
    body = _compact_event()["message"]["content"]
    assert len(chunk_discord_content(body)) >= 8, (
        "the fixture shrank below the size that produced the 9-message flood"
    )


# ── AC1: the compaction prompt is never posted ───────────────────────────


def test_compact_continuation_is_not_posted_as_user_input() -> None:
    """AC1/AC2: the incident event must not render as a 👤 bubble."""
    rendered = render_event(_compact_event())
    assert rendered is None or rendered.kind != "user_input", (
        "the compaction priming prompt still renders as the user's own words"
    )


def test_compact_continuation_body_never_reaches_discord() -> None:
    """None of the harness prose may survive into whatever is rendered."""
    rendered = render_event(_compact_event())
    assert rendered is not None
    assert "This session is being continued" not in rendered.body
    assert "Continue the conversation from where it left off" not in rendered.body
    assert len(rendered.body) < 200, f"still posting a wall of text: {len(rendered.body)} chars"


# ── AC3: say that it happened, in one line ───────────────────────────────


def test_compaction_is_reported_as_a_single_line() -> None:
    """AC3: not silently dropped — compaction explains why Claude may have
    'forgotten' the earlier part of the thread, so the fact is kept."""
    rendered = render_event(_compact_event())
    assert rendered is not None
    assert rendered.body == "🗜️ コンテキストを圧縮しました"
    assert rendered.kind == "tool_result"
    assert rendered.session_id == _compact_event()["sessionId"]


def test_compact_flags_alone_are_enough() -> None:
    """The harness's own flags decide it, not the English wording.

    ``isVisibleInTranscriptOnly`` literally means "the CLI shows this in the
    transcript view and nowhere else", which is the same judgement Discord needs.
    Matching only on the opening sentence would break the day the CLI rewords it.
    """
    event = _user_str("まったく違う本文", isCompactSummary=True)
    rendered = render_event(event)
    assert rendered is not None and rendered.body == "🗜️ コンテキストを圧縮しました"

    event = _user_str("まったく違う本文", isVisibleInTranscriptOnly=True)
    rendered = render_event(event)
    assert rendered is not None and rendered.body == "🗜️ コンテキストを圧縮しました"


def test_the_opening_sentence_is_still_recognised_without_flags() -> None:
    """An older CLI wrote the block without the flags; catch it by its wording."""
    event = _user_str(
        "This session is being continued from a previous conversation that ran "
        "out of context. The summary below covers the earlier portion.\n\nSummary:\n..."
    )
    rendered = render_event(event)
    assert rendered is not None and rendered.body == "🗜️ コンテキストを圧縮しました"


# ── Slash commands typed in the pane are scaffolding too ─────────────────


def test_slash_command_invocation_is_not_user_input() -> None:
    """``<command-name>`` is the CLI's storage form for a slash command.

    Named in AC1 alongside the compaction prompt, and the same shape as the
    ``<bash-*>`` markers of #487: the TUI renders it, never shows it raw.
    """
    event = _user_str(
        "<command-name>/compact</command-name>\n"
        "            <command-message>compact</command-message>\n"
        "            <command-args></command-args>"
    )
    rendered = render_event(event)
    assert rendered is not None
    assert rendered.kind == "tool_use"
    assert rendered.body == "🔧 /compact"
    assert "<command-name>" not in rendered.body


def test_local_command_stdout_is_not_user_input() -> None:
    """``<local-command-stdout>`` is a slash command's output, ANSI and all."""
    event = _user_str(
        "<local-command-stdout>\x1b[2mCompacted (ctrl+o to see full summary)\x1b[22m"
        "</local-command-stdout>"
    )
    rendered = render_event(event)
    assert rendered is not None
    assert rendered.kind == "tool_result"
    assert rendered.body == "Compacted (ctrl+o to see full summary)"
    assert "\x1b[" not in rendered.body, "raw ANSI escapes reached Discord"
    assert "<local-command-stdout>" not in rendered.body


def test_empty_local_command_stdout_is_dropped() -> None:
    """No empty bubbles (the #380 rule)."""
    assert render_event(_user_str("<local-command-stdout></local-command-stdout>")) is None


# ── AC4: the filter must not be greedy ───────────────────────────────────


@pytest.mark.parametrize(
    "text",
    [
        "compact して",
        "This session is being continued というメッセージが 9 通流れてきました。直してください",
        "`<command-name>` タグって何？",
        "<command-name> の話をしたい",
        "コンテキストを圧縮しました、と出るようにしてほしい",
        "Continue the conversation from where it left off — これを英語で見せられても困る",
    ],
)
def test_a_human_talking_about_the_scaffolding_is_still_a_human(text: str) -> None:
    """AC4: a person quoting these markers is a person, not the harness.

    The same boundary #380 drew: the decision is "does the stored event *start*
    with the marker / carry the harness's flags", never "does it mention it".
    """
    rendered = render_event(_user_str(text))
    assert rendered is not None
    assert rendered.kind == "user_input"
    assert rendered.body == text


def test_an_ordinary_user_message_is_untouched() -> None:
    rendered = render_event(_user_str("次は #628 をお願いします"))
    assert rendered is not None
    assert rendered.kind == "user_input"
    assert rendered.body == "次は #628 をお願いします"


def test_an_assistant_message_is_not_mistaken_for_a_compact_summary() -> None:
    """Only ``user``-role events carry the harness's injections."""
    event = {
        "type": "assistant",
        "uuid": "a1",
        "sessionId": "s",
        "message": {
            "role": "assistant",
            "content": [
                {
                    "type": "text",
                    "text": "This session is being continued … と書かれた行を見つけました",
                }
            ],
        },
    }
    rendered = render_event(event)
    assert rendered is not None
    assert rendered.kind == "assistant_text"
    assert "This session is being continued" in rendered.body
