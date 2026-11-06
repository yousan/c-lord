"""#1265 — two workspaces that are each other's remote are not both deleted in one sweep.

A's submodule has a commit only on its local branch ``work``. B's submodule
adds A's as a remote, fetches it and makes ``work`` from ``a/work``; A then
adds B's as a remote and fetches. Each now has a remote-tracking ref that
reaches the commit, so each read as "pushed" — and one ``cleanup_orphaned``
sweep deleted both workspaces, and with them the commit's only copies.

A remote whose path is under the sessions root (another workspace, which the
same sweep may delete) is no copy anywhere: its tracking refs vouch for
nothing, as #1246 already made it for tags.
"""

from __future__ import annotations

from pathlib import Path

import pytest

from c_lord import session_dir as sd

from .test_submodule_unpushed_1198 import _commit, _workspace, git


def _second_ws(tmp: Path, name: str) -> Path:
    ws = tmp / "sessions" / name
    git(tmp, "clone", "-q", "--single-branch", str(tmp / "parent.git"), str(ws))
    git(ws, "-c", "protocol.file.allow=always", "submodule", "update", "-q", "--init")
    return ws


def _cycle(tmp_path: Path) -> tuple[Path, Path, str]:
    a, _ = _workspace(tmp_path)
    b = _second_ws(tmp_path, "4343")
    sa = a / "sub"
    git(sa, "checkout", "-q", "-b", "work")
    sha = _commit(sa, "w.txt", "only copy\n")
    git(sa, "checkout", "-q", "main")
    git(b / "sub", "remote", "add", "a", str(sa))
    git(b / "sub", "fetch", "-q", "a")
    git(b / "sub", "branch", "-q", "work", "a/work")
    git(sa, "remote", "add", "b", str(b / "sub"))
    git(sa, "fetch", "-q", "b")
    return a, b, sha


def test_one_sweep_cycle_heads_variant(tmp_path: Path) -> None:
    """Same cycle as the #1246 tag test, with branches + tracking refs (the #1245 heads path)."""
    a, b, sha = _cycle(tmp_path)
    mgr = sd.SessionDirManager(base_dir=str(tmp_path / "sessions"), source_repo="/repo")
    results = mgr.cleanup_orphaned(set())
    reasons = [(r.thread_id, r.removed, r.reason) for r in results]
    assert a.exists() or b.exists(), f"one sweep deleted both; {sha} is gone: {reasons}"


def test_cycle_kept_with_sessions_root_unknown(tmp_path: Path) -> None:
    """Without the sessions root, no local-path remote outside the workspace vouches."""
    a, b, _ = _cycle(tmp_path)
    assert not sd.worktree_status(str(a)).clean
    assert not sd.worktree_status(str(b)).clean


def test_top_level_remote_in_a_neighbour_workspace_does_not_vouch(tmp_path: Path) -> None:
    """The workspace's own repo: a branch only on a neighbour workspace is unpushed."""
    a, _ = _workspace(tmp_path)
    b = _second_ws(tmp_path, "4343")
    git(a, "checkout", "-q", "-b", "work")
    _commit(a, "w.txt", "only copy\n")
    git(a, "checkout", "-q", "main")
    git(b, "remote", "add", "a", str(a))
    git(b, "fetch", "-q", "a")
    git(b, "branch", "-q", "work", "a/work")
    git(a, "remote", "add", "b", str(b))
    git(a, "fetch", "-q", "b")
    status = sd.worktree_status(str(a), sessions_root=str(tmp_path / "sessions"))
    assert not status.clean, sd.describe_changes(status)
    assert status.unpushed_commits == 1


# ---- review of f870f5b: production-shaped roots and how a remote is named ----
#
# c-lord's managers are per channel (ChannelRepoCog: ``base_dir=<base>/<channel>``),
# so a neighbour in another channel was outside "the sessions root" and vouched.
# And the workspace's own repo trusted any ``refs/remotes/*``: a remote whose URL
# is rewritten by ``insteadOf``, whose ``pushurl`` is the neighbour, or a ref
# fetched by path with no ``remote.<name>`` at all.


def _channel_ws(tmp: Path, channel: str, thread: str) -> Path:
    ws = tmp / "sessions" / channel / thread
    ws.parent.mkdir(parents=True, exist_ok=True)
    git(tmp, "clone", "-q", "--single-branch", str(tmp / "parent.git"), str(ws))
    git(ws, "-c", "protocol.file.allow=always", "submodule", "update", "-q", "--init")
    return ws


def _two_channels(tmp: Path) -> tuple[Path, Path]:
    import shutil

    seed, _ = _workspace(tmp)  # parent.git with the submodule recorded
    shutil.rmtree(seed)
    return _channel_ws(tmp, "111", "4242"), _channel_ws(tmp, "222", "4343")


