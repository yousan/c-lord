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


# --------------------------------------------------------------------------
# 時間切れの fetch — #1133 (スレッドの開始を止めない) / #1134 (書きかけを残さない)
# --------------------------------------------------------------------------


def _fake_slow_git(tmp_path: Path, monkeypatch: pytest.MonkeyPatch, secs: int = 30) -> Path:
    """``fetch`` だけ「受信し続けて終わらない」git を PATH の先頭に置く。

    本物の時間切れと同じく、受信中の pack を ``objects/pack/tmp_pack_*`` に
    書き続ける。書いているのは子プロセス (本物では index-pack / ssh) なので、
    親の git だけを殺しても書きかけは育ち続ける。
    """
    real = subprocess.run(["which", "git"], capture_output=True, text=True).stdout.strip()
    bindir = tmp_path / "fakebin"
    bindir.mkdir()
    log = tmp_path / "fetch-calls.log"
    script = bindir / "git"
    script.write_text(
        "#!/bin/sh\n"
        'if [ "$1" = "-C" ] && [ "$3" = "fetch" ]; then\n'
        f'  echo call >> "{log}"\n'
        '  pack="$2/objects/pack/tmp_pack_fake$$"\n'
        '  ( while true; do echo x >> "$pack"; sleep 0.05; done ) &\n'
        f"  sleep {secs}\n"
        "  exit 0\n"
        "fi\n"
        f'exec "{real}" "$@"\n'
    )
    script.chmod(0o755)
    monkeypatch.setenv("PATH", f"{bindir}{os.pathsep}{os.environ['PATH']}")
    return log


def _garbage(mirror: Path) -> list[str]:
    return sorted(p.name for p in (mirror / "objects" / "pack").glob("tmp_*"))


