"""/clord-status inside a thread shows that thread only (#893).

Someone typing ``/clord-status`` in a thread almost always wants *this* thread's
attach target and state. The channel-wide table (and its 「+ N closed」 line)
made them hunt for their own row and filled the thread with a long list. In a
thread the reply is now the thread's own row, with a 「すべてを表示」 button that
shows the old channel-wide output to whoever pressed it, and to nobody else.

The button is not persistent, so after a restart nothing can answer it. It is
therefore offered only where the startup sweep (#796, ``stale_stop_buttons``)
will come and retire it — a live-looking button that does nothing is the
#634 / #752 / #796 failure this repository keeps removing.
"""

from __future__ import annotations

from collections.abc import Mapping
from types import SimpleNamespace
from typing import Any
from unittest.mock import AsyncMock, MagicMock

import discord
import pytest

import c_lord.thread_state_sync as tss

CHANNEL_ID = 999
MINE = 111
OTHER = 222


def _rec(thread_id: int, topic: str, **kw):
    fields = {
        "thread_id": thread_id,
        "state": "alive",
        "topic": topic,
        "summary": topic,
        "last_used_at": "2026-09-01 10:00:00",
        "working_dir": f"/home/y/c-lord-sessions/{CHANNEL_ID}/{thread_id}",
        "closed_reason": None,
        "closed_at": None,
    }
    fields.update(kw)
    return SimpleNamespace(**fields)


def _make_cog(monkeypatch, *, dirs=None, records=None, windows=None, swept=None):
    from c_lord.cogs import session_manage as sm
    from c_lord.cogs.session_manage import SessionManageCog

    if dirs is None:
        dirs = [
            SimpleNamespace(thread_id=MINE, path="/d/111", source_repo="git@github.com:y/cb.git"),
            SimpleNamespace(thread_id=OTHER, path="/d/222", source_repo="git@github.com:y/cb.git"),
        ]
    if records is None:
        records = {MINE: _rec(MINE, "自分の作業"), OTHER: _rec(OTHER, "よその作業")}
    if windows is None:
        windows = [
            {"thread_id": str(MINE), "session_name": "cb", "window_name": "w3"},
            {"thread_id": str(OTHER), "session_name": "cb", "window_name": "w4"},
        ]
    swept = swept or {}

    bot = MagicMock()
    repo = MagicMock()
    repo.get = AsyncMock(side_effect=lambda tid: records.get(tid))
    repo.get_swept = AsyncMock(side_effect=lambda tid: swept.get(tid))
    repo.list_all = AsyncMock(return_value=list(records.values()))
    cog = SessionManageCog(bot=bot, repo=repo)

    sdm = MagicMock()
    sdm.find_session_dirs = MagicMock(return_value=dirs)
    cog._resolve_session_dir_manager = AsyncMock(return_value=sdm)
    cog._resolve_tmux_manager = AsyncMock(return_value=MagicMock(session_name="cb"))
    monkeypatch.setattr(tss, "_list_all_windows", lambda: windows)
    monkeypatch.setattr(sm, "_dir_size_bytes", lambda p: 1_000_000)
    return cog


def _thread(thread_id: int = MINE):
    th = MagicMock(spec=discord.Thread)
    th.id = thread_id
    th.parent_id = CHANNEL_ID
    th.name = "W3 │ 自分の作業"
    th.parent = MagicMock()
    th.parent.name = "claude_base"
    return th


def _channel():
    ch = MagicMock(spec=discord.TextChannel)
    ch.id = CHANNEL_ID
    ch.parent_id = None
    ch.name = "claude_base"
    return ch


async def _run(cog, channel, *, show_all=False, thread_scope=True) -> tuple[str, Mapping[str, Any]]:
    respond, ack = AsyncMock(), AsyncMock()
    await cog._clord_status_impl(
        channel=channel, show_all=show_all, respond=respond, ack=ack, thread_scope=thread_scope
    )
    assert respond.await_count == 1
    call = respond.await_args
    assert call is not None
    content = call.args[0] if call.args else call.kwargs.get("content")
    assert isinstance(content, str)
    return content, call.kwargs


