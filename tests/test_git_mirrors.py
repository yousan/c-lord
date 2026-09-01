"""#643 — 同じリポジトリを何度もクローンするのをやめる。

本番実測 (tachikoma / 2026-08-31): ``c-lord-sessions`` 121 GB のうち **117 GB が
1リポジトリ (``project_30_ehon-ya``) の240クローン**で、その ``.git`` だけで
39.2 GB。中身は240個とも同じ。全履歴の bare ミラー1個は **256 MB** しかない。

このモジュールが守らなければならない不変条件は2つ:

1. **ミラーが無くても・作れなくても、クローンは今までどおり成功する。** ミラーは
   高速化と節約のためのものであって、動作の前提ではない
2. **ミラーのパスがミラー置き場の外に出ない。** origin URL は ``/clord repo:`` で
   利用者入力から届く (#514)
"""

from __future__ import annotations

import os
import subprocess
from pathlib import Path

import pytest

from c_lord.git_mirrors import (
    MIRRORS_DIR_NAME,
    ensure_mirror,
    mirror_name,
    mirrors_enabled,
    mirrors_root_for,
    normalise_origin,
)


def _git(path: Path, *args: str) -> subprocess.CompletedProcess[str]:
    return subprocess.run(
        ["git", *args],
        cwd=str(path),
        check=True,
        capture_output=True,
        text=True,
        env={
            **os.environ,
            "GIT_AUTHOR_NAME": "t",
            "GIT_AUTHOR_EMAIL": "t@e",
            "GIT_COMMITTER_NAME": "t",
            "GIT_COMMITTER_EMAIL": "t@e",
        },
    )


@pytest.fixture
def upstream(tmp_path: Path) -> Path:
    """ネットワークを使わずに「remote」の役をする実リポジトリ。"""
    repo = tmp_path / "upstream"
    repo.mkdir()
    _git(repo, "init", "-q", "-b", "main")
    (repo / "README.md").write_text("hello\n")
    _git(repo, "add", "-A")
    _git(repo, "commit", "-qm", "init")
    return repo


# --------------------------------------------------------------------------
# origin URL の正規化 — 同じリポジトリが同じミラーに解決されること
# --------------------------------------------------------------------------


def test_the_four_url_forms_of_one_repo_share_one_mirror() -> None:
    """本番に実在する4つの表記が同じミラーを指す。

    実測では ``yousan/c-lord`` が ``git@`` 125件 / ``https://...git`` 10件 /
    ``https://...`` 6件に分かれていた。ここが分かれるとミラーが3つでき、
    節約が丸ごと消える。
    """
    forms = [
        "git@github.com:yousan/c-lord.git",
        "https://github.com/yousan/c-lord.git",
        "https://github.com/yousan/c-lord",
        "ssh://git@github.com/yousan/c-lord.git",
    ]
    assert len({normalise_origin(f) for f in forms}) == 1
    assert len({mirror_name(f) for f in forms}) == 1


def test_different_repos_get_different_mirrors() -> None:
    assert mirror_name("git@github.com:yousan/c-lord.git") != mirror_name(
        "git@github.com:yousan/pt-jp.git"
    )


def test_case_and_trailing_slash_do_not_split_the_mirror() -> None:
    assert mirror_name("https://GitHub.com/Yousan/C-Lord/") == mirror_name(
        "git@github.com:yousan/c-lord.git"
    )


# --------------------------------------------------------------------------
# パスの安全性 — origin は利用者入力 (#514)
# --------------------------------------------------------------------------


@pytest.mark.parametrize(
    "hostile",
    [
        "git@github.com:../../../etc/passwd",
        "https://example.com/a/../../../../tmp/x",
        "../../../../etc/passwd",
        "https://example.com/" + "a" * 500,
        "git@github.com:a/b\n../../c",
    ],
)
def test_mirror_name_can_never_escape_the_mirror_dir(hostile: str, tmp_path: Path) -> None:
    """名前に区切りも ``..`` も残らない。"""
    name = mirror_name(hostile)
    assert "/" not in name
    assert "\\" not in name
    assert ".." not in name
    assert "\n" not in name
    root = tmp_path / MIRRORS_DIR_NAME
    assert (root / name).resolve().parent == root.resolve()


