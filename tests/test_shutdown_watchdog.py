"""Shutdown watchdog: a bot that cannot finish stopping names the cause and exits (#699).

After SIGTERM the event loop can finish while ``asyncio.run()`` waits forever
on an executor worker stuck in a blocking call. At that point nothing on the
loop can log or time out, and ``py-spy`` is blocked by ptrace restrictions on
the host, so the hang could neither end nor be diagnosed. The watchdog is a
plain thread armed at signal time: past its deadline it dumps every thread's
stack (faulthandler) and hard-exits.
"""

from __future__ import annotations

import io
import subprocess
import sys
import textwrap
import time
from pathlib import Path

import pytest

from c_lord import shutdown_watchdog as wd


@pytest.fixture(autouse=True)
def _reset() -> None:
    wd._reset_for_tests()


class TestTimeoutFromEnv:
    def test_default(self, monkeypatch: pytest.MonkeyPatch) -> None:
        monkeypatch.delenv("CLORD_SHUTDOWN_TIMEOUT_SECONDS", raising=False)
        assert wd.shutdown_timeout_from_env() == wd.DEFAULT_SHUTDOWN_TIMEOUT_SECONDS

    def test_override(self, monkeypatch: pytest.MonkeyPatch) -> None:
        monkeypatch.setenv("CLORD_SHUTDOWN_TIMEOUT_SECONDS", "3.5")
        assert wd.shutdown_timeout_from_env() == 3.5

    def test_zero_disables(self, monkeypatch: pytest.MonkeyPatch) -> None:
        monkeypatch.setenv("CLORD_SHUTDOWN_TIMEOUT_SECONDS", "0")
        assert wd.shutdown_timeout_from_env() is None

    def test_garbage_falls_back_to_default(self, monkeypatch: pytest.MonkeyPatch) -> None:
        monkeypatch.setenv("CLORD_SHUTDOWN_TIMEOUT_SECONDS", "soon")
        assert wd.shutdown_timeout_from_env() == wd.DEFAULT_SHUTDOWN_TIMEOUT_SECONDS


class TestArm:
    def test_fires_dumps_stacks_and_exits(self) -> None:
        exits: list[int] = []
        out = io.StringIO()
        thread = wd.arm_shutdown_watchdog(0.1, exit_fn=exits.append, dump=lambda: out.write("X"))
        assert thread is not None
        thread.join(5)
        assert exits == [wd.WATCHDOG_EXIT_CODE]
        assert out.getvalue() == "X"

    def test_arming_twice_keeps_one_watchdog(self) -> None:
        exits: list[int] = []
        first = wd.arm_shutdown_watchdog(0.1, exit_fn=exits.append, dump=lambda: None)
        second = wd.arm_shutdown_watchdog(0.1, exit_fn=exits.append, dump=lambda: None)
        assert first is not None
        assert second is first
        first.join(5)
        assert exits == [wd.WATCHDOG_EXIT_CODE]

    def test_disabled_does_nothing(self) -> None:
        assert wd.arm_shutdown_watchdog(None, exit_fn=lambda _c: None) is None


# The #699 mechanism, reproduced for real: main() returns, then asyncio.run()
# joins a default-executor worker that is blocked in subprocess.run forever.
_HUNG_BOT = textwrap.dedent(
    """
    import asyncio, os, signal, subprocess, sys
    sys.path.insert(0, {root!r})
    from c_lord.main import install_shutdown_signal_handlers

    async def main():
        loop = asyncio.get_running_loop()
        done = asyncio.Event()
        async def _shutdown():
            done.set()
        install_shutdown_signal_handlers(loop, _shutdown)
        # A worker that never comes back (stands in for a tmux client that hangs).
        loop.run_in_executor(None, lambda: subprocess.run(["sleep", "30"], capture_output=True))
        print("ready", flush=True)
        await done.wait()
        print("main returned", flush=True)

    asyncio.run(main())
    print("asyncio.run returned", flush=True)
    """
)


def test_sigterm_with_a_stuck_worker_exits_and_names_the_stack(tmp_path: Path) -> None:
    script = tmp_path / "hung_bot.py"
    script.write_text(_HUNG_BOT.format(root=str(Path(__file__).resolve().parent.parent)))
    proc = subprocess.Popen(
        [sys.executable, str(script)],
        stdout=subprocess.PIPE,
        stderr=subprocess.PIPE,
        text=True,
        env={"PATH": "/usr/bin:/bin", "CLORD_SHUTDOWN_TIMEOUT_SECONDS": "1"},
    )
    try:
        assert proc.stdout is not None
        assert proc.stdout.readline().strip() == "ready"
        started = time.monotonic()
        proc.terminate()
        try:
            out, err = proc.communicate(timeout=15)
        except subprocess.TimeoutExpired:
            pytest.fail("process hung after SIGTERM (#699): asyncio.run never returned")
        elapsed = time.monotonic() - started
    finally:
        if proc.poll() is None:
            proc.kill()
            proc.communicate()
    assert "main returned" in out
    assert proc.returncode == wd.WATCHDOG_EXIT_CODE
    assert elapsed < 10
    assert "did not finish within" in err  # the log line naming the watchdog
    assert "subprocess.py" in err  # faulthandler: the stuck worker's stack
