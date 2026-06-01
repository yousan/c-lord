"""Tests for the single-instance flock guard (#212).

Prevents the staging incident (2026-05-27) where two bots ran on the same
token and double-processed Discord events. ``acquire_single_instance_lock``
takes an exclusive non-blocking flock on a lockfile under the data dir.
A second call against the same data dir while the first lock is held must
refuse to start (sys.exit(1)). ``CLORD_ALLOW_MULTI_INSTANCE=1`` bypasses
the guard for advanced users.
"""

from __future__ import annotations

from pathlib import Path

import pytest

from c_lord.main import acquire_single_instance_lock


class TestSingleInstanceLock:
    def test_first_acquire_returns_handle(
        self, tmp_path: Path, monkeypatch: pytest.MonkeyPatch
    ) -> None:
        monkeypatch.delenv("CLORD_ALLOW_MULTI_INSTANCE", raising=False)
        handle = acquire_single_instance_lock(tmp_path)
        assert handle is not None
        handle.close()

    def test_second_acquire_exits_nonzero(
        self, tmp_path: Path, monkeypatch: pytest.MonkeyPatch
    ) -> None:
        monkeypatch.delenv("CLORD_ALLOW_MULTI_INSTANCE", raising=False)
        first = acquire_single_instance_lock(tmp_path)
        assert first is not None
        try:
            with pytest.raises(SystemExit) as exc_info:
                acquire_single_instance_lock(tmp_path)
            assert exc_info.value.code != 0
        finally:
            first.close()

    def test_release_allows_reacquire(
        self, tmp_path: Path, monkeypatch: pytest.MonkeyPatch
    ) -> None:
        """Closing the handle releases the lock — no stale-lock blocking."""
        monkeypatch.delenv("CLORD_ALLOW_MULTI_INSTANCE", raising=False)
        first = acquire_single_instance_lock(tmp_path)
        assert first is not None
        first.close()
        second = acquire_single_instance_lock(tmp_path)
        assert second is not None
        second.close()

    def test_bypass_env_skips_guard(self, tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> None:
        monkeypatch.setenv("CLORD_ALLOW_MULTI_INSTANCE", "1")
        first = acquire_single_instance_lock(tmp_path)
        second = acquire_single_instance_lock(tmp_path)
        for h in (first, second):
            if h is not None:
                h.close()

    def test_lockfile_lives_under_data_dir(
        self, tmp_path: Path, monkeypatch: pytest.MonkeyPatch
    ) -> None:
        monkeypatch.delenv("CLORD_ALLOW_MULTI_INSTANCE", raising=False)
        handle = acquire_single_instance_lock(tmp_path)
        assert handle is not None
        try:
            lock_files = list(tmp_path.glob(".bot.lock"))
            assert lock_files, "expected .bot.lock to exist under data dir"
        finally:
            handle.close()
