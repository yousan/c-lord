"""Tests for TurnProgress — the silence filler during a long turn (#539).

Measured on production (17 threads / 25 turns / 147 gaps, 2026-08-26): visible
output already arrives every ~39s median, so a *periodic* progress line would
stack noise on top of adequate output. What hurts is the tail — 15% of gaps
exceed 2 minutes, 7% exceed 5 minutes. So this component only speaks when the
thread has actually gone quiet, and gets out of the way the moment real output
returns.
"""

from __future__ import annotations

import pytest

from c_lord.discord_ui.turn_progress import TurnProgress


class _Recorder:
    """Stands in for the post/edit/delete sinks and records what happened."""

    def __init__(self) -> None:
        self.posts: list[str] = []
        self.edits: list[tuple[object, str]] = []
        self.deletes: list[object] = []
        self._next = 0
        self.post_error: Exception | None = None
        self.delete_error: Exception | None = None

    async def post(self, text: str):
        if self.post_error is not None:
            raise self.post_error
        self.posts.append(text)
        self._next += 1
        return f"msg{self._next}"

    async def edit(self, handle, text: str) -> None:
        self.edits.append((handle, text))

    async def delete(self, handle) -> None:
        if self.delete_error is not None:
            raise self.delete_error
        self.deletes.append(handle)


class _Clock:
    def __init__(self) -> None:
        self.t = 1000.0

    def __call__(self) -> float:
        return self.t

    def advance(self, seconds: float) -> None:
        self.t += seconds


def _make(rec: _Recorder, clock: _Clock, **kw) -> TurnProgress:
    return TurnProgress(
        post=rec.post,
        edit=rec.edit,
        delete=rec.delete,
        clock=clock,
        **kw,
    )


class TestQuietThreshold:
    @pytest.mark.asyncio
    async def test_silent_below_threshold(self) -> None:
        """A gap shorter than the threshold must produce nothing at all."""
        rec, clock = _Recorder(), _Clock()
        p = _make(rec, clock, quiet_seconds=90.0)
        p.begin_turn()

        clock.advance(89.0)
        await p.tick()

        assert rec.posts == []

    @pytest.mark.asyncio
    async def test_posts_once_past_threshold(self) -> None:
        """Past the threshold exactly one message appears, however often we tick."""
        rec, clock = _Recorder(), _Clock()
        p = _make(rec, clock, quiet_seconds=90.0, update_seconds=15.0)
        p.begin_turn()

        clock.advance(91.0)
        await p.tick()
        await p.tick()
        clock.advance(5.0)
        await p.tick()

        assert len(rec.posts) == 1

    @pytest.mark.asyncio
    async def test_not_armed_never_posts(self) -> None:
        """Outside a turn the thread must stay silent no matter how long it idles."""
        rec, clock = _Recorder(), _Clock()
        p = _make(rec, clock, quiet_seconds=90.0)

        clock.advance(6000.0)
        await p.tick()

        assert rec.posts == []


class TestUpdatesInPlace:
    @pytest.mark.asyncio
    async def test_updates_by_editing_not_posting(self) -> None:
        """AC2: the thread grows by at most one message, then only edits."""
        rec, clock = _Recorder(), _Clock()
        p = _make(rec, clock, quiet_seconds=90.0, update_seconds=15.0)
        p.begin_turn()

        clock.advance(91.0)
        await p.tick()
        for _ in range(6):
            clock.advance(16.0)
            await p.tick()

        assert len(rec.posts) == 1, "a second message was posted instead of editing"
        assert len(rec.edits) == 6
        assert all(h == "msg1" for h, _ in rec.edits)

    @pytest.mark.asyncio
    async def test_does_not_edit_faster_than_interval(self) -> None:
        rec, clock = _Recorder(), _Clock()
        p = _make(rec, clock, quiet_seconds=90.0, update_seconds=15.0)
        p.begin_turn()

        clock.advance(91.0)
        await p.tick()
        clock.advance(5.0)
        await p.tick()

        assert rec.edits == []


