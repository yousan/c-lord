"""Tests for c_lord.transcript.formatter — JSONL event → Discord-bound text."""

from __future__ import annotations

from c_lord.transcript.formatter import ZWSP_MARKER, render_event


def _assistant(blocks: list[dict]) -> dict:
    return {"type": "assistant", "message": {"role": "assistant", "content": blocks}}


def _user(content) -> dict:
    return {"type": "user", "message": {"role": "user", "content": content}}


def test_assistant_text_block_is_mirrored() -> None:
    ev = _assistant([{"type": "text", "text": "hello world"}])
    out = render_event(ev)
    assert out is not None
    assert out.body == "hello world"
    assert out.kind == "assistant_text"


def test_assistant_tool_use_bash_shows_command() -> None:
    ev = _assistant([{"type": "tool_use", "name": "Bash", "input": {"command": "ls -la"}}])
    out = render_event(ev)
    assert out is not None
    assert "Bash" in out.body and "ls -la" in out.body
    assert out.kind == "tool_use"


def test_assistant_tool_use_read_shows_path() -> None:
    ev = _assistant([{"type": "tool_use", "name": "Read", "input": {"file_path": "/etc/hosts"}}])
    out = render_event(ev)
    assert out is not None
    assert "Read" in out.body and "/etc/hosts" in out.body


def test_assistant_tool_use_grep_shows_pattern() -> None:
    ev = _assistant([{"type": "tool_use", "name": "Grep", "input": {"pattern": "foo.*bar"}}])
    out = render_event(ev)
    assert out is not None
    assert "Grep" in out.body and "foo.*bar" in out.body


def test_assistant_tool_use_unknown_shows_name_only() -> None:
    ev = _assistant([{"type": "tool_use", "name": "MysteryTool", "input": {"x": 1}}])
    out = render_event(ev)
    assert out is not None
    assert "MysteryTool" in out.body


def test_assistant_thinking_block_is_hidden_by_default() -> None:
    ev = _assistant([{"type": "thinking", "thinking": "secret pondering"}])
    assert render_event(ev) is None


def test_assistant_mixed_blocks_concatenated_in_order() -> None:
    ev = _assistant(
        [
            {"type": "thinking", "thinking": "..."},
            {"type": "text", "text": "let me run a command"},
            {"type": "tool_use", "name": "Bash", "input": {"command": "echo hi"}},
        ]
    )
    out = render_event(ev)
    assert out is not None
    assert out.body.index("let me run a command") < out.body.index("echo hi")
    assert "secret" not in out.body  # thinking suppressed


def test_user_tool_result_is_mirrored_as_collapsed_output() -> None:
    ev = _user(
        [
            {
                "type": "tool_result",
                "tool_use_id": "toolu_x",
                "content": "line1\nline2\nline3",
            }
        ]
    )
    out = render_event(ev)
    assert out is not None
    assert out.kind == "tool_result"
    assert "line1" in out.body


def test_user_tool_result_with_list_content_is_concatenated() -> None:
    ev = _user(
        [
            {
                "type": "tool_result",
                "content": [
                    {"type": "text", "text": "part-a "},
                    {"type": "text", "text": "part-b"},
                ],
            }
        ]
    )
    out = render_event(ev)
    assert out is not None
    assert "part-a" in out.body and "part-b" in out.body


def test_user_tool_result_empty_is_skipped() -> None:
    ev = _user([{"type": "tool_result", "content": "   "}])
    assert render_event(ev) is None


def test_user_meta_event_is_skipped() -> None:
    ev = {"type": "user", "isMeta": True, "message": {"role": "user", "content": "auto stuff"}}
    assert render_event(ev) is None


def test_user_input_with_zwsp_marker_is_skipped_as_clord_origin() -> None:
    # c-lord-driven send-keys prefixes input with ZWSP so we don't double-post to Discord.
    ev = _user(f"{ZWSP_MARKER}hello from discord")
    assert render_event(ev) is None


def test_user_input_without_marker_is_mirrored() -> None:
    # External (human typed in pane) input must be mirrored to Discord.
    ev = _user("typed directly in tmux")
    out = render_event(ev)
    assert out is not None
    assert out.kind == "user_input"
    assert "typed directly in tmux" in out.body


