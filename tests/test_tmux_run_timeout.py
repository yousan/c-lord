"""``c_lord.tmux._run`` never blocks forever (#699).

``_run`` is reached from ~40 ``asyncio.to_thread`` call sites. A tmux client
that never answers used to pin its executor worker for good, and
``asyncio.run()`` joins every worker without a timeout at shutdown — so the
bot finished ``close()`` and then hung, alive and silent, until someone
SIGKILLed it (2026-09-04 17:22, production).
"""

from __future__ import annotations

import subprocess
import time
from unittest.mock import patch

from c_lord import tmux


class TestRunTimeout:
    def test_passes_a_timeout_to_subprocess(self) -> None:
        with patch("c_lord.tmux.subprocess.run") as mock_run:
            tmux._run(["tmux", "-V"])
        timeout = mock_run.call_args.kwargs.get("timeout")
        assert timeout is not None and timeout > 0

    def test_timeout_returns_a_failed_result_instead_of_raising(self) -> None:
        """The contract is "never raises on non-zero exit" — a hang is a failure, not a raise."""
        exc = subprocess.TimeoutExpired(cmd=["tmux", "list-windows"], timeout=1)
        with patch("c_lord.tmux.subprocess.run", side_effect=exc):
            result = tmux._run(["tmux", "list-windows"])
        assert result.returncode != 0
        assert result.stdout == ""
        assert "timed out" in result.stderr

    def test_a_hung_command_is_cut_off(self) -> None:
        """Real process: a command that never returns comes back within the timeout."""
        with patch.object(tmux, "_RUN_TIMEOUT_SECONDS", 0.5):
            started = time.monotonic()
            result = tmux._run(["sleep", "30"])
            elapsed = time.monotonic() - started
        assert result.returncode != 0
        assert elapsed < 5
