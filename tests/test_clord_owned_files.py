"""#749 — c-lord が自分で置いたファイルは「利用者の未コミット作業」に数えない。

c-lord は毎ターン ``<session_dir>/.claude/skills/discord-read/SKILL.md`` を書く
(#259)。clone の ``.gitignore`` はそれを知らないので untracked になり、
``git status --porcelain`` が空でなくなる。掃除はそれを「未コミットの作業」と
読んで削除を拒む — **c-lord が自分のゴミを人質に取る**。本番では孤児スイープが
105 回連続で 0 バイトしか回収せず、80 件中 60 件・9.6 GB がこれだった。

さらに同じ判定を使う 30 日スイープ (#554) は、そういうスレッドに
「書きかけの成果物はディスク上にそのままあります」と**嘘の案内**を出していた。

**安全不変条件は崩さない**: 利用者の変更が 1 つでもあれば残す。ここで固定するのは
「何を利用者の変更と数えないか」の境界だけで、どのテストも「迷ったら残す」側に倒れる。
"""

from __future__ import annotations

import logging
import os
import subprocess
import time
from pathlib import Path

import pytest

from c_lord.database.repository import SessionRecord
from c_lord.orphan_dirs import remove_orphan_dir, sweep_orphan_dirs
from c_lord.session_cleanup import (
    DirOutcome,
    inspect_survivors,
    notice_for,
    remove_clean_session_dir,
)
from c_lord.session_dir import _is_clean, worktree_status
from c_lord.skills.injector import LEGACY_SKILL_NAMES, inject_read_skill

_GIT_ENV = {
    **os.environ,
    "GIT_AUTHOR_NAME": "t",
    "GIT_AUTHOR_EMAIL": "t@e",
    "GIT_COMMITTER_NAME": "t",
    "GIT_COMMITTER_EMAIL": "t@e",
}


def _git(path: Path, *args: str) -> None:
    subprocess.run(["git", *args], cwd=str(path), check=True, capture_output=True, env=_GIT_ENV)


def _clone(path: Path) -> Path:
    """A committed checkout — what ``create_session_dir`` leaves behind."""
    path.mkdir(parents=True)
    (path / "README.md").write_text("hello\n")
    _git(path, "init", "-q")
    _git(path, "add", "-A")
    _git(path, "commit", "-qm", "init")
    return path


def _legacy_skill(ws: Path, name: str) -> None:
    """What a pre-#712 session dir still has on disk."""
    d = ws / ".claude" / "skills" / name
    d.mkdir(parents=True)
    (d / "SKILL.md").write_text("# legacy\n")


def _age(path: Path, days: float) -> None:
    when = time.time() - days * 86400.0
    os.utime(path, (when, when))


# ── AC5: 判定の境界 ─────────────────────────────────────────────────────────


def test_only_the_injected_discord_read_skill_is_clean(tmp_path: Path) -> None:
    """本番 60 件の姿。c-lord が書いた SKILL.md 1 枚は作業ではない。"""
    ws = _clone(tmp_path / "ws")
    inject_read_skill(ws, env_path="/srv/c-lord/.env")

    assert _is_clean(str(ws)) is True


def test_the_injected_skill_plus_a_user_file_is_still_dirty(tmp_path: Path) -> None:
    ws = _clone(tmp_path / "ws")
    inject_read_skill(ws)
    (ws / "foo.py").write_text("print('wip')\n")

    assert _is_clean(str(ws)) is False


def test_retired_skill_dirs_are_not_work(tmp_path: Path) -> None:
    """#712 で製品から消した discord-reply / discord-prompt-choice の残骸。

    c-lord は毎ターンこのディレクトリを丸ごと ``rmtree`` する (``remove_legacy_skills``)
    — 中に何があっても次のターンで消える場所なので、作業の置き場になりえない。
    """
    ws = _clone(tmp_path / "ws")
    for name in LEGACY_SKILL_NAMES:
        _legacy_skill(ws, name)

    assert _is_clean(str(ws)) is True


def test_other_files_under_dot_claude_are_still_work(tmp_path: Path) -> None:
    """``.claude/`` は c-lord の持ち物ではない。利用者の設定や skill が入る場所。"""
    ws = _clone(tmp_path / "ws")
    inject_read_skill(ws)
    (ws / ".claude" / "settings.local.json").write_text("{}\n")

    assert _is_clean(str(ws)) is False


def test_a_users_own_skill_is_still_work(tmp_path: Path) -> None:
    ws = _clone(tmp_path / "ws")
    inject_read_skill(ws)
    mine = ws / ".claude" / "skills" / "my-skill"
    mine.mkdir(parents=True)
    (mine / "SKILL.md").write_text("# mine\n")

    assert _is_clean(str(ws)) is False