# --- Claude Code bash-mode (``! command``) markers (#487) ---
# The CLI stores a ``!``-prefixed pane command as ``user``-role string events
# wrapped in ``<bash-input>`` / ``<bash-stdout>`` / ``<bash-stderr>`` tags. These
# are a bash *execution*, not human-typed input, so they must be classified as
# tool activity (folded into progress.txt in minimal mode) — never posted raw
# as a 👤 user_input bubble.


def test_user_bash_input_marker_classified_as_tool_use_not_raw() -> None:
    ev = _user("<bash-input> ls -la</bash-input>")
    out = render_event(ev)
    assert out is not None
    # A bash execution, mirrored like the Bash tool — not a human 👤 input.
    assert out.kind == "tool_use"
    assert "Bash" in out.body and "ls -la" in out.body
    # The raw storage tag must never reach Discord.
    assert "<bash-input>" not in out.body
    assert "</bash-input>" not in out.body


def test_user_bash_input_real_capture_is_not_leaked_as_user_input() -> None:
    # Verbatim string captured from a real JSONL transcript (the reported bug).
    ev = _user(
        "<bash-input> google-chrome --user-data-dir=$HOME/.clord/discord-evidence-profile "
        '"https://discord.com/channels/1285582352596209694/1514545583459926117"</bash-input>'
    )
    out = render_event(ev)
    assert out is not None
    assert out.kind != "user_input"  # regression guard for the leak
    assert "<bash-input>" not in out.body
    assert "google-chrome" in out.body


def test_user_bash_output_marker_classified_as_tool_result_not_raw() -> None:
    ev = _user("<bash-stdout>hello stdout</bash-stdout><bash-stderr>some warning</bash-stderr>")
    out = render_event(ev)
    assert out is not None
    assert out.kind == "tool_result"
    assert "hello stdout" in out.body
    assert "some warning" in out.body
    for tag in ("<bash-stdout>", "</bash-stdout>", "<bash-stderr>", "</bash-stderr>"):
        assert tag not in out.body


def test_user_bash_output_stderr_only_real_capture() -> None:
    ev = _user(
        "<bash-stdout></bash-stdout><bash-stderr>"
        "[ERROR:ui/aura/env.cc:257] The platform failed to initialize.  Exiting.\n"
        "</bash-stderr>"
    )
    out = render_event(ev)
    assert out is not None
    assert out.kind == "tool_result"
    assert "failed to initialize" in out.body
    assert "<bash-stderr>" not in out.body


def test_user_bash_output_empty_is_dropped() -> None:
    ev = _user("<bash-stdout></bash-stdout><bash-stderr></bash-stderr>")
    assert render_event(ev) is None


def test_user_bash_output_no_output_placeholder_is_dropped() -> None:
    # Claude Code emits this placeholder when a ``!`` command produced no output.
    ev = _user(
        "<bash-stdout>(Bash completed with no output)</bash-stdout><bash-stderr></bash-stderr>"
    )
    assert render_event(ev) is None


def test_framing_event_types_are_skipped() -> None:
    for t in (
        "file-history-snapshot",
        "ai-title",
        "last-prompt",
        "pr-link",
        "permission-mode",
        "queue-operation",
        "attachment",
        "system",
    ):
        assert render_event({"type": t}) is None, t


def test_render_carries_session_id_when_present() -> None:
    ev = _assistant([{"type": "text", "text": "hi"}])
    ev["sessionId"] = "abc-123"
    out = render_event(ev)
    assert out is not None
    assert out.session_id == "abc-123"


def test_render_returns_none_for_unknown_top_level_type() -> None:
    assert render_event({"type": "mystery"}) is None


# --- Claude Code harness task notifications (#380) ---
# When a background task finishes (or is killed), the harness injects a
# ``user``-role *string* event wrapped in ``<task-notification>``. Nobody typed
# it, so mirroring it as a 👤 bubble shows the reader a message they never sent —
# raw XML, including the internal ``<output-file>`` path. Same shape and same
# reasoning as the ``<bash-*>`` markers handled in #487: harness bookkeeping, not
# human input, so it belongs in progress.txt as tool activity.

