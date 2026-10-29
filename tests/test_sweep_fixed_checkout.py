"""30日スイープは c-lord が clone した session dir だけを消す — Issue #902。

2026-10-13、週次精査の固定 checkout ``/home/yousan/c-lord-audit``（スケジュール実行の
作業場所。中に git 管理外の手順書 ``AUDIT.md``）が、1か月前に一度だけそこを使った
古いスレッドの行のスイープで丸ごと ``rmtree`` された。同じ場所を今週も使っていた
週次精査スレッドは、次の依頼で空の clone・会話ゼロから始まった。

固定 checkout は利用者が用意したもので、c-lord が作ったものではない (#687)。
消してよいのは ``<base>/<channel_id>/<thread_id>`` — c-lord がそのスレッドのために
clone したディレクトリだけ。
"""

from __future__ import annotations

import logging
import subprocess
from pathlib import Path

from c_lord.database.repository import SessionRecord
from c_lord.session_cleanup import DirOutcome, remove_clean_session_dir, retire_swept_workspace

THREAD = 1545252058666635266


def _repo(path: Path) -> Path:
    path.mkdir(parents=True, exist_ok=True)
    subprocess.run(["git", "init", "-q"], cwd=path, check=True)
    subprocess.run(["git", "config", "user.email", "t@e.st"], cwd=path, check=True)
    subprocess.run(["git", "config", "user.name", "t"], cwd=path, check=True)
    (path / "a.txt").write_text("x", encoding="utf-8")
    subprocess.run(["git", "add", "-A"], cwd=path, check=True)
    subprocess.run(["git", "commit", "-qm", "init"], cwd=path, check=True)
    # #903: the commit is on origin, so only the location decides here.
    subprocess.run(["git", "update-ref", "refs/remotes/origin/main", "HEAD"], cwd=path, check=True)
    return path


def _rec(path: Path, thread_id: int = THREAD) -> SessionRecord:
    return SessionRecord(
        thread_id=thread_id,
        session_id="a" * 32,
        working_dir=str(path),
        model="opus",
        origin="discord",
        summary=None,
        created_at="2026-09-01 00:00:00",
        last_used_at="2026-09-11 00:00:00",
    )


def test_fixed_checkout_outside_the_sessions_root_is_kept(tmp_path: Path, caplog) -> None:
    """AC1 + AC3: 本番で消えた ``/home/yousan/c-lord-audit`` の再現。"""
    root = tmp_path / "c-lord-sessions"
    audit = _repo(tmp_path / "home" / "yousan" / "c-lord-audit")

    with caplog.at_level(logging.INFO, logger="c_lord.session_cleanup"):
        outcome = remove_clean_session_dir(_rec(audit), sessions_root=root)

    assert outcome is not DirOutcome.REMOVED
    assert outcome is DirOutcome.KEPT_FOREIGN
    assert audit.is_dir()
    assert "not a session dir (fixed checkout)" in caplog.text
    assert str(audit) in caplog.text


def test_another_threads_session_dir_is_kept(tmp_path: Path) -> None:
    """``<root>/<ch>/<別スレッド>`` もこのスレッドのものではない。"""
    root = tmp_path / "c-lord-sessions"
    other = _repo(root / "1505747831447883806" / "1550325631785508896")

    assert remove_clean_session_dir(_rec(other), sessions_root=root) is DirOutcome.KEPT_FOREIGN
    assert other.is_dir()


def test_own_session_dir_is_still_removed(tmp_path: Path) -> None:
    """消せるものは従来どおり消える（#575 の回帰なし）。"""
    root = tmp_path / "c-lord-sessions"
    own = _repo(root / "1505747831447883806" / str(THREAD))

    assert remove_clean_session_dir(_rec(own), sessions_root=root) is DirOutcome.REMOVED
    assert not own.exists()


def test_own_session_dir_via_a_relative_root_is_removed(tmp_path: Path, monkeypatch) -> None:
    """既定の ``data/sessions`` は相対パス。cwd 基準で同じ場所を指すなら同じもの。"""
    monkeypatch.chdir(tmp_path)
    own = _repo(tmp_path / "data" / "sessions" / "1" / str(THREAD))

    assert remove_clean_session_dir(_rec(own), sessions_root="data/sessions") is (
        DirOutcome.REMOVED
    )


def test_dir_another_live_row_still_points_at_is_kept(tmp_path: Path, caplog) -> None:
    """AC2: スイープ対象でない行が同じ working_dir を指していれば残す。"""
    root = tmp_path / "c-lord-sessions"
    own = _repo(root / "1505747831447883806" / str(THREAD))

    with caplog.at_level(logging.INFO, logger="c_lord.session_cleanup"):
        outcome = remove_clean_session_dir(_rec(own), sessions_root=root, claimed={str(own) + "/"})

    assert outcome is DirOutcome.KEPT_FOREIGN
    assert own.is_dir()
    assert "still used by another session" in caplog.text


def test_fixed_checkout_from_the_production_incident_is_kept_even_if_claimed(
    tmp_path: Path,
) -> None:
    """AC2 の本番形: 週次精査スレッド W9 の行が同じ固定 checkout を指している。"""
    root = tmp_path / "c-lord-sessions"
    audit = _repo(tmp_path / "c-lord-audit")

    outcome = remove_clean_session_dir(_rec(audit), sessions_root=root, claimed={str(audit)})

    assert outcome is not DirOutcome.REMOVED
    assert audit.is_dir()


def test_unknown_sessions_root_keeps_everything(tmp_path: Path) -> None:
    """どこが session dir か分からないなら、消さない側に倒す。"""
    own = _repo(tmp_path / "c-lord-sessions" / "1" / str(THREAD))

    assert remove_clean_session_dir(_rec(own), sessions_root=None) is DirOutcome.KEPT_FOREIGN
    assert own.is_dir()


def test_retiring_a_fixed_checkout_row_frees_its_window_number(tmp_path: Path, monkeypatch) -> None:
    """固定 checkout はスレッドの持ち物ではない — 残しても、スレッドの w{N} は返す。"""
    from c_lord import session_cleanup

    released: list[int] = []
    monkeypatch.setattr(session_cleanup, "release_window_number", released.append)
    audit = _repo(tmp_path / "c-lord-audit")

    outcome = retire_swept_workspace(_rec(audit), sessions_root=tmp_path / "c-lord-sessions")

    assert outcome is DirOutcome.KEPT_FOREIGN
    assert released == [THREAD]
    assert audit.is_dir()