# ── AC1: a thread gets its own row ───────────────────────────────────────────


async def test_in_a_thread_only_that_threads_row_is_shown(monkeypatch):
    content, kwargs = await _run(_make_cog(monkeypatch), _thread())

    assert "自分の作業" in content
    assert "よその作業" not in content, "other threads' rows belong to 「すべてを表示」"
    assert "tmux attach -t cb:w3" in content, "one row → the attach command can be exact"
    assert "closed" not in content.split("```")[0], "no channel-wide counts in the header"
    assert not kwargs.get("ephemeral"), "the reply stays visible to everyone, as before"


async def test_the_thread_reply_carries_the_show_all_button(monkeypatch):
    from c_lord.discord_ui.views import StatusShowAllView

    _content, kwargs = await _run(_make_cog(monkeypatch), _thread())

    view = kwargs.get("view")
    assert isinstance(view, StatusShowAllView)
    labels = [getattr(c, "label", "") for c in view.children]
    assert any("すべてを表示" in (label or "") for label in labels)


async def test_a_closed_thread_says_so_and_offers_no_attach(monkeypatch):
    cog = _make_cog(monkeypatch, windows=[])
    content, _ = await _run(cog, _thread())

    assert "closed" in content
    assert "tmux attach -t cb:" not in content


# ── AC2: the button shows the channel-wide output, privately ─────────────────


def _button_interaction(channel):
    interaction = MagicMock(spec=discord.Interaction)
    interaction.channel = channel
    interaction.user = MagicMock(id=42)
    interaction.extras = {}
    interaction.response = MagicMock()
    interaction.response.defer = AsyncMock()
    interaction.response.send_message = AsyncMock()
    interaction.followup = MagicMock()
    interaction.followup.send = AsyncMock()
    interaction.delete_original_response = AsyncMock()
    return interaction


async def test_pressing_show_all_answers_only_the_presser(monkeypatch):
    cog = _make_cog(monkeypatch)
    channel_content, _ = await _run(cog, _channel(), thread_scope=True)

    interaction = _button_interaction(_thread())
    await cog._show_channel_status(interaction)

    interaction.response.defer.assert_awaited_once_with(ephemeral=True)
    sends = interaction.followup.send.await_args_list + (
        interaction.response.send_message.await_args_list
    )
    assert len(sends) == 1
    sent = sends[0]
    assert sent.kwargs.get("ephemeral") is True, "the list is for the presser only"
    text = sent.args[0] if sent.args else sent.kwargs.get("content")
    assert text == channel_content, "exactly what /clord-status in the channel shows"
    assert "自分の作業" in text and "よその作業" in text


# ── AC3: channel and `all` are unchanged ─────────────────────────────────────


async def test_channel_invocation_is_unchanged(monkeypatch):
    cog = _make_cog(monkeypatch)
    scoped, scoped_kw = await _run(cog, _channel(), thread_scope=True)
    legacy, _ = await _run(cog, _channel(), thread_scope=False)

    assert scoped == legacy
    assert "view" not in scoped_kw
    assert "自分の作業" in scoped and "よその作業" in scoped


async def test_all_in_a_thread_is_still_the_channel_wide_all_view(monkeypatch):
    cog = _make_cog(monkeypatch)
    in_thread, kw = await _run(cog, _thread(), show_all=True)
    in_channel, _ = await _run(cog, _channel(), show_all=True)

    assert in_thread == in_channel
    assert "view" not in kw


async def test_text_twin_is_not_thread_scoped(monkeypatch):
    """``!clord-status`` cannot answer privately, so it keeps the channel view."""
    cog = _make_cog(monkeypatch)
    content, kw = await _run(cog, _thread(), thread_scope=False)

    assert "よその作業" in content
    assert "view" not in kw


# ── AC4: only offered where the startup sweep will retire it ─────────────────


async def test_no_button_when_the_startup_sweep_is_disabled(monkeypatch):
    monkeypatch.setenv("CLORD_STOP_SWEEP_MAX", "0")
    content, kw = await _run(_make_cog(monkeypatch), _thread())

    assert "view" not in kw, "nothing would retire it after a restart"
    assert "自分の作業" in content