def _sweep_both_channels(tmp: Path) -> list[tuple[int | None, bool, str]]:
    out = []
    for channel in ("111", "222"):
        mgr = sd.SessionDirManager(base_dir=str(tmp / "sessions" / channel), source_repo="/repo")
        out += [(r.thread_id, r.removed, r.reason) for r in mgr.cleanup_orphaned(set())]
    return out


def _cross_fetch(a: Path, b: Path) -> str:
    """A's ``work`` holds the only commit; B makes ``work`` from it; A fetches B."""
    git(a, "checkout", "-q", "-b", "work")
    sha = _commit(a, "w.txt", "only copy\n")
    git(a, "checkout", "-q", "main")
    git(b, "remote", "add", "a", str(a))
    git(b, "fetch", "-q", "a")
    git(b, "branch", "-q", "work", "a/work")
    git(a, "remote", "add", "b", str(b))
    git(a, "fetch", "-q", "b")
    return sha


def test_cross_channel_cycle_top_level(tmp_path: Path) -> None:
    a, b = _two_channels(tmp_path)
    sha = _cross_fetch(a, b)
    reasons = _sweep_both_channels(tmp_path)
    assert a.exists() or b.exists(), f"both channels' sweeps deleted {sha}: {reasons}"


def test_cross_channel_cycle_submodule(tmp_path: Path) -> None:
    a, b = _two_channels(tmp_path)
    sha = _cross_fetch(a / "sub", b / "sub")
    reasons = _sweep_both_channels(tmp_path)
    assert a.exists() or b.exists(), f"both channels' sweeps deleted {sha}: {reasons}"


def test_channel_manager_knows_the_global_sessions_root(tmp_path: Path) -> None:
    base = tmp_path / "sessions"
    mgr = sd.SessionDirManager(base_dir=str(base / "111"), source_repo="/repo")
    assert mgr.sessions_root == str(base)
    flat = sd.SessionDirManager(base_dir=str(base), source_repo="/repo")
    assert flat.sessions_root == str(base)
    given = sd.SessionDirManager(base_dir=str(base / "x"), source_repo="/repo", sessions_root="/r")
    assert given.sessions_root == "/r"


async def test_channel_repo_cog_gives_managers_the_global_root(tmp_path: Path) -> None:
    from c_lord.cogs.channel_repo import ChannelRepoCog
    from c_lord.database.channel_repo import ChannelRepository
    from c_lord.discord_ui.authorization import Authorizer

    from .test_channel_repo import _make_bot

    repo = ChannelRepository(str(tmp_path / "c.db"))
    await repo.init_db()
    cog = ChannelRepoCog(
        _make_bot(),
        repo=repo,
        allowed_user_ids=None,
        authorizer=Authorizer(allow_anyone=True),
        session_dir_base=str(tmp_path / "sessions"),
    )
    await repo.save(channel_id=100, source_repo="/tmp/fake-repo.git")
    manager = await cog.resolve_manager(100)
    assert manager is not None
    assert manager.base_dir == str(tmp_path / "sessions" / "100")
    assert manager.sessions_root == str(tmp_path / "sessions")


def _neighbour_with_work(tmp: Path) -> tuple[Path, Path, str]:
    """W (channel 111) has ``work`` only locally; N (channel 222) fetched it as ``work``."""
    w, n = _two_channels(tmp)
    git(w, "checkout", "-q", "-b", "work")
    sha = _commit(w, "w.txt", "only copy\n")
    git(w, "checkout", "-q", "main")
    git(n, "fetch", "-q", str(w), "work:work")
    return w, n, sha


def _root(tmp: Path) -> str:
    return str(tmp / "sessions")


def test_insteadof_to_a_neighbour_does_not_vouch(tmp_path: Path) -> None:
    w, n, _ = _neighbour_with_work(tmp_path)
    git(w, "config", f"url.{n}.insteadOf", "wsb:x")
    git(w, "remote", "add", "b", "wsb:x")
    git(w, "fetch", "-q", "b")
    assert git(w, "rev-parse", "b/work") == git(w, "rev-parse", "work")
    st = sd.worktree_status(str(w), sessions_root=_root(tmp_path))
    assert not st.clean, sd.describe_changes(st)


def test_pushurl_to_a_neighbour_does_not_vouch(tmp_path: Path) -> None:
    w, n = _two_channels(tmp_path)
    mirror = tmp_path / "mirror.git"
    git(tmp_path, "clone", "-q", "--bare", str(tmp_path / "parent.git"), str(mirror))
    git(w, "remote", "add", "m", str(mirror))
    git(w, "remote", "set-url", "--push", "m", str(n))
    git(w, "fetch", "-q", "m")
    git(w, "checkout", "-q", "-b", "work")
    _commit(w, "w.txt", "only copy\n")
    git(w, "checkout", "-q", "main")
    git(w, "push", "-q", "m", "work")
    assert git(w, "rev-parse", "m/work") == git(w, "rev-parse", "work")
    st = sd.worktree_status(str(w), sessions_root=_root(tmp_path))
    assert not st.clean, sd.describe_changes(st)


