"""Where Claude in a session reaches this bot's REST API (#258).

The API server records the address it actually bound; ``start_claude`` hands
it to Claude as ``CLORD_API_URL``.  Kept free of aiohttp so :mod:`c_lord.tmux`
can import it on hosts that run without the API.

Why it is not read from the environment alone: with ``CLORD_API_PORT`` unset
the API walks to the next free port (#258), so the port it ends up on is only
known after the bind.  An ``CLORD_API_URL`` the operator wrote down still wins —
it may name a reverse proxy rather than the socket — but nobody has to keep it
in step with the port by hand any more.
"""

from __future__ import annotations

import os

_ENV = "CLORD_API_URL"

# The bound address, or None while no API is listening in this process.
_bound: str | None = None


def advertise(host: str, port: int) -> None:
    """Record that the API is listening on *host*:*port*."""
    global _bound
    # A wildcard bind is reachable on loopback; the wildcard itself is not an
    # address anyone can connect to.
    if host in ("", "0.0.0.0"):
        host = "127.0.0.1"
    elif host == "::":
        host = "::1"
    if ":" in host:
        host = f"[{host}]"
    _bound = f"http://{host}:{port}"


def clear() -> None:
    """Record that the API stopped (or never started)."""
    global _bound
    _bound = None


def current() -> str | None:
    """The URL Claude should use for this bot's API, or None when there is none.

    ``CLORD_API_URL`` from the environment wins when set — but only while the
    API is actually listening: a URL to a server that failed to bind would send
    Claude's curl to whatever else holds that port.
    """
    if _bound is None:
        return None
    return os.getenv(_ENV) or _bound