def test_the_status_button_is_one_the_sweep_recognises():
    import asyncio

    from c_lord.discord_ui.views import StatusShowAllView
    from c_lord.stale_stop_buttons import is_unroutable_custom_id

    async def _ids() -> list[str]:
        return [c.custom_id for c in StatusShowAllView(AsyncMock()).children]  # type: ignore[attr-defined]

    ids = asyncio.run(_ids())
    assert ids and all(is_unroutable_custom_id(i) for i in ids)


async def test_a_previous_process_status_button_is_retired_on_startup(monkeypatch):
    from c_lord.discord_ui.views import StatusShowAllView
    from c_lord.stale_stop_buttons import sweep_dead_buttons
    from tests.test_dead_menu_buttons import (
        THREAD_ID,
        _bot,
        _Button,
        _Msg,
        _retired,
        _Row,
        _session_repo,
        _snowflake,
        _Thread,
    )

    view = StatusShowAllView(AsyncMock())
    old = _Msg(
        _snowflake(60),
        content="c-lord status · このスレッド · #claude_base",
        components=[_Row(*(_Button(c.custom_id) for c in view.children))],  # type: ignore[attr-defined]
    )
    removed = await sweep_dead_buttons(_bot(_Thread(THREAD_ID, [old])), _session_repo(THREAD_ID))

    assert removed == 1 and _retired(old)
    assert old.edits[-1]["content"].startswith("c-lord status"), "the status line stays"


def test_sweep_reach_matches_what_the_sweep_lists():
    from c_lord.stale_stop_buttons import sweep_will_visit

    assert sweep_will_visit(_rec(1, "t"))
    assert sweep_will_visit(_rec(1, "t", closed_reason="manual", closed_at="2026-10-01"))
    assert not sweep_will_visit(None)
    assert not sweep_will_visit(_rec(1, "t", state="dead"))
    assert not sweep_will_visit(_rec(1, "t", closed_reason="swept"))


# ── AC5: authorization ───────────────────────────────────────────────────────


async def test_an_outsider_pressing_show_all_gets_nothing():
    from c_lord.discord_ui.authorization import Authorizer
    from c_lord.discord_ui.views import StatusShowAllView

    on_show = AsyncMock()
    view = StatusShowAllView(on_show, authorizer=Authorizer(allowed_user_ids={42}))
    outsider = _button_interaction(_thread())
    outsider.user = MagicMock(spec=discord.Member, id=99, roles=[])

    assert await view.interaction_check(outsider) is False
    on_show.assert_not_awaited()


# ── AC6: threads with nothing to show still get words, not a traceback ───────


async def test_a_thread_with_no_record(monkeypatch):
    cog = _make_cog(monkeypatch)
    content, kw = await _run(cog, _thread(333))

    assert "記録がありません" in content
    assert "view" not in kw, "the sweep never visits a thread with no record"


async def test_a_swept_thread(monkeypatch):
    swept = {333: _rec(333, "昔の作業", closed_reason="swept", closed_at="2026-08-01 09:00:00")}
    cog = _make_cog(monkeypatch, swept=swept)
    content, kw = await _run(cog, _thread(333))

    assert "片付け済み" in content and "2026-08-01" in content
    assert "view" not in kw


async def test_a_recorded_thread_whose_dir_is_gone(monkeypatch):
    records = {MINE: _rec(MINE, "自分の作業"), OTHER: _rec(OTHER, "よその作業")}
    dirs = [SimpleNamespace(thread_id=OTHER, path="/d/222", source_repo="git@github.com:y/cb.git")]
    cog = _make_cog(monkeypatch, dirs=dirs, records=records, windows=[])
    content, _ = await _run(cog, _thread())

    assert "作業ディレクトリ" in content and "deleted" in content
    assert "よその作業" not in content


@pytest.fixture(autouse=True)
def _no_sweep_cap(monkeypatch):
    monkeypatch.delenv("CLORD_STOP_SWEEP_MAX", raising=False)
