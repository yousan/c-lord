"""Tests for SessionDirManager and related helpers."""

from __future__ import annotations

from pathlib import Path
from unittest.mock import MagicMock, patch

import pytest

from c_lord.session_dir import (
    SessionDirInfo,
    SessionDirManager,
    _is_clean,
    _is_local_repo,
)

# ---------------------------------------------------------------------------
# SessionDirInfo
# ---------------------------------------------------------------------------


class TestSessionDirInfo:
    def test_fields(self) -> None:
        info = SessionDirInfo(
            path="/tmp/sessions/12345",
            thread_id=12345,
            source_repo="/home/user/repo",
            commit="abc1234",
            is_clean=True,
        )
        assert info.path == "/tmp/sessions/12345"
        assert info.thread_id == 12345
        assert info.source_repo == "/home/user/repo"
        assert info.commit == "abc1234"
        assert info.is_clean is True

    def test_frozen(self) -> None:
        import dataclasses

        info = SessionDirInfo(
            path="/tmp/sessions/99",
            thread_id=99,
            source_repo="/repo",
            commit="aaa",
            is_clean=True,
        )
        with pytest.raises(dataclasses.FrozenInstanceError):
            info.path = "/other"  # type: ignore[misc]


# ---------------------------------------------------------------------------
# _is_local_repo
# ---------------------------------------------------------------------------


class TestIsLocalRepo:
    def test_absolute_path(self) -> None:
        assert _is_local_repo("/home/user/repo") is True

    def test_relative_path(self) -> None:
        assert _is_local_repo("./repo") is True
        assert _is_local_repo("../repo") is True

    def test_url(self) -> None:
        assert _is_local_repo("https://github.com/user/repo.git") is False
        assert _is_local_repo("git@github.com:user/repo.git") is False


# ---------------------------------------------------------------------------
# _is_clean (subprocess mock)
# ---------------------------------------------------------------------------


class TestIsClean:
    def test_clean_directory(self) -> None:
        mock_result = MagicMock()
        mock_result.returncode = 0
        mock_result.stdout = ""
        with patch("c_lord.session_dir._run", return_value=mock_result):
            assert _is_clean("/some/path") is True

    def test_dirty_directory(self) -> None:
        mock_result = MagicMock()
        mock_result.returncode = 0
        mock_result.stdout = " M some_file.py\n"
        with patch("c_lord.session_dir._run", return_value=mock_result):
            assert _is_clean("/some/path") is False

    def test_git_error_treated_as_dirty(self) -> None:
        mock_result = MagicMock()
        mock_result.returncode = 128
        mock_result.stdout = ""
        with patch("c_lord.session_dir._run", return_value=mock_result):
            assert _is_clean("/some/path") is False


# ---------------------------------------------------------------------------
# SessionDirManager.create_session_dir
# ---------------------------------------------------------------------------


class TestCreateSessionDir:
    def test_creates_clone_for_local_repo(self, tmp_path: Path) -> None:
        base = str(tmp_path / "sessions")
        source = "/home/user/my-repo"

        mock_result = MagicMock(returncode=0, stderr="")
        with patch("c_lord.session_dir._run", return_value=mock_result) as mock_run:
            mgr = SessionDirManager(base_dir=base, source_repo=source)
            # Pre-create the directory to simulate successful clone
            Path(base).mkdir(parents=True)
            result = mgr.create_session_dir(12345)

        assert result == str(Path(base) / "12345")
        # Verify git clone --local was called
        call_args = mock_run.call_args[0][0]
        assert "clone" in call_args
        assert "--local" in call_args
        assert source in call_args

    def test_creates_clone_for_remote_repo(self, tmp_path: Path) -> None:
        base = str(tmp_path / "sessions")
        source = "https://github.com/user/repo.git"

        mock_result = MagicMock(returncode=0, stderr="")
        with patch("c_lord.session_dir._run", return_value=mock_result) as mock_run:
            mgr = SessionDirManager(base_dir=base, source_repo=source)
            Path(base).mkdir(parents=True)
            result = mgr.create_session_dir(12345)

        assert result == str(Path(base) / "12345")
        call_args = mock_run.call_args[0][0]
        assert "--depth=1" in call_args
        assert "--single-branch" in call_args
        assert "--local" not in call_args

    def test_idempotent_existing_dir(self, tmp_path: Path) -> None:
        base = str(tmp_path / "sessions")
        session_dir = Path(base) / "12345"
        session_dir.mkdir(parents=True)

        with patch("c_lord.session_dir._run") as mock_run:
            mgr = SessionDirManager(base_dir=base, source_repo="/repo")
            result = mgr.create_session_dir(12345)

        assert result == str(session_dir)
        mock_run.assert_not_called()

    def test_clone_failure_raises(self, tmp_path: Path) -> None:
        base = str(tmp_path / "sessions")

        mock_result = MagicMock(returncode=128, stderr="fatal: repository not found")
        with patch("c_lord.session_dir._run", return_value=mock_result):
            mgr = SessionDirManager(base_dir=base, source_repo="/nonexistent")
            with pytest.raises(RuntimeError, match="git clone failed"):
                mgr.create_session_dir(12345)