class TestGetsOutOfTheWay:
    @pytest.mark.asyncio
    async def test_removed_when_real_output_returns(self) -> None:
        """The filler exists only while the thread is quiet."""
        rec, clock = _Recorder(), _Clock()
        p = _make(rec, clock, quiet_seconds=90.0)
        p.begin_turn()

        clock.advance(91.0)
        await p.tick()
        assert len(rec.posts) == 1

        await p.note_output()

        assert rec.deletes == ["msg1"]

    @pytest.mark.asyncio
    async def test_output_resets_the_silence_window(self) -> None:
        rec, clock = _Recorder(), _Clock()
        p = _make(rec, clock, quiet_seconds=90.0)
        p.begin_turn()

        clock.advance(80.0)
        await p.note_output()
        clock.advance(80.0)  # 160s since turn start, but only 80s since output
        await p.tick()

        assert rec.posts == []

    @pytest.mark.asyncio
    async def test_end_turn_removes_and_disarms(self) -> None:
        rec, clock = _Recorder(), _Clock()
        p = _make(rec, clock, quiet_seconds=90.0)
        p.begin_turn()
        clock.advance(91.0)
        await p.tick()

        await p.end_turn()
        clock.advance(600.0)
        await p.tick()

        assert rec.deletes == ["msg1"]
        assert len(rec.posts) == 1, "posted again after the turn ended"


class TestWorkingVsWaiting:
    @pytest.mark.asyncio
    async def test_says_working_while_tools_run(self) -> None:
        """AC3: tool activity during the silence reads as progress, not a stall."""
        rec, clock = _Recorder(), _Clock()
        p = _make(rec, clock, quiet_seconds=90.0, stalled_seconds=60.0)
        p.begin_turn()

        clock.advance(80.0)
        p.note_activity("Bash(rg -n 'timeout' c_lord/)")
        clock.advance(20.0)  # 100s of silence, but a tool moved 20s ago
        await p.tick()

        assert len(rec.posts) == 1
        body = rec.posts[0]
        assert "作業中" in body
        assert "Bash(rg -n 'timeout' c_lord/)" in body

    @pytest.mark.asyncio
    async def test_says_waiting_when_nothing_moves(self) -> None:
        """AC3: no tool activity either → this is a stall, and says so."""
        rec, clock = _Recorder(), _Clock()
        p = _make(rec, clock, quiet_seconds=90.0, stalled_seconds=60.0)
        p.begin_turn()

        clock.advance(200.0)  # nothing at all happened
        await p.tick()

        assert len(rec.posts) == 1
        assert "待機中" in rec.posts[0]

    @pytest.mark.asyncio
    async def test_counts_tools_run_this_turn(self) -> None:
        """The 作業中 line carries how much work has gone by."""
        rec, clock = _Recorder(), _Clock()
        p = _make(rec, clock, quiet_seconds=90.0, stalled_seconds=60.0)
        p.begin_turn()

        clock.advance(91.0)  # quiet long enough to speak…
        for i in range(27):
            p.note_activity(f"Read(file{i}.py)")  # …but tools are still moving
        await p.tick()

        assert "作業中" in rec.posts[0]
        assert "27" in rec.posts[0]

    @pytest.mark.asyncio
    async def test_waiting_line_reports_how_long_nothing_moved(self) -> None:
        """A stall reports the stall, not a tool count it can no longer vouch for."""
        rec, clock = _Recorder(), _Clock()
        p = _make(rec, clock, quiet_seconds=90.0, stalled_seconds=60.0)
        p.begin_turn()
        p.note_activity("Read(a.py)")

        clock.advance(91.0)  # tools stopped 91s ago
        await p.tick()

        assert "待機中" in rec.posts[0]
        assert "91" in rec.posts[0]

    @pytest.mark.asyncio
    async def test_rendered_as_discord_subtext(self) -> None:
        """The line must be subtext so it stays visually quiet."""
        rec, clock = _Recorder(), _Clock()
        p = _make(rec, clock, quiet_seconds=90.0)
        p.begin_turn()
        clock.advance(91.0)
        await p.tick()

        assert rec.posts[0].startswith("-# ")


