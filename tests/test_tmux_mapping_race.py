"""Concurrency test for the window→thread mapping (#485).

The phantom-answer incident started here: while a second session was starting,
``_find_window_for_thread`` returned ``None`` for a *live* window because
``_rebuild_mapping`` used ``clear()`` + repopulate-in-place, leaving the shared
map momentarily EMPTY. A concurrent capture/send then mis-fired (dropped
keystrokes; a bridge falsely concluding the menu had resolved).

These tests hammer ``_find_window_for_thread`` from many threads while other
threads rebuild the map. tmux is mocked (CI has no live windows); the
``show-option`` responses sleep briefly to widen the rebuild's populate window
so the race is reliably exercised. The map must always resolve a live window —
never ``None``, never the wrong window.
"""

from __future__ import annotations

import threading
import time
from unittest.mock import MagicMock, patch

from c_lord.tmux import TmuxSessionManager

# Stable set of live windows for the whole test, keyed by tmux ``window_id``
# (#649 — a window *name* identifies no single window).
_WINDOWS = {"@1": ("w1", "1001"), "@2": ("w2", "1002"), "@3": ("w3", "1003")}


def _fake_run(args):
    if "list-windows" in args:
        fmt = args[args.index("-F") + 1] if "-F" in args else "#{window_name}"
        # Widen the rebuild's populate window so the (old) clear()-race is hit.
        time.sleep(0.002)
        out = ""
        for wid, (name, tid) in _WINDOWS.items():
            row = fmt
            for token, value in (
                ("#{window_id}", wid),
                ("#{window_name}", name),
                ("#{@thread_id}", tid),
                ("#{pane_current_path}", f"/base/{tid}"),
            ):
                row = row.replace(token, value)
            out += row + "\n"
        return MagicMock(returncode=0, stdout=out)
    if "show-option" in args and "@thread_id" in args:
        win = args[args.index("-t") + 1]  # a window_id since #649
        time.sleep(0.002)
        entry = _WINDOWS.get(win)
        return MagicMock(returncode=0, stdout=(entry[1] if entry else "") + "\n")
    return MagicMock(returncode=0, stdout="")


def _mgr() -> TmuxSessionManager:
    mgr = TmuxSessionManager(mapping_path="")
    mgr._available = True
    mgr.session_name = "t"
    return mgr


def test_find_window_never_none_or_wrong_under_concurrent_rebuild() -> None:
    mgr = _mgr()
    stop = threading.Event()
    none_seen: list[int] = []
    wrong_seen: list[str] = []
    lock = threading.Lock()

    with patch("c_lord.tmux._run", side_effect=_fake_run):

        def reader() -> None:
            while not stop.is_set():
                # Force the rebuild fallback path (the one that returned None).
                mgr._thread_to_window.pop(1001, None)
                w = mgr._find_window_for_thread(1001)
                if w is None:
                    with lock:
                        none_seen.append(1)
                elif w != "@1":
                    with lock:
                        wrong_seen.append(w)

        def rebuilder() -> None:
            while not stop.is_set():
                mgr._rebuild_mapping()

        threads = [threading.Thread(target=reader) for _ in range(6)]
        threads += [threading.Thread(target=rebuilder) for _ in range(3)]
        for t in threads:
            t.start()
        time.sleep(2.5)
        stop.set()
        for t in threads:
            t.join(timeout=3)

    assert not none_seen, (
        f"_find_window_for_thread returned None {len(none_seen)}x for a LIVE window "
        "— mapping race (#485)"
    )
    assert not wrong_seen, f"returned the wrong window: {wrong_seen[:5]}"


def test_rebuild_result_is_correct_single_threaded() -> None:
    mgr = _mgr()
    with patch("c_lord.tmux._run", side_effect=_fake_run):
        mgr._rebuild_mapping()
    assert mgr._thread_to_window == {1001: "@1", 1002: "@2", 1003: "@3"}
    assert mgr._next_work_id == 4
