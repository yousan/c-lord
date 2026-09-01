"""Issue #627: the mirror must read *this thread's* transcript, nothing else.

One working copy holds many Claude Code sessions: every ``claude -p`` call and
every sub-agent started in that cwd writes its own ``<session-id>.jsonl`` into
the same ``~/.claude/projects/<slug>/`` directory.  The #627 example dir held
**182** of them.

``latest_session_jsonl`` picked the mtime-latest, which answers "who wrote
last", not "whose transcript is this" — and ``tail.py`` re-picked on every poll,
so one line written by a sub-invocation moved the mirror onto its transcript.
The user's thread then filled with a conversation they never had: 300 messages
on 2026-08-27, including **96 fake 👤 user messages** and the same JSON blob 63
times.

These tests pin the rule that replaces it: only a transcript that carries
c-lord's own input marker is eligible, the pin only moves forward, and when
nothing is eligible the mirror posts *nothing* rather than somebody else's
conversation.
"""

from __future__ import annotations

import asyncio
import json
import logging
import os
from pathlib import Path

import pytest

from c_lord.transcript.resolver import (
    CLORD_INPUT_MARKER,
    ThreadSessionResolver,
    is_clord_driven_jsonl,
    latest_session_jsonl,
)
from c_lord.transcript.tail import tail_events

ZWSP = "​"


def _clord_user_line(text: str, uuid: str) -> str:
    """A user event as Claude Code stores a prompt c-lord drove into the pane."""
    return json.dumps(
        {
            "type": "user",
            "uuid": uuid,
            "sessionId": "s",
            "message": {"role": "user", "content": ZWSP + text},
        },
        ensure_ascii=False,
    )


def _plain_line(uuid: str, text: str = "hi") -> str:
    """An event with no c-lord marker — what a ``claude -p`` transcript looks like."""
    return json.dumps(
        {
            "type": "assistant",
            "uuid": uuid,
            "sessionId": "s",
            "message": {"role": "assistant", "content": [{"type": "text", "text": text}]},
        },
        ensure_ascii=False,
    )


def _write(path: Path, *lines: str, mtime: float | None = None) -> Path:
    path.write_text("\n".join(lines) + "\n", encoding="utf-8")
    if mtime is not None:
        os.utime(path, (mtime, mtime))
    return path


def _append(path: Path, *lines: str, mtime: float | None = None) -> None:
    with path.open("a", encoding="utf-8") as f:
        f.write("\n".join(lines) + "\n")
    if mtime is not None:
        os.utime(path, (mtime, mtime))


# ── The marker itself ────────────────────────────────────────────────────


def test_marker_is_the_raw_utf8_zero_width_space() -> None:
    """The probe is a byte search, so it must match how Claude Code writes it.

    Claude Code serialises with JS ``JSON.stringify``, which emits U+200B as raw
    UTF-8 rather than a ``\\u200b`` escape.  Verified against production
    transcripts on 2026-08-31 (64 raw occurrences, 0 escaped).
    """
    assert CLORD_INPUT_MARKER == b"\xe2\x80\x8b"
    assert CLORD_INPUT_MARKER in _clord_user_line("こんにちは", "u1").encode("utf-8")


@pytest.mark.parametrize("separators", [(",", ":"), (", ", ": ")])
def test_marker_survives_either_json_separator_style(
    tmp_path: Path, separators: tuple[str, str]
) -> None:
    """The verdict must not hinge on a serialiser's spacing.

    Claude Code writes ``"content":"``; ``json.dumps`` defaults to
    ``"content": "``.  Both are the same event.
    """
    line = json.dumps(
        {
            "type": "user",
            "uuid": "u1",
            "message": {"role": "user", "content": ZWSP + "やって"},
        },
        ensure_ascii=False,
        separators=separators,
    )
    assert is_clord_driven_jsonl(_write(tmp_path / "s.jsonl", line)) is True


def test_a_stray_zero_width_space_in_tool_output_is_not_the_marker(
    tmp_path: Path,
) -> None:
    """Only a ZWSP *opening a ``content`` string* is c-lord's prompt marker.

    A sub-agent that happens to read a file containing a zero-width space must
    not thereby look like this thread's session.
    """
    line = json.dumps(
        {
            "type": "assistant",
            "uuid": "u1",
            "message": {
                "role": "assistant",
                "content": [{"type": "text", "text": "a" + ZWSP + "b"}],
            },
        },
        ensure_ascii=False,
    )
    assert is_clord_driven_jsonl(_write(tmp_path / "s.jsonl", line)) is False


