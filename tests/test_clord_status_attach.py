"""/clord-status must name the tmux window a thread is *actually* in (#616).

The old wiring resolved one session name for the whole channel
(``_resolve_tmux_manager(parent_id, thread_id=None)``) and pasted it into a
``<session>:work<#>`` template. After #615 a channel's threads live in several
sessions — one per bound repository — so that template pointed at windows that
do not exist. These tests drive ``SessionManageCog._clord_status_impl`` with a
tmux layout spread over three sessions.
"""

from __future__ import annotations

from types import SimpleNamespace
from unittest.mock import AsyncMock, MagicMock

import discord
import pytest

import c_lord.thread_state_sync as tss


def _make_cog(dirs, records, windows, monkeypatch):
    from c_lord.cogs import session_manage as sm
    from c_lord.cogs.session_manage import SessionManageCog

    bot = MagicMock()
    repo = MagicMock()
    repo.get = AsyncMock(side_effect=lambda tid: records.get(tid))
    repo.list_all = AsyncMock(return_value=list(records.values()))
    cog = SessionManageCog(bot=bot, repo=repo)

    sdm = MagicMock()
    sdm.find_session_dirs = MagicMock(return_value=dirs)
    cog._resolve_session_dir_manager = AsyncMock(return_value=sdm)
    # A bound channel still resolves *a* manager — it just no longer decides
    # which session each row is in.
    cog._resolve_tmux_manager = AsyncMock(return_value=MagicMock(session_name="claude_base"))

    monkeypatch.setattr(tss, "_list_all_windows", lambda: windows)
    monkeypatch.setattr(sm, "_dir_size_bytes", lambda p: 1_000_000)
    return cog


def _rec(thread_id: int, topic: str):
    return SimpleNamespace(
        thread_id=thread_id,
        state="waiting",
        topic=topic,
        summary=topic,
        last_used_at="2026-09-01 10:00:00",
        working_dir=f"/home/y/c-lord-sessions/999/{thread_id}",
    )


def _channel(channel_id: int = 999):
    ch = MagicMock(spec=discord.TextChannel)
    ch.id = channel_id
    ch.parent_id = None
    ch.name = "claude_base"
    return ch


@pytest.mark.asyncio
async def test_rows_name_the_session_each_thread_is_really_in(monkeypatch):
    dirs = [
        SimpleNamespace(thread_id=111, path="/d/111", source_repo="git@github.com:y/qa.git"),
        SimpleNamespace(thread_id=222, path="/d/222", source_repo="git@github.com:y/cb.git"),
    ]
    records = {111: _rec(111, "Qiita記事"), 222: _rec(222, "認証まわり")}
    windows = [
        # thread 111 was bound to another repo, so its window moved sessions.
        {"thread_id": "111", "session_name": "qiita-article", "window_name": "w1"},
        {"thread_id": "222", "session_name": "claude_base", "window_name": "w9"},
        {"thread_id": "333", "session_name": "other-bot", "window_name": "w1"},
    ]
    cog = _make_cog(dirs, records, windows, monkeypatch)

    respond, ack = AsyncMock(), AsyncMock()
    await cog._clord_status_impl(
        channel=_channel(), show_all=False, respond=respond, ack=ack
    )

    out = " ".join(str(a) for c in respond.await_args_list for a in c.args)
    assert "qiita-article:w1" in out, "the row must name the session the thread is really in"
    assert "claude_base:w9" in out
    assert "work<#>" not in out, "no derived template may survive"


@pytest.mark.asyncio
async def test_legacy_work_window_is_reported_verbatim(monkeypatch):
    dirs = [SimpleNamespace(thread_id=111, path="/d/111", source_repo="git@github.com:y/cb.git")]
    records = {111: _rec(111, "古い窓")}
    windows = [{"thread_id": "111", "session_name": "claude_base", "window_name": "work5"}]
    cog = _make_cog(dirs, records, windows, monkeypatch)

    respond, ack = AsyncMock(), AsyncMock()
    await cog._clord_status_impl(
        channel=_channel(), show_all=False, respond=respond, ack=ack
    )

    out = " ".join(str(a) for c in respond.await_args_list for a in c.args)
    assert "claude_base:work5" in out


@pytest.mark.asyncio
async def test_thread_without_a_window_offers_no_attach_target(monkeypatch):
    dirs = [SimpleNamespace(thread_id=111, path="/d/111", source_repo="git@github.com:y/cb.git")]
    records = {111: _rec(111, "停止済み")}
    cog = _make_cog(dirs, records, [], monkeypatch)

    respond, ack = AsyncMock(), AsyncMock()
    await cog._clord_status_impl(
        channel=_channel(), show_all=True, respond=respond, ack=ack
    )

    out = " ".join(str(a) for c in respond.await_args_list for a in c.args)
    assert "closed" in out
    assert ":w" not in out.split("```")[-1], "a closed row must not fabricate an attach target"
