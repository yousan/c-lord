"""Concurrent ``create_session()`` must not mint two windows with one name (#649).

``resolve_tmux_manager()`` hands each Discord thread its *own*
``TmuxSessionManager`` (cached per channel and per thread), but threads bound to
the same repo all resolve to the same tmux **session**. The create-and-sort
critical section (#374) is guarded by ``self._lock`` — an *instance* attribute —
and the ``w{N}`` counter was ``self._next_work_id``, also per instance. So two
managers pointing at one session could both mint ``w134`` and both run
``new-window -n w134``: tmux does not enforce unique window names, so two windows
answered to that name.

Everything downstream targets ``session:name``, and tmux resolves an ambiguous
name to the first match. In production that put thread A's ``@thread_id`` on
thread B's window: keystrokes for one thread would land in another thread's
checkout, and the thread whose window went untagged never started at all.

These tests drive two managers at one fake tmux server and assert the two
properties that were violated: window names stay unique, and each thread's tag
lands on the window sitting in *its own* checkout.
"""

from __future__ import annotations

import contextlib
import threading
from unittest.mock import MagicMock, patch

from c_lord.tmux import TmuxSessionManager

_SESSION = "c-lord"
_BASE = "/home/yousan/c-lord-sessions/1505747831447883806"
# The two threads from the incident (2026-08-31 07:18:24 / 07:18:25).
_THREAD_A = 1543882704230031461
_THREAD_B = 1543882706067394591

# How long the first thread inside ``new-window`` waits for a second one to join
# it there. Unfixed, the second arrives at once. Fixed, it is held at the session
# lock and never arrives, so this elapses and the first proceeds alone.
_RENDEZVOUS_TIMEOUT = 1.5


class FakeTmux:
    """In-memory tmux server, faithful about the one thing that matters here.

    ``new-window -n NAME`` does **not** reject a name another window already
    holds, and a ``session:NAME`` target resolves to the *first* window with that
    name — exactly the permissiveness that turns a duplicated ``w{N}`` into a
    thread typing into another thread's checkout.
    """

    def __init__(self) -> None:
        self.windows: list[dict[str, str]] = []
        self._next_id = 1
        self._state_lock = threading.Lock()
        # Trips when two creators reach ``new-window`` simultaneously.
        self._rendezvous = threading.Barrier(2)

    # ── target resolution ──────────────────────────────────────────────

    def _resolve(self, target: str) -> dict[str, str] | None:
        """``@7`` → that exact window. ``session:NAME`` → the FIRST name match."""
        if target.startswith("@"):
            return next((w for w in self.windows if w["window_id"] == target), None)
        name = target.split(":", 1)[1] if ":" in target else target
        if not name:
            return None
        return next((w for w in self.windows if w["window_name"] == name), None)

    @staticmethod
    def _render(win: dict[str, str], fmt: str, index: int) -> str:
        for token, value in (
            ("#{session_name}", _SESSION),
            ("#{window_id}", win["window_id"]),
            ("#{window_index}", str(index)),
            ("#{window_name}", win["window_name"]),
            ("#{window_active}", "0"),
            ("#{pane_current_path}", win["path"]),
            ("#{@thread_id}", win.get("thread_id", "")),
        ):
            fmt = fmt.replace(token, value)
        return fmt

    # ── command dispatch ───────────────────────────────────────────────

    def run(self, args: list[str]) -> MagicMock:
        def ok(stdout: str = "") -> MagicMock:
            return MagicMock(returncode=0, stdout=stdout, stderr="")

        def fail() -> MagicMock:
            return MagicMock(returncode=1, stdout="", stderr="")

        cmd = args[1] if len(args) > 1 else ""

        if cmd in ("has-session", "list-sessions"):
            return ok()
        if cmd == "list-clients":  # no attached client
            return fail()

        if cmd == "list-windows":
            fmt = args[args.index("-F") + 1] if "-F" in args else "#{window_name}"
            with self._state_lock:
                snapshot = list(self.windows)
            return ok("".join(f"{self._render(w, fmt, i)}\n" for i, w in enumerate(snapshot)))

        if cmd == "new-window":
            name = args[args.index("-n") + 1]
            path = args[args.index("-c") + 1] if "-c" in args else ""
            # Let a concurrent creator catch up, so both commit the name they
            # each picked. Broken/timed-out barrier == nobody else came.
            with contextlib.suppress(threading.BrokenBarrierError):
                self._rendezvous.wait(timeout=_RENDEZVOUS_TIMEOUT)
            with self._state_lock:
                self.windows.append(
                    {
                        "window_id": f"@{self._next_id}",
                        "window_name": name,
                        "path": path,
                    }
                )
                self._next_id += 1
            return ok()

        if cmd == "show-option" and "@thread_id" in args:
            win = self._resolve(args[args.index("-t") + 1])
            value = (win or {}).get("thread_id", "")
            # tmux exits non-zero when the option is unset.
            return ok(f"{value}\n") if value else fail()

        if cmd == "set-option" and "@thread_id" in args:
            win = self._resolve(args[args.index("-t") + 1])
            if win is None:
                return fail()
            with self._state_lock:
                if "-uw" in args:
                    win.pop("thread_id", None)
                else:
                    win["thread_id"] = args[-1]
            return ok()

        if cmd == "rename-window":
            win = self._resolve(args[args.index("-t") + 1])
            if win is None:
                return fail()
            with self._state_lock:
                win["window_name"] = args[-1]
            return ok()

        if cmd == "list-panes":
            return ok("zsh\n")

        # resize-window / move-window / swap-window / set-window-option / …
        return ok()