def test_a_plain_transcript_does_not_carry_the_marker(tmp_path: Path) -> None:
    ours = _write(tmp_path / "ours.jsonl", _clord_user_line("やって", "u1"))
    theirs = _write(tmp_path / "theirs.jsonl", _plain_line("u2"), _plain_line("u3"))
    assert is_clord_driven_jsonl(ours) is True
    assert is_clord_driven_jsonl(theirs) is False


def test_marker_is_found_across_a_read_chunk_boundary(tmp_path: Path) -> None:
    """The probe reads in slices; a marker straddling a slice must still match."""
    from c_lord.transcript import resolver as resolver_mod

    path = tmp_path / "big.jsonl"
    padding = _plain_line("pad", "x" * 500)
    _write(path, *([padding] * 40), _clord_user_line("最後の一言", "u9"))

    # Force a boundary that lands inside the file rather than past its end.
    original = resolver_mod._PROBE_CHUNK_BYTES
    try:
        resolver_mod._PROBE_CHUNK_BYTES = 64
        assert is_clord_driven_jsonl(path) is True
    finally:
        resolver_mod._PROBE_CHUNK_BYTES = original


def test_a_missing_file_is_not_ours(tmp_path: Path) -> None:
    """Unreadable is never a reason to fall back to somebody else's transcript."""
    assert is_clord_driven_jsonl(tmp_path / "gone.jsonl") is False


# ── AC1: only this thread's transcript is eligible ───────────────────────


def test_resolver_ignores_sub_invocations_even_when_they_wrote_last(
    tmp_path: Path,
) -> None:
    """AC1: the #627 shape — 1 c-lord session, many ``claude -p`` transcripts.

    The sub-invocations are *newer*, which is exactly what made the old
    mtime rule hand the mirror over to them.
    """
    project = tmp_path / "proj"
    project.mkdir()
    ours = _write(project / "ours.jsonl", _clord_user_line("調べて", "u1"), mtime=100)
    for i in range(20):
        _write(project / f"sub-{i:02d}.jsonl", _plain_line(f"s{i}"), mtime=200 + i)

    # The rule being replaced would pick the newest sub-invocation.
    assert latest_session_jsonl(project) == project / "sub-19.jsonl"
    assert ThreadSessionResolver(project).resolve() == ours


def test_resolver_picks_the_newest_of_several_clord_sessions(tmp_path: Path) -> None:
    """A thread accumulates sessions over time (``/clear``); follow the newest."""
    project = tmp_path / "proj"
    project.mkdir()
    _write(project / "old.jsonl", _clord_user_line("むかし", "u1"), mtime=100)
    newer = _write(project / "new.jsonl", _clord_user_line("いま", "u2"), mtime=300)
    _write(project / "sub.jsonl", _plain_line("s1"), mtime=400)

    assert ThreadSessionResolver(project).resolve() == newer


# ── AC4: nothing eligible → post nothing, and say so ─────────────────────


def test_resolver_returns_none_when_no_transcript_is_ours(tmp_path: Path) -> None:
    """AC4: never silently read another session — return ``None`` instead."""
    project = tmp_path / "proj"
    project.mkdir()
    for i in range(5):
        _write(project / f"sub-{i}.jsonl", _plain_line(f"s{i}"), mtime=100 + i)

    assert ThreadSessionResolver(project).resolve() is None


def test_resolver_warns_once_when_nothing_is_ours(
    tmp_path: Path, caplog: pytest.LogCaptureFixture
) -> None:
    """AC4: the silence is explained in the log, but not once per poll."""
    project = tmp_path / "proj"
    project.mkdir()
    _write(project / "sub.jsonl", _plain_line("s1"), mtime=100)

    resolver = ThreadSessionResolver(project)
    with caplog.at_level(logging.WARNING, logger="c_lord.transcript.resolver"):
        for _ in range(5):
            assert resolver.resolve() is None

    warnings = [r for r in caplog.records if "no c-lord-driven transcript" in r.message]
    assert len(warnings) == 1, f"warned {len(warnings)} times, expected exactly 1"


def test_resolver_missing_project_dir_returns_none(tmp_path: Path) -> None:
    assert ThreadSessionResolver(tmp_path / "nope").resolve() is None


# ── A /clear's successor is adopted once c-lord writes into it ───────────


