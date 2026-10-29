"""tail.py の取りこぼし — 部分行・壊れた行・消えるファイル・pool 設定・#773 通知の失敗。

``test_tail.py`` は async の follow loop を時間で回して主経路を押さえている。ここは
1 poll を同期で回す (:func:`_poll_once`) ことで、タイミングに頼らず「1 回の読み取りが
どこで切れても何が出るか」を固定する。#537 以降 follow loop は 1 poll = 1 worker hop
なので、この単位で見るのが実装と同じ粒度になる。

transcript は c-lord が名付けたもの (``.clord-session`` の claim、#773 の rule 0) を
使う — いま本番の全セッションがこの経路で解決される。イベントの形は Claude Code が
実際に書くキーの組（``timestamp`` は ``message`` の後ろ）。
"""

from __future__ import annotations

import asyncio
import json
import logging
import uuid as uuid_mod
from pathlib import Path

import pytest

from c_lord.transcript import tail as tail_mod
from c_lord.transcript.claim import write_claim
from c_lord.transcript.tail import (
    _open_initial_state,
    _poll_once,
    _seed_seen_uuids,
    _signature,
    _size_of,
    _tail_worker_count,
    tail_events,
)


def _line(event: dict) -> str:
    return json.dumps(event, ensure_ascii=False) + "\n"


def _assistant(uid: str, text: str = "ok") -> dict:
    return {
        "parentUuid": "p",
        "isSidechain": False,
        "message": {"role": "assistant", "content": [{"type": "text", "text": text}]},
        "type": "assistant",
        "uuid": uid,
        "timestamp": "2026-10-28T12:25:43.722Z",
    }


def _claimed(tmp_path: Path, *lines: str) -> tuple[Path, Path, str]:
    """A project dir with c-lord's claim and its transcript holding *lines*."""
    project = tmp_path / "proj"
    project.mkdir()
    sid = str(uuid_mod.uuid4())
    assert write_claim(project, sid)
    path = project / f"{sid}.jsonl"
    path.write_text("".join(lines), "utf-8")
    return project, path, sid


def _append(path: Path, text: str) -> None:
    with path.open("a", encoding="utf-8") as f:
        f.write(text)


# ── partial and broken lines ────────────────────────────────────────────────


class TestPartialLines:
    """Claude Code appends one record per line; a poll can land while a large
    record (prod transcripts reach ~100 MB, #537) is only partly on disk."""

    def test_a_half_written_line_waits_for_its_newline(self, tmp_path: Path) -> None:
        project, path, _ = _claimed(tmp_path, _line(_assistant("u0")))
        state = _open_initial_state(project, False)
        whole = _line(_assistant("u1", "x" * 300))

        _append(path, whole[:120])
        assert _poll_once(state) == []

        _append(path, whole[120:])
        assert [e["uuid"] for e in _poll_once(state)] == ["u1"]
        assert _poll_once(state) == []

    def test_a_line_split_across_the_read_cap_is_reassembled(
        self, tmp_path: Path, monkeypatch: pytest.MonkeyPatch
    ) -> None:
        """Above ``_READ_CHUNK_BYTES`` a poll stops early and says so
        (``more_to_read``); the cut line is kept in the buffer, not dropped."""
        monkeypatch.setattr(tail_mod, "_READ_CHUNK_BYTES", 64)
        project, path, _ = _claimed(tmp_path, _line(_assistant("u0")))
        state = _open_initial_state(project, False)
        _append(path, _line(_assistant("u1", "a" * 100)) + _line(_assistant("u2", "b" * 100)))

        got: list[str] = []
        polls = 0
        while True:
            got += [e["uuid"] for e in _poll_once(state)]
            polls += 1
            if not state.more_to_read:
                break
        assert got == ["u1", "u2"]
        assert polls > 2

    def test_a_broken_line_is_skipped_and_the_next_one_delivered(self, tmp_path: Path) -> None:
        project, path, _ = _claimed(tmp_path, _line(_assistant("u0")))
        state = _open_initial_state(project, False)
        _append(path, '{"type": "assistant", "uuid": "torn"\n' + _line(_assistant("u1")))
        assert [e["uuid"] for e in _poll_once(state)] == ["u1"]

    def test_a_broken_line_in_the_baseline_does_not_stop_seeding(self, tmp_path: Path) -> None:
        """#433: the baseline's uuids are what keep a rewrite from replaying
        history. One unparsable line must not leave the ones after it unseeded."""
        project, path, _ = _claimed(
            tmp_path,
            _line(_assistant("u0")),
            '{"type": "assistant", "uuid": "torn"\n',
            _line(_assistant("u1")),
        )
        state = _open_initial_state(project, False)
        assert {"u0", "u1"} <= state.seen_uuids

        # A rewrite that comes out shorter (the torn line is gone): offset 0,
        # and only the genuinely new event is delivered.
        path.write_text(_line(_assistant("u1")) + _line(_assistant("u2")))
        assert [e["uuid"] for e in _poll_once(state)] == ["u2"]

    def test_seeding_survives_a_file_shorter_than_the_baseline(self, tmp_path: Path) -> None:
        """The file shrank between ``stat`` and the seeding read."""
        _, path, _ = _claimed(tmp_path, _line(_assistant("u0")))
        seen: set[str] = set()
        _seed_seen_uuids(path, path.stat().st_size + 4096, seen)
        assert seen == {"u0"}

    def test_seeding_a_missing_file_is_a_no_op(self, tmp_path: Path) -> None:
        seen: set[str] = set()
        _seed_seen_uuids(tmp_path / "gone.jsonl", 100, seen)
        assert seen == set()


