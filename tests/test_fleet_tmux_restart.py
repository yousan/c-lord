"""#701: a tmux server swap must not kill every thread in silence.

2026-09-08: one work thread ran ``tmux -f /dev/null new-session -d -s …``
believing it was isolated.  ``-f /dev/null`` only skips the config file — the
socket stays the shared default one — so the fleet's tmux server was replaced
and every thread running at that moment died at once.  Each of them reported
``❌ Claude exited without producing a response (possible startup failure or
crash)``, so the two bystanders had no way to learn that the cause was not
theirs; one of them nearly lost a finished piece of work.

These tests fix two things:

* the *detection* — c-lord can tell that the server it started the turn on is
  not the server it is looking at now (AC3), proven against a real, ``-L``
  isolated tmux server as well as against mocks;
* the *report* — a turn cut off that way says so, instead of blaming a crash
  that never happened (AC4).
"""

from __future__ import annotations

import os
import shutil
import subprocess
import tempfile
import uuid
from pathlib import Path
from unittest.mock import MagicMock, patch

import pytest

from c_lord.claude.tmux_runner import FLEET_TMUX_RESTART_ERROR_PREFIX, TmuxClaudeRunner
from c_lord.cogs._run_helper import _make_error_embed
from c_lord.cogs.event_processor import EventProcessor
from c_lord.cogs.run_config import RunConfig
from c_lord.tmux import TmuxSessionManager, server_fingerprint

# A pane that shows nothing at all — what ``capture-pane`` yields once the
# window (and the server holding it) is gone.
_EMPTY_PANE = ""


# ── AC3: detecting that the server was replaced ────────────────────────────


class TestServerFingerprint:
    """The fingerprint must change iff the *server process* changed."""

    def test_fingerprint_combines_pid_and_server_start_time(self) -> None:
        with patch("c_lord.tmux._run") as run:
            run.return_value = subprocess.CompletedProcess([], 0, "1725546:1788800000\n", "")
            assert server_fingerprint() == "1725546:1788800000"
        args = run.call_args[0][0]
        assert args[0] == "tmux"
        assert "display-message" in args

    def test_no_server_running_is_unknown_not_a_change(self) -> None:
        """A failed query must be ``None`` — "don't know", never a fake identity.

        Reporting a swap because the query failed would blame the fleet for
        every unrelated tmux hiccup.
        """
        with patch("c_lord.tmux._run") as run:
            run.return_value = subprocess.CompletedProcess([], 1, "", "no server running")
            assert server_fingerprint() is None

    def test_socket_name_selects_another_server(self) -> None:
        """The isolation switch #701 is about — ``-L`` selects another server.

        Exposed so this project's own tests and rigs can exercise real tmux
        without ever touching the fleet's default socket.
        """
        with patch("c_lord.tmux._run") as run:
            run.return_value = subprocess.CompletedProcess([], 0, "1:2\n", "")
            server_fingerprint(socket_name="i701rig")
        args = run.call_args[0][0]
        assert args[:3] == ["tmux", "-L", "i701rig"]

    def test_manager_reports_none_when_tmux_is_unavailable(self) -> None:
        mgr = TmuxSessionManager(session_name="clord-test")
        mgr._available = False
        assert mgr.server_fingerprint() is None


@pytest.mark.skipif(shutil.which("tmux") is None, reason="tmux not installed")
class TestServerFingerprintAgainstRealTmux:
    """AC5: the same detection, against a live server on an isolated socket.

    Every command here carries ``-L`` *and* runs under a private
    ``TMUX_TMPDIR``, which is exactly the discipline whose absence caused
    #701.  Nothing in this class can reach the default socket.
    """

    @pytest.fixture
    def rig(self, monkeypatch: pytest.MonkeyPatch):
        """An isolated tmux server: private socket dir + private ``-L`` label."""
        tmpdir = tempfile.mkdtemp(prefix="i701-", dir=tempfile.gettempdir())
        monkeypatch.setenv("TMUX_TMPDIR", tmpdir)
        label = f"i701-{uuid.uuid4().hex[:8]}"

        def tmux(*args: str) -> subprocess.CompletedProcess[str]:
            env = {**os.environ, "TMUX_TMPDIR": tmpdir}
            return subprocess.run(
                ["tmux", "-L", label, *args], capture_output=True, text=True, env=env
            )

        try:
            yield label, tmux
        finally:
            tmux("kill-server")
            shutil.rmtree(tmpdir, ignore_errors=True)

    def test_a_replaced_server_reads_as_a_different_fingerprint(self, rig) -> None:
        label, tmux = rig
        assert server_fingerprint(socket_name=label) is None, "no server yet"

        assert tmux("new-session", "-d", "-s", "rig").returncode == 0
        before = server_fingerprint(socket_name=label)
        assert before is not None

        # Ordinary fleet activity must NOT look like a swap: same server.
        tmux("new-window", "-t", "rig")
        tmux("new-session", "-d", "-s", "rig2")
        assert server_fingerprint(socket_name=label) == before, (
            "creating windows/sessions changed the fingerprint — it would report "
            "a fleet restart on every ordinary turn"
        )

        # The #701 accident, reproduced on a socket that only this test owns.
        tmux("kill-server")
        assert tmux("new-session", "-d", "-s", "rig").returncode == 0
        after = server_fingerprint(socket_name=label)
        assert after is not None
        assert after != before, "a replaced tmux server must not read as the same server"


