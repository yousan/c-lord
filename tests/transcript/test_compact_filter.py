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
    # #914: a notice for the thread, not a tool result folded into progress.txt.
    assert rendered.kind == "notice"
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


# ── #914: the one line reaches the thread, not just progress.txt ─────────


async def _mirror_compaction(tmp_path: Path, *before: dict, after: tuple[dict, ...] = ()):
    """Run a minimal-mode mirror over a compaction; return (sink, file, progress posts)."""
    import asyncio
    import os

    from c_lord.discord_ui.turn_progress import TurnProgress
    from c_lord.transcript.mirror import TranscriptMirror

    from .helpers import clord_transcript

    project = tmp_path / "proj"
    project.mkdir()
    jsonl = clord_transcript(project / "s.jsonl")
    os.utime(jsonl, (1, 1))
    sink_calls: list[str] = []
    file_calls: list[tuple[str, str]] = []
    progress_posts: list[str] = []
    clock = {"t": 1000.0}

    async def sink(text: str) -> None:
        sink_calls.append(text)

    async def file_sink(text: str, path: str) -> None:
        file_calls.append((text, Path(path).read_text(encoding="utf-8")))

    async def p_post(text: str):
        progress_posts.append(text)
        return "m"

    async def p_noop(*_a: object) -> None:
        return None

    mirror = TranscriptMirror(
        thread_id=914,
        project_dir=project,
        sink=sink,
        file_sink=file_sink,
        verbosity="minimal",
        poll_interval=0.05,
        idle_flush_seconds=0.05,
        progress=TurnProgress(
            post=p_post, edit=p_noop, delete=p_noop, clock=lambda: clock["t"]
        ),
    )
    mirror.start()
    try:
        await asyncio.sleep(0.15)
        with jsonl.open("a", encoding="utf-8") as f:
            for event in (*before, _compact_event(), *after):
                f.write(json.dumps(event, ensure_ascii=False) + "\n")
        await asyncio.sleep(0.3)
        clock["t"] += 6000.0  # an idle thread stays idle
        await asyncio.sleep(0.3)
    finally:
        await mirror.stop()
    return sink_calls, file_calls, progress_posts


async def test_idle_compaction_posts_one_line_to_the_thread(tmp_path: Path) -> None:
    """AC1/AC3: compacted while waiting — exactly one 🗜️ line, nothing else."""
    sink_calls, file_calls, progress_posts = await _mirror_compaction(tmp_path)
    assert [c for c in sink_calls if "🗜️" in c] == ["-# 🗜️ コンテキストを圧縮しました"]
    assert file_calls == []
    # Not mistaken for tool activity: no "⏳ 待機中" on an idle thread.
    assert progress_posts == []


async def test_compaction_mid_turn_is_in_the_thread_not_the_attachment(tmp_path: Path) -> None:
    """AC1/AC2: in a turn, the line is posted and the summary text goes nowhere."""
    tool = {
        "type": "assistant",
        "message": {
            "role": "assistant",
            "content": [{"type": "tool_use", "name": "Bash", "input": {"command": "ls"}}],
        },
    }
    answer = {
        "type": "assistant",
        "message": {"role": "assistant", "content": [{"type": "text", "text": "続きです"}]},
    }
    sink_calls, file_calls, _ = await _mirror_compaction(
        tmp_path, tool, after=(answer, {"type": "system", "subtype": "turn_duration"})
    )
    assert [c for c in sink_calls if "🗜️" in c] == ["-# 🗜️ コンテキストを圧縮しました"]
    for text, progress in file_calls:
        assert "🗜️" not in progress
        assert "This session is being continued" not in text + progress
    assert not any("This session is being continued" in c for c in sink_calls)