class TestNeverBreaksTheTurn:
    @pytest.mark.asyncio
    async def test_post_failure_is_swallowed(self) -> None:
        rec, clock = _Recorder(), _Clock()
        rec.post_error = RuntimeError("discord is down")
        p = _make(rec, clock, quiet_seconds=90.0)
        p.begin_turn()

        clock.advance(91.0)
        await p.tick()  # must not raise

        clock.advance(91.0)
        await p.tick()

    @pytest.mark.asyncio
    async def test_delete_failure_is_swallowed(self) -> None:
        rec, clock = _Recorder(), _Clock()
        p = _make(rec, clock, quiet_seconds=90.0)
        p.begin_turn()
        clock.advance(91.0)
        await p.tick()

        rec.delete_error = RuntimeError("already deleted")
        await p.note_output()  # must not raise

        # The handle is dropped anyway, so the next gap starts clean.
        clock.advance(91.0)
        await p.tick()
        assert len(rec.posts) == 2


class TestLabelReadability:
    """Defects found by looking at the real staging output, not the mockup."""

    @pytest.mark.asyncio
    async def test_does_not_double_the_tool_emoji(self) -> None:
        """Rendered tool bodies already start with 🔧 — don't add a second one."""
        rec, clock = _Recorder(), _Clock()
        p = _make(rec, clock, quiet_seconds=90.0, stalled_seconds=60.0)
        p.begin_turn()

        clock.advance(91.0)
        p.note_activity("🔧 Bash: `ls`")
        await p.tick()

        assert "🔧 🔧" not in rec.posts[0], rec.posts[0]
        assert rec.posts[0].count("🔧") == 1

    @pytest.mark.asyncio
    async def test_long_paths_keep_the_filename(self) -> None:
        """A session-dir path must not eat the line and hide which file it is."""
        rec, clock = _Recorder(), _Clock()
        p = _make(rec, clock, quiet_seconds=90.0, stalled_seconds=60.0)
        p.begin_turn()

        clock.advance(91.0)
        p.note_activity(
            "🔧 Read: `/home/yousan/c-lord-sessions-staging-4/1514535896328700015/"
            "1542002829714260048/c_lord/cogs/claude_chat.py`"
        )
        await p.tick()

        body = rec.posts[0]
        assert "claude_chat.py" in body, body
        assert "c-lord-sessions-staging-4" not in body, body

    @pytest.mark.asyncio
    async def test_line_stays_short(self) -> None:
        rec, clock = _Recorder(), _Clock()
        p = _make(rec, clock, quiet_seconds=90.0, stalled_seconds=60.0)
        p.begin_turn()

        clock.advance(91.0)
        p.note_activity("🔧 Bash: `" + "x" * 500 + "`")
        await p.tick()

        assert len(rec.posts[0]) < 140, rec.posts[0]


class TestElapsedIsMeasuredFromTheRequest:
    @pytest.mark.asyncio
    async def test_restart_resets_the_clock(self) -> None:
        """The number's whole job is "how long you have been waiting".

        Staging showed it under-reporting (1:34 when the user had waited 1:52):
        without an explicit start the clock begins at the first *transcript*
        event, which is after Claude has finished booting. c-lord knows when it
        accepted the prompt, so it says so.
        """
        rec, clock = _Recorder(), _Clock()
        p = _make(rec, clock, quiet_seconds=90.0, stalled_seconds=60.0)

        p.begin_turn()  # e.g. armed by a stale signal
        clock.advance(300.0)
        p.begin_turn(restart=True)  # c-lord accepts the prompt: clock starts here

        clock.advance(91.0)
        p.note_activity("Read(a.py)")
        await p.tick()

        assert "1:31" in rec.posts[0], rec.posts[0]

    @pytest.mark.asyncio
    async def test_plain_begin_turn_stays_idempotent(self) -> None:
        """Repeated arming inside one turn must not keep resetting the clock."""
        rec, clock = _Recorder(), _Clock()
        p = _make(rec, clock, quiet_seconds=90.0, stalled_seconds=60.0)

        p.begin_turn()
        clock.advance(91.0)
        p.begin_turn()  # a later tool event re-arms — must be a no-op
        p.note_activity("Read(a.py)")
        await p.tick()

        assert "1:31" in rec.posts[0], rec.posts[0]