def test_an_extra_file_next_to_the_injected_skill_is_still_work(tmp_path: Path) -> None:
    """c-lord が discord-read/ に書くのは SKILL.md だけ。隣の別ファイルは誰かの物。"""
    ws = _clone(tmp_path / "ws")
    inject_read_skill(ws)
    (ws / ".claude" / "skills" / "discord-read" / "notes.md").write_text("mine\n")

    assert _is_clean(str(ws)) is False


def test_a_tracked_skill_overwritten_by_the_injector_is_clean(tmp_path: Path) -> None:
    """リポジトリが SKILL.md を追跡していても、差分が作業ツリーだけなら c-lord の上書き。

    c-lord 自身のリポジトリがこの形になっている（2026-09-08 に注入物がコミットに
    混ざった）。index は HEAD のまま・作業ツリーだけ違う = 毎ターン注入が上書きした
    結果で、そこに置いた未コミットの編集は次のターンで必ず消える。
    """
    ws = _clone(tmp_path / "ws")
    inject_read_skill(ws, env_path="/old/.env")
    _git(ws, "add", "-A")
    _git(ws, "commit", "-qm", "oops: injected skill got committed")
    inject_read_skill(ws, env_path="/new/.env")

    assert _is_clean(str(ws)) is True


def test_a_staged_change_to_the_skill_is_work(tmp_path: Path) -> None:
    """``git add`` したのは誰かの判断。index に載った変更は数える。"""
    ws = _clone(tmp_path / "ws")
    inject_read_skill(ws, env_path="/old/.env")
    _git(ws, "add", "-A")
    _git(ws, "commit", "-qm", "tracked")
    inject_read_skill(ws, env_path="/new/.env")
    _git(ws, "add", "-A")

    assert _is_clean(str(ws)) is False


def test_a_clean_claude_code_worktree_is_not_work(tmp_path: Path) -> None:
    """``.claude/worktrees/<name>`` は Claude Code のサブエージェントが作る worktree。

    親の ``git status`` には中身を見ずに ``?? .claude/worktrees/<name>/`` とだけ出る。
    **中を確かめて**、未コミットの変更が無いときだけ作業に数えない。
    """
    ws = _clone(tmp_path / "ws")
    inject_read_skill(ws)
    _git(ws, "worktree", "add", "-q", ".claude/worktrees/agent-a1", "-b", "agent-a1")

    assert _is_clean(str(ws)) is True


def test_uncommitted_work_inside_a_worktree_is_work(tmp_path: Path) -> None:
    ws = _clone(tmp_path / "ws")
    _git(ws, "worktree", "add", "-q", ".claude/worktrees/agent-a1", "-b", "agent-a1")
    (ws / ".claude" / "worktrees" / "agent-a1" / "half.py").write_text("x = 1\n")

    assert _is_clean(str(ws)) is False


def test_a_plain_directory_under_worktrees_is_work(tmp_path: Path) -> None:
    """git の worktree でないものは中身を確かめられない — 残す。"""
    ws = _clone(tmp_path / "ws")
    loose = ws / ".claude" / "worktrees" / "scratch"
    loose.mkdir(parents=True)
    (loose / "notes.txt").write_text("x\n")

    assert _is_clean(str(ws)) is False


def test_a_path_that_needs_quoting_is_still_classified(tmp_path: Path) -> None:
    """空白や非 ASCII を含むパスで判定が崩れない（``-z`` で読む）。"""
    ws = _clone(tmp_path / "ws")
    inject_read_skill(ws)
    (ws / "下書き と メモ.md").write_text("wip\n")

    status = worktree_status(str(ws))

    assert status.user_changes == ("?? 下書き と メモ.md",)
    assert status.clord_files == ("?? .claude/skills/discord-read/SKILL.md",)


def test_not_a_git_repo_is_never_clean(tmp_path: Path) -> None:
    plain = tmp_path / "plain"
    plain.mkdir()
    inject_read_skill(plain)

    status = worktree_status(str(plain))

    assert status.is_repo is False
    assert _is_clean(str(plain)) is False


# ── AC1: 孤児スイープが c-lord のファイルだけのディレクトリを消す ────────────


def test_orphan_with_only_clord_files_is_removed(tmp_path: Path) -> None:
    ws = _clone(tmp_path / "111" / "222")
    inject_read_skill(ws)
    _legacy_skill(ws, "discord-reply")

    assert remove_orphan_dir(ws) is DirOutcome.REMOVED
    assert not ws.exists()


