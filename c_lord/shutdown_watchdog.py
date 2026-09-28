"""Hard deadline for shutdown: dump every thread's stack, then exit (#699).

After SIGTERM, ``bot.close()`` can finish while the process still never exits:
``asyncio.run()`` ends by joining every default-executor worker with no
timeout, so one worker stuck in a blocking call keeps the process alive and
silent forever. By then the event loop is gone — nothing on it can log or time
out — and ``py-spy`` is blocked by ptrace restrictions on the host, so the hang
could neither end on its own nor be diagnosed (2026-09-04 17:22, production).

The watchdog is a plain daemon thread armed when the stop signal arrives. It
does not depend on the loop. Past its deadline it logs why, writes every
thread's stack to stderr (the bot log) with :mod:`faulthandler`, and calls
``os._exit`` — so a stuck shutdown both ends and names its cause.
"""

from __future__ import annotations

import faulthandler
import logging
import os
import sys
import threading
from collections.abc import Callable

logger = logging.getLogger(__name__)

#: Seconds from the stop signal to the forced exit. Must stay below the
#: supervisor's own grace (``scripts/staging.sh`` waits 15 s before SIGKILL) so
#: the bot dumps its stacks before anything kills it blind.
DEFAULT_SHUTDOWN_TIMEOUT_SECONDS = 10.0

#: Exit status of a forced exit — non-zero so a supervisor sees it was not clean.
WATCHDOG_EXIT_CODE = 70

_ENV_KEY = "CLORD_SHUTDOWN_TIMEOUT_SECONDS"

_lock = threading.Lock()
_armed: threading.Thread | None = None
_cancel = threading.Event()


def shutdown_timeout_from_env() -> float | None:
    """Deadline from ``CLORD_SHUTDOWN_TIMEOUT_SECONDS``; ``0`` disables the watchdog."""
    raw = os.getenv(_ENV_KEY, "").strip()
    if not raw:
        return DEFAULT_SHUTDOWN_TIMEOUT_SECONDS
    try:
        value = float(raw)
    except ValueError:
        logger.warning(
            "%s=%r is not a number; using %.0fs",
            _ENV_KEY,
            raw,
            DEFAULT_SHUTDOWN_TIMEOUT_SECONDS,
        )
        return DEFAULT_SHUTDOWN_TIMEOUT_SECONDS
    return value if value > 0 else None


def _dump_all_threads() -> None:
    faulthandler.dump_traceback(file=sys.stderr, all_threads=True)


def arm_shutdown_watchdog(
    timeout: float | None,
    *,
    exit_fn: Callable[[int], object] = os._exit,
    dump: Callable[[], object] = _dump_all_threads,
) -> threading.Thread | None:
    """Start the deadline once; later calls return the already-armed thread.

    Args:
        timeout: Seconds until the forced exit, or ``None`` to do nothing.
        exit_fn: How to exit (``os._exit``: atexit and thread joins are exactly
            what is stuck, so a normal exit would hang the same way).
        dump: Writes the diagnostic stack dump.

    Returns:
        The watchdog thread, or ``None`` when disabled.
    """
    global _armed
    if timeout is None:
        return None
    with _lock:
        if _armed is not None:
            return _armed

        def _fire() -> None:
            if _cancel.wait(timeout):
                return
            logger.critical(
                "shutdown did not finish within %.0fs of the stop signal — dumping all "
                "thread stacks and exiting with status %d (#699)",
                timeout,
                WATCHDOG_EXIT_CODE,
            )
            for handler in logging.getLogger().handlers:
                handler.flush()
            try:
                dump()
            finally:
                sys.stderr.flush()
                exit_fn(WATCHDOG_EXIT_CODE)

        _cancel.clear()
        _armed = threading.Thread(target=_fire, name="clord-shutdown-watchdog", daemon=True)
        _armed.start()
        logger.info("shutdown watchdog armed (%.0fs)", timeout)
        return _armed


def _reset_for_tests() -> None:
    """Disarm and forget the watchdog (tests only)."""
    global _armed
    with _lock:
        _cancel.set()
        if _armed is not None:
            _armed.join(5)
        _armed = None
        _cancel.clear()
