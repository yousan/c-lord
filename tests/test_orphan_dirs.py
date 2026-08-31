"""#613 — 孤児作業ディレクトリの回収。

#575 の削除は ``sessions`` 行を消すときに一緒にディレクトリを消す。だから
**行が最初から無いディレクトリには永久に触れない**。本番実測で313件・17.3GB が
そこに溜まっていた。このモジュールはその穴を塞ぐ。

危険な方向は一つしかない: **消しすぎ**。失われた作業は戻らない。だから
どの判定も迷ったら「残す」に倒れることをここで固定する。
"""

from __future__ import annotations

import os
import subprocess
import time
from pathlib import Path

import pytest

from c_lord.orphan_dirs import (
    ORPHAN_SWEEP_DAYS_DEFAULT,
    OrphanCandidate,
    find_orphan_dirs,
    orphan_sweep_days,
    remove_orphan_dir,
    sweep_orphan_dirs,
)
from c_lord.session_cleanup import DirOutcome

DAY = 86400.0


def _git(path: Path, *args: str) -> None:
    subprocess.run(
        ["git", *args],
        cwd=str(path),
        check=True,
        capture_output=True,
        env={
            **os.environ,
            "GIT_AUTHOR_NAME": "t",
            "GIT_AUTHOR_EMAIL": "t@e",
            "GIT_COMMITTER_NAME": "t",
            "GIT_COMMITTER_EMAIL": "t@e",
        },
    )


def _make_workspace(root: Path, channel: str, thread: str, *, git: bool = True) -> Path:
    path = root / channel / thread
    path.mkdir(parents=True)
    (path / "README.md").write_text("hello\n")
    if git:
        _git(path, "init", "-q")
        _git(path, "add", "-A")
        _git(path, "commit", "-qm", "init")
    return path


def _age(path: Path, days: float) -> None:
    """Backdate every mtime the sweep looks at."""
    when = time.time() - days * DAY
    for p in (path, path / ".git"):
        if p.exists():
            os.utime(p, (when, when))


# ── 選別 ─────────────────────────────────────────────────────────────────


def test_finds_a_directory_with_no_db_row(tmp_path: Path) -> None:
    ws = _make_workspace(tmp_path, "111", "222")
    _age(ws, 40)

    found = find_orphan_dirs(tmp_path, known_dirs=set(), min_idle_days=30)

    assert [c.path for c in found] == [ws]


def test_skips_a_directory_that_still_has_a_db_row(tmp_path: Path) -> None:
    ws = _make_workspace(tmp_path, "111", "222")
    _age(ws, 40)

    found = find_orphan_dirs(tmp_path, known_dirs={str(ws)}, min_idle_days=30)

    assert found == []


def test_skips_a_row_recorded_under_a_different_but_equivalent_path(tmp_path: Path) -> None:
    """A row may hold ``/a/b/../b/c``. Resolving both sides keeps it safe."""
    ws = _make_workspace(tmp_path, "111", "222")
    _age(ws, 40)
    odd = str(tmp_path / "111" / ".." / "111" / "222")

    found = find_orphan_dirs(tmp_path, known_dirs={odd}, min_idle_days=30)

    assert found == []


def test_skips_a_directory_touched_recently(tmp_path: Path) -> None:
    ws = _make_workspace(tmp_path, "111", "222")
    _age(ws, 3)

    found = find_orphan_dirs(tmp_path, known_dirs=set(), min_idle_days=30)

    assert found == []


def test_ignores_paths_that_are_not_channel_id_slash_thread_id(tmp_path: Path) -> None:
    """The sweep only ever walks ``<root>/<digits>/<digits>``.

    Anything else under the root belongs to somebody else — a stray checkout, a
    backup, a mistake — and deleting it is not this loop's business.
    """
    stray = tmp_path / "notes" / "draft"
    stray.mkdir(parents=True)
    _age(stray, 99)
    deep = tmp_path / "111" / "222" / "nested"
    deep.mkdir(parents=True)
    _age(deep, 99)

    found = find_orphan_dirs(tmp_path, known_dirs=set(), min_idle_days=30)

    assert [c.path for c in found] == []


def test_reports_how_idle_each_candidate_is(tmp_path: Path) -> None:
    ws = _make_workspace(tmp_path, "111", "222")
    _age(ws, 45)

    (candidate,) = find_orphan_dirs(tmp_path, known_dirs=set(), min_idle_days=30)

    assert isinstance(candidate, OrphanCandidate)
    assert 44 <= candidate.idle_days <= 46


