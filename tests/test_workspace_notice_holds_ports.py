"""#1221: two workspace notices still used the pre-#1182 "running or not" rule.

#1182 decided that paused / restarting containers still hold their host ports
(``DevContainer.holds_ports``): the stop and sleep notices call them "▶️ 動いたまま".
But the restore line called the same paused container "⏹ 停止したまま", so one
container was "running" in one notice and "stopped" in the next. And
"⏹ 停止（:N を解放）" listed the ports of containers that were already exited before
the stop — ports that were never held, so "freed" was simply wrong. No real docker.
"""

from __future__ import annotations

from unittest.mock import AsyncMock

from c_lord import devenv
from c_lord.cogs import session_manage as sm
from c_lord.workspace_notice import (
    DockerOutcome,
    WorkspaceAction,
    WorkspaceReason,
    restored_devenv_line,
    workspace_notice_embed,
)

from .test_workspace_stop_exited_containers import _setup


def _c(name: str, status: str, port: int) -> devenv.DevContainer:
    return devenv.DevContainer(f"id-{name}", name, status, (port,), "p", "mount")


def test_restore_line_does_not_call_a_paused_container_stopped() -> None:
    """The stop notice says a paused container is ▶️ 動いたまま; the restore line must agree."""
    paused = _c("db", "paused", 5432)
    stop = workspace_notice_embed(
        WorkspaceAction.STOP,
        reason=WorkspaceReason.MANUAL,
        containers=[paused],
        docker=DockerOutcome.LEFT_RUNNING,
    )
    docker_row = next(f.value for f in stop.fields if f.name == "開発環境 (docker)")
    assert "動いたまま" in docker_row
    line = restored_devenv_line([paused])
    assert line is None or "停止したまま" not in line, line


async def test_stopped_notice_claims_to_free_a_port_an_exited_container_never_held(
    monkeypatch,  # noqa: ANN001
) -> None:
    a, old = _c("a", "running", 5000), _c("old", "exited", 7000)
    monkeypatch.setattr(sm, "containers_for_session_dir", AsyncMock(return_value=[a, old]))

    async def ok(argv):  # noqa: ANN001
        return 0, ""

    monkeypatch.setattr(devenv, "_docker", ok)
    cog = _setup(monkeypatch)
    t = await cog._stop_teardown(555, 999, close_reason=WorkspaceReason.MANUAL)
    row = sm.docker_value(t.docker, t.containers)
    assert ":7000" not in row, row
    assert ":5000" in row, row  # the port this stop did free is still named