# ── the file goes away ─────────────────────────────────────────────────────


class TestFileGoesAway:
    def test_truncation_restarts_without_replaying(self, tmp_path: Path) -> None:
        """#433: shrink → offset 0; already-seen uuids are not yielded again."""
        project, path, _ = _claimed(tmp_path, _line(_assistant("u0")) + _line(_assistant("u1")))
        state = _open_initial_state(project, False)
        path.write_text(_line(_assistant("u9")))
        assert [e["uuid"] for e in _poll_once(state)] == ["u9"]

    def test_a_deleted_transcript_yields_nothing_and_does_not_raise(self, tmp_path: Path) -> None:
        """Claude Code removes transcripts past ``cleanupPeriodDays``."""
        project, path, _ = _claimed(tmp_path, _line(_assistant("u0")))
        state = _open_initial_state(project, False)
        path.unlink()
        assert _poll_once(state) == []

    def test_vanishing_between_resolve_and_stat(
        self, tmp_path: Path, monkeypatch: pytest.MonkeyPatch
    ) -> None:
        project, path, _ = _claimed(tmp_path, _line(_assistant("u0")))
        state = _open_initial_state(project, False)
        ghost = project / "deleted-after-resolve.jsonl"
        monkeypatch.setattr(state.resolver, "resolve", lambda: ghost)
        assert _poll_once(state) == []
        assert state.current_path is None
        assert state.resolved is True

    def test_an_unreadable_transcript_yields_nothing(
        self, tmp_path: Path, monkeypatch: pytest.MonkeyPatch
    ) -> None:
        project, path, _ = _claimed(tmp_path, _line(_assistant("u0")))
        state = _open_initial_state(project, False)
        _append(path, _line(_assistant("u1")))
        real_open = Path.open

        def _emfile(self: Path, *args, **kwargs):  # noqa: ANN002, ANN003
            if self == path and args and args[0] in ("r", "rb"):
                raise OSError(24, "Too many open files")
            return real_open(self, *args, **kwargs)

        monkeypatch.setattr(Path, "open", _emfile)
        assert _poll_once(state) == []
        assert state.current_path is None

    def test_a_project_dir_not_created_yet(self, tmp_path: Path) -> None:
        """Claude Code creates the dir lazily; the tail must start anyway (#773
        then reports it as unresolved, it does not crash)."""
        state = _open_initial_state(tmp_path / "not-yet", False)
        assert state.pre_existing == {}
        assert _poll_once(state) == []
        assert state.resolved is False

    def test_an_unlistable_project_dir_starts_empty(
        self, tmp_path: Path, monkeypatch: pytest.MonkeyPatch
    ) -> None:
        project, _, _ = _claimed(tmp_path, _line(_assistant("u0")))

        def _denied(self, pattern):  # noqa: ANN001
            raise PermissionError(13, "Permission denied")

        monkeypatch.setattr(Path, "glob", _denied)
        assert _open_initial_state(project, False).pre_existing == {}