def test_urlless_fetch_from_a_neighbour_does_not_vouch(tmp_path: Path) -> None:
    w, n, _ = _neighbour_with_work(tmp_path)
    git(w, "fetch", "-q", str(n), "refs/heads/*:refs/remotes/b/*")
    st = sd.worktree_status(str(w), sessions_root=_root(tmp_path))
    assert not st.clean, sd.describe_changes(st)


def test_urlless_fetch_in_a_submodule_does_not_vouch(tmp_path: Path) -> None:
    a, b = _two_channels(tmp_path)
    sa, sb = a / "sub", b / "sub"
    # Made by plumbing: no "created here" in the reflog, so the ref is not
    # verified for that reason — only for having no configured remote.
    tree = git(sa, "rev-parse", "HEAD^{tree}").strip()
    sha = git(sa, "commit-tree", tree, "-p", "HEAD", "-m", "only copy").strip()
    git(sa, "branch", "work", sha)
    git(sb, "fetch", "-q", str(sa), "work:work")
    git(sa, "fetch", "-q", str(sb), "refs/heads/*:refs/remotes/b/*")
    st = sd.worktree_status(str(a), sessions_root=_root(tmp_path))
    assert not st.clean, sd.describe_changes(st)


def test_local_mirror_outside_the_sessions_root_still_vouches(tmp_path: Path) -> None:
    """Over-keep control: a push to a real mirror outside the sessions root is a copy."""
    w, _ = _two_channels(tmp_path)
    mirror = tmp_path / "mirror.git"
    git(tmp_path, "clone", "-q", "--bare", str(tmp_path / "parent.git"), str(mirror))
    git(w, "remote", "add", "m", str(mirror))
    git(w, "checkout", "-q", "-b", "work")
    _commit(w, "w.txt", "pushed\n")
    git(w, "checkout", "-q", "main")
    git(w, "push", "-q", "m", "work")
    st = sd.worktree_status(str(w), sessions_root=_root(tmp_path))
    assert st.clean, sd.describe_changes(st)


def test_insteadof_to_a_network_url_still_vouches(tmp_path: Path) -> None:
    """Over-keep control: a rewrite that does not lead into the sessions root."""
    w, _ = _two_channels(tmp_path)
    mirror = tmp_path / "mirror.git"
    git(tmp_path, "clone", "-q", "--bare", str(tmp_path / "parent.git"), str(mirror))
    git(w, "config", f"url.{mirror}.insteadOf", "gh:org/repo")
    git(w, "remote", "add", "m", "gh:org/repo")
    git(w, "checkout", "-q", "-b", "work")
    _commit(w, "w.txt", "pushed\n")
    git(w, "checkout", "-q", "main")
    git(w, "push", "-q", "m", "work")
    st = sd.worktree_status(str(w), sessions_root=_root(tmp_path))
    assert st.clean, sd.describe_changes(st)


# ---- W57 round 16: a percent-encoded file:// URL -----------------------------------
#
# git decodes ``%xx`` in a ``file://`` URL, so ``file:///…/s%65ssions/…`` names the
# neighbour workspace. c-lord compared the path still encoded, read it as outside
# the sessions root, and one sweep deleted both again.


def _seed_root(tmp: Path) -> Path:
    import shutil

    seed, _ = _workspace(tmp)
    shutil.rmtree(seed)
    return tmp / "sessions"


def _flat_ws(tmp: Path, root: Path, name: str) -> Path:
    ws = root / name
    git(tmp, "clone", "-q", "--single-branch", str(tmp / "parent.git"), str(ws))
    git(ws, "-c", "protocol.file.allow=always", "submodule", "update", "-q", "--init")
    return ws


def _pct(t: Path) -> str:
    s = str(t)
    i = s.index("sessions")
    return "file://" + s[:i] + "s%65ssions" + s[i + len("sessions") :]


def _sweep_root(root: Path) -> list[tuple[int | None, bool, str]]:
    mgr = sd.SessionDirManager(base_dir=str(root), source_repo="/repo")
    return [(r.thread_id, r.removed, r.reason) for r in mgr.cleanup_orphaned(set())]