def test_orphan_with_user_work_is_kept(tmp_path: Path) -> None:
    """AC2: 本番の残り 20 件の形。"""
    ws = _clone(tmp_path / "111" / "222")
    inject_read_skill(ws)
    (ws / ".env.dev-local").write_text("SECRET=1\n")

    assert remove_orphan_dir(ws) is DirOutcome.KEPT_DIRTY
    assert (ws / ".env.dev-local").exists()


# ── AC3 / AC4: なぜ残したかが INFO で分かる ───────────────────────────────


def test_keeping_logs_the_entries_at_info(tmp_path: Path, caplog: pytest.LogCaptureFixture) -> None:
    ws = _clone(tmp_path / "111" / "222")
    inject_read_skill(ws)
    (ws / "wip.md").write_text("x\n")

    with caplog.at_level(logging.INFO, logger="c_lord.orphan_dirs"):
        remove_orphan_dir(ws)

    (line,) = [r for r in caplog.records if "keeping" in r.getMessage()]
    assert line.levelno == logging.INFO
    assert str(ws) in line.getMessage()
    assert "wip.md" in line.getMessage()
    # c-lord の SKILL.md は残した理由に数えない
    assert "discord-read" not in line.getMessage()


def test_keeping_a_non_repo_says_so_at_info(
    tmp_path: Path, caplog: pytest.LogCaptureFixture
) -> None:
    ws = tmp_path / "111" / "222"
    ws.mkdir(parents=True)
    (ws / "out.bin").write_bytes(b"x")

    with caplog.at_level(logging.INFO, logger="c_lord.orphan_dirs"):
        remove_orphan_dir(ws)

    (line,) = [r for r in caplog.records if "keeping" in r.getMessage()]
    assert line.levelno == logging.INFO
    assert "not a git repo" in line.getMessage()


@pytest.mark.asyncio
async def test_summary_separates_clord_leftovers_from_user_work(
    tmp_path: Path, caplog: pytest.LogCaptureFixture
) -> None:
    only_clord = _clone(tmp_path / "111" / "1")
    inject_read_skill(only_clord)
    plain_clean = _clone(tmp_path / "111" / "2")
    user_work = _clone(tmp_path / "111" / "3")
    inject_read_skill(user_work)
    (user_work / "draft.md").write_text("x\n")
    not_repo = tmp_path / "111" / "4"
    not_repo.mkdir()
    (not_repo / "a.txt").write_text("x\n")
    for ws in (only_clord, plain_clean, user_work, not_repo):
        _age(ws, 40)

    with caplog.at_level(logging.INFO, logger="c_lord.orphan_dirs"):
        result = await sweep_orphan_dirs(tmp_path, known_dirs=set(), min_idle_days=30)

    assert (result.removed, result.kept) == (2, 2)
    assert result.removed_clord_only == 1
    assert result.kept_user_work == 1
    assert result.kept_not_repo == 1
    (summary,) = [r.getMessage() for r in caplog.records if "directory(ies)" in r.getMessage()]
    assert "1 held only c-lord's own files" in summary
    assert "1 with uncommitted user work" in summary
    assert "1 not a git repo" in summary


# ── AC7: 「書きかけの成果物が残っています」と嘘をつかない ───────────────────


def _record(path: Path) -> SessionRecord:
    return SessionRecord(
        thread_id=1535220696232370267,
        session_id="a" * 32,
        working_dir=str(path),
        model="opus",
        origin="discord",
        summary=None,
        created_at="2026-07-01 00:00:00",
        last_used_at="2026-08-01 00:00:00",
    )


def test_thirty_day_notice_does_not_claim_leftover_work(tmp_path: Path) -> None:
    """本番で 7 件が受け取った嘘の案内の再現。中身は c-lord の SKILL.md だけ。"""
    ws = _clone(tmp_path / "111" / "222")
    inject_read_skill(ws)
    record = _record(ws)

    assert remove_clean_session_dir(record) is DirOutcome.REMOVED
    text = notice_for(record, inspect_survivors(record, projects_root=tmp_path / "projects"))

    assert "書きかけの成果物" not in text
    assert "/clord-reattach" not in text


def test_thirty_day_notice_still_points_at_real_leftover_work(tmp_path: Path) -> None:
    ws = _clone(tmp_path / "111" / "222")
    inject_read_skill(ws)
    (ws / "article.md").write_text("半分書いた記事\n")
    record = _record(ws)

    assert remove_clean_session_dir(record) is DirOutcome.KEPT_DIRTY
    text = notice_for(record, inspect_survivors(record, projects_root=tmp_path / "projects"))

    assert "書きかけの成果物" in text
    assert (ws / "article.md").exists()
