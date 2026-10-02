"""30日スイープが作業フォルダも片付ける — Issue #575。

#554 のスイープは ``sessions`` 行だけを消していた。行はスレッドと作業ディレクトリを
結ぶ唯一の手掛かりなので、**行だけ消すとフォルダは誰にも辿れないまま残る**。本番で
118GB / 719 ディレクトリが溜まっていた直接の原因がこれ。

期間は c-lord が決めない。Claude Code の ``cleanupPeriodDays``（会話ログの保持期間）に
揃える — Claude が会話を忘れた時点で、その会話のために用意したフォルダも用済みに
なるため。決定は 2026-08-27 (yousan)。
"""

from __future__ import annotations

import subprocess
from pathlib import Path

import pytest

from c_lord.database.repository import SessionRecord
from c_lord.session_cleanup import DirOutcome, remove_clean_session_dir


def _repo(path: Path, *, dirty: bool = False) -> None:
    path.mkdir(parents=True, exist_ok=True)
    subprocess.run(["git", "init", "-q"], cwd=path, check=True)
    subprocess.run(["git", "config", "user.email", "t@e.st"], cwd=path, check=True)
    subprocess.run(["git", "config", "user.name", "t"], cwd=path, check=True)
    (path / "a.txt").write_text("x", encoding="utf-8")
    subprocess.run(["git", "add", "-A"], cwd=path, check=True)
    subprocess.run(["git", "commit", "-qm", "init"], cwd=path, check=True)
    if dirty:
        (path / "uncommitted.txt").write_text("work in progress", encoding="utf-8")


def _rec(path: Path | None) -> SessionRecord:
    return SessionRecord(
        thread_id=1,
        session_id="a" * 32,
        working_dir=str(path) if path else "",
        model="opus",
        origin="discord",
        summary=None,
        created_at="2026-01-01 00:00:00",
        last_used_at="2026-01-01 00:00:00",
    )


class TestRemovesOnlyWhatIsSafe:
    def test_a_clean_worktree_is_removed(self, tmp_path) -> None:
        d = tmp_path / "ws"
        _repo(d)

        outcome = remove_clean_session_dir(_rec(d))

        assert outcome is DirOutcome.REMOVED
        assert not d.exists()

    def test_uncommitted_work_is_never_removed(self, tmp_path) -> None:
        """The whole point of the guard. Losing a half-written change to a
        background sweep nobody asked for is unrecoverable."""
        d = tmp_path / "ws"
        _repo(d, dirty=True)

        outcome = remove_clean_session_dir(_rec(d))

        assert outcome is DirOutcome.KEPT_DIRTY
        assert (d / "uncommitted.txt").exists()

    def test_an_untracked_file_counts_as_dirty(self, tmp_path) -> None:
        """``git status --porcelain`` reports untracked files, and it should:
        an image someone dropped in is work too."""
        d = tmp_path / "ws"
        _repo(d)
        (d / "screenshot.png").write_bytes(b"\x89PNG")

        assert remove_clean_session_dir(_rec(d)) is DirOutcome.KEPT_DIRTY
        assert (d / "screenshot.png").exists()

    def test_a_directory_that_is_not_a_git_repo_is_kept(self, tmp_path) -> None:
        """Cleanliness cannot be established, so nothing is destroyed."""
        d = tmp_path / "plain"
        d.mkdir()
        (d / "notes.md").write_text("something", encoding="utf-8")

        assert remove_clean_session_dir(_rec(d)) is DirOutcome.KEPT_DIRTY
        assert d.exists()

    def test_an_already_gone_directory_is_reported_as_absent(self, tmp_path) -> None:
        assert remove_clean_session_dir(_rec(tmp_path / "nope")) is DirOutcome.ABSENT

    def test_a_record_without_a_working_dir_is_absent(self) -> None:
        assert remove_clean_session_dir(_rec(None)) is DirOutcome.ABSENT

    @pytest.mark.parametrize("bad", ["/", "/home", "/home/yousan"])
    def test_refuses_suspiciously_shallow_paths(self, bad: str) -> None:
        """A corrupt ``working_dir`` must not turn the sweep into ``rm -rf /home``.

        Session dirs are always ``<base>/<channel_id>/<thread_id>``; anything
        that shallow is not one, whatever the row claims.
        """
        rec = SessionRecord(
            thread_id=1,
            session_id="a" * 32,
            working_dir=bad,
            model="opus",
            origin="discord",
            summary=None,
            created_at="2026-01-01 00:00:00",
            last_used_at="2026-01-01 00:00:00",
        )
        assert remove_clean_session_dir(rec) is DirOutcome.KEPT_UNSAFE


class TestPeriodFollowsClaudeCode:
    def test_sweep_days_track_the_claude_code_setting(self, tmp_path, monkeypatch) -> None:
        """#575: c-lord does not pick the number — it mirrors Claude Code's."""
        import json

        home = tmp_path / "home"
        (home / ".claude").mkdir(parents=True)
        (home / ".claude" / "settings.json").write_text(
            json.dumps({"cleanupPeriodDays": 60}), encoding="utf-8"
        )
        monkeypatch.setenv("HOME", str(home))

        from c_lord.session_cleanup import sweep_days

        assert sweep_days() == 60

    def test_notice_names_where_the_number_came_from(self) -> None:
        """The reader is being told their thread was cleaned up; they are owed
        the reason the period is what it is, and that c-lord did not choose it."""
        from c_lord.session_cleanup import Survivors, notice_for

        text = notice_for(_rec(None), Survivors(session_dir=False, transcript=False), days=30)

        assert "cleanupPeriodDays" in text


class TestSweepFreesWindowNumber:
    """#595: the 30-day sweep is the automatic delete — it frees the ``w{N}``.

    Only when the checkout actually went: a sweep that kept uncommitted work
    left the workspace in place, so the thread still holds its number.
    """

    def test_clean_checkout_releases_the_number(self, tmp_path: Path, monkeypatch) -> None:
        from c_lord import session_cleanup

        released: list[int] = []
        monkeypatch.setattr(session_cleanup, "release_window_number", released.append)
        _repo(tmp_path / "c-lord-sessions" / "1" / "2")

        rec = _rec(tmp_path / "c-lord-sessions" / "1" / "2")
        assert session_cleanup.retire_swept_workspace(rec) is DirOutcome.REMOVED
        assert released == [1]

    def test_missing_checkout_releases_the_number(self, tmp_path: Path, monkeypatch) -> None:
        from c_lord import session_cleanup

        released: list[int] = []
        monkeypatch.setattr(session_cleanup, "release_window_number", released.append)

        assert session_cleanup.retire_swept_workspace(_rec(None)) is DirOutcome.ABSENT
        assert released == [1]

    def test_dirty_checkout_keeps_the_number(self, tmp_path: Path, monkeypatch) -> None:
        from c_lord import session_cleanup

        released: list[int] = []
        monkeypatch.setattr(session_cleanup, "release_window_number", released.append)
        _repo(tmp_path / "c-lord-sessions" / "1" / "2", dirty=True)

        rec = _rec(tmp_path / "c-lord-sessions" / "1" / "2")
        assert session_cleanup.retire_swept_workspace(rec) is DirOutcome.KEPT_DIRTY
        assert released == []