def test_a_missing_root_is_not_an_error(tmp_path: Path) -> None:
    assert find_orphan_dirs(tmp_path / "nope", known_dirs=set(), min_idle_days=30) == []


# ── 削除 ─────────────────────────────────────────────────────────────────


def test_removes_a_clean_checkout(tmp_path: Path) -> None:
    ws = _make_workspace(tmp_path, "111", "222")

    assert remove_orphan_dir(ws) is DirOutcome.REMOVED
    assert not ws.exists()


def test_keeps_a_checkout_with_uncommitted_work(tmp_path: Path) -> None:
    ws = _make_workspace(tmp_path, "111", "222")
    (ws / "draft.md").write_text("half-written\n")

    assert remove_orphan_dir(ws) is DirOutcome.KEPT_DIRTY
    assert (ws / "draft.md").exists()


def test_keeps_a_directory_that_is_not_a_git_repo(tmp_path: Path) -> None:
    """153 of the 313 orphans on the production host are not repos.

    Cleanliness cannot be established there, so there is no way to tell a
    leftover from someone's build output. Keep them.
    """
    ws = _make_workspace(tmp_path, "111", "222", git=False)

    assert remove_orphan_dir(ws) is DirOutcome.KEPT_DIRTY
    assert ws.exists()


def test_refuses_a_suspiciously_shallow_path(tmp_path: Path) -> None:
    assert remove_orphan_dir(Path("/home")) is DirOutcome.KEPT_UNSAFE
    assert Path("/home").is_dir()


def test_a_vanished_directory_is_absent_not_an_error(tmp_path: Path) -> None:
    assert remove_orphan_dir(tmp_path / "a" / "b" / "gone") is DirOutcome.ABSENT


def test_never_follows_a_symlink_out_of_the_tree(tmp_path: Path) -> None:
    """A symlinked workspace must not let the sweep delete its target."""
    real = tmp_path / "elsewhere"
    real.mkdir()
    (real / "keep.txt").write_text("precious\n")
    link = tmp_path / "111" / "222"
    link.parent.mkdir(parents=True)
    link.symlink_to(real, target_is_directory=True)
    _age(real, 99)

    found = find_orphan_dirs(tmp_path, known_dirs=set(), min_idle_days=30)

    assert found == []
    assert (real / "keep.txt").exists()


# ── ひと掃き ───────────────────────────────────────────────────────────────


@pytest.mark.asyncio
async def test_sweep_removes_only_the_clean_orphans(tmp_path: Path) -> None:
    clean = _make_workspace(tmp_path, "111", "1")
    dirty = _make_workspace(tmp_path, "111", "2")
    (dirty / "wip.md").write_text("x\n")
    known = _make_workspace(tmp_path, "111", "3")
    fresh = _make_workspace(tmp_path, "111", "4")
    for ws in (clean, dirty, known):
        _age(ws, 40)
    _age(fresh, 1)

    result = await sweep_orphan_dirs(tmp_path, known_dirs={str(known)}, min_idle_days=30)

    assert result.removed == 1
    assert result.kept == 1
    assert not clean.exists()
    assert dirty.exists() and known.exists() and fresh.exists()


@pytest.mark.asyncio
async def test_sweep_stops_at_the_per_pass_cap(tmp_path: Path) -> None:
    for i in range(5):
        _age(_make_workspace(tmp_path, "111", str(i)), 40)

    result = await sweep_orphan_dirs(tmp_path, known_dirs=set(), min_idle_days=30, max_per_pass=2)

    assert result.removed == 2


@pytest.mark.asyncio
async def test_sweep_over_a_missing_root_does_nothing(tmp_path: Path) -> None:
    result = await sweep_orphan_dirs(tmp_path / "nope", known_dirs=set(), min_idle_days=30)

    assert result.removed == 0 and result.kept == 0


# ── 設定 ─────────────────────────────────────────────────────────────────