class TestHelpers:
    def test_size_of_a_missing_file_is_zero(self, tmp_path: Path) -> None:
        assert _size_of(tmp_path / "gone.jsonl") == 0

    def test_signature_of_a_missing_file_is_empty(self, tmp_path: Path) -> None:
        """#906: an empty signature differs from any real one, so a file that
        reappears with different bytes is still recognised as rewritten."""
        assert _signature(tmp_path / "gone.jsonl", 100) == b""

    def test_signature_is_the_bytes_just_before_the_offset(self, tmp_path: Path) -> None:
        path = tmp_path / "s.jsonl"
        path.write_bytes(b"a" * 5000 + b"tail")
        sig = _signature(path, 5004)
        assert len(sig) == 4096
        assert sig.endswith(b"tail")


# ── CLORD_TAIL_WORKERS (#537) ───────────────────────────────────────────────


class TestTailWorkerCount:
    """An ``.env`` typo must neither crash the bot nor make an unbounded pool."""

    @pytest.mark.parametrize(
        ("raw", "expected"),
        [("", 8), ("  ", 8), ("4", 4), (" 16 ", 16), ("1", 1), ("999", 32)],
    )
    def test_values(self, monkeypatch: pytest.MonkeyPatch, raw: str, expected: int) -> None:
        monkeypatch.setenv("CLORD_TAIL_WORKERS", raw)
        assert _tail_worker_count() == expected

    @pytest.mark.parametrize("raw", ["eight", "0", "-3", "2.5"])
    def test_bad_values_fall_back_with_a_warning(
        self, monkeypatch: pytest.MonkeyPatch, caplog: pytest.LogCaptureFixture, raw: str
    ) -> None:
        monkeypatch.setenv("CLORD_TAIL_WORKERS", raw)
        with caplog.at_level(logging.WARNING, logger="c_lord.transcript.tail"):
            assert _tail_worker_count() == 8
        assert "CLORD_TAIL_WORKERS" in caplog.text


# ── the async loop's two remaining branches ─────────────────────────────────


async def _collect(agen, n: int, timeout: float = 3.0) -> list:
    got: list = []

    async def pull() -> None:
        async for ev in agen:
            got.append(ev)
            if len(got) >= n:
                return

    await asyncio.wait_for(pull(), timeout=timeout)
    return got


async def test_a_backlog_is_drained_without_sleeping_between_chunks(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    """Above the read cap the loop goes straight round (#537's 109 MB catch-up
    must not trickle one chunk per poll interval)."""
    monkeypatch.setattr(tail_mod, "_READ_CHUNK_BYTES", 128)
    project, path, _ = _claimed(tmp_path)
    for i in range(20):
        _append(path, _line(_assistant(f"u{i}", "z" * 50)))
    # A 10 s poll interval: only the no-sleep path can deliver 20 events in time.
    agen = tail_events(project, poll_interval=10.0, from_start=True).__aiter__()
    try:
        got = await _collect(agen, 20, timeout=3.0)
    finally:
        await agen.aclose()
    assert [e["uuid"] for e in got] == [f"u{i}" for i in range(20)]


async def test_a_failing_unresolved_callback_does_not_stop_the_tail(
    tmp_path: Path, caplog: pytest.LogCaptureFixture
) -> None:
    """#773: the alarm is a report, not a stop. A reader that raises is logged
    and the tail still delivers once a transcript appears."""
    project = tmp_path / "proj"
    project.mkdir()
    calls = 0

    async def _broken(report) -> None:  # noqa: ANN001
        nonlocal calls
        calls += 1
        raise RuntimeError("discord down")

    agen = tail_events(
        project, poll_interval=0.02, on_unresolved=_broken, unresolved_after=0.05
    ).__aiter__()

    async def producer() -> None:
        while calls < 2:
            await asyncio.sleep(0.02)
        sid = str(uuid_mod.uuid4())
        write_claim(project, sid)
        (project / f"{sid}.jsonl").write_text(_line(_assistant("after")))

    prod = asyncio.create_task(producer())
    try:
        with caplog.at_level(logging.WARNING, logger="c_lord.transcript.tail"):
            got = await _collect(agen, 1, timeout=3.0)
    finally:
        prod.cancel()
        await asyncio.gather(prod, return_exceptions=True)
        await agen.aclose()
    assert [e["uuid"] for e in got] == ["after"]
    assert calls >= 2
    assert "unresolved-transcript callback failed" in caplog.text
