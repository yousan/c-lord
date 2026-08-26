"""Discover the docker dev environment a workspace started — Issue #573.

A workspace can bring up a dev environment (``docker compose up``, ``supabase
start``). When the workspace goes away the containers keep running: they hold
host ports, and nothing records whose they were. On the production host this had
already happened — 12 ``supabase_*_1539493775645347900`` containers still bound
to a session dir whose Claude had long exited, holding ports 55321-55327 with no
owner.

The link is recoverable **from docker alone**, because docker stamps the launch
directory and project name onto every container it creates. Nothing here depends
on Claude having remembered to record anything, which is the failure mode #491
taught us not to build on.

Two independent signals are used, and both are needed:

``com.docker.compose.project.working_dir``
    Set by ``docker compose`` on every container in a project.

bind-mount sources
    ``supabase start`` sets no compose label at all. The production orphans were
    found through their mounts; a label-only implementation misses them.

docker is **not** a dependency of c-lord. Every entry point here returns an
empty result when docker is absent or unusable rather than raising — a host
without docker is a normal host, not a broken one.
"""

from __future__ import annotations

import asyncio
import contextlib
import json
import logging
from collections.abc import Iterable
from dataclasses import dataclass

logger = logging.getLogger(__name__)

#: Label ``docker compose`` writes with the directory the project was started from.
COMPOSE_WORKING_DIR_LABEL = "com.docker.compose.project.working_dir"
COMPOSE_PROJECT_LABEL = "com.docker.compose.project"

#: Seconds to wait on a docker call before giving up. A wedged docker daemon
#: must never stall a turn, so this is short and failure is silent.
_TIMEOUT = 20.0


@dataclass(frozen=True)
class DevContainer:
    """One container belonging to a workspace's dev environment."""

    container_id: str
    name: str
    status: str
    ports: tuple[int, ...]
    project: str | None
    source: str
    """Which signal matched: ``compose-label`` or ``mount``. Kept for the
    notice text and for debugging why something was (not) claimed."""

    @property
    def running(self) -> bool:
        return self.status == "running"


async def _docker(argv: list[str]) -> tuple[int, str]:
    """Run a docker command. Returns ``(returncode, stdout)``; never raises.

    ``create_subprocess_exec`` (never ``shell=True``) — argv elements reach
    docker as arguments, so a hostile container name or path can never become
    shell. A missing binary, a wedged daemon, and a non-zero exit are all
    reported as a non-zero return code, because callers treat them the same way:
    there is no dev environment we can see.
    """
    try:
        proc = await asyncio.create_subprocess_exec(
            *argv,
            stdout=asyncio.subprocess.PIPE,
            stderr=asyncio.subprocess.PIPE,
        )
    except (FileNotFoundError, OSError):
        return 127, ""
    try:
        stdout, _ = await asyncio.wait_for(proc.communicate(), timeout=_TIMEOUT)
    except TimeoutError:
        with contextlib.suppress(ProcessLookupError):
            proc.kill()
        logger.warning("docker %s timed out after %.0fs", " ".join(argv[1:3]), _TIMEOUT)
        return 124, ""
    return proc.returncode or 0, stdout.decode("utf-8", "replace")


def _host_ports(port_bindings: dict[str, list[dict[str, str]]] | None) -> tuple[int, ...]:
    """Host ports this container publishes, deduplicated and sorted."""
    ports: set[int] = set()
    for bindings in (port_bindings or {}).values():
        for binding in bindings or []:
            raw = (binding or {}).get("HostPort") or ""
            if raw.isdigit():
                ports.add(int(raw))
    return tuple(sorted(ports))


def _under(path: str, root: str) -> bool:
    """True when *path* is *root* itself or lives inside it.

    The ``+ "/"`` matters: session dirs are siblings under one base directory,
    so a bare ``startswith`` would let thread ``123`` claim thread ``1234``'s
    containers.
    """
    if not path or not root:
        return False
    root = root.rstrip("/")
    return path == root or path.startswith(root + "/")


async def _inspect_all() -> list[dict]:
    """Every container on the host, as parsed ``docker inspect`` documents.

    Two docker calls total regardless of how many containers exist: one ``ps``
    for the ids, one ``inspect`` for all of them.
    """
    rc, out = await _docker(["docker", "ps", "-a", "--format", "{{.ID}}"])
    if rc != 0:
        return []
    ids = [line.strip() for line in out.splitlines() if line.strip()]
    if not ids:
        return []

    rc, out = await _docker(["docker", "inspect", *ids, "--format", "{{json .}}"])
    if rc != 0:
        return []

    docs: list[dict] = []
    for line in out.splitlines():
        line = line.strip()
        if not line:
            continue
        try:
            docs.append(json.loads(line))
        except json.JSONDecodeError:
            logger.debug("devenv: skipping unparsable docker inspect line")
    return docs


def _match(doc: dict, session_dir: str) -> str | None:
    """Which signal links *doc* to *session_dir*, or None when nothing does."""
    labels = (doc.get("Config") or {}).get("Labels") or {}
    if _under(labels.get(COMPOSE_WORKING_DIR_LABEL) or "", session_dir):
        return "compose-label"
    for mount in doc.get("Mounts") or []:
        if _under((mount or {}).get("Source") or "", session_dir):
            return "mount"
    return None


def _to_container(doc: dict, source: str) -> DevContainer:
    labels = (doc.get("Config") or {}).get("Labels") or {}
    return DevContainer(
        container_id=doc.get("Id") or "",
        name=(doc.get("Name") or "").lstrip("/"),
        status=(doc.get("State") or {}).get("Status") or "unknown",
        ports=_host_ports((doc.get("HostConfig") or {}).get("PortBindings")),
        project=labels.get(COMPOSE_PROJECT_LABEL),
        source=source,
    )


async def containers_for_session_dir(session_dir: str) -> list[DevContainer]:
    """Containers this workspace's session dir started. Empty when docker is absent."""
    if not session_dir:
        return []
    found: list[DevContainer] = []
    for doc in await _inspect_all():
        source = _match(doc, session_dir)
        if source is not None:
            found.append(_to_container(doc, source))
    return found


async def orphaned_containers(known_session_dirs: Iterable[str]) -> list[DevContainer]:
    """Containers tied to a session dir c-lord no longer knows about.

    A container with no link to *any* session dir is simply not ours (the host's
    own ``minecraft`` / ``grafana`` / …) and is never reported.
    """
    known = {d.rstrip("/") for d in known_session_dirs if d}
    base_dirs = {d.rsplit("/", 1)[0] for d in known if "/" in d}
    if not base_dirs:
        return []

    orphans: list[DevContainer] = []
    for doc in await _inspect_all():
        for base in base_dirs:
            source = _match(doc, base)
            if source is None:
                continue
            if any(_match(doc, known_dir) for known_dir in known):
                break  # belongs to a workspace we still track
            orphans.append(_to_container(doc, source))
            break
    return orphans
