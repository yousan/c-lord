"""#1182 — only stopped (exited) containers, yet the notice said "▶️ 動いたまま".

"Stop, then delete" is the usual order: after /workspace-stop the containers are
``exited`` and hold no port. ``docker ps -a`` still finds them, and the shared
teardown of /workspace-stop and /workspace-delete counted them as running — so
the notice told the user the dev environment was left running, which reads as
a failed cleanup.
"""

from __future__ import annotations

from unittest.mock import AsyncMock, MagicMock

from c_lord.cogs import session_manage as sm
from c_lord.session_dir import CleanupResult

from .test_session_manage_gaps import _capture, _cog, _thread, _tmux


def _setup(monkeypatch):
    monkeypatch.setattr(sm, "release_window_number", MagicMock())
    cog = _cog()
    mgr = _tmux()
    mgr.kill_session = MagicMock(return_value=True)
    sdm = MagicMock()
    sdm.base_dir = "/s"
    sdm.cleanup_for_thread = MagicMock(return_value=CleanupResult("/s/555", 555, True, "removed"))
    cog._resolve_tmux_manager = AsyncMock(return_value=mgr)
    cog._resolve_session_dir_manager = AsyncMock(return_value=sdm)
    chat = MagicMock()
    chat.end_turn_for_workspace_stop = MagicMock()

    async def waited(_t: int) -> None:
        return None

    chat.wait_for_turn_end = waited
    cog.bot.get_cog = MagicMock(side_effect=lambda n: chat if n == "ClaudeChatCog" else None)
    return cog


def _exited() -> sm.DevContainer:
    return sm.DevContainer("id1", "db", "exited", (55322,), "p", "mount")


async def test_only_exited_containers_are_not_called_running(monkeypatch) -> None:
    """``docker ps -a`` finds exited containers; nothing is running, nothing holds a port."""
    monkeypatch.setattr(sm, "containers_for_session_dir", AsyncMock(return_value=[_exited()]))
    # real stop_containers: touches only running ones -> returns [] without calling docker
    cog = _setup(monkeypatch)
    respond, ack, sent, _ = _capture()
    await cog._workspace_delete_impl(channel=_thread(), respond=respond, ack=ack)
    d = sent[-1]["embed"].description
    assert "動いたまま" not in d, d


async def test_shared_teardown_calls_exited_only_left_running(monkeypatch) -> None:
    """The root is in the shared teardown, so /workspace-stop said the same."""
    monkeypatch.setattr(sm, "containers_for_session_dir", AsyncMock(return_value=[_exited()]))
    cog = _setup(monkeypatch)
    t = await cog._stop_teardown(555, 999, close_reason=sm.WorkspaceReason.MANUAL)
    assert t.docker is not sm.DockerOutcome.LEFT_RUNNING, t.docker


async def test_running_container_that_refuses_to_stop_is_still_left_running(monkeypatch) -> None:
    c = sm.DevContainer("id1", "db", "running", (55322,), "p", "mount")
    monkeypatch.setattr(sm, "containers_for_session_dir", AsyncMock(return_value=[c, _exited()]))
    monkeypatch.setattr(sm, "stop_containers", AsyncMock(return_value=[]))
    cog = _setup(monkeypatch)
    t = await cog._stop_teardown(555, 999, close_reason=sm.WorkspaceReason.MANUAL)
    assert t.docker is sm.DockerOutcome.LEFT_RUNNING
