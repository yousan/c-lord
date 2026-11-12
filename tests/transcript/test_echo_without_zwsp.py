"""Discord input must not come back as 👤 when the CLI strips the ZWSP (#808).

The mirror used to tell c-lord's own input from human pane input by one thing:
a zero-width space c-lord prefixes to everything it types. Claude Code 2.1.278+
removes that character before writing the ``user`` event, so every Discord
message in every thread came back from the bot as a 👤 line.

#773 already stopped trusting the ZWSP for *which transcript* belongs to a
thread. This is the other half: *whose input* an event is must not depend on
what the CLI does to the input either. c-lord records what it typed (the #682
``pane_echo`` registry) on every path that types a prompt, and the mirror asks
that record — the ZWSP is kept only as an additional, backward-compatible sign.
"""

from __future__ import annotations

import asyncio
import json
import os
from pathlib import Path
from unittest.mock import MagicMock, patch

import pytest

from c_lord.tmux import TmuxSessionManager
from c_lord.transcript.formatter import ZWSP_MARKER
from c_lord.transcript.mirror import TranscriptMirror
from c_lord.transcript.pane_echo import pane_echo

from .helpers import clord_transcript

TID = 808


@pytest.fixture(autouse=True)
def _clean_registry():
    pane_echo.clear()
    yield
    pane_echo.clear()


# ── producers: every path that types a prompt records it ────────────────


def _mgr() -> TmuxSessionManager:
    mgr = TmuxSessionManager(mapping_path="")
    mgr._available = True
    mgr.session_name = "t"
    mgr._find_window_for_thread = lambda _tid: "w1"  # type: ignore[method-assign]
    return mgr


def _send_input(text: str) -> bool:
    with (
        patch("c_lord.tmux._run", return_value=MagicMock(returncode=0, stdout="")),
        patch.object(TmuxSessionManager, "_confirm_submitted", return_value=True),
    ):
        return _mgr().send_input(TID, text)


def test_send_input_records_what_it_typed() -> None:
    assert _send_input("今のこのtmuxのセション名って何？") is True
    assert pane_echo.consume_match(TID, "今のこのtmuxのセション名って何？") is True


def test_send_input_records_a_multiline_message_with_an_attachment() -> None:
    text = (
        "あとDiscordの入力がエコーバックしている？\n\n"
        "--- Attached file: image.png (145.5 KB) ---\n"
        "Saved to: /home/u/c-lord-sessions/1/2/.clord-attachments/image.png"
    )
    assert _send_input(text) is True
    # The CLI writes the same text back, minus the marker.
    assert pane_echo.consume_match(TID, text) is True


def test_send_input_that_never_reached_the_pane_records_nothing() -> None:
    with patch("c_lord.tmux._run", return_value=MagicMock(returncode=1, stdout="", stderr="x")):
        _mgr().send_input(TID, "届かなかった発言")
    assert pane_echo.consume_match(TID, "届かなかった発言") is False


def test_cold_start_prompt_is_recorded() -> None:
    with patch("c_lord.tmux._run", return_value=MagicMock(returncode=0, stdout="")):
        assert _mgr().start_claude(TID, "最初のメッセージ", "sonnet") is True
    assert pane_echo.consume_match(TID, "最初のメッセージ") is True


def test_wake_without_a_prompt_records_nothing() -> None:
    with patch("c_lord.tmux._run", return_value=MagicMock(returncode=0, stdout="")):
        assert _mgr().start_claude(TID, None, "sonnet") is True
    assert pane_echo._entries == {}


def test_a_message_queued_behind_a_long_turn_is_still_recognised() -> None:
    """The CLI writes a queued message only when it dequeues it (real transcripts:
    ``queue-operation`` enqueue → dequeue → ``user``), which is after the running
    turn ends — routinely far longer than the 5 minutes a menu answer needs."""
    with patch("c_lord.transcript.pane_echo.time.monotonic", return_value=0.0):
        assert _send_input("作業中に送った追加の指示") is True
    with patch("c_lord.transcript.pane_echo.time.monotonic", return_value=45 * 60.0):
        assert pane_echo.consume_match(TID, "作業中に送った追加の指示") is True


def test_many_messages_queued_behind_one_turn_are_all_recognised() -> None:
    for i in range(12):
        assert _send_input(f"追加の指示 {i}") is True
    assert all(pane_echo.consume_match(TID, f"追加の指示 {i}") for i in range(12))


# ── consumer: the mirror, against a CLI that dropped the marker ────────


def _fresh_jsonl(tmp_path: Path) -> tuple[Path, Path]:
    project = tmp_path / "proj"
    project.mkdir()
    jsonl = project / "s.jsonl"
    clord_transcript(jsonl)
    os.utime(jsonl, (1, 1))
    return project, jsonl


def _user(content: str) -> dict:
    return {"type": "user", "message": {"role": "user", "content": content}}


async def _mirror(tmp_path: Path, events: list[dict]) -> list[str]:
    project, jsonl = _fresh_jsonl(tmp_path)
    posted: list[str] = []

    async def sink(text: str) -> None:
        posted.append(text)

    mirror = TranscriptMirror(thread_id=TID, project_dir=project, sink=sink, poll_interval=0.05)
    mirror.start()
    try:
        await asyncio.sleep(0.15)
        with jsonl.open("a", encoding="utf-8") as f:
            for e in events:
                f.write(json.dumps(e, ensure_ascii=False) + "\n")
        await asyncio.sleep(0.3)
    finally:
        await mirror.stop()
    return posted


async def test_discord_message_is_not_echoed_when_the_cli_strips_the_marker(
    tmp_path: Path,
) -> None:
    """AC1: the 2.1.278+ transcript carries no ZWSP — still no 👤 re-post."""
    text = "今のこのtmuxのセション名って何？"
    assert _send_input(text) is True
    posted = await _mirror(tmp_path, [_user(text)])
    assert not [p for p in posted if "セション名" in p], posted


async def test_human_pane_input_is_still_mirrored(tmp_path: Path) -> None:
    """AC2: what a person types straight into the pane still shows as 👤."""
    assert _send_input("Discord からの発言") is True
    posted = await _mirror(tmp_path, [_user("ターミナルで直接打った発言")])
    assert any("👤" in p and "ターミナルで直接打った発言" in p for p in posted), posted


async def test_old_cli_marked_echo_retires_the_record(tmp_path: Path) -> None:
    """AC4: a CLI that keeps the ZWSP behaves as before — and the record the
    marked echo made redundant is spent, so a person who later types the same
    words in the pane is not silenced by it."""
    assert _send_input("はい") is True
    posted = await _mirror(tmp_path, [_user(f"{ZWSP_MARKER}はい"), _user("はい")])
    assert [p for p in posted if "はい" in p] == ["👤 はい"], posted