def test_resolver_adopts_a_new_session_once_it_carries_the_marker(
    tmp_path: Path,
) -> None:
    """Claude Code creates the successor jsonl *before* the marked prompt lands.

    The first look at it therefore legitimately says "not ours"; the verdict has
    to be revisited when the file grows, or a ``/clear`` would strand the mirror
    on the previous session forever.
    """
    project = tmp_path / "proj"
    project.mkdir()
    first = _write(project / "first.jsonl", _clord_user_line("最初", "u1"), mtime=100)
    resolver = ThreadSessionResolver(project)
    assert resolver.resolve() == first

    # /clear: a new session file appears, still without c-lord's prompt in it.
    successor = _write(project / "second.jsonl", _plain_line("meta"), mtime=200)
    assert resolver.resolve() == first, "adopted a session c-lord has not driven"

    # Now c-lord's marked prompt is written into it.
    _append(successor, _clord_user_line("つづき", "u2"), mtime=300)
    assert resolver.resolve() == successor


# ── AC3: the pin only moves forward ──────────────────────────────────────


def test_resolver_never_moves_back_to_an_older_transcript(tmp_path: Path) -> None:
    """AC3: an mtime flap must not send the mirror back over a consumed file.

    Going back resets the read offset to 0, which is how a whole transcript gets
    re-posted (the #433 shape, reached through a different door — #627).
    """
    project = tmp_path / "proj"
    project.mkdir()
    older = _write(project / "a.jsonl", _clord_user_line("ふるい", "u1"), mtime=100)
    newer = _write(project / "b.jsonl", _clord_user_line("あたらしい", "u2"), mtime=200)

    resolver = ThreadSessionResolver(project)
    assert resolver.resolve() == newer

    # The older file is touched (a resume, a stray write, an editor).
    os.utime(older, (900, 900))
    assert resolver.resolve() == newer, "moved back to a transcript already consumed"


# ── Cost: the glob does not run on every poll (#537) ─────────────────────