# ── AC4: the interrupted threads are told why ─────────────────────────────


@pytest.fixture
def tmux_manager() -> MagicMock:
    mgr = MagicMock()
    mgr.capture_pane.return_value = _EMPTY_PANE
    mgr.is_claude_running.return_value = False
    mgr.start_claude.return_value = True
    mgr.send_input.return_value = True
    mgr.duplicate_window_names.return_value = []
    mgr.session_name = "clord"
    # The healthy default: one server, unchanged for the whole turn.
    mgr.server_fingerprint.return_value = "1725546:1788800000"
    return mgr


@pytest.fixture
def runner(tmux_manager: MagicMock) -> TmuxClaudeRunner:
    return TmuxClaudeRunner(
        tmux_manager=tmux_manager, thread_id=12345, model="sonnet", timeout_seconds=10
    )


async def _run_to_result(runner: TmuxClaudeRunner):
    runner.timeout_seconds = 0.2
    events = []
    with (
        patch("c_lord.claude.tmux_runner._POLL_INTERVAL", 0.02),
        patch("c_lord.claude.tmux_runner._POST_STARTUP_DELAY", 0.0),
        patch("c_lord.claude.tmux_runner._STARTUP_TIMEOUT", 0.0),
        patch("c_lord.claude.tmux_runner._IDLE_TIMEOUT", 100.0),
    ):
        async for event in runner.run("調べて"):
            events.append(event)
    results = [e for e in events if e.is_complete]
    assert len(results) == 1
    return results[0]


class TestInterruptedTurnSaysTheFleetTmuxDied:
    """The bystander's view: why did my thread stop?"""

    @pytest.mark.asyncio
    async def test_server_swap_is_reported_instead_of_a_crash(
        self, runner: TmuxClaudeRunner, tmux_manager: MagicMock
    ) -> None:
        """AC4: the turn was cut off by the fleet, and says so."""
        # First read is the turn's baseline; every later read sees the server
        # that replaced it — the shape of the 2026-09-08 accident.
        seen = iter(["1725546:1788800000"])
        tmux_manager.server_fingerprint.side_effect = lambda: next(seen, "2524993:1788899999")

        result = await _run_to_result(runner)

        assert result.error is not None
        assert result.error.startswith(FLEET_TMUX_RESTART_ERROR_PREFIX), result.error
        assert "exited without producing a response" not in result.error
        assert "startup failure or crash" not in result.error

    @pytest.mark.asyncio
    async def test_a_turn_that_never_started_says_it_too(
        self, runner: TmuxClaudeRunner, tmux_manager: MagicMock
    ) -> None:
        """The gap staging found: the fleet can die during *startup*.

        ``start_claude`` then fails and the runner returns before the poll loop
        ever runs, so the fleet check has to sit on that exit as well. Measured
        on staging-3: without it the thread was told "this thread's tmux window
        was never created — check that the channel is bound with /clord-init",
        which sends the reader to a setting that is perfectly fine.
        """
        tmux_manager.start_claude.return_value = False
        tmux_manager.session_exists.return_value = False
        seen = iter(["1725546:1788800000"])
        tmux_manager.server_fingerprint.side_effect = lambda: next(seen, None)

        with patch("c_lord.claude.tmux_runner._SERVER_RECHECK_DELAY", 0.0):
            result = await _run_to_result(runner)

        assert result.error is not None
        assert result.error.startswith(FLEET_TMUX_RESTART_ERROR_PREFIX), result.error
        assert "clord-init" not in result.error

    @pytest.mark.asyncio
    async def test_a_start_failure_without_a_fleet_death_keeps_its_own_reason(
        self, runner: TmuxClaudeRunner, tmux_manager: MagicMock
    ) -> None:
        """The same exit must not blame the fleet when tmux is fine."""
        tmux_manager.start_claude.return_value = False
        tmux_manager.session_exists.return_value = False

        result = await _run_to_result(runner)

        assert result.error is not None
        assert not result.error.startswith(FLEET_TMUX_RESTART_ERROR_PREFIX), result.error

    @pytest.mark.asyncio
    async def test_unchanged_server_still_reports_the_crash(
        self, runner: TmuxClaudeRunner, tmux_manager: MagicMock
    ) -> None:
        """No false positives: a plain dead claude keeps its own diagnosis."""
        result = await _run_to_result(runner)

        assert result.error is not None
        assert not result.error.startswith(FLEET_TMUX_RESTART_ERROR_PREFIX)
        assert "exited without producing a response" in result.error

    @pytest.mark.asyncio
    async def test_unknown_fingerprint_is_not_treated_as_a_swap(
        self, runner: TmuxClaudeRunner, tmux_manager: MagicMock
    ) -> None:
        """tmux that cannot be queried is "don't know", not "the fleet died"."""
        tmux_manager.server_fingerprint.return_value = None

        result = await _run_to_result(runner)

        assert result.error is not None
        assert not result.error.startswith(FLEET_TMUX_RESTART_ERROR_PREFIX)

    @pytest.mark.asyncio
    async def test_a_server_killed_outright_is_reported_too(
        self, runner: TmuxClaudeRunner, tmux_manager: MagicMock
    ) -> None:
        """The #504 shape: the fleet's tmux is killed and nothing replaces it.

        ``systemctl --user restart c-lord.service`` kills the tmux server living
        in its cgroup — no successor, so the fingerprint reads as *gone* rather
        than as a different server.  The threads it took down are just as
        entitled to know why.
        """
        seen = iter(["1725546:1788800000"])
        tmux_manager.server_fingerprint.side_effect = lambda: next(seen, None)

        with patch("c_lord.claude.tmux_runner._SERVER_RECHECK_DELAY", 0.0):
            result = await _run_to_result(runner)

        assert result.error is not None
        assert result.error.startswith(FLEET_TMUX_RESTART_ERROR_PREFIX), result.error

    @pytest.mark.asyncio
    async def test_one_failed_query_is_not_a_fleet_death(
        self, runner: TmuxClaudeRunner, tmux_manager: MagicMock
    ) -> None:
        """A single unreadable query re-asks before blaming the fleet."""
        answers = iter(["1725546:1788800000", None])
        tmux_manager.server_fingerprint.side_effect = lambda: next(answers, "1725546:1788800000")

        with patch("c_lord.claude.tmux_runner._SERVER_RECHECK_DELAY", 0.0):
            result = await _run_to_result(runner)

        assert result.error is not None
        assert not result.error.startswith(FLEET_TMUX_RESTART_ERROR_PREFIX), result.error


