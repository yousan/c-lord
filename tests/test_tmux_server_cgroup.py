"""A brand-new tmux server must not land in c-lord's own cgroup (#503).

c-lord is usually the first process on the host to touch tmux, so the server
it spawns via ``tmux new-session`` inherits c-lord's cgroup. systemd kills a
unit's entire cgroup on stop, so a plain ``systemctl --user restart
c-lord.service`` took every tmux session down with it — including the human's
unrelated work sessions (2026-08-07: 16 sessions lost twice).

The fix routes only the *server start* through ``systemd-run --user``, so the
server lands in its own transient unit. Sessions created against an existing
server must keep using plain tmux, and hosts without systemd must still work.
"""

from __future__ import annotations

from unittest.mock import MagicMock, patch

from c_lord.tmux import TmuxSessionManager


def _mgr() -> TmuxSessionManager:
    mgr = TmuxSessionManager(mapping_path="")
    mgr._available = True
    mgr.session_name = "clord"
    return mgr


class _Tmux:
    """Fake tmux/systemd-run. Tracks whether a server and session exist."""

    def __init__(self, *, server_running: bool, systemd_run_ok: bool = True) -> None:
        self.server_running = server_running
        self.session_exists = False
        self.systemd_run_ok = systemd_run_ok
        self.calls: list[list[str]] = []

    def __call__(self, args: list[str]) -> MagicMock:
        self.calls.append(args)

        if args[0] == "systemd-run":
            if not self.systemd_run_ok:
                return MagicMock(returncode=1, stdout="", stderr="systemd-run: not found")
            # The unit starts and the wrapped tmux brings up the server.
            self.server_running = True
            self.session_exists = True
            return MagicMock(returncode=0, stdout="", stderr="")

        if args[:2] == ["tmux", "has-session"]:
            return MagicMock(returncode=0 if self.session_exists else 1, stdout="", stderr="")

        if args[:2] == ["tmux", "list-sessions"]:
            return MagicMock(returncode=0 if self.server_running else 1, stdout="", stderr="")

        if args[:2] == ["tmux", "new-session"]:
            self.server_running = True
            self.session_exists = True
            return MagicMock(returncode=0, stdout="", stderr="")

        return MagicMock(returncode=0, stdout="", stderr="")

    # ── helpers ──
    def used_systemd_run(self) -> bool:
        return any(c[0] == "systemd-run" for c in self.calls)

    def direct_new_session_calls(self) -> list[list[str]]:
        return [c for c in self.calls if c[:2] == ["tmux", "new-session"]]


def test_server_start_is_placed_in_its_own_systemd_unit() -> None:
    """No server yet: the very first `tmux new-session` must go via systemd-run."""
    fake = _Tmux(server_running=False)

    with patch("c_lord.tmux._run", side_effect=fake):
        mgr = _mgr()
        assert mgr._ensure_session() is True

    assert fake.used_systemd_run(), "server start must not inherit c-lord's cgroup"
    # ...and the tmux command must not also be run directly (that would start a
    # second server in our cgroup, defeating the whole point).
    assert fake.direct_new_session_calls() == []

    systemd_call = next(c for c in fake.calls if c[0] == "systemd-run")
    assert "--user" in systemd_call
    # Without these the unit is "finished" the moment the tmux client exits and
    # systemd reaps the freshly forked server along with the cgroup.
    assert "--property=KillMode=process" in systemd_call
    assert "--property=RemainAfterExit=yes" in systemd_call
    assert systemd_call[-5:] == ["tmux", "new-session", "-d", "-s", "clord"]


def test_socket_selecting_env_is_forwarded_into_the_unit() -> None:
    """systemd-run does not inherit our env — TMUX_TMPDIR must be passed through.

    Regression for a gap caught on staging: without this the unit started a
    server on the *default* socket, died with "duplicate session", and the
    readiness check on the intended socket timed out into the fallback — so the
    server silently landed in c-lord's cgroup after all.
    """
    fake = _Tmux(server_running=False)

    with (
        patch("c_lord.tmux._run", side_effect=fake),
        patch.dict("os.environ", {"TMUX_TMPDIR": "/tmp/isolated", "PATH": "/custom/bin"}),
    ):
        mgr = _mgr()
        assert mgr._ensure_session() is True

    systemd_call = next(c for c in fake.calls if c[0] == "systemd-run")
    assert "--setenv=TMUX_TMPDIR=/tmp/isolated" in systemd_call
    assert "--setenv=PATH=/custom/bin" in systemd_call
    # Forwarded env must sit before the `--` separator, or systemd-run treats it
    # as part of the command line.
    assert systemd_call.index("--setenv=TMUX_TMPDIR=/tmp/isolated") < systemd_call.index("--")


def test_unset_env_is_not_forwarded_as_empty() -> None:
    """An unset variable must be omitted, not passed as VAR= (which clears it)."""
    fake = _Tmux(server_running=False)

    with (
        patch("c_lord.tmux._run", side_effect=fake),
        patch.dict("os.environ", {"PATH": "/usr/bin"}, clear=True),
    ):
        mgr = _mgr()
        assert mgr._ensure_session() is True

    systemd_call = next(c for c in fake.calls if c[0] == "systemd-run")
    assert not any(a.startswith("--setenv=TMUX_TMPDIR") for a in systemd_call)


def test_existing_server_creates_the_session_directly() -> None:
    """A server is already up: no transient unit, just talk to it."""
    fake = _Tmux(server_running=True)

    with patch("c_lord.tmux._run", side_effect=fake):
        mgr = _mgr()
        assert mgr._ensure_session() is True

    assert not fake.used_systemd_run(), "must not spawn a unit per session"
    assert fake.direct_new_session_calls() == [["tmux", "new-session", "-d", "-s", "clord"]]


def test_falls_back_to_plain_tmux_when_systemd_run_is_unavailable() -> None:
    """No systemd (container / CI): still bring the session up, just unisolated."""
    fake = _Tmux(server_running=False, systemd_run_ok=False)

    with patch("c_lord.tmux._run", side_effect=fake):
        mgr = _mgr()
        assert mgr._ensure_session() is True

    assert fake.used_systemd_run(), "should have attempted isolation first"
    assert fake.direct_new_session_calls() == [["tmux", "new-session", "-d", "-s", "clord"]]


def test_existing_session_touches_nothing() -> None:
    """Session already there — the common path must stay a single has-session."""
    fake = _Tmux(server_running=True)
    fake.session_exists = True

    with patch("c_lord.tmux._run", side_effect=fake):
        mgr = _mgr()
        assert mgr._ensure_session() is True

    assert not fake.used_systemd_run()
    assert fake.direct_new_session_calls() == []