def test_resolver_globs_only_when_the_directory_changes(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    """Appending to a transcript does not create a directory entry.

    So the expensive listing (28.9 ms for the 2481-file production dir) has to be
    gated on the directory's own mtime, or #537's per-poll cost stays.
    """
    project = tmp_path / "proj"
    project.mkdir()
    ours = _write(project / "ours.jsonl", _clord_user_line("やあ", "u1"), mtime=100)
    # A directory that has been quiet for a while: a listing taken right after a
    # change is deliberately not trusted (a same-tick creation could have been
    # missed), so age the directory to model the steady state being asserted.
    os.utime(project, (1, 1))

    resolver = ThreadSessionResolver(project)
    assert resolver.resolve() == ours

    globs: list[str] = []
    real_glob = Path.glob

    def counting_glob(self, pattern):
        globs.append(pattern)
        return real_glob(self, pattern)

    monkeypatch.setattr(Path, "glob", counting_glob)

    for _ in range(10):
        resolver.resolve()
    assert globs == [], "re-listed the project dir with nothing added to it"

    # A new file bumps the directory mtime → one re-listing.
    _write(project / "sub.jsonl", _plain_line("s1"), mtime=500)
    resolver.resolve()
    assert len(globs) == 1, f"expected exactly one re-listing, got {len(globs)}"


# ── End to end through the tail ──────────────────────────────────────────


async def _collect_for(agen, duration: float) -> list:
    collected: list = []

    async def pull() -> None:
        async for ev in agen:
            collected.append(ev)

    task = asyncio.create_task(pull())
    await asyncio.sleep(duration)
    task.cancel()
    await asyncio.gather(task, return_exceptions=True)
    return collected


async def test_tail_does_not_emit_a_sub_invocations_conversation(tmp_path: Path) -> None:
    """AC2, end to end: sub-invocation traffic never reaches the consumer.

    This is the production shape of the bug: the thread's own session is idle
    while ``claude -p`` calls churn out transcripts next to it.
    """
    project = tmp_path / "proj"
    project.mkdir()
    ours = _write(project / "ours.jsonl", _clord_user_line("待機中", "u0"), mtime=100)

    agen = tail_events(project, poll_interval=0.05).__aiter__()

    async def sub_invocations() -> None:
        await asyncio.sleep(0.1)
        for i in range(120):
            _write(
                project / f"sub-{i:03d}.jsonl",
                _plain_line(f"sub-{i}", "I'll open the image and read it."),
                mtime=1000 + i,
            )
        await asyncio.sleep(0.15)
        # The thread's own session speaks — this, and only this, must arrive.
        _append(ours, _plain_line("mine", "終わりました"), mtime=2000)

    prod = asyncio.create_task(sub_invocations())
    try:
        events = await _collect_for(agen, 0.9)
    finally:
        prod.cancel()
        await asyncio.gather(prod, return_exceptions=True)
        await agen.aclose()

    assert [e["uuid"] for e in events] == ["mine"], (
        "a transcript that is not this thread's reached the consumer"
    )


async def test_tail_stays_silent_when_no_transcript_is_ours(tmp_path: Path) -> None:
    """AC4 end to end: silence beats posting another session's conversation."""
    project = tmp_path / "proj"
    project.mkdir()

    agen = tail_events(project, poll_interval=0.05).__aiter__()

    async def sub_invocations() -> None:
        await asyncio.sleep(0.1)
        for i in range(5):
            _write(project / f"sub-{i}.jsonl", _plain_line(f"s{i}"), mtime=100 + i)

    prod = asyncio.create_task(sub_invocations())
    try:
        events = await _collect_for(agen, 0.6)
    finally:
        prod.cancel()
        await asyncio.gather(prod, return_exceptions=True)
        await agen.aclose()

    assert events == []


async def test_tail_follows_a_clear_to_the_new_clord_session(tmp_path: Path) -> None:
    """``/clear`` still works: the successor is followed once c-lord drives it."""
    project = tmp_path / "proj"
    project.mkdir()
    first = _write(project / "first.jsonl", _clord_user_line("最初", "u0"), mtime=100)

    agen = tail_events(project, poll_interval=0.05).__aiter__()

    async def clear_and_continue() -> None:
        await asyncio.sleep(0.15)
        _append(first, _plain_line("before-clear", "はい"), mtime=200)
        await asyncio.sleep(0.15)
        second = project / "second.jsonl"
        _write(second, _clord_user_line("あたらしく", "u1"), mtime=300)
        await asyncio.sleep(0.15)
        _append(second, _plain_line("after-clear", "了解"), mtime=400)

    prod = asyncio.create_task(clear_and_continue())
    try:
        events = await _collect_for(agen, 1.0)
    finally:
        prod.cancel()
        await asyncio.gather(prod, return_exceptions=True)
        await agen.aclose()

    assert [e["uuid"] for e in events] == ["before-clear", "u1", "after-clear"]


async def test_tail_does_not_replay_a_pre_existing_transcript_it_adopts_late(
    tmp_path: Path,
) -> None:
    """AC3: adopting a file that predates the tail must not replay its history.

    A thread whose transcript carries no marker yet (an old session, a start that
    failed before the prompt landed) becomes eligible the moment c-lord drives
    the next turn.  Only that turn may be posted — the history that was already
    on disk when the tail started was never ours to deliver.
    """
    project = tmp_path / "proj"
    project.mkdir()
    existing = _write(
        project / "existing.jsonl",
        _plain_line("old-1", "むかしの発言 1"),
        _plain_line("old-2", "むかしの発言 2"),
        mtime=100,
    )

    agen = tail_events(project, poll_interval=0.05).__aiter__()

    async def drive_a_turn() -> None:
        await asyncio.sleep(0.2)
        _append(existing, _clord_user_line("今の依頼", "now-1"), mtime=300)
        await asyncio.sleep(0.15)
        _append(existing, _plain_line("now-2", "今の返事"), mtime=400)

    prod = asyncio.create_task(drive_a_turn())
    try:
        events = await _collect_for(agen, 0.9)
    finally:
        prod.cancel()
        await asyncio.gather(prod, return_exceptions=True)
        await agen.aclose()

    assert [e["uuid"] for e in events] == ["now-1", "now-2"], (
        "history that predates the tail was replayed when the file was adopted"
    )


async def test_tail_ignores_a_sub_invocation_that_is_newer_than_the_pin(
    tmp_path: Path,
) -> None:
    """The exact 2026-08-27 shape: sub-invocations keep writing *after* the pin.

    The old rule re-picked the mtime-latest on every poll, so each of the 182
    ``claude -p`` transcripts took the mirror in turn — and because switching
    reset the read offset to 0, each one was posted from its first line.
    """
    project = tmp_path / "proj"
    project.mkdir()
    ours = _write(project / "ours.jsonl", _clord_user_line("抽出して", "u0"), mtime=100)

    agen = tail_events(project, poll_interval=0.05).__aiter__()

    async def churn() -> None:
        await asyncio.sleep(0.1)
        for i in range(30):
            _write(
                project / f"sub-{i:03d}.jsonl",
                _plain_line(f"sub-{i}-a", '{"is_flyer":false}'),
                _plain_line(f"sub-{i}-b", "I'll open the image and read it."),
                mtime=5000 + i,
            )
        await asyncio.sleep(0.2)
        _append(ours, _plain_line("real", "30 件抽出しました"), mtime=9999)

    prod = asyncio.create_task(churn())
    try:
        events = await _collect_for(agen, 0.9)
    finally:
        prod.cancel()
        await asyncio.gather(prod, return_exceptions=True)
        await agen.aclose()

    assert [e["uuid"] for e in events] == ["real"]
