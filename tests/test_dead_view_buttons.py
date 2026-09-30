"""#796: the third kind of #752 residue — buttons of non-persistent views.

``TextAnsweredMenuView`` (「⚡ これは新しい指示でした」) and ``ReopenSessionView``
(「▶️ 再開する」) are ``timeout=None`` but *not* persistent: discord.py gives
their buttons a random ``custom_id`` (``os.urandom(16).hex()``) and nothing
re-registers them, so once the process that drew them exits no handler exists
anywhere. Pressing one after a restart answers 「この操作は失敗しました」.
Production 2026-09-23: 3 such ⚡ buttons still live, all in archived threads.
"""

from __future__ import annotations

import datetime as dt
import os

import pytest

from c_lord.database.models import init_db
from tests.test_dead_menu_buttons import (
    HUMAN_ID,
    THREAD_ID,
    _ask_menu,
    _bot,
    _Button,
    _Msg,
    _retired,
    _Row,
    _session_repo,
    _snowflake,
    _Thread,
)

TEXT_ANSWERED = (
    "✏️ **この文章を、開いていた質問への回答として送りました:** Bでお願いします\n"
    "-# 質問への回答ではなく新しい指示のつもりだった場合は、下のボタンを押してください。"
)


def _auto_id() -> str:
    """What discord.py assigns a decorator button with no explicit custom_id."""
    return os.urandom(16).hex()


def _text_answered(msg_id: int, **kw) -> _Msg:
    return _Msg(msg_id, content=TEXT_ANSWERED, components=[_Row(_Button(_auto_id()))], **kw)


@pytest.mark.asyncio
async def test_a_previous_process_text_answered_button_is_stripped() -> None:
    """AC2 — RED before #796: the sweep only knew ⏹ Stop and ``ask_…`` menus."""
    from c_lord.stale_stop_buttons import sweep_dead_buttons

    dead = _text_answered(_snowflake(60 * 24 * 3))
    thread = _Thread(THREAD_ID, [dead])

    removed = await sweep_dead_buttons(_bot(thread), _session_repo(THREAD_ID))

    assert _retired(dead), "a button no process can answer must stop looking pressable"
    assert not dead.deleted, "the notice records where the sentence went — keep it"
    content = dead.edits[-1].get("content") or ""
    assert content.startswith(TEXT_ANSWERED), "keep what the notice said"
    assert "無効" in content and "再起動" in content, "say in one line why the button is gone"
    assert "embed" not in dead.edits[-1]
    assert removed == 1


@pytest.mark.asyncio
async def test_a_long_notice_still_fits_discords_2000_characters() -> None:
    from c_lord.stale_stop_buttons import sweep_dead_buttons

    dead = _Msg(_snowflake(60), content="あ" * 1995, components=[_Row(_Button(_auto_id()))])
    await sweep_dead_buttons(_bot(_Thread(THREAD_ID, [dead])), _session_repo(THREAD_ID))

    content = dead.edits[-1]["content"]
    assert len(content) <= 2000 and "無効" in content


@pytest.mark.asyncio
async def test_persistent_and_harmless_buttons_are_left_alone() -> None:
    """AC3 — only buttons nothing can route are residue."""
    from c_lord.stale_stop_buttons import sweep_dead_buttons

    rearmed_menu = _ask_menu(_snowflake(50))  # persistent ``ask_…``, re-armed by #671
    link_only = _Msg(_snowflake(40), components=[_Row(_Button(None, url="https://x.test"))])
    already_disabled = _Msg(_snowflake(30), components=[_Row(_Button(_auto_id(), disabled=True))])
    fixed_id = _Msg(_snowflake(25), components=[_Row(_Button("upgrade_approve"))])
    human = _text_answered(_snowflake(20), author_id=HUMAN_ID)
    fresh = _text_answered(_snowflake(-1))  # drawn by this process — it works
    thread = _Thread(THREAD_ID, [rearmed_menu, link_only, already_disabled, fixed_id, human, fresh])

    await sweep_dead_buttons(
        _bot(thread),
        _session_repo(THREAD_ID),
        keep_menu=lambda _t, mid: mid == rearmed_menu.id,
    )

    for msg in (rearmed_menu, link_only, already_disabled, fixed_id, human, fresh):
        assert not msg.edits and not msg.deleted, msg.id


@pytest.mark.asyncio
async def test_threads_swept_before_796_are_read_again_once(tmp_path, monkeypatch) -> None:
    """Production already ran the #752 sweep, so every thread's cursor sits past
    the ⚡ buttons it did not know about. Cursors written before #796 must not
    count, or those buttons are stepped over for good."""
    import aiosqlite

    import c_lord.stale_stop_buttons as sweep_mod
    from c_lord.database.sweep_cursor_repo import SweepCursorRepository
    from c_lord.stale_stop_buttons import sweep_dead_buttons

    db = str(tmp_path / "sessions.db")
    await init_db(db)
    dead = _text_answered(_snowflake(120))
    thread = _Thread(THREAD_ID, [dead, _Msg(_snowflake(90), content="later")])
    # What a #752-era sweep left behind: a cursor past the ⚡ button.
    async with aiosqlite.connect(db) as conn:
        await conn.execute(
            "INSERT INTO ui_sweep_cursors (thread_id, last_message_id) VALUES (?, ?)",
            (THREAD_ID, _snowflake(80)),
        )
        await conn.commit()
    thread.last_message_id = _snowflake(70)  # something new since, so it is visited
    monkeypatch.setattr(sweep_mod, "_PROCESS_STARTED_AT", dt.datetime.now(dt.timezone.utc))  # noqa: UP017

    cursors = SweepCursorRepository(db)
    await sweep_dead_buttons(_bot(thread), _session_repo(THREAD_ID), cursors=cursors)

    assert _retired(dead)
    # …and the cursor it writes now is honoured on the next start.
    assert (await cursors.get_all()).get(THREAD_ID) is not None


@pytest.mark.asyncio
async def test_a_stop_is_still_deleted_not_retired() -> None:
    """A ⏹ Stop also carries a random custom_id — it stays in its own lane."""
    from c_lord.discord_ui.views import STOP_MESSAGE_PREFIX
    from c_lord.stale_stop_buttons import sweep_dead_buttons

    stop = _Msg(
        _snowflake(60),
        content=f"{STOP_MESSAGE_PREFIX} (`w1`)",
        components=[_Row(_Button(_auto_id()))],
    )
    await sweep_dead_buttons(_bot(_Thread(THREAD_ID, [stop])), _session_repo(THREAD_ID))

    assert stop.deleted and not stop.edits


def test_the_real_views_get_random_custom_ids() -> None:
    """The sweep recognises these views by discord.py's generated custom_id.
    If one ever gets a fixed id, it must also get a handler after restart."""
    import asyncio

    from c_lord.discord_ui.views import ReopenSessionView, TextAnsweredMenuView
    from c_lord.stale_stop_buttons import is_unroutable_custom_id

    async def _noop(_i) -> None:
        return None

    async def _build() -> list[str]:
        ids = []
        for cls in (TextAnsweredMenuView, ReopenSessionView):
            ids += [c.custom_id for c in cls(_noop).children]  # type: ignore[attr-defined]
        return ids

    ids = asyncio.run(_build())
    assert ids and all(is_unroutable_custom_id(i) for i in ids)
    assert not is_unroutable_custom_id(f"ask_{THREAD_ID}_0_0")
