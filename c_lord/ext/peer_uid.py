"""Which Unix user is on the other end of a loopback TCP connection (#457).

``127.0.0.1`` is a **network** boundary, not a **UID** boundary. Every Unix
user on the host can open the bot's control-plane port, and ``POST /api/spawn``
starts a Claude Code session as *the bot's* user — so binding to loopback buys
nothing against the other ten accounts on this machine. The kernel does know
who connected, it just is not in the socket API for TCP: ``SO_PEERCRED`` is a
Unix-domain-socket feature.

``/proc/net/tcp`` and ``/proc/net/tcp6`` list every TCP socket on the host with
the UID that owns it. A connection is identified by its 4-tuple, which is
unique kernel-wide, so looking up the row whose *local* end is our peer and
whose *remote* end is our own socket yields exactly the connecting process's
UID — no guessing, no race with PID reuse (the row exists for as long as the
connection we are serving does).

macOS has no ``/proc`` (#899). There the same question goes to ``lsof``, which
lists each socket as ``local->remote`` with its owner's UID. Our own accepted
socket shows up too (its *remote* end is the peer), so only the row whose
*local* end is the peer and whose remote end is us counts. A non-root lsof
cannot see another user's processes at all, so a foreign caller simply finds
no row — and no row is a refusal.

Deliberately strict: anything that does not resolve to exactly one UID returns
``None``, which the caller must treat as "unproven" and refuse. A wrong UID
here is an authorization bypass; no answer is always better than a guess.
"""

from __future__ import annotations

import logging
import socket
import subprocess
import sys
from collections.abc import Callable, Iterable, Sequence
from pathlib import Path

logger = logging.getLogger(__name__)

#: Kernel tables listing every TCP socket and its owning UID.
PROC_NET_TCP: tuple[Path, ...] = (Path("/proc/net/tcp"), Path("/proc/net/tcp6"))

#: Bound on one ``lsof`` call. It runs per request, and a hung one must not
#: hold the request open — a timeout is just another "unproven".
_LSOF_TIMEOUT_SECONDS = 5.0

#: Column indexes in a ``/proc/net/tcp`` row (after ``str.split()``).
_LOCAL, _REMOTE, _UID = 1, 2, 7


def encode_address(ip: str, port: int) -> str | None:
    """Render ``ip:port`` the way ``/proc/net/tcp`` prints it.

    The kernel writes each 4-byte word of the address in host byte order, so on
    a little-endian machine ``127.0.0.1`` comes out as ``0100007F``. Encoding
    our side to that format is safer than decoding every row to text: the rows
    are attacker-influenced input, the two values we hold are not.

    Returns ``None`` for an address that is not IPv4/IPv6 (a Unix socket path,
    for instance).
    """
    for family in (socket.AF_INET, socket.AF_INET6):
        try:
            packed = socket.inet_pton(family, ip)
        except (OSError, UnicodeEncodeError):
            continue
        words = b"".join(packed[i : i + 4][::-1] for i in range(0, len(packed), 4))
        return f"{words.hex().upper()}:{port:04X}"
    return None


def _address_of(sock: Sequence[object] | None) -> tuple[str, int] | None:
    """Pull ``(ip, port)`` out of a ``getpeername``/``getsockname`` tuple."""
    if not isinstance(sock, (tuple, list)) or len(sock) < 2:
        return None
    ip, port = sock[0], sock[1]
    if not isinstance(ip, str) or not isinstance(port, int):
        return None
    return ip, port


def _uids_for_connection(table: Path, local: str, remote: str) -> set[int]:
    """UIDs of the rows in *table* whose 4-tuple is ``local`` → ``remote``."""
    try:
        rows = table.read_text().splitlines()
    except OSError:
        return set()

    uids: set[int] = set()
    for row in rows[1:]:  # row 0 is the column header
        parts = row.split()
        if len(parts) <= _UID:
            continue
        if parts[_LOCAL] != local or parts[_REMOTE] != remote:
            continue
        try:
            uids.add(int(parts[_UID]))
        except ValueError:
            continue
    return uids


