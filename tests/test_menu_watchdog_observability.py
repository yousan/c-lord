"""A menu the watchdog can see but cannot parse must not vanish silently (#359).

``MenuWatchdogLoop._maybe_bridge_open_menu`` gates on a cheap signature test
(``"Chat about this"`` / ``"Would you like to proceed?"``) and only then pays for
a full capture and parse::

    question = _parse_ask_from_pane(norm) or _parse_plan_from_pane(norm)
    if question is None:
        return          # <- nothing logged, nothing kept

So a pane that *is* showing a menu but whose layout defeats the parser is
abandoned on every 60-second tick, forever, leaving no trace anywhere: no log
line, no Discord message, no captured pane. From the user's side that is exactly
#359 — "the buttons never appeared" — and from ours it is unfalsifiable, which is
why the ~57% figure could never be attributed.

This is the one remaining silent drop in the watchdog. #579's failure cap already
logs and tells the user when a *post* keeps failing; this is the case where we
never get as far as having something to post.

The fixture is a real menu capture scrolled the way the alternate screen scrolls
it — Claude Code keeps no scrollback, so a menu whose question and early options
have moved off the top leaves only its tail on screen (the same mechanism #549
fixed for missing context). The signature survives; the options do not.
"""

from __future__ import annotations

from pathlib import Path

import pytest

from c_lord.claude.tmux_runner import (
    _ASK_SIGNATURE,
    _normalize_capture,
    _parse_ask_from_pane,
    _parse_plan_from_pane,
)

_FIX = Path(__file__).parent / "fixtures" / "panes"
UNPARSABLE = (_FIX / "ask_menu_scrolled_unparsable.txt").read_text()
PARSABLE = (_FIX / "ask_user_question_3options.txt").read_text()


def _parses(pane: str) -> bool:
    norm = _normalize_capture(pane)
    return (_parse_ask_from_pane(norm) or _parse_plan_from_pane(norm)) is not None


class TestFixtureIsTheRealThing:
    """Pin the fixture's defining property, so it cannot rot into a no-op."""

    def test_unparsable_fixture_still_looks_like_a_menu(self) -> None:
        assert _ASK_SIGNATURE in _normalize_capture(UNPARSABLE), (
            "the watchdog's cheap gate must still fire on this pane — otherwise "
            "the fixture no longer exercises the silent-drop path"
        )

    def test_unparsable_fixture_does_not_parse(self) -> None:
        assert not _parses(UNPARSABLE), (
            "fixture must reproduce the bug: signature present, parse fails"
        )

    def test_control_fixture_parses(self) -> None:
        assert _parses(PARSABLE), "control: an ordinary menu must still parse"


class TestUnparsableMenuIsRecorded:
    """The observation itself: seeing-but-not-parsing must leave evidence."""

    def test_records_pane_and_reports(self, tmp_path: Path) -> None:
        from c_lord.thread_state_sync import record_unparsable_menu

        saved = record_unparsable_menu(
            thread_id=4242,
            session_name="c-lord",
            window_name="w7",
            pane_text=UNPARSABLE,
            directory=tmp_path,
        )
        assert saved is not None, "an unparsable menu must be captured for diagnosis"
        assert saved.exists()
        assert _ASK_SIGNATURE in saved.read_text(), "the captured pane must be the real thing"

    def test_repeat_ticks_do_not_pile_up(self, tmp_path: Path) -> None:
        """The sweep revisits the same stuck menu every 60s — keep one copy.

        Without this a single stuck menu writes 1,440 files a day, which is the
        same "loop buries the signal" failure #579 had to undo.
        """
        from c_lord.thread_state_sync import record_unparsable_menu

        first = record_unparsable_menu(
            thread_id=4242, session_name="s", window_name="w",
            pane_text=UNPARSABLE, directory=tmp_path,
        )
        again = record_unparsable_menu(
            thread_id=4242, session_name="s", window_name="w",
            pane_text=UNPARSABLE, directory=tmp_path,
        )
        assert first is not None
        assert again is None, "the same pane must not be captured twice"
        assert len(list(tmp_path.glob("*.txt"))) == 1

    def test_a_different_pane_is_captured_separately(self, tmp_path: Path) -> None:
        from c_lord.thread_state_sync import record_unparsable_menu

        record_unparsable_menu(
            thread_id=1, session_name="s", window_name="w",
            pane_text=UNPARSABLE, directory=tmp_path,
        )
        other = record_unparsable_menu(
            thread_id=1, session_name="s", window_name="w",
            pane_text=UNPARSABLE + "\na different frame\n", directory=tmp_path,
        )
        assert other is not None
        assert len(list(tmp_path.glob("*.txt"))) == 2

    def test_logs_with_thread_context(
        self, tmp_path: Path, caplog: pytest.LogCaptureFixture
    ) -> None:
        """CLAUDE.md log discipline: greppable by ``thread=<id>``."""
        from c_lord.thread_state_sync import record_unparsable_menu

        with caplog.at_level("WARNING", logger="c_lord.thread_state_sync"):
            record_unparsable_menu(
                thread_id=99887766, session_name="c-lord", window_name="w3",
                pane_text=UNPARSABLE, directory=tmp_path,
            )
        joined = "\n".join(r.getMessage() for r in caplog.records)
        assert "thread=99887766" in joined
        assert "parse" in joined.lower() or "解析" in joined

    def test_an_unwritable_directory_never_breaks_the_sweep(self, tmp_path: Path) -> None:
        """Diagnostics must never take the watchdog down."""
        from c_lord.thread_state_sync import record_unparsable_menu

        blocked = tmp_path / "nope"
        blocked.write_text("I am a file, not a directory")
        assert (
            record_unparsable_menu(
                thread_id=1, session_name="s", window_name="w",
                pane_text=UNPARSABLE, directory=blocked,
            )
            is None
        )

    def test_capture_store_is_bounded(self, tmp_path: Path) -> None:
        """A long-running bot must not fill the disk with captures."""
        from c_lord.thread_state_sync import _MAX_UNPARSABLE_CAPTURES, record_unparsable_menu

        for i in range(_MAX_UNPARSABLE_CAPTURES + 5):
            record_unparsable_menu(
                thread_id=i, session_name="s", window_name="w",
                pane_text=UNPARSABLE + f"\nframe {i}\n", directory=tmp_path,
            )
        assert len(list(tmp_path.glob("*.txt"))) <= _MAX_UNPARSABLE_CAPTURES