def test_mirrors_root_sits_beside_the_workspaces_not_inside_one(tmp_path: Path) -> None:
    """``<sessions root>/.mirrors`` — チャンネルディレクトリと同じ階層。

    ワークスペースは ``<root>/<channel>/<thread>``。ミラーをその形の中に置くと
    #613 の掃除の射程に入りかねないので、``<root>`` 直下に、数字でない名前で置く。
    """
    base_dir = tmp_path / "sessions" / "1440498354797674496"
    root = mirrors_root_for(str(base_dir))
    assert root == tmp_path / "sessions" / MIRRORS_DIR_NAME
    assert not MIRRORS_DIR_NAME.isdigit()


# --------------------------------------------------------------------------
# 無効化
# --------------------------------------------------------------------------


def test_enabled_by_default(monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.delenv("CLORD_GIT_MIRRORS", raising=False)
    assert mirrors_enabled() is True


@pytest.mark.parametrize("value", ["0", "false", "no", "off", "FALSE"])
def test_disabled_by_env(value: str, monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.setenv("CLORD_GIT_MIRRORS", value)
    assert mirrors_enabled() is False


def test_ensure_mirror_returns_none_when_disabled(
    tmp_path: Path, upstream: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    monkeypatch.setenv("CLORD_GIT_MIRRORS", "0")
    assert ensure_mirror(tmp_path / MIRRORS_DIR_NAME, str(upstream)) is None


# --------------------------------------------------------------------------
# ミラーの作成
# --------------------------------------------------------------------------


def test_creates_a_bare_mirror(tmp_path: Path, upstream: Path) -> None:
    root = tmp_path / MIRRORS_DIR_NAME
    path = ensure_mirror(root, str(upstream))
    assert path is not None
    assert path.is_dir()
    assert (path / "objects").is_dir()
    assert not (path / ".git").exists(), "bare なので .git は無い"
    got = subprocess.run(
        ["git", "-C", str(path), "config", "--get", "core.bare"],
        capture_output=True,
        text=True,
    )
    assert got.stdout.strip() == "true"


def test_mirror_never_prunes_its_own_objects(tmp_path: Path, upstream: Path) -> None:
    """``gc.auto=0``。

    ミラーはクローンから参照されている。自動 gc がオブジェクトを刈ると、
    参照している側が壊れる。ミラーは足すだけの置き場でなければならない。
    """
    path = ensure_mirror(tmp_path / MIRRORS_DIR_NAME, str(upstream))
    assert path is not None
    got = subprocess.run(
        ["git", "-C", str(path), "config", "--get", "gc.auto"],
        capture_output=True,
        text=True,
    )
    assert got.stdout.strip() == "0"


def test_readme_tells_a_human_what_this_is(tmp_path: Path, upstream: Path) -> None:
    """人間が消す事故がこの設計の唯一の弱点なので、そこに手当てする。"""
    root = tmp_path / MIRRORS_DIR_NAME
    ensure_mirror(root, str(upstream))
    readme = root / "README.md"
    assert readme.is_file()
    assert "c-lord" in readme.read_text()


def test_second_call_reuses_the_mirror(tmp_path: Path, upstream: Path) -> None:
    root = tmp_path / MIRRORS_DIR_NAME
    first = ensure_mirror(root, str(upstream))
    assert first is not None
    marker = first / "clord-marker"
    marker.write_text("x")
    second = ensure_mirror(root, str(upstream))
    assert second == first
    assert marker.exists(), "作り直してはいけない"


def test_existing_mirror_is_updated_from_upstream(tmp_path: Path, upstream: Path) -> None:
    """新しいコミットがミラーに入ること。

    入らないと、クローン側が自分で持つ量が増えていき節約が減る。
    """
    root = tmp_path / MIRRORS_DIR_NAME
    ensure_mirror(root, str(upstream))
    (upstream / "NEW.md").write_text("new\n")
    _git(upstream, "add", "-A")
    _git(upstream, "commit", "-qm", "second")
    head = _git(upstream, "rev-parse", "HEAD").stdout.strip()

    path = ensure_mirror(root, str(upstream))
    assert path is not None
    got = subprocess.run(["git", "-C", str(path), "cat-file", "-e", head])
    assert got.returncode == 0, "fetch されていない"


def test_no_partial_mirror_is_ever_left_behind(tmp_path: Path) -> None:
    """クローンに失敗したら **何も残さない**。

    半端なミラーを参照したクローンは、オブジェクトが欠けて壊れる。
    """
    root = tmp_path / MIRRORS_DIR_NAME
    assert ensure_mirror(root, str(tmp_path / "does-not-exist")) is None
    leftovers = [p for p in root.iterdir() if p.name != "README.md"] if root.is_dir() else []
    assert leftovers == []


def test_failure_never_raises(tmp_path: Path) -> None:
    """クローンを止めてよい理由にはならない。"""
    assert ensure_mirror(tmp_path / MIRRORS_DIR_NAME, "git@example.invalid:no/such.git") is None


# --------------------------------------------------------------------------
# session_dir との結線 — ここが繋がっていなければ 39 GB は減らない
# --------------------------------------------------------------------------


def _seed_upstream(tmp_path: Path) -> Path:
    repo = tmp_path / "remote"
    repo.mkdir()
    _git(repo, "init", "-q", "-b", "main")
    # 圧縮できない中身にして、共有の有無が .git のサイズに出るようにする。
    (repo / "blob.bin").write_bytes(os.urandom(3_000_000))
    _git(repo, "add", "-A")
    _git(repo, "commit", "-qm", "init")
    return repo


def test_a_clone_shares_objects_with_the_mirror(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    """**このIssueの回収量そのもの。** 2つ目のクローンの ``.git`` が小さいこと。

    ``file://`` を使うのは、``_is_local_repo`` が真になる素のパスだと
    ``git clone --local`` の経路に入ってしまい、remote クローンの検証に
    ならないため。git から見た扱いは remote と同じ。
    """
    from c_lord.session_dir import SessionDirManager

    upstream = _seed_upstream(tmp_path)

    def kb(path: Path) -> int:
        return sum(f.stat().st_size for f in path.rglob("*") if f.is_file()) // 1024

    # 共有なし (今までの挙動) — 比較の基準。
    plain_mgr = SessionDirManager(
        base_dir=str(tmp_path / "plain" / "111"), source_repo=f"file://{upstream}"
    )
    monkeypatch.setenv("CLORD_GIT_MIRRORS", "0")
    plain = Path(plain_mgr.create_session_dir(1000))
    monkeypatch.delenv("CLORD_GIT_MIRRORS")

    base = tmp_path / "sessions" / "111"
    manager = SessionDirManager(base_dir=str(base), source_repo=f"file://{upstream}")
    first = Path(manager.create_session_dir(1001))
    second = Path(manager.create_session_dir(1002))

    assert (tmp_path / "sessions" / MIRRORS_DIR_NAME).is_dir()
    for shared in (first, second):
        assert (shared / ".git" / "objects" / "info" / "alternates").is_file(), (
            f"{shared.name} がミラーを参照していない"
        )
        assert kb(shared / ".git") * 4 < kb(plain / ".git"), (
            f"共有できていない: 共有なし {kb(plain / '.git')} KB / "
            f"共有あり {kb(shared / '.git')} KB"
        )
    # 共有していても中身は完全であること。
    assert (second / "blob.bin").is_file()
    assert (
        subprocess.run(
            ["git", "-C", str(second), "fsck", "--connectivity-only"],
            capture_output=True,
        ).returncode
        == 0
    )
    # 追跡されているファイルに差分が無いこと。``??`` は c-lord 自身が注入する
    # ``.claude/skills/`` (#52) なので、共有とは無関係。
    status = subprocess.run(
        ["git", "-C", str(second), "status", "--porcelain"],
        capture_output=True,
        text=True,
    ).stdout.splitlines()
    assert [line for line in status if not line.startswith("??")] == []


def test_local_repos_do_not_get_a_mirror(tmp_path: Path) -> None:
    """``--local`` は既に hardlink で共有しているので、ミラーは無駄。

    本番実測でも ``/home/yousan/c-lord`` の300クローンは合計 0.5 GB しかない。
    """
    from c_lord.session_dir import SessionDirManager

    upstream = _seed_upstream(tmp_path)
    base = tmp_path / "sessions" / "222"
    manager = SessionDirManager(base_dir=str(base), source_repo=str(upstream))
    manager.create_session_dir(2001)
    assert not (tmp_path / "sessions" / MIRRORS_DIR_NAME).exists()


def test_clone_still_works_when_mirrors_are_disabled(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    from c_lord.session_dir import SessionDirManager

    monkeypatch.setenv("CLORD_GIT_MIRRORS", "0")
    upstream = _seed_upstream(tmp_path)
    base = tmp_path / "sessions" / "333"
    manager = SessionDirManager(base_dir=str(base), source_repo=f"file://{upstream}")
    created = Path(manager.create_session_dir(3001))
    assert (created / "blob.bin").is_file()
    assert not (created / ".git" / "objects" / "info" / "alternates").exists()
    assert not (tmp_path / "sessions" / MIRRORS_DIR_NAME).exists()


def test_clone_still_works_when_the_mirror_cannot_be_built(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    """**不変条件1**: ミラーが作れなくてもクローンは成功する。"""
    from c_lord import session_dir as sd

    monkeypatch.setattr(sd, "ensure_mirror", lambda *a, **k: None)
    upstream = _seed_upstream(tmp_path)
    base = tmp_path / "sessions" / "444"
    manager = sd.SessionDirManager(base_dir=str(base), source_repo=f"file://{upstream}")
    created = Path(manager.create_session_dir(4001))
    assert (created / "blob.bin").is_file()


def test_a_raising_mirror_helper_cannot_break_a_clone(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    """**不変条件1** の強い版: 例外が出てもクローンは成功する。"""
    from c_lord import session_dir as sd

    def boom(*_a: object, **_k: object) -> None:
        raise RuntimeError("mirror exploded")

    monkeypatch.setattr(sd, "ensure_mirror", boom)
    upstream = _seed_upstream(tmp_path)
    base = tmp_path / "sessions" / "555"
    manager = sd.SessionDirManager(base_dir=str(base), source_repo=f"file://{upstream}")
    created = Path(manager.create_session_dir(5001))
    assert (created / "blob.bin").is_file()


def test_the_orphan_sweep_never_sees_the_mirror_dir(tmp_path: Path) -> None:
    """#613 の掃除がミラーを候補に上げないことを固定する。

    ミラーは「行を持たない・触られていないディレクトリ」に見えるので、
    掃除の入口が形で弾いていることをテストで留める。緩めば 256 MB の
    ミラーが消え、それを参照しているクローンが軒並み履歴を失う。
    """
    from c_lord.orphan_dirs import find_orphan_dirs

    root = tmp_path / "sessions"
    (root / MIRRORS_DIR_NAME / "some-repo-abc123.git").mkdir(parents=True)
    (root / "111" / "222").mkdir(parents=True)

    found = find_orphan_dirs(root, set(), min_idle_days=0)
    paths = {str(c.path) for c in found}
    assert str(root / "111" / "222") in paths
    assert not any(MIRRORS_DIR_NAME in p for p in paths)
