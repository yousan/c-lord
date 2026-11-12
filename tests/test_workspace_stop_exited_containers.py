"""#1182 — only stopped (exited) containers, yet the notice said "▶️ 動いたまま".

"Stop, then delete" is the usual order: after /workspace-stop the containers are
``exited`` and hold no port. ``docker ps -a`` still finds them, and the shared
teardown of /workspace-stop and /workspace-delete counted them as running — so
the notice told the user the dev environment was left running, which reads as
a failed cleanup.
"""

from __future__ import annotations

# <<REDACTED 2 lines>>
import pytest

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


async def test_shared_teardown_does_not_call_exited_only_left_running(monkeypatch) -> None:
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


# --- review of #1182: paused / restarting containers still hold their ports ---------


@pytest.mark.parametrize("status", ["paused", "restarting"])
async def test_a_port_holding_container_is_not_called_none(monkeypatch, status: str) -> None:  # noqa: ANN001
    c = sm.DevContainer("id1", "db", status, (55322,), "p", "mount")
    monkeypatch.setattr(sm, "containers_for_session_dir", AsyncMock(return_value=[c]))
    cog = _setup(monkeypatch)
    t = await cog._stop_teardown(555, 999, close_reason=sm.WorkspaceReason.MANUAL)
    assert t.docker is sm.DockerOutcome.LEFT_RUNNING, status


@pytest.mark.parametrize("status", ["paused", "restarting"])
async def test_the_delete_notice_says_left_running(monkeypatch, status: str) -> None:  # noqa: ANN001
    c = sm.DevContainer("id1", "db", status, (55322,), "p", "mount")
    monkeypatch.setattr(sm, "containers_for_session_dir", AsyncMock(return_value=[c]))
    cog = _setup(monkeypatch)
    respond, ack, sent, _ = _capture()
    await cog._workspace_delete_impl(channel=_thread(), respond=respond, ack=ack)
    assert "動いたまま" in sent[-1]["embed"].description


async def test_running_stopped_but_paused_left_is_left_running(monkeypatch) -> None:  # noqa: ANN001
    """One running (stopped now) and one paused (left): the ports are not all free."""
    run = sm.DevContainer("id1", "web", "running", (8080,), "p", "mount")
    paused = sm.DevContainer("id2", "db", "paused", (55322,), "p", "mount")
    monkeypatch.setattr(sm, "containers_for_session_dir", AsyncMock(return_value=[run, paused]))
    monkeypatch.setattr(sm, "stop_containers", AsyncMock(return_value=["web"]))
    cog = _setup(monkeypatch)
    t = await cog._stop_teardown(555, 999, close_reason=sm.WorkspaceReason.MANUAL)
    assert t.docker is sm.DockerOutcome.LEFT_RUNNING


def _port_field(sent: list) -> str:  # noqa: ANN001
    for msg in reversed(sent):
        embed = msg.get("embed") if isinstance(msg, dict) else None
        if embed is None:
            continue
        for f in embed.fields:
            if "掴んだままのポート" in f.name:
                return f.value
        return ""
    return ""


async def test_the_stop_notice_names_the_port_still_held_not_the_one_freed(monkeypatch) -> None:  # noqa: ANN001
    """Review: running `web` stopped, paused `db` left — the warning names db :55322."""
    run = sm.DevContainer("id1", "web", "running", (8080,), "p", "mount")
    paused = sm.DevContainer("id2", "db", "paused", (55322,), "p", "mount")
    monkeypatch.setattr(sm, "containers_for_session_dir", AsyncMock(return_value=[run, paused]))
    monkeypatch.setattr(sm, "stop_containers", AsyncMock(return_value=["web"]))
    cog = _setup(monkeypatch)
    respond, ack, sent, _ = _capture()
    await cog._close_workspace_impl(channel=_thread(), respond=respond, ack=ack)
    field = _port_field(sent)
    assert "`db` :55322" in field and "8080" not in field, field


@pytest.mark.parametrize("status", ["paused", "restarting"])
async def test_the_stop_notice_names_a_paused_containers_port(monkeypatch, status: str) -> None:  # noqa: ANN001
    c = sm.DevContainer("id1", "db", status, (55322,), "p", "mount")
    monkeypatch.setattr(sm, "containers_for_session_dir", AsyncMock(return_value=[c]))
    cog = _setup(monkeypatch)
    respond, ack, sent, _ = _capture()
    await cog._close_workspace_impl(channel=_thread(), respond=respond, ack=ack)
    assert "`db` :55322" in _port_field(sent)