def _manager() -> TmuxSessionManager:
    """A manager as ``resolve_tmux_manager`` builds them: fresh, same session."""
    mgr = TmuxSessionManager(session_name=_SESSION, mapping_path="")
    mgr._available = True
    return mgr


def _race_two_creates(fake: FakeTmux) -> dict[int, str]:
    """Have two independent managers create windows for two threads at once."""
    managers = {_THREAD_A: _manager(), _THREAD_B: _manager()}
    assert managers[_THREAD_A] is not managers[_THREAD_B]

    results: dict[int, str] = {}
    start = threading.Barrier(len(managers))

    def worker(thread_id: int) -> None:
        start.wait()
        results[thread_id] = managers[thread_id].create_session(thread_id, f"{_BASE}/{thread_id}")

    with patch("c_lord.tmux._run", side_effect=fake.run):
        workers = [threading.Thread(target=worker, args=(tid,)) for tid in managers]
        for t in workers:
            t.start()
        for t in workers:
            t.join(timeout=30)
        assert not any(t.is_alive() for t in workers), "create_session deadlocked"

    return results


def test_concurrent_creates_do_not_produce_two_windows_with_the_same_name() -> None:
    """The #649 root cause: per-instance lock + per-instance counter → twin ``w{N}``."""
    fake = FakeTmux()
    _race_two_creates(fake)

    names = [w["window_name"] for w in fake.windows]
    assert len(fake.windows) == 2, f"expected one window per thread, got {fake.windows}"
    assert len(set(names)) == len(names), (
        f"two windows share a name: {names} — a session:NAME target is now ambiguous, "
        "which is how thread A's keystrokes reached thread B's checkout (#649)"
    )


def test_each_thread_is_tagged_on_the_window_holding_its_own_checkout() -> None:
    """The #649 symptom: ``@218`` sat in ...704's dir but was tagged ...706."""
    fake = FakeTmux()
    _race_two_creates(fake)

    for thread_id in (_THREAD_A, _THREAD_B):
        tagged = [w for w in fake.windows if w.get("thread_id") == str(thread_id)]
        assert len(tagged) == 1, (
            f"thread {thread_id} is claimed by {len(tagged)} windows: {fake.windows}"
        )
        assert tagged[0]["path"] == f"{_BASE}/{thread_id}", (
            f"thread {thread_id}'s tag landed on a window sitting in "
            f"{tagged[0]['path']} — another thread's checkout (#649)"
        )