class TestWhatTheThreadSees:
    """The embed and the turn outcome, i.e. what actually reaches Discord."""

    def test_embed_names_the_fleet_tmux_not_a_crash(self) -> None:
        embed = _make_error_embed(f"{FLEET_TMUX_RESTART_ERROR_PREFIX} server 1 → 2")
        text = f"{embed.title}\n{embed.description}"
        assert "tmux" in text
        assert "中断" in text, "the reader must be told the turn was cut off, not that it failed"

    @pytest.mark.asyncio
    async def test_the_turn_is_not_announced_as_finished(self) -> None:
        """A cut-off turn produced nothing, so it must not summon the owner."""
        thread = MagicMock()
        thread.send = MagicMock()

        async def _send(*args, **kwargs):
            return MagicMock()

        thread.send.side_effect = _send
        config = RunConfig(thread=thread, runner=MagicMock(), prompt="test")
        processor = EventProcessor(config)

        from c_lord.claude.types import MessageType, StreamEvent

        await processor.process(
            StreamEvent(
                message_type=MessageType.RESULT,
                is_complete=True,
                error=f"{FLEET_TMUX_RESTART_ERROR_PREFIX} server 1 → 2",
            )
        )

        assert config.outcome.no_response is True


# ── AC1: the convention, enforced rather than merely written ──────────────


class TestPytestNeverTouchesTheFleetSocket:
    """The suite itself must be unable to repeat the accident.

    ``pytest`` is run on the bot host while the fleet is live, and a few tests
    drive real tmux. The ``_isolated_tmux_socket`` fixture in ``conftest.py``
    points ``TMUX_TMPDIR`` at a private directory for the whole session, so a
    test that forgets ``-L`` still cannot reach ``/tmp/tmux-<uid>/default``.
    """

    def test_the_suite_runs_under_a_private_tmux_tmpdir(self) -> None:
        tmpdir = os.environ.get("TMUX_TMPDIR")
        assert tmpdir, "TMUX_TMPDIR is unset — tests would use the fleet's default socket"
        assert Path(tmpdir).is_dir()
        assert Path(tmpdir).name.startswith("clord-tests-tmux-"), tmpdir

    @pytest.mark.skipif(shutil.which("tmux") is None, reason="tmux not installed")
    def test_a_plain_new_session_lands_in_that_private_dir(self) -> None:
        """The proof, not the promise: a bare ``tmux new-session`` is contained.

        This is the exact command shape of the 2026-09-08 accident, minus the
        isolation flag. It must create its socket under the test directory and
        leave the fleet's socket untouched.
        """
        tmpdir = Path(os.environ["TMUX_TMPDIR"])
        session = f"i701-{uuid.uuid4().hex[:8]}"
        assert (
            subprocess.run(
                ["tmux", "new-session", "-d", "-s", session], capture_output=True, text=True
            ).returncode
            == 0
        )
        try:
            sockets = [p for p in tmpdir.rglob("*") if p.is_socket()]
            assert sockets, f"no tmux socket under {tmpdir} — the session escaped isolation"
            listed = subprocess.run(
                ["tmux", "list-sessions", "-F", "#{session_name}"],
                capture_output=True,
                text=True,
            )
            assert session in listed.stdout
            assert "clord" not in listed.stdout, (
                "the test's tmux client can see the fleet's sessions — not isolated"
            )
        finally:
            subprocess.run(["tmux", "kill-session", "-t", session], capture_output=True)
