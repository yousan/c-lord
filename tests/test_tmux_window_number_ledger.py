"""#595: a thread keeps its ``w{N}`` across sleep / stop; only deletion frees it.

The number in a thread's name (``W5 │ …``) is the handle a user follows in the
sidebar. It used to be "whatever ``max + 1`` was when the window was last
created", so every recreation (sleep → wake, tmux server restart, move to
another session) handed the same thread a new number — and handed its old one
to somebody else.

The fix keeps the numbers in the file that already records thread → window
name per session (``~/.cache/c-lord/<session>-window-map.json``, #113) and stops
pruning it when a window dies: an entry is dropped only when the thread's
workspace is deleted (yousan, 2026-09-08). No new DB state.

These tests drive the real ``TmuxSessionManager`` against a small stateful fake
of the tmux CLI, so create → kill → create is exercised end to end.
"""

from __future__ import annotations

import json
import re
from collections.abc import Iterator
from pathlib import Path
from unittest.mock import MagicMock, patch

import pytest

from c_lord.tmux import TmuxSessionManager, release_window_number

SESSION = "c-lord"
BASE = "/home/u/c-lord-sessions/1"


class FakeTmux:
    """Just enough of the tmux CLI for create_session / kill_session."""

    def __init__(self) -> None:
        self.windows: list[dict[str, str]] = []
        self._next_id = 1
        self.calls: list[list[str]] = []

    def add(self, session: str, name: str, thread_id: int | None, path: str) -> str:
        wid = f"@{self._next_id}"
        self._next_id += 1
        self.windows.append(
            {
                "session_name": session,
                "window_id": wid,
                "window_name": name,
                "@thread_id": str(thread_id) if thread_id is not None else "",
                "pane_current_path": path,
            }
        )
        return wid

    def names(self, session: str = SESSION) -> list[str]:
        return [w["window_name"] for w in self.windows if w["session_name"] == session]

    def _find(self, target: str) -> dict[str, str] | None:
        if target.startswith("@"):
            return next((w for w in self.windows if w["window_id"] == target), None)
        session, _, name = target.partition(":")
        return next(
            (w for w in self.windows if w["session_name"] == session and w["window_name"] == name),
            None,
        )

    @staticmethod
    def _opt(args: list[str], flag: str) -> str | None:
        return args[args.index(flag) + 1] if flag in args else None

    def _render(self, fmt: str, w: dict[str, str]) -> str:
        return re.sub(r"#\{([^}]+)\}", lambda m: w.get(m.group(1), ""), fmt)

    def run(self, args: list[str], **_kw: object) -> MagicMock:
        self.calls.append(args)
        out = ""
        # tmux chains commands with a bare ";".
        chunk: list[str] = []
        chunks: list[list[str]] = []
        for a in args[1:]:
            if a == ";":
                chunks.append(chunk)
                chunk = []
            else:
                chunk.append(a)
        chunks.append(chunk)
        rc = 0
        for c in chunks:
            r, o = self._one(c)
            rc = rc or r
            out += o
        return MagicMock(returncode=rc, stdout=out, stderr="")

    def _one(self, c: list[str]) -> tuple[int, str]:
        sub = c[0] if c else ""
        if sub == "list-windows":
            fmt = self._opt(c, "-F") or "#{window_name}"
            if "-a" in c:
                rows = self.windows
            else:
                rows = [w for w in self.windows if w["session_name"] == self._opt(c, "-t")]
            return 0, "".join(self._render(fmt, w) + "\n" for w in rows)
        if sub == "new-window":
            session = self._opt(c, "-t") or SESSION
            wid = self.add(session, self._opt(c, "-n") or "", None, self._opt(c, "-c") or "")
            return 0, wid + "\n"
        if sub == "kill-window":
            w = self._find(self._opt(c, "-t") or "")
            if w is None:
                return 1, ""
            self.windows.remove(w)
            return 0, ""
        if sub == "show-option":
            w = self._find(self._opt(c, "-t") or "")
            return (0, w.get(c[-1], "") + "\n") if w else (1, "")
        if sub == "set-option":
            w = self._find(self._opt(c, "-t") or "")
            if w is not None:
                w[c[-2]] = c[-1]
            return 0, ""
        if sub == "rename-window":
            w = self._find(self._opt(c, "-t") or "")
            if w is not None:
                w["window_name"] = c[-1]
            return 0, ""
        if sub == "move-window":
            w = self._find(self._opt(c, "-s") or "")
            if w is not None:
                w["session_name"] = (self._opt(c, "-t") or "").rstrip(":")
            return 0, ""
        if sub == "display-message":
            w = self._find(self._opt(c, "-t") or "")
            return (0, self._render(c[-1], w) + "\n") if w else (1, "")
        return 0, ""


def _manager(tmp_path: Path, session: str = SESSION) -> TmuxSessionManager:
    mgr = TmuxSessionManager(
        session_name=session, mapping_path=str(tmp_path / f"{session}-window-map.json")
    )
    mgr._available = True
    mgr._sort_windows_unlocked = lambda: None  # type: ignore[method-assign]
    mgr._ensure_window_size_manual = lambda: None  # type: ignore[method-assign]
    mgr._strip_sensitive_env = lambda: None  # type: ignore[method-assign]
    mgr._fit_window_to_client = lambda *_: None  # type: ignore[method-assign]
    return mgr


@pytest.fixture
def tmux() -> Iterator[FakeTmux]:
    fake = FakeTmux()
    with patch("c_lord.tmux._run", side_effect=fake.run):
        yield fake


def _ledger(tmp_path: Path, session: str = SESSION) -> dict[str, str]:
    return json.loads((tmp_path / f"{session}-window-map.json").read_text())


