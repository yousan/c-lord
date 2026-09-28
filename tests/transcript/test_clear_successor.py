"""The mirror follows a ``/clear`` typed *anywhere* — c-lord's or a human's (#803).

Production, 2026-09-28 10:09 JST, thread 1511558219011592312: someone typed
``/clear`` straight into the tmux pane.  Claude Code started
``4cccc5c4-….jsonl``; the claim still named ``95d7e727-…``, so rule 0 kept the
mirror on a transcript nobody wrote to again, and the 10:31 final answer never
reached Discord.  ``/clear`` via Discord cannot be the only path that moves the
claim — the resolver has to notice the successor itself.

What makes a file the successor (and not an old clear transcript lying around):

* it **opens with** the ``/clear`` command event, and
* that clear happened **after the claimed session started** and **no earlier
  than the claimed transcript's last write** (give or take a few seconds —
  Claude Code appends a few bookkeeping lines to the old file as it switches).
  A live session keeps writing, so an older clear can never satisfy this.
"""

from __future__ import annotations

import json
import os
import uuid
from datetime import datetime, timezone
from pathlib import Path

from c_lord.transcript.claim import clear_successor, read_claim, write_claim
from c_lord.transcript.resolver import ThreadSessionResolver


def _iso(ts: float) -> str:
    return datetime.fromtimestamp(ts, tz=timezone.utc).strftime("%Y-%m-%dT%H:%M:%S.%f")[:-3] + "Z"


def _session(project_dir: Path, *, started: float, last_write: float) -> str:
    sid = str(uuid.uuid4())
    path = project_dir / f"{sid}.jsonl"
    line = {
        "type": "user",
        "sessionId": sid,
        "timestamp": _iso(started),
        "message": {"role": "user", "content": "hello"},
    }
    path.write_text(json.dumps(line) + "\n", encoding="utf-8")
    os.utime(path, (last_write, last_write))
    return sid


def _cleared(project_dir: Path, *, at: float, last_write: float | None = None) -> str:
    sid = str(uuid.uuid4())
    path = project_dir / f"{sid}.jsonl"
    lines = [
        {"type": "mode", "mode": "normal", "sessionId": sid},
        {"type": "user", "timestamp": _iso(at), "message": {"role": "user", "content": "<x>"}},
        {
            "type": "user",
            "sessionId": sid,
            "timestamp": _iso(at),
            "message": {
                "role": "user",
                "content": "<command-name>/clear</command-name>\n"
                "            <command-message>clear</command-message>",
            },
        },
    ]
    path.write_text("".join(json.dumps(x, separators=(",", ":")) + "\n" for x in lines))
    mtime = last_write if last_write is not None else at
    os.utime(path, (mtime, mtime))
    return sid


T0 = 1_790_000_000.0


class TestClearSuccessor:
    def test_a_clear_after_the_last_write_is_the_successor(self, tmp_path: Path) -> None:
        old = _session(tmp_path, started=T0, last_write=T0 + 600)
        new = _cleared(tmp_path, at=T0 + 600.5, last_write=T0 + 900)

        assert clear_successor(tmp_path, old) == new

    def test_old_file_touched_just_after_the_clear_still_counts(self, tmp_path: Path) -> None:
        """Claude Code appends bookkeeping to the old transcript as it switches."""
        old = _session(tmp_path, started=T0, last_write=T0 + 602)
        new = _cleared(tmp_path, at=T0 + 600)

        assert clear_successor(tmp_path, old) == new

    def test_an_older_clear_is_not_a_successor(self, tmp_path: Path) -> None:
        """A clear transcript from before this session must not take the mirror back."""
        _cleared(tmp_path, at=T0 - 3600, last_write=T0 - 60)
        claimed = _session(tmp_path, started=T0, last_write=T0 + 600)

        assert clear_successor(tmp_path, claimed) is None

    def test_a_live_session_is_not_replaced(self, tmp_path: Path) -> None:
        """The claimed session kept writing after that clear — the clear was not its."""
        claimed = _session(tmp_path, started=T0, last_write=T0 + 600)
        _cleared(tmp_path, at=T0 + 300)

        assert clear_successor(tmp_path, claimed) is None

    def test_two_quick_clears_do_not_flip_back(self, tmp_path: Path) -> None:
        first = _session(tmp_path, started=T0, last_write=T0 + 10)
        s1 = _cleared(tmp_path, at=T0 + 10, last_write=T0 + 11)
        s2 = _cleared(tmp_path, at=T0 + 11, last_write=T0 + 12)

        assert clear_successor(tmp_path, first) == s2
        assert clear_successor(tmp_path, s2) is None
        assert clear_successor(tmp_path, s1) == s2

    def test_a_plain_new_file_is_not_a_successor(self, tmp_path: Path) -> None:
        """A ``claude -p`` sub-invocation writing meanwhile (#627)."""
        claimed = _session(tmp_path, started=T0, last_write=T0 + 600)
        _session(tmp_path, started=T0 + 601, last_write=T0 + 700)

        assert clear_successor(tmp_path, claimed) is None


class TestResolverFollowsAPaneClear:
    def test_resolver_moves_to_the_successor_and_moves_the_claim(self, tmp_path: Path) -> None:
        """The production case: ``/clear`` typed in the pane, not via Discord."""
        old = _session(tmp_path, started=T0, last_write=T0 + 600)
        write_claim(tmp_path, old)
        resolver = ThreadSessionResolver(tmp_path)
        assert resolver.resolve() == tmp_path / f"{old}.jsonl"

        new = _cleared(tmp_path, at=T0 + 600.2, last_write=T0 + 700)

        assert resolver.resolve() == tmp_path / f"{new}.jsonl"
        # ...and a later --resume opens the cleared conversation, not the old one.
        assert read_claim(tmp_path) == new

    def test_resolver_recovers_a_clear_that_happened_before_it_started(
        self, tmp_path: Path
    ) -> None:
        """A bot restart (or this fix's deploy) must heal a thread already stuck."""
        old = _session(tmp_path, started=T0, last_write=T0 + 600)
        write_claim(tmp_path, old)
        new = _cleared(tmp_path, at=T0 + 600.2, last_write=T0 + 700)

        assert ThreadSessionResolver(tmp_path).resolve() == tmp_path / f"{new}.jsonl"
        assert read_claim(tmp_path) == new