# ---------------------------------------------------------------------------
# SessionDirManager.find_session_dirs
# ---------------------------------------------------------------------------


class TestFindSessionDirs:
    def test_finds_numeric_dirs_with_git(self, tmp_path: Path) -> None:
        # Valid session dir
        session = tmp_path / "12345"
        session.mkdir()
        (session / ".git").mkdir()

        # Non-numeric dir (should be excluded)
        other = tmp_path / "not-a-session"
        other.mkdir()
        (other / ".git").mkdir()

        # Numeric dir without .git (should be excluded)
        no_git = tmp_path / "99999"
        no_git.mkdir()

        with (
            patch("c_lord.session_dir._get_commit", return_value="abc1234"),
            patch("c_lord.session_dir._is_clean", return_value=True),
        ):
            mgr = SessionDirManager(base_dir=str(tmp_path), source_repo="/repo")
            dirs = mgr.find_session_dirs()

        assert len(dirs) == 1
        assert dirs[0].thread_id == 12345
        assert dirs[0].is_clean is True

    def test_empty_when_no_session_dirs(self, tmp_path: Path) -> None:
        mgr = SessionDirManager(base_dir=str(tmp_path), source_repo="/repo")
        assert mgr.find_session_dirs() == []


# ---------------------------------------------------------------------------
# SessionDirManager.cleanup_for_thread
# ---------------------------------------------------------------------------


class TestCleanupForThread:
    def test_removes_clean_directory(self, tmp_path: Path) -> None:
        session = tmp_path / "999"
        session.mkdir()
        (session / ".git").mkdir()
        (session / "file.txt").write_text("content")

        with patch("c_lord.session_dir._is_clean", return_value=True):
            mgr = SessionDirManager(base_dir=str(tmp_path), source_repo="/repo")
            result = mgr.cleanup_for_thread(999)

        assert result.removed is True
        assert result.thread_id == 999
        assert not session.exists()

    def test_skips_dirty_directory(self, tmp_path: Path) -> None:
        session = tmp_path / "999"
        session.mkdir()
        (session / ".git").mkdir()

        with patch("c_lord.session_dir._is_clean", return_value=False):
            mgr = SessionDirManager(base_dir=str(tmp_path), source_repo="/repo")
            result = mgr.cleanup_for_thread(999)

        assert result.removed is False
        assert "uncommitted changes" in result.reason
        assert session.exists()

    def test_noop_when_dir_missing(self, tmp_path: Path) -> None:
        mgr = SessionDirManager(base_dir=str(tmp_path), source_repo="/repo")
        result = mgr.cleanup_for_thread(42)
        assert result.removed is False
        assert "does not exist" in result.reason


# ---------------------------------------------------------------------------
# SessionDirManager.cleanup_orphaned
# ---------------------------------------------------------------------------


class TestCleanupOrphaned:
    def test_skips_active_sessions(self, tmp_path: Path) -> None:
        session = tmp_path / "555"
        session.mkdir()
        (session / ".git").mkdir()

        with (
            patch("c_lord.session_dir._get_commit", return_value="abc"),
            patch("c_lord.session_dir._is_clean", return_value=True),
        ):
            mgr = SessionDirManager(base_dir=str(tmp_path), source_repo="/repo")
            results = mgr.cleanup_orphaned(active_thread_ids={555})

        assert len(results) == 1
        assert results[0].removed is False
        assert "active" in results[0].reason

    def test_removes_orphaned_clean_directory(self, tmp_path: Path) -> None:
        session = tmp_path / "777"
        session.mkdir()
        (session / ".git").mkdir()

        with (
            patch("c_lord.session_dir._get_commit", return_value="abc"),
            patch("c_lord.session_dir._is_clean", return_value=True),
        ):
            mgr = SessionDirManager(base_dir=str(tmp_path), source_repo="/repo")
            results = mgr.cleanup_orphaned(active_thread_ids=set())

        assert len(results) == 1
        assert results[0].removed is True