def test_rebuild_does_not_log_a_window_colliding_with_itself(caplog) -> None:
    """AC5: duplicate *names* must not read as one window fighting itself.

    Production logged ``Duplicate @thread_id … claimed by w134, w134`` 42,001
    times. Two real windows were in conflict, but the message named them both by
    the name they shared, so the line looked like a logging bug rather than the
    incident it was. Claims are keyed by ``window_id`` now, and the log leads
    with it.
    """
    import logging

    fake = FakeTmux()
    # The production shape: two windows named w134, both tagged with one thread.
    fake.windows = [
        {
            "window_id": "@218",
            "window_name": "w134",
            "path": f"{_BASE}/{_THREAD_A}",
            "thread_id": str(_THREAD_B),
        },
        {
            "window_id": "@219",
            "window_name": "w134",
            "path": f"{_BASE}/{_THREAD_B}",
            "thread_id": str(_THREAD_B),
        },
    ]
    mgr = _manager()

    with patch("c_lord.tmux._run", side_effect=fake.run), caplog.at_level(logging.WARNING):
        mgr._rebuild_mapping()

    conflict = [r.getMessage() for r in caplog.records if "Duplicate @thread_id" in r.getMessage()]
    assert conflict, "a genuine two-window conflict must still be reported"
    assert "claimed by w134, w134" not in conflict[0], conflict[0]
    assert "@218" in conflict[0] and "@219" in conflict[0], conflict[0]
    # The bound window is one of the two real windows, addressed by id.
    assert mgr._thread_to_window[_THREAD_B] in ("@218", "@219")


def test_duplicate_window_names_reports_only_shared_names() -> None:
    fake = FakeTmux()
    fake.windows = [
        {"window_id": "@218", "window_name": "w134", "path": ""},
        {"window_id": "@219", "window_name": "w134", "path": ""},
        {"window_id": "@247", "window_name": "w136", "path": ""},
    ]
    mgr = _manager()

    with patch("c_lord.tmux._run", side_effect=fake.run):
        assert mgr.duplicate_window_names() == ["w134"]

    fake.windows.pop()  # unique names left → nothing to report
    fake.windows[1]["window_name"] = "w135"
    with patch("c_lord.tmux._run", side_effect=fake.run):
        assert mgr.duplicate_window_names() == []


def test_ambiguous_window_text_does_not_send_the_user_to_restart_claude() -> None:
    """AC6: the advice must match the failure.

    ``/restart-claude`` restarts Claude *inside* a window; it cannot remove a
    duplicate name. Two threads followed that advice for a day and stayed dead.
    """
    from c_lord.claude.tmux_runner import _ambiguous_window, _delivery_failure, _missing_window

    text = _ambiguous_window("Claude の起動", _THREAD_B, "c-lord", ["w134"])

    assert "w134" in text, "name the window the user has to go look at"
    assert "c-lord" in text, "name the session, so the suggested command is runnable as-is"
    assert "tmux list-windows" in text, "give the command that actually shows the problem"
    # The pane is alive, so the message must deny that diagnosis rather than make it.
    assert "ペインが落ちているのではなく" in text, "correct the diagnosis the old wording made"
    assert "`/restart-claude` では直りません" in text, (
        "say plainly that the old advice will not work"
    )
    assert "`/restart-claude` でセッションを立て直して" not in text, "never send them there"
    # A distinct diagnosis, not a re-skin of either neighbouring message.
    assert text != _delivery_failure("Claude の起動", "hello")
    assert text != _missing_window("Claude の起動", _THREAD_B)