def test_default_period_is_thirty_days(monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.delenv("CLORD_ORPHAN_SWEEP_DAYS", raising=False)

    assert orphan_sweep_days() == ORPHAN_SWEEP_DAYS_DEFAULT


def test_period_can_be_raised(monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.setenv("CLORD_ORPHAN_SWEEP_DAYS", "60")

    assert orphan_sweep_days() == 60


def test_zero_disables_the_sweep(monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.setenv("CLORD_ORPHAN_SWEEP_DAYS", "0")

    assert orphan_sweep_days() == 0


def test_nonsense_falls_back_to_the_default(monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.setenv("CLORD_ORPHAN_SWEEP_DAYS", "とても")

    assert orphan_sweep_days() == ORPHAN_SWEEP_DAYS_DEFAULT


# ── ループ ────────────────────────────────────────────────────────────────


class _FakeRepo:
    def __init__(self, dirs: set[str]) -> None:
        self._dirs = dirs
        self.calls = 0

    async def all_working_dirs(self) -> set[str]:
        self.calls += 1
        return self._dirs


class _FakeContainer:
    def __init__(self, name: str) -> None:
        self.name = name


@pytest.mark.asyncio
async def test_tick_reports_containers_that_have_no_workspace(tmp_path: Path) -> None:
    """#612 — ``orphaned_containers`` はここから呼ばれる。

    #573 で実装されながら参照0件だったのがこの関数。呼び出し元を持たない機能は
    存在しないのと同じ、というのが #570 から続く教訓。
    """
    from c_lord.orphan_dirs import OrphanSweepLoop

    seen: list[set[str]] = []

    async def scan(known):
        seen.append(set(known))
        return [_FakeContainer("supabase_db_x"), _FakeContainer("supabase_studio_x")]

    loop = OrphanSweepLoop(
        _FakeRepo({"/somewhere/live"}), tmp_path, min_idle_days=30, container_scan=scan
    )

    result = await loop.tick()

    assert result.orphan_containers == ("supabase_db_x", "supabase_studio_x")
    assert seen == [{"/somewhere/live"}]


@pytest.mark.asyncio
async def test_tick_survives_a_host_without_docker(tmp_path: Path) -> None:
    from c_lord.orphan_dirs import OrphanSweepLoop

    async def scan(known):
        raise FileNotFoundError("docker: command not found")

    loop = OrphanSweepLoop(_FakeRepo(set()), tmp_path, min_idle_days=30, container_scan=scan)

    result = await loop.tick()

    assert result.orphan_containers == ()


@pytest.mark.asyncio
async def test_tick_asks_the_database_which_directories_are_live(tmp_path: Path) -> None:
    """行の一覧が孤児判定の唯一の根拠なので、毎回読み直す。"""
    from c_lord.orphan_dirs import OrphanSweepLoop

    ws = _make_workspace(tmp_path, "111", "222")
    _age(ws, 40)
    repo = _FakeRepo({str(ws)})

    async def scan(known):
        return []

    loop = OrphanSweepLoop(repo, tmp_path, min_idle_days=30, container_scan=scan)
    await loop.tick()

    assert repo.calls == 1
    assert ws.exists(), "a directory the database still knows about must survive"


def test_the_loop_stays_off_when_the_period_is_zero(tmp_path: Path) -> None:
    from c_lord.orphan_dirs import OrphanSweepLoop

    loop = OrphanSweepLoop(_FakeRepo(set()), tmp_path, min_idle_days=0)
    loop.start()

    assert loop._task is None


# ── 掃除が自分の計測を壊さないこと ─────────────────────────────────────────


@pytest.mark.asyncio
async def test_a_kept_directory_is_still_old_on_the_next_pass(tmp_path: Path) -> None:
    """``git status`` は ``.git`` の mtime を進める。それをアイドル判定に使うと、
    **掃除が自分の見ている時計を毎回リセットする**。

    残す判定（未コミットあり）を受けたディレクトリには毎回 ``git status`` が
    走るので、``.git`` を見る実装では2周目以降そのディレクトリが永久に
    「たった今使われた」に見える。本番データで実際に踏んだ: 孤児160件すべてが
    ``.git`` mtime で「0.0日前」を返し、候補が0件になった。
    """
    dirty = _make_workspace(tmp_path, "111", "222")
    (dirty / "wip.md").write_text("x\n")
    _age(dirty, 40)

    first = await sweep_orphan_dirs(tmp_path, known_dirs=set(), min_idle_days=30)
    assert first.kept == 1, "未コミットのあるディレクトリは残る"

    # 1周目の git status が .git を触った後でも、まだ古いままでなければならない。
    again = find_orphan_dirs(tmp_path, known_dirs=set(), min_idle_days=30)

    assert [c.path for c in again] == [dirty]


def test_a_fresh_git_directory_does_not_make_an_old_checkout_look_used(
    tmp_path: Path,
) -> None:
    ws = _make_workspace(tmp_path, "111", "222")
    _age(ws, 40)
    now = time.time()
    os.utime(ws / ".git", (now, now))  # git status がやることと同じ

    found = find_orphan_dirs(tmp_path, known_dirs=set(), min_idle_days=30)

    assert [c.path for c in found] == [ws]