def _run_lsof(args: list[str]) -> str | None:
    """stdout of ``lsof``, or ``None`` when it is missing, hangs or fails."""
    try:
        result = subprocess.run(
            args, capture_output=True, text=True, timeout=_LSOF_TIMEOUT_SECONDS, check=False
        )
    except (OSError, subprocess.SubprocessError) as exc:
        logger.warning("peer UID lookup via lsof failed: %s", exc)
        return None
    # lsof exits 1 when nothing matched; that is an empty answer, not an error.
    if result.returncode not in (0, 1):
        logger.warning("peer UID lookup via lsof exited %d", result.returncode)
        return None
    return result.stdout


def _lsof_endpoint(ip: str, port: int) -> str:
    """``ip:port`` as ``lsof -nP`` prints it (IPv6 in brackets)."""
    return f"[{ip}]:{port}" if ":" in ip else f"{ip}:{port}"


def _lsof_peer_uid(
    peer: tuple[str, int], ours: tuple[str, int], lsof: Callable[[list[str]], str | None]
) -> int | None:
    """UID owning the socket whose local end is *peer* and remote end is *ours*."""
    for ip in (peer[0], ours[0]):
        try:
            socket.inet_pton(socket.AF_INET6 if ":" in ip else socket.AF_INET, ip)
        except (OSError, UnicodeEncodeError):
            return None  # never hand lsof anything but a literal address

    host = f"[{peer[0]}]" if ":" in peer[0] else peer[0]
    out = lsof(["lsof", "-nP", "-w", f"-iTCP@{host}:{peer[1]}", "-Fpun"])
    if out is None:
        return None

    wanted = f"{_lsof_endpoint(*peer)}->{_lsof_endpoint(*ours)}"
    uids: set[int] = set()
    uid: int | None = None
    for line in out.splitlines():
        tag, value = line[:1], line[1:]
        if tag == "p":
            uid = None  # a new process; its u line follows
        elif tag == "u":
            try:
                uid = int(value)
            except ValueError:
                uid = None
        elif tag == "n" and value == wanted:
            if uid is None:
                return None  # a match we cannot attribute is not an answer
            uids.add(uid)
    if len(uids) == 1:
        return uids.pop()
    if uids:
        logger.warning("peer UID lookup via lsof was ambiguous for %s: %s", wanted, sorted(uids))
    return None


def resolve_peer_uid(
    peer: Sequence[object] | None,
    sockname: Sequence[object] | None,
    tables: Iterable[Path] = PROC_NET_TCP,
    *,
    platform: str = sys.platform,
    lsof: Callable[[list[str]], str | None] = _run_lsof,
) -> int | None:
    """UID of the process that opened the connection, or ``None`` if unproven.

    Args:
        peer: The remote end, as ``socket.getpeername()`` returns it — for a
            server, the client.
        sockname: Our own end, as ``socket.getsockname()`` returns it.
        tables: Kernel socket tables to search (overridable for tests).
        platform: ``sys.platform``; only ``darwin`` falls back to ``lsof``,
            and only when none of *tables* exists.
        lsof: Runs ``lsof`` and returns its stdout (overridable for tests).

    Returns:
        The connecting user's UID. ``None`` when neither the tables nor (on
        macOS) ``lsof`` can prove it, when the connection is not in it (a remote peer — its
        socket lives on another host), or when the rows disagree.
    """
    peer_addr = _address_of(peer)
    sock_addr = _address_of(sockname)
    if peer_addr is None or sock_addr is None:
        return None

    tables = tuple(tables)
    if platform == "darwin" and not any(t.exists() for t in tables):
        return _lsof_peer_uid(peer_addr, sock_addr, lsof)

    local = encode_address(*peer_addr)
    remote = encode_address(*sock_addr)
    if local is None or remote is None:
        return None

    # A 4-tuple lives in exactly one table (the IPv4 and IPv6 encodings of an
    # address never collide), so the first table that knows this connection is
    # the answer — no need to read the other one on every request.
    for table in tables:
        uids = _uids_for_connection(table, local, remote)
        if len(uids) == 1:
            return uids.pop()
        if uids:
            # Cannot happen for a real 4-tuple; if it ever does, refuse rather
            # than pick one — this value is an authorization decision.
            logger.warning(
                "peer UID lookup was ambiguous for %s -> %s: %s", local, remote, sorted(uids)
            )
            return None
    return None