_REAL_TASK_NOTIFICATION = (
    "<task-notification>\n"
    "<task-id>bdnbr8ftm</task-id>\n"
    "<tool-use-id>toolu_01RC55pzXpo4stAsQhv38oht</tool-use-id>\n"
    "<output-file>/tmp/claude-1000/-home-yousan-c-lord-sessions-1505747831447883806-"
    "1541974007887175760/3baa0184-ca9d-49c2-86e2-4c5298e02403/tasks/bdnbr8ftm.output</output-file>\n"
    "<status>completed</status>\n"
    '<summary>Background command "Final merge attempt for 539" completed (exit code 0)</summary>\n'
    "</task-notification>"
)


def test_task_notification_is_not_mirrored_as_user_input() -> None:
    """The whole complaint: it shows up as something the reader never typed."""
    out = render_event(_user(_REAL_TASK_NOTIFICATION))
    assert out is not None
    assert out.kind != "user_input", "harness bookkeeping was mirrored as a 👤 message"
    assert out.kind == "tool_result"


def test_task_notification_keeps_the_readable_summary() -> None:
    out = render_event(_user(_REAL_TASK_NOTIFICATION))
    assert out is not None
    assert 'Background command "Final merge attempt for 539" completed' in out.body


def test_task_notification_drops_internal_ids_and_paths() -> None:
    """Measured 2026-08-27: every leaked message exposed a filesystem path."""
    out = render_event(_user(_REAL_TASK_NOTIFICATION))
    assert out is not None
    assert "/tmp/claude-" not in out.body, out.body
    assert "bdnbr8ftm" not in out.body, out.body
    assert "toolu_01RC55pzXpo4stAsQhv38oht" not in out.body, out.body


def test_task_notification_never_leaks_raw_tags() -> None:
    out = render_event(_user(_REAL_TASK_NOTIFICATION))
    assert out is not None
    assert "<task-notification>" not in out.body
    assert "<summary>" not in out.body
    assert "<output-file>" not in out.body


def test_killed_task_notification_says_so() -> None:
    ev = _user(
        "<task-notification>\n"
        "<task-id>x1</task-id>\n"
        "<status>killed</status>\n"
        '<summary>Background command "watch logs" was stopped</summary>\n'
        "</task-notification>"
    )
    out = render_event(ev)
    assert out is not None
    assert out.kind == "tool_result"
    assert "was stopped" in out.body


def test_task_notification_without_summary_falls_back_to_status() -> None:
    ev = _user(
        "<task-notification>\n<task-id>x1</task-id>\n<status>failed</status>\n</task-notification>"
    )
    out = render_event(ev)
    assert out is not None
    assert "failed" in out.body
    assert "x1" not in out.body


def test_empty_task_notification_is_dropped() -> None:
    """Nothing readable in it → do not put an empty bubble in the thread."""
    assert render_event(_user("<task-notification>\n</task-notification>")) is None


def test_malformed_task_notification_still_does_not_leak_tags() -> None:
    """#487's fallback rule: strip rather than leak the raw storage form."""
    out = render_event(_user("<task-notification>oops no closing tag"))
    assert out is None or ("<task-notification>" not in out.body and out.kind != "user_input")


def test_real_user_input_is_still_mirrored() -> None:
    """Regression guard: a human message must still read as one."""
    out = render_event(_user("これは人間が打ったメッセージです"))
    assert out is not None
    assert out.kind == "user_input"
    assert out.body == "これは人間が打ったメッセージです"


def test_zwsp_echo_of_a_pasted_task_notification_is_still_dropped() -> None:
    """A human pasting one must not be re-rendered as harness activity.

    c-lord marks its own send-keys echo with ZWSP so it is not mirrored back.
    The #380 check sits before that guard in ``_render_user``, so pin the
    ordering: a ZWSP-marked echo stays an echo even when its text happens to be
    a task notification.
    """
    ev = _user(
        ZWSP_MARKER
        + "<task-notification>\n<summary>pasted by a human</summary>\n</task-notification>"
    )
    assert render_event(ev) is None


def test_message_merely_mentioning_the_tag_is_still_user_input() -> None:
    """Talking *about* the tag is a human message, not harness bookkeeping."""
    out = render_event(_user("`<task-notification>` が 👤 で流れる件、直しました"))
    assert out is not None
    assert out.kind == "user_input"