@pytest.mark.parametrize("sub", [False, True])
def test_pct_file_url_cycle_one_sweep(tmp_path: Path, sub: bool) -> None:
    root = _seed_root(tmp_path)
    a, b = _flat_ws(tmp_path, root, "4242"), _flat_ws(tmp_path, root, "4343")
    pa, pb = (a / "sub", b / "sub") if sub else (a, b)
    git(pa, "checkout", "-q", "-b", "work")
    sha = _commit(pa, "w.txt", "only copy\n")
    git(pa, "checkout", "-q", "main")
    git(pb, "remote", "add", "a", _pct(pa))
    git(pb, "fetch", "-q", "a")
    git(pb, "branch", "-q", "work", "a/work")
    git(pa, "remote", "add", "b", _pct(pb))
    git(pa, "fetch", "-q", "b")
    r = _sweep_root(root)
    assert a.exists() or b.exists(), f"one sweep deleted both; {sha} gone: {r}"


def test_pct_file_url_tag_cycle_one_sweep(tmp_path: Path) -> None:
    """#1246 tag path with the same percent-encoded neighbour URL."""
    sd._ls_remote_cache.clear()
    root = _seed_root(tmp_path)
    a, b = _flat_ws(tmp_path, root, "4242"), _flat_ws(tmp_path, root, "4343")
    sa, sb = a / "sub", b / "sub"
    git(sa, "checkout", "-q", "--detach")
    sha = _commit(sa, "wip.txt", "only copy\n")
    git(sa, "tag", "wip")
    git(sa, "checkout", "-q", "main")
    git(sb, "remote", "add", "a", _pct(sa))
    git(sb, "fetch", "-q", "a", "tag", "wip")
    git(sa, "remote", "add", "b", _pct(sb))
    r = _sweep_root(root)
    assert a.exists() or b.exists(), f"one sweep deleted both; {sha} gone: {r}"


# ---- #1295: ``%xx`` that is not UTF-8 in a file:// URL -------------------------------
#
# git puts the raw byte back (``%FF`` is the byte 0xFF), but c-lord decoded with
# ``errors='replace'`` and compared a path holding U+FFFD instead. That path does
# not exist, so it read as "outside the sessions root", the neighbour vouched for
# a commit only it held, and the workspace was deleted.


def _only_on_neighbour(tmp: Path, root: Path) -> tuple[Path, Path, str]:
    a, b = _flat_ws(tmp, root, "4242"), _flat_ws(tmp, root, "4343")
    git(a, "checkout", "-q", "-b", "work")
    sha = _commit(a, "w.txt", "only copy\n")
    git(a, "checkout", "-q", "main")
    git(b, "fetch", "-q", str(a), "work:work")
    return a, b, sha


def _fetch_through(a: Path, url: str, sha: str) -> None:
    import subprocess

    git(a, "remote", "add", "b", url)
    got = subprocess.run(["git", "fetch", "-q", "b"], cwd=a, capture_output=True)
    if got.returncode != 0:
        pytest.skip(f"git cannot fetch: {got.stderr[:200]!r}")
    assert git(a, "rev-parse", "b/work").strip() == sha


def _skip_unless_non_utf8_names(tmp_path: Path) -> None:
    """Skip where the file system refuses non-UTF-8 file names (macOS APFS:
    EILSEQ). Linux keeps running these tests."""
    import errno
    import os

    probe = bytes(tmp_path) + b"/probe\xff"
    try:
        os.mkdir(probe)
    except OSError as exc:
        if exc.errno == errno.EILSEQ:
            pytest.skip("this file system refuses non-UTF-8 file names (EILSEQ)")
        raise
    os.rmdir(probe)


def test_non_utf8_root_byte(tmp_path: Path) -> None:
    """Root dir name holds a byte that is not UTF-8 (\\xff); URL spells it %FF."""
    import os

    _skip_unless_non_utf8_names(tmp_path)
    from urllib.parse import quote

    root = _seed_root(tmp_path)
    new = Path(os.fsdecode(bytes(tmp_path) + b"/s\xffs"))
    root.rename(new)
    a, b, sha = _only_on_neighbour(tmp_path, new)
    url = "file://" + quote(os.fsencode(str(b)))
    _fetch_through(a, url, sha)
    st = sd.worktree_status(str(a), sessions_root=str(new))
    assert not st.clean, f"{url!r}: {sha} only on neighbour, called clean"


def test_non_utf8_symlink_outside(tmp_path: Path) -> None:
    """Ordinary root; a link outside it named b'\\xff' leads to the neighbour; URL spells %FF."""
    import os

    _skip_unless_non_utf8_names(tmp_path)

    root = _seed_root(tmp_path)
    a, b, sha = _only_on_neighbour(tmp_path, root)
    (tmp_path / "x").mkdir()
    os.symlink(bytes(b), bytes(tmp_path) + b"/x/\xff")
    url = "file://" + str(tmp_path) + "/x/%FF"
    _fetch_through(a, url, sha)
    st = sd.worktree_status(str(a), sessions_root=str(root))
    assert not st.clean, f"{url!r}: {sha} only on neighbour, called clean"
    r = _sweep_root(root)
    assert a.exists(), r
