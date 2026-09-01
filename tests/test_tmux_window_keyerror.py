"""Regression: KeyError in tmux window-cache deletes (Issue #410, found by #404).

The traffic monitor surfaced a prod traceback: ``_find_window_for_thread`` did a
check-then-``del`` on ``self._thread_to_window``. ``capture_pane`` runs in a
thread executor and is called many times per turn, so two concurrent calls can
both see the same stale cache entry — the first ``del`` wins, the second raised
``KeyError`` and killed the Claude turn. The fix makes the deletes idempotent
(``pop(..., None)``).
"""

from __future__ import annotations

from unittest.mock import MagicMock, patch

from c_lord.tmux import TmuxSessionManager


def test_find_window_for_thread_survives_concurrent_cache_eviction() -> None:
    """A concurrent call evicting the cache entry must not raise KeyError."""
    mgr = TmuxSessionManager(mapping_path="")
    mgr._available = True
    mgr._thread_to_window[123] = "work1"

    def evict_then_mismatch(args):
        # Simulate another thread popping this entry between our get() and del.
        mgr._thread_to_window.pop(123, None)
        return MagicMock(returncode=0, stdout="999\n")  # mismatch → reaches the delete

    with (
        patch("c_lord.tmux._run", side_effect=evict_then_mismatch),
        patch.object(mgr, "_rebuild_mapping"),
    ):
        result = mgr._find_window_for_thread(123)  # must NOT raise KeyError

    assert result is None


def test_remap_window_cleanup_removes_old_mapping() -> None:
    """remap_window's old-mapping cleanup (same del-on-shared-cache pattern as the
    crash above) gets the same idempotent ``pop`` fix. This guards its behaviour:
    the old thread→window mapping is removed and the window is rebound.
    """
    mgr = TmuxSessionManager(mapping_path="")
    mgr._thread_to_window[111] = "@1"  # old mapping to the window being remapped

    with (
        patch.object(mgr, "_check_available", return_value=True),
        # #649: list-windows now reports id\tname, and the mapping keys on the id.
        patch("c_lord.tmux._run", return_value=MagicMock(returncode=0, stdout="@1\twork1\n")),
    ):
        ok = mgr.remap_window(999, "work1")

    assert ok is True
    assert mgr._thread_to_window[999] == "@1"
    assert 111 not in mgr._thread_to_window