class TestStopKeepsTheNumber:
    def test_recreated_window_gets_the_same_number(self, tmux: FakeTmux, tmp_path: Path) -> None:
        """RED before #595: sleep → wake moved the thread from w2 to w4."""
        mgr = _manager(tmp_path)
        assert mgr.create_session(1, f"{BASE}/1") == "w1"
        assert mgr.create_session(2, f"{BASE}/2") == "w2"
        assert mgr.create_session(3, f"{BASE}/3") == "w3"

        assert mgr.kill_session(2)  # sleep / stop
        assert mgr.create_session(2, f"{BASE}/2") == "w2"

    def test_highest_number_survives_its_window(self, tmux: FakeTmux, tmp_path: Path) -> None:
        """The max+1 rule used to hand a sleeping thread's top number to a newcomer."""
        mgr = _manager(tmp_path)
        mgr.create_session(1, f"{BASE}/1")
        mgr.create_session(2, f"{BASE}/2")
        mgr.kill_session(2)

        assert mgr.create_session(9, f"{BASE}/9") == "w3", "w2 still belongs to thread 2"
        assert mgr.create_session(2, f"{BASE}/2") == "w2"

    def test_survives_a_tmux_server_restart(self, tmux: FakeTmux, tmp_path: Path) -> None:
        """Every window gone, a fresh manager (bot restart) — the file remembers."""
        first = _manager(tmp_path)
        for tid in (1, 2, 3):
            first.create_session(tid, f"{BASE}/{tid}")
        tmux.windows.clear()

        fresh = _manager(tmp_path)
        assert fresh.create_session(3, f"{BASE}/3") == "w3"
        assert fresh.create_session(1, f"{BASE}/1") == "w1"

    def test_kill_keeps_the_ledger_entry(self, tmux: FakeTmux, tmp_path: Path) -> None:
        mgr = _manager(tmp_path)
        mgr.create_session(7, f"{BASE}/7")
        mgr.kill_session(7)
        assert _ledger(tmp_path) == {"7": "w1"}


class TestDeleteFreesTheNumber:
    def test_released_number_can_be_reused(self, tmux: FakeTmux, tmp_path: Path) -> None:
        mgr = _manager(tmp_path)
        mgr.create_session(1, f"{BASE}/1")
        mgr.create_session(2, f"{BASE}/2")
        mgr.kill_session(2)

        release_window_number(2, cache_dir=str(tmp_path))

        assert "2" not in _ledger(tmp_path)
        assert mgr.create_session(9, f"{BASE}/9") == "w2"

    def test_release_reaches_every_session_file(self, tmux: FakeTmux, tmp_path: Path) -> None:
        """/workspace-delete need not know which session the thread was in."""
        (tmp_path / "a-window-map.json").write_text(json.dumps({"5": "w1", "6": "w2"}))
        (tmp_path / "b-window-map.json").write_text(json.dumps({"5": "w9"}))
        (tmp_path / "unrelated.json").write_text(json.dumps({"5": "keep"}))

        release_window_number(5, cache_dir=str(tmp_path))

        assert json.loads((tmp_path / "a-window-map.json").read_text()) == {"6": "w2"}
        assert json.loads((tmp_path / "b-window-map.json").read_text()) == {}
        assert json.loads((tmp_path / "unrelated.json").read_text()) == {"5": "keep"}

    def test_release_without_a_cache_dir_is_harmless(self, tmp_path: Path) -> None:
        release_window_number(5, cache_dir=str(tmp_path / "missing"))


class TestNoCollision:
    def test_reserved_number_taken_live_falls_back(self, tmux: FakeTmux, tmp_path: Path) -> None:
        """A live window already named w2 (e.g. from an older build) wins."""
        (tmp_path / f"{SESSION}-window-map.json").write_text(json.dumps({"2": "w2"}))
        tmux.add(SESSION, "w2", 8, f"{BASE}/8")

        mgr = _manager(tmp_path)
        assert mgr.create_session(2, f"{BASE}/2") == "w3"

    def test_managers_sharing_a_session_do_not_erase_each_other(
        self, tmux: FakeTmux, tmp_path: Path
    ) -> None:
        """Two managers per session exist (#649); a save must merge, not overwrite."""
        a = _manager(tmp_path)
        b = _manager(tmp_path)
        a.create_session(1, f"{BASE}/1")
        b.create_session(2, f"{BASE}/2")
        a.kill_session(1)

        assert _ledger(tmp_path) == {"1": "w1", "2": "w2"}
        assert b.create_session(3, f"{BASE}/3") == "w3"


class TestMoveBetweenSessions:
    def test_moved_window_keeps_its_number_when_free(self, tmux: FakeTmux, tmp_path: Path) -> None:
        """#427 move: the session label changes (#618), the number need not."""
        src = _manager(tmp_path, "games")
        src.create_session(1, f"{BASE}/x")
        src.create_session(2, f"{BASE}/2")  # thread 2 is games:w2

        dst = _manager(tmp_path, "monitoring")
        dst.create_session(5, f"{BASE}/5")  # monitoring:w1
        assert dst.create_session(2, f"{BASE}/2") == "w2"
        assert _ledger(tmp_path, "monitoring") == {"5": "w1", "2": "w2"}
        assert "2" not in _ledger(tmp_path, "games"), "the source must not keep reserving it"

    def test_moved_window_renumbers_when_taken(self, tmux: FakeTmux, tmp_path: Path) -> None:
        src = _manager(tmp_path, "games")
        src.create_session(2, f"{BASE}/2")  # games:w1

        dst = _manager(tmp_path, "monitoring")
        dst.create_session(5, f"{BASE}/5")  # monitoring:w1
        assert dst.create_session(2, f"{BASE}/2") == "w2"