def test_a_timed_out_fetch_leaves_no_half_written_pack_1134(
    tmp_path: Path, upstream: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    """時間切れで止めた fetch の書きかけ (本番で1回約3GB) を c-lord が片付ける。"""
    import time

    import c_lord.git_mirrors as gm

    root = tmp_path / MIRRORS_DIR_NAME
    mirror = ensure_mirror(root, str(upstream))
    assert mirror is not None
    _fake_slow_git(tmp_path, monkeypatch)
    monkeypatch.setattr(gm, "_FETCH_TIMEOUT", 1.0)

    assert ensure_mirror(root, str(upstream)) == mirror
    time.sleep(0.3)  # 止め損ねた子が居れば、ここで書きかけを作り直す
    assert _garbage(mirror) == []


def test_after_a_timed_out_fetch_the_next_clone_does_not_wait_1133(
    tmp_path: Path, upstream: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    """前回の更新が時間切れだったミラーは、次は待たずにそのまま使う。"""
    import time

    import c_lord.git_mirrors as gm

    root = tmp_path / MIRRORS_DIR_NAME
    mirror = ensure_mirror(root, str(upstream))
    _fake_slow_git(tmp_path, monkeypatch)
    monkeypatch.setattr(gm, "_FETCH_TIMEOUT", 2.0)

    ensure_mirror(root, str(upstream))  # 1回目: 時間切れまで待つ (2秒)
    started = time.monotonic()
    assert ensure_mirror(root, str(upstream)) == mirror
    assert time.monotonic() - started < 1.0, "2回目も fetch の時間切れを待った"


def test_a_failing_mirror_is_refetched_in_the_background_and_recovers_1133(
    tmp_path: Path, upstream: Path
) -> None:
    """時間切れの印が古くなったら、待たずに裏で取り直す。成功したら印を消して元に戻る。"""
    import c_lord.git_mirrors as gm

    root = tmp_path / MIRRORS_DIR_NAME
    mirror = ensure_mirror(root, str(upstream))
    assert mirror is not None
    mark = mirror / gm.FETCH_TIMED_OUT_MARK
    mark.write_text("")
    old = mark.stat().st_mtime - gm._BACKGROUND_RETRY_AFTER - 60
    os.utime(mark, (old, old))

    (upstream / "NEW.md").write_text("new\n")
    _git(upstream, "add", "-A")
    _git(upstream, "commit", "-qm", "second")
    head = _git(upstream, "rev-parse", "HEAD").stdout.strip()

    assert ensure_mirror(root, str(upstream)) == mirror
    gm.wait_for_background_fetches(timeout=30)
    got = subprocess.run(["git", "-C", str(mirror), "cat-file", "-e", head])
    assert got.returncode == 0, "裏の fetch が走っていない"
    assert not mark.exists(), "成功したのに印が残っている"


def test_a_recent_timeout_mark_skips_the_fetch_entirely_1133(
    tmp_path: Path, upstream: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    """時間切れの直後は裏でも取り直さない (1回3GB をスレッドごとに繰り返さない)。"""
    import c_lord.git_mirrors as gm

    root = tmp_path / MIRRORS_DIR_NAME
    mirror = ensure_mirror(root, str(upstream))
    assert mirror is not None
    (mirror / gm.FETCH_TIMED_OUT_MARK).write_text("")
    log = _fake_slow_git(tmp_path, monkeypatch)
    assert ensure_mirror(root, str(upstream)) == mirror
    gm.wait_for_background_fetches(timeout=5)
    assert not log.exists(), "印が新しいのに fetch した"


def test_leftover_half_written_packs_are_swept_when_no_fetch_runs_1134(
    tmp_path: Path, upstream: Path
) -> None:
    """既に残っている書きかけ (本番 openclaw の7個) も次の ensure_mirror で消える。

    今まさに誰かが書いている (新しい) ものには触らない。
    """
    import c_lord.git_mirrors as gm

    root = tmp_path / MIRRORS_DIR_NAME
    mirror = ensure_mirror(root, str(upstream))
    assert mirror is not None
    pack_dir = mirror / "objects" / "pack"
    stale = pack_dir / "tmp_pack_k7BymR"
    stale.write_bytes(b"x" * 1024)
    old = stale.stat().st_mtime - 3 * 86400
    os.utime(stale, (old, old))
    fresh = pack_dir / "tmp_pack_live01"
    fresh.write_bytes(b"x")

    ensure_mirror(root, str(upstream))
    gm.wait_for_background_fetches(timeout=30)
    assert _garbage(mirror) == ["tmp_pack_live01"]
    # 書きかけが残っていた = 前に fetch が殺された。待たずに裏で取り直している
    assert str(mirror) in gm._background


def test_a_fetch_already_running_is_not_waited_for_1133(
    tmp_path: Path, upstream: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    """別のスレッドが同じミラーを更新中なら、終わるのを待たずにそのまま使う。"""
    import fcntl
    import time

    import c_lord.git_mirrors as gm

    root = tmp_path / MIRRORS_DIR_NAME
    mirror = ensure_mirror(root, str(upstream))
    assert mirror is not None
    log = _fake_slow_git(tmp_path, monkeypatch)
    with open(mirror / gm._FETCH_LOCK, "a") as fh:
        fcntl.flock(fh, fcntl.LOCK_EX | fcntl.LOCK_NB)
        started = time.monotonic()
        assert ensure_mirror(root, str(upstream)) == mirror
        assert time.monotonic() - started < 1.0
    assert not log.exists()


def test_a_timed_out_mirror_clone_leaves_nothing_behind_1134(
    tmp_path: Path, upstream: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    """``clone --mirror`` が時間切れでも、書きかけの一時ディレクトリは残らない。"""
    import time

    import c_lord.git_mirrors as gm

    real = subprocess.run(["which", "git"], capture_output=True, text=True).stdout.strip()
    bindir = tmp_path / "fakebin"
    bindir.mkdir()
    script = bindir / "git"
    script.write_text(
        "#!/bin/sh\n"
        'if [ "$1" = "clone" ]; then\n'
        '  for a; do dest="$a"; done\n'
        '  mkdir -p "$dest/objects/pack"\n'
        '  ( while true; do echo x >> "$dest/objects/pack/tmp_pack_c"; sleep 0.05; done ) &\n'
        "  sleep 30\n"
        "fi\n"
        f'exec "{real}" "$@"\n'
    )
    script.chmod(0o755)
    monkeypatch.setenv("PATH", f"{bindir}{os.pathsep}{os.environ['PATH']}")
    monkeypatch.setattr(gm, "_CLONE_TIMEOUT", 1.0)

    root = tmp_path / MIRRORS_DIR_NAME
    assert ensure_mirror(root, str(upstream)) is None
    time.sleep(0.3)
    assert [p.name for p in root.iterdir() if p.name != "README.md"] == []


# --------------------------------------------------------------------------
# #1152 の回帰 — #1173 (別の fetch の書きかけを消さない) / #1174 (bot と一緒に止まる)
# --------------------------------------------------------------------------


def test_timeout_cleanup_spares_a_pack_another_fetch_is_writing_1173(
    tmp_path: Path, upstream: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    """時間切れの片付けが、同じミラーで別の git がいま受信中の pack を消さない。

    消すと、手で打った fetch や別の更新がその場で失敗する。c-lord が消してよいのは
    自分が止めた git の書きかけだけ。
    """
    import threading
    import time

    import c_lord.git_mirrors as gm

    root = tmp_path / MIRRORS_DIR_NAME
    mirror = ensure_mirror(root, f"file://{upstream}")
    assert mirror is not None
    _fake_slow_git(tmp_path, monkeypatch)
    monkeypatch.setattr(gm, "_FETCH_TIMEOUT", 1.5)

    foreign = mirror / "objects" / "pack" / "tmp_pack_foreign"
    stop = threading.Event()

    def other_writer() -> None:  # c-lord の flock を持たない別の fetch
        time.sleep(0.5)  # c-lord の fetch が before を撮ったあとに受信を始める
        while not stop.is_set():
            with open(foreign, "a") as fh:
                fh.write("y")
            time.sleep(0.05)

    t = threading.Thread(target=other_writer)
    t.start()
    try:
        ensure_mirror(root, f"file://{upstream}")  # 1.5 秒で時間切れ
        alive_and_written = foreign.exists()
    finally:
        stop.set()
        t.join()
    assert alive_and_written, "別のプロセスがいま書いている tmp_pack を消した"
    assert [p for p in _garbage(mirror) if p != "tmp_pack_foreign"] == [], (
        "止めた自分の fetch の書きかけが残った"
    )


def _kill_procs_mentioning(text: str) -> None:
    """コマンドラインに *text* を含むプロセスだけを止める (試験の後始末。pkill は使わない)。"""
    import contextlib
    import signal

    for proc in Path("/proc").glob("[0-9]*"):
        try:
            cmdline = (proc / "cmdline").read_bytes().replace(b"\0", b" ").decode()
        except OSError:
            continue
        if text in cmdline:
            with contextlib.suppress(OSError):
                os.kill(int(proc.name), signal.SIGKILL)


def test_background_fetch_git_dies_with_the_bot_1174(
    tmp_path: Path, upstream: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    """bot を Ctrl-C で止めたら、裏で走っていたミラー更新の git も一緒に止まる。

    git は時間切れでグループごと止めるために自分のプロセスグループで走る (#1134)。
    そのせいで端末の Ctrl-C が git に届かず、systemd の外で動かす利用者では bot が
    終わったあとも git がミラーに書き続け、次の bot がもう1本 fetch を出していた。
    """
    import signal
    import sys
    import textwrap
    import time

    import c_lord.git_mirrors as gm

    root = tmp_path / MIRRORS_DIR_NAME
    origin = f"file://{upstream}"
    mirror = ensure_mirror(root, origin)
    assert mirror is not None
    _fake_slow_git(tmp_path, monkeypatch, secs=20)
    mark = mirror / gm.FETCH_TIMED_OUT_MARK
    mark.touch()
    old = time.time() - 7200
    os.utime(mark, (old, old))
    # A terminal's Ctrl-C reaches a process whose SIGINT is not ignored; a test run
    # launched as a background job inherits it ignored, so restore the default.
    code = textwrap.dedent(f"""
        import signal, time, c_lord.git_mirrors as gm
        signal.signal(signal.SIGINT, signal.default_int_handler)
        gm.ensure_mirror({str(root)!r}, {origin!r})
        time.sleep(0.5)
        print("started", flush=True)
    """)
    # 端末で Ctrl-C したときと同じく、bot のプロセスグループに SIGINT を送る
    p = subprocess.Popen(
        [sys.executable, "-c", code + "time.sleep(30)"],
        start_new_session=True,
        stdout=subprocess.PIPE,
        stderr=subprocess.DEVNULL,
        text=True,
    )
    try:
        assert p.stdout is not None
        p.stdout.readline()
        # Stop it while git is receiving — what the Issue is about. Under load the
        # background fetch may not have started yet at "started".
        pack_dir = mirror / "objects" / "pack"
        deadline = time.monotonic() + 15
        while not list(pack_dir.glob("tmp_pack_fake*")) and time.monotonic() < deadline:
            time.sleep(0.05)
        os.killpg(p.pid, signal.SIGINT)
        p.wait(timeout=10)
        time.sleep(0.5)
        packs = list((mirror / "objects" / "pack").glob("tmp_pack_fake*"))
        sizes1 = [x.stat().st_size for x in packs if x.exists()]
        time.sleep(0.5)
        sizes2 = [x.stat().st_size for x in packs if x.exists()]
    finally:
        _kill_procs_mentioning(str(tmp_path / "fakebin"))
    assert sizes1 == sizes2, f"bot が終わったあとも git の受信が続いている: {sizes1} -> {sizes2}"


@pytest.fixture(autouse=True)
def _not_stopping():  # noqa: ANN202
    import c_lord.git_mirrors as gm

    stopping = getattr(gm, "_stopping", None)
    if stopping is not None:
        stopping.clear()
    yield
    if stopping is not None:
        stopping.clear()


@pytest.mark.skipif(not Path("/proc/self/fd").is_dir(), reason="no /proc (macOS): growth only")
def test_an_open_pack_is_seen_under_a_relative_or_symlinked_root_1173(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    """独立レビュー: /proc は実体の絶対パスを返す。相対・シンボリックリンク経由の
    ミラーの置き場所でも「誰かが開いている」と分かる（旧既定の data/sessions は相対）。"""
    import c_lord.git_mirrors as gm

    real = tmp_path / "real"
    (real / "pack").mkdir(parents=True)
    pack = real / "pack" / "tmp_pack_held"
    pack.write_text("x")
    (tmp_path / "link").symlink_to(real)
    monkeypatch.chdir(tmp_path)
    via_link = Path("link") / "pack" / "tmp_pack_held"
    with open(pack):
        assert gm._open_by_someone([via_link]) == {via_link}
        assert gm._abandoned([via_link]) == []


def test_a_fetch_stopped_with_the_bot_is_not_marked_timed_out_1174(
    tmp_path: Path, upstream: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    """独立レビュー: 停止で SIGKILL した git も -9 で終わる。時間切れの印を付けると、
    デプロイのたびに1時間ミラーを更新しなくなる。"""
    import threading
    import time

    import c_lord.git_mirrors as gm

    root = tmp_path / MIRRORS_DIR_NAME
    mirror = ensure_mirror(root, f"file://{upstream}")
    assert mirror is not None
    _fake_slow_git(tmp_path, monkeypatch)
    t = threading.Thread(target=ensure_mirror, args=(root, f"file://{upstream}"))
    t.start()
    try:
        deadline = time.monotonic() + 10
        while not gm._running_groups and time.monotonic() < deadline:
            time.sleep(0.05)
        assert gm.stop_running_git() == 1
        t.join(timeout=15)
    finally:
        # A failure above must not leave the fake git's writer running.
        _kill_procs_mentioning(str(tmp_path / "fakebin"))
    assert not (mirror / gm.FETCH_TIMED_OUT_MARK).exists()


def test_a_git_started_after_the_stop_is_stopped_too_1174(
    tmp_path: Path, upstream: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    """A background fetch that starts git right after ``stop_running_git`` (atexit runs
    while daemon threads still live) must not escape it."""
    import time

    import c_lord.git_mirrors as gm

    root = tmp_path / MIRRORS_DIR_NAME
    mirror = ensure_mirror(root, f"file://{upstream}")
    assert mirror is not None
    _fake_slow_git(tmp_path, monkeypatch, secs=20)
    gm.stop_running_git()
    started = time.monotonic()
    try:
        gm._run(["git", "-C", str(mirror), "fetch", "--quiet"], timeout=15)
    finally:
        _kill_procs_mentioning(str(tmp_path / "fakebin"))
    assert time.monotonic() - started < 5
