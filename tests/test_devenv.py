"""Tests for docker dev-environment discovery — Issue #573.

A workspace can start a dev environment (``docker compose up``, ``supabase
start``). When the workspace goes away the containers keep running, holding
host ports, with nothing recording whose they were. Observed on the production
host: 12 ``supabase_*_1539493775645347900`` containers still bound to a session
dir whose Claude had long exited, holding ports 55321-55327.

Discovery must not depend on Claude having remembered anything (#491): docker
stamps the launch directory and project name onto every container, so the link
is recoverable from docker alone.
"""

from __future__ import annotations

import json

import pytest

from c_lord.devenv import (
    DevContainer,
    containers_for_session_dir,
    orphaned_containers,
)

SESSION_DIR = "/home/yousan/c-lord-sessions/1505747831447883806/1541974051277250632"
OTHER_DIR = "/home/yousan/c-lord-sessions/1505747831447883806/9999999999999999999"


def _inspect(
    name: str,
    *,
    status: str = "running",
    working_dir: str | None = None,
    mounts: list[str] | None = None,
    ports: dict[str, list[dict[str, str]]] | None = None,
    labels: dict[str, str] | None = None,
) -> str:
    doc = {
        "Id": f"id-{name}",
        "Name": f"/{name}",
        "State": {"Status": status},
        "Config": {"Labels": dict(labels or {})},
        "Mounts": [{"Source": m, "Destination": "/x"} for m in (mounts or [])],
        "HostConfig": {"PortBindings": ports or {}},
    }
    if working_dir is not None:
        doc["Config"]["Labels"]["com.docker.compose.project.working_dir"] = working_dir
    return json.dumps(doc)


class FakeDocker:
    """Stands in for the two ``docker`` calls the module makes."""

    def __init__(self, docs: list[str], *, available: bool = True) -> None:
        self.docs = docs
        self.available = available
        self.calls: list[list[str]] = []

    async def __call__(self, argv: list[str]) -> tuple[int, str]:
        self.calls.append(argv)
        if not self.available:
            return 127, ""
        if "ps" in argv:
            ids = [json.loads(d)["Id"] for d in self.docs]
            return 0, "\n".join(ids) + ("\n" if ids else "")
        if "inspect" in argv:
            return 0, "\n".join(self.docs) + "\n"
        return 0, ""


@pytest.mark.asyncio
class TestDiscovery:
    async def test_finds_container_by_compose_working_dir_label(self, monkeypatch) -> None:
        fake = FakeDocker([_inspect("app-web-1", working_dir=SESSION_DIR)])
        monkeypatch.setattr("c_lord.devenv._docker", fake)

        found = await containers_for_session_dir(SESSION_DIR)

        assert [c.name for c in found] == ["app-web-1"]

    async def test_finds_container_by_bind_mount(self, monkeypatch) -> None:
        """``supabase start`` sets no compose label — only a bind mount.

        This is how the 12 orphans on the production host were actually found;
        a label-only implementation misses them entirely.
        """
        fake = FakeDocker(
            [_inspect("supabase_db_123", mounts=[f"{SESSION_DIR}/supabase/config.toml"])]
        )
        monkeypatch.setattr("c_lord.devenv._docker", fake)

        found = await containers_for_session_dir(SESSION_DIR)

        assert [c.name for c in found] == ["supabase_db_123"]

    async def test_does_not_match_a_sibling_session_dir(self, monkeypatch) -> None:
        """Prefix matching must not let one thread claim another's containers."""
        fake = FakeDocker([_inspect("other-web-1", working_dir=OTHER_DIR)])
        monkeypatch.setattr("c_lord.devenv._docker", fake)

        assert await containers_for_session_dir(SESSION_DIR) == []

    async def test_reports_status_and_host_ports(self, monkeypatch) -> None:
        fake = FakeDocker(
            [
                _inspect(
                    "supabase_db_123",
                    status="exited",
                    working_dir=SESSION_DIR,
                    ports={"5432/tcp": [{"HostIp": "", "HostPort": "55322"}]},
                )
            ]
        )
        monkeypatch.setattr("c_lord.devenv._docker", fake)

        (c,) = await containers_for_session_dir(SESSION_DIR)

        assert c.status == "exited"
        assert c.ports == (55322,)
        assert c.running is False

    async def test_returns_empty_when_docker_is_missing(self, monkeypatch) -> None:
        """docker is not a dependency of c-lord — absence is normal, not an error."""
        fake = FakeDocker([], available=False)
        monkeypatch.setattr("c_lord.devenv._docker", fake)

        assert await containers_for_session_dir(SESSION_DIR) == []

    async def test_never_uses_a_shell(self, monkeypatch) -> None:
        fake = FakeDocker([_inspect("app-web-1", working_dir=SESSION_DIR)])
        monkeypatch.setattr("c_lord.devenv._docker", fake)

        await containers_for_session_dir(SESSION_DIR)

        for argv in fake.calls:
            assert argv[0] == "docker"
            assert not any(tok in ("sh", "-c", "bash") for tok in argv)


@pytest.mark.asyncio
class TestOrphans:
    async def test_container_of_a_vanished_workspace_is_orphaned(self, monkeypatch) -> None:
        fake = FakeDocker(
            [
                _inspect("live-web-1", working_dir=SESSION_DIR),
                _inspect("ghost-db-1", working_dir=OTHER_DIR),
            ]
        )
        monkeypatch.setattr("c_lord.devenv._docker", fake)

        orphans = await orphaned_containers(known_session_dirs={SESSION_DIR})

        assert [c.name for c in orphans] == ["ghost-db-1"]

    async def test_unrelated_containers_are_not_orphans(self, monkeypatch) -> None:
        """A container with no link to any session dir is simply not ours."""
        fake = FakeDocker([_inspect("minecraft", working_dir="/home/yousan/games/Minecraft")])
        monkeypatch.setattr("c_lord.devenv._docker", fake)

        assert await orphaned_containers(known_session_dirs={SESSION_DIR}) == []


def test_devcontainer_is_hashable_and_comparable() -> None:
    a = DevContainer(
        container_id="x", name="n", status="running", ports=(1,), project=None, source="mount"
    )
    b = DevContainer(
        container_id="x", name="n", status="running", ports=(1,), project=None, source="mount"
    )
    assert a == b
    assert len({a, b}) == 1
