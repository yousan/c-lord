"""#1112: ``/tmux-list`` lists each tmux session once.

The tmux session name follows the *repo*, not the channel (CLAUDE.md decision
10), so two channels bound to one repo share a session. ``/tmux-list`` walks the
channel bindings, and used to add that session's windows once per channel —
every window shown twice, and the count in the title doubled.
"""

from __future__ import annotations

from unittest.mock import AsyncMock, MagicMock

from c_lord.cogs.session_manage import SessionManageCog


def _cog() -> SessionManageCog:
    bot = MagicMock()
    bot.get_cog = MagicMock(return_value=None)
    authorizer = MagicMock()
    authorizer.is_allowed = MagicMock(return_value=True)
    return SessionManageCog(bot=bot, repo=MagicMock(), authorizer=authorizer)


def _manager(session_name: str, windows: list[dict[str, str]]) -> MagicMock:
    mgr = MagicMock()
    mgr.session_name = session_name
    mgr.list_sessions = MagicMock(return_value=windows)
    return mgr


async def _list(cog: SessionManageCog) -> list[str]:
    sent: list[dict] = []

    async def respond(content=None, *, embed=None, ephemeral=False, **kw):
        sent.append({"content": content, "embed": embed})

    async def ack(*, ephemeral=False):
        return None

    await cog._tmux_list_impl(respond=respond, ack=ack)
    embed = sent[-1]["embed"]
    return [embed.title] + [f.name for f in embed.fields]


async def test_two_channels_on_one_repo_list_its_windows_once() -> None:
    cog = _cog()
    cog._get_all_bindings = AsyncMock(return_value=[{"channel_id": 1}, {"channel_id": 2}])
    windows = [{"window_name": "w1", "working_dir": "/s/11", "thread_id": "11"}]
    # The manager is cached per channel, so each channel has its own object
    # for the same tmux session.
    managers = {1: _manager("c-lord", windows), 2: _manager("c-lord", windows)}
    cog._resolve_tmux_manager = AsyncMock(side_effect=lambda cid, thread_id: managers[cid])

    assert await _list(cog) == ["🖥️ Tmux Windows (1)", "`w1`"]


async def test_channels_on_different_repos_are_both_listed() -> None:
    cog = _cog()
    cog._get_all_bindings = AsyncMock(return_value=[{"channel_id": 1}, {"channel_id": 2}])
    managers = {
        1: _manager("c-lord", [{"window_name": "w1", "working_dir": "/a", "thread_id": "11"}]),
        2: _manager("other", [{"window_name": "w2", "working_dir": "/b", "thread_id": "22"}]),
    }
    cog._resolve_tmux_manager = AsyncMock(side_effect=lambda cid, thread_id: managers[cid])

    assert await _list(cog) == ["🖥️ Tmux Windows (2)", "`w1`", "`w2`"]
