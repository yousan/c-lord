"""#918: /model session / channel / global scopes — resolution, protection, display.

Baseline of every case (the Issue's): global opus/high (here: the start-up
environment), channels A and B with no settings of their own.
"""

from __future__ import annotations

import asyncio
import sqlite3

import pytest

from c_lord.database.models import init_db
from c_lord.database.repository import SessionRepository
from c_lord.model_scopes import (
    ModelScopeError,
    ModelScopeService,
    StartupEnv,
    render_result,
    render_view,
)

A, B = 100, 200


@pytest.fixture
async def svc(tmp_path):
    db = str(tmp_path / "s.db")
    await init_db(db)
    return ModelScopeService(
        db, StartupEnv("opus", "high", model_source=".env", effort_source=".env")
    )


async def session(svc, thread, channel, *, turn=True):
    await SessionRepository(svc.db_path).save(thread, f"sid-{thread}", "/tmp/x")
    if turn:
        await svc.begin_turn(thread, channel)


async def saved(svc, thread):
    v = await svc.view(channel_id=None, thread_id=thread)
    return (v.session_saved.model, v.session_saved.effort)


async def new_for(svc, channel):
    r = await svc.resolve_new(channel)
    return (r.model, r.effort)


def snapshot(svc) -> list:
    con = sqlite3.connect(svc.db_path)
    try:
        return [
            con.execute(f"SELECT * FROM {t} ORDER BY 1").fetchall()
            for t in ("settings", "channel_model_settings", "session_model_settings")
        ]
    finally:
        con.close()


# ── 対象範囲 ─────────────────────────────────────────────────────────────────


async def test_session_scope_changes_only_that_session(svc):
    await session(svc, 1, A)
    await session(svc, 2, A)
    await session(svc, 3, B)
    await svc.change(scope="session", model="sonnet", thread_id=1, channel_id=A)
    assert await saved(svc, 1) == ("sonnet", "high")
    assert await saved(svc, 2) == ("opus", "high")
    assert await saved(svc, 3) == ("opus", "high")
    assert await new_for(svc, A) == ("opus", "high")
    assert await new_for(svc, B) == ("opus", "high")


async def test_session_scope_both_fields(svc):
    await session(svc, 1, A)
    await svc.change(scope="session", model="sonnet", effort="medium", thread_id=1, channel_id=A)
    assert await saved(svc, 1) == ("sonnet", "medium")


async def test_channel_scope_changes_only_new_sessions_of_that_channel(svc):
    await session(svc, 1, A)
    await svc.change(scope="channel", model="sonnet", channel_id=A)
    assert await new_for(svc, A) == ("sonnet", "high")
    assert await new_for(svc, B) == ("opus", "high")
    assert await saved(svc, 1) == ("opus", "high")  # existing untouched


async def test_scope_channel_from_thread_targets_the_parent_channel(svc):
    await session(svc, 1, A)
    await svc.change(scope="channel", model="sonnet", thread_id=1, channel_id=A)
    assert await new_for(svc, A) == ("sonnet", "high")
    assert await saved(svc, 1) == ("opus", "high")


async def test_global_scope_changes_new_sessions_not_existing(svc):
    await session(svc, 1, A)
    await svc.change(scope="global", model="haiku")
    assert await new_for(svc, A) == ("haiku", "high")
    assert await new_for(svc, B) == ("haiku", "high")
    assert await saved(svc, 1) == ("opus", "high")


async def test_channel_model_override_survives_global_change(svc):
    await svc.change(scope="channel", model="sonnet", channel_id=A)
    await svc.change(scope="global", model="haiku")
    assert (await new_for(svc, A))[0] == "sonnet"
    assert (await new_for(svc, B))[0] == "haiku"


async def test_other_instance_is_not_affected(svc, tmp_path):
    other_db = str(tmp_path / "other.db")
    await init_db(other_db)
    other = ModelScopeService(other_db, svc.env)
    await svc.change(scope="global", model="sonnet", effort="low")
    assert await new_for(other, A) == ("opus", "high")


# ── 既存反映・保護 ───────────────────────────────────────────────────────────


async def test_channel_include_existing_reflects_to_unset_sessions_only_in_that_channel(svc):
    await session(svc, 1, A)
    await session(svc, 2, B)
    r = await svc.change(scope="channel", model="sonnet", channel_id=A, include_existing=True)
    assert await saved(svc, 1) == ("sonnet", "high")
    assert await saved(svc, 2) == ("opus", "high")
    assert r.fields[0].applied == 1 and r.fields[0].kept_session == 0


async def test_running_turn_keeps_its_pair_and_next_turn_changes(svc):
    await session(svc, 1, A, turn=False)
    running = await svc.begin_turn(1, A)
    await svc.change(scope="channel", model="sonnet", channel_id=A, include_existing=True)
    assert (running.model, running.effort) == ("opus", "high")  # the turn in flight
    nxt = await svc.begin_turn(1, A)
    assert (nxt.model, nxt.effort) == ("sonnet", "high")
    assert nxt.needs_restart  # the process was launched with opus
    again = await svc.begin_turn(1, A)
    assert not again.needs_restart


async def test_idle_sessions_included_closed_excluded(svc):
    repo = SessionRepository(svc.db_path)
    await session(svc, 1, A)
    await session(svc, 2, A)
    await repo.set_closed(2, True)
    await svc.change(scope="channel", model="sonnet", channel_id=A, include_existing=True)
    assert await saved(svc, 1) == ("sonnet", "high")
    assert await saved(svc, 2) == ("opus", "high")


async def test_session_explicit_model_is_protected(svc):
    await session(svc, 1, A)
    await svc.change(scope="session", model="haiku", thread_id=1, channel_id=A)
    r = await svc.change(scope="channel", model="sonnet", channel_id=A, include_existing=True)
    assert (await saved(svc, 1))[0] == "haiku"
    assert r.fields[0].kept_session == 1 and r.fields[0].applied == 0


async def test_explicit_equal_to_current_value_is_still_protected(svc):
    await session(svc, 1, A)
    await svc.change(scope="session", model="opus", thread_id=1, channel_id=A)
    await svc.change(scope="channel", model="sonnet", channel_id=A, include_existing=True)
    assert (await saved(svc, 1))[0] == "opus"


async def test_global_include_existing_reaches_sessions_without_channel_model(svc):
    await session(svc, 1, A)
    await session(svc, 2, B)
    await svc.change(scope="global", model="haiku", include_existing=True)
    assert (await saved(svc, 1))[0] == "haiku"
    assert (await saved(svc, 2))[0] == "haiku"


async def test_global_include_existing_protects_channel_model(svc):
    await session(svc, 1, A)
    await session(svc, 2, B)
    await svc.change(scope="channel", model="sonnet", channel_id=A)
    # A's session was created before A's override, so it still holds opus.
    r = await svc.change(scope="global", model="haiku", include_existing=True)
    assert (await saved(svc, 1))[0] == "opus"
    assert (await saved(svc, 2))[0] == "haiku"
    assert r.fields[0].kept_channel == 1 and r.fields[0].applied == 1


async def test_model_only_explicit_session_still_receives_effort(svc):
    await session(svc, 1, A)
    await svc.change(scope="session", model="sonnet", thread_id=1, channel_id=A)
    await svc.change(scope="global", effort="low", include_existing=True)
    assert await saved(svc, 1) == ("sonnet", "low")


async def test_effort_only_channel_blocks_effort_but_not_model_from_global(svc):
    await session(svc, 1, A)
    await svc.change(scope="channel", effort="max", channel_id=A)
    r = await svc.change(scope="global", model="sonnet", effort="medium", include_existing=True)
    # model comes through; effort is protected by the channel's explicit value
    # (the session itself keeps the effort it was saved with).
    assert await saved(svc, 1) == ("sonnet", "high")
    by_name = {f.name: f for f in r.fields}
    assert by_name["model"].applied == 1
    assert by_name["effort"].kept_channel == 1 and by_name["effort"].applied == 0


async def test_repeated_upper_changes_without_include_existing_leave_saved_values(svc):
    await session(svc, 1, A)
    for m in ("sonnet", "haiku", "opus"):
        await svc.change(scope="channel", model=m, channel_id=A)
        await svc.change(scope="global", model=m)
    assert await saved(svc, 1) == ("opus", "high")


# ── 独立設定・解除 ───────────────────────────────────────────────────────────


async def test_model_and_effort_are_independent(svc):
    await session(svc, 1, A)
    await svc.change(scope="session", model="sonnet", thread_id=1, channel_id=A)
    await svc.change(scope="session", effort="low", thread_id=1, channel_id=A)
    assert await saved(svc, 1) == ("sonnet", "low")


async def test_session_model_inherit_adopts_current_upper_layers_and_keeps_effort(svc):
    await session(svc, 1, A)
    await svc.change(scope="session", model="haiku", thread_id=1, channel_id=A)
    await svc.change(scope="session", effort="low", thread_id=1, channel_id=A)
    await svc.change(scope="channel", model="sonnet", channel_id=A)
    await svc.change(scope="session", model="inherit", thread_id=1, channel_id=A)
    assert await saved(svc, 1) == ("sonnet", "low")
    v = await svc.view(channel_id=A, thread_id=1)
    assert v.session_explicit == (None, "low")
    assert v.session_saved.model_src == "channel"


async def test_session_effort_inherit_only_touches_effort(svc):
    await session(svc, 1, A)
    await svc.change(scope="session", model="sonnet", effort="low", thread_id=1, channel_id=A)
    await svc.change(scope="session", effort="inherit", thread_id=1, channel_id=A)
    assert await saved(svc, 1) == ("sonnet", "high")


async def test_channel_inherit_keeps_existing_by_default_and_reflects_when_asked(svc):
    await session(svc, 1, A)
    await svc.change(scope="channel", model="sonnet", channel_id=A, include_existing=True)
    assert (await saved(svc, 1))[0] == "sonnet"
    await svc.change(scope="channel", model="inherit", channel_id=A)
    assert (await saved(svc, 1))[0] == "sonnet"  # kept
    assert (await new_for(svc, A))[0] == "opus"
    await svc.change(scope="channel", model="inherit", channel_id=A, include_existing=True)
    assert (await saved(svc, 1))[0] == "opus"


async def test_global_inherit_falls_back_to_environment(svc):
    await svc.change(scope="global", model="haiku")
    await svc.change(scope="global", model="inherit")
    assert (await new_for(svc, A))[0] == "opus"


# ── 表示 ─────────────────────────────────────────────────────────────────────


async def test_view_does_not_write(svc):
    await session(svc, 1, A)
    before = snapshot(svc)
    await svc.view(channel_id=A, thread_id=1)
    await svc.view(channel_id=A)
    assert snapshot(svc) == before


async def test_view_shows_each_layer_and_origin(svc):
    await svc.change(scope="channel", model="sonnet", channel_id=A)
    await svc.change(scope="global", effort="medium")
    v = await svc.view(channel_id=A)
    text = render_view(v, svc)
    assert "チャンネル" in text and "グローバル" in text and ".env" in text
    assert "新規に適用 → sonnet / medium" in text
    assert "設定元 → チャンネル / グローバル" in text
    assert "—" in text  # unset cells


async def test_saved_value_differing_from_current_layers_is_shown_as_saved(svc):
    await session(svc, 1, A)
    await svc.change(scope="global", model="haiku")  # no include-existing
    v = await svc.view(channel_id=A, thread_id=1)
    text = render_view(v, svc)
    assert "このセッション → opus / high" in text
    assert "haiku / high" not in text.split("このセッション")[1].split("\n")[0]


async def test_running_vs_next_only_shown_when_they_differ(svc):
    await session(svc, 1, A)
    same = render_view(await svc.view(channel_id=A, thread_id=1), svc)
    assert "実行中" not in same
    await svc.change(scope="session", model="sonnet", thread_id=1, channel_id=A)
    diff = render_view(await svc.view(channel_id=A, thread_id=1), svc)
    assert "実行中 → opus / high" in diff and "次のターンから sonnet / high" in diff


async def test_env_row_is_the_startup_snapshot_and_names_its_source(svc, tmp_path):
    env_file = tmp_path / ".env"
    env_file.write_text("CLAUDE_MODEL=haiku\nDISCORD_BOT_TOKEN=SECRET-TOKEN\n")
    s2 = ModelScopeService(
        svc.db_path, StartupEnv("sonnet", None, model_source="組み込み既定", effort_source="未設定")
    )
    text = render_view(await s2.view(channel_id=A), s2)
    assert "haiku" not in text  # the unapplied edit is not shown
    assert "SECRET" not in text
    assert "model=組み込み既定" in text


def test_result_message_distinguishes_new_only_applied_and_kept():
    from c_lord.model_scopes import ChangeResult, FieldOutcome

    new_only = ChangeResult("channel", [FieldOutcome("model", "set", "sonnet")], False)
    assert "新規セッションのみ" in render_result(new_only)
    inc = ChangeResult(
        "global",
        [FieldOutcome("model", "set", "sonnet", applied=3, kept_session=2, kept_channel=1)],
        True,
    )
    out = render_result(inc)
    assert "既存 3 件へ反映" in out and "維持 2 件（セッション）" in out
    assert "維持 1 件（チャンネル）" in out


# ── 異常系 ───────────────────────────────────────────────────────────────────


@pytest.mark.parametrize(
    "kwargs",
    [
        dict(scope="channel", model="no pe", channel_id=A),  # malformed model
        dict(scope="channel", model="--evil", channel_id=A),
        dict(scope="channel", effort="ultra", channel_id=A),  # bad effort
        dict(scope="channel", model="haiku", effort="high", channel_id=A),  # unsupported combo
        dict(scope="planet", model="sonnet", channel_id=A),  # bad scope
        dict(scope="session", model="sonnet", channel_id=A),  # session outside a thread
        dict(scope="session", model="sonnet", thread_id=1, channel_id=A, include_existing=True),
        dict(scope="channel", channel_id=A, include_existing=True),  # nothing to include
        dict(scope="global", model="sonnet", effort="nope"),  # partial must not apply
    ],
)
async def test_invalid_requests_change_nothing(svc, kwargs):
    await session(svc, 1, A)
    before = snapshot(svc)
    with pytest.raises(ModelScopeError):
        await svc.change(**kwargs)
    assert snapshot(svc) == before


async def test_haiku_with_existing_channel_effort_is_rejected_whole(svc):
    await svc.change(scope="channel", effort="high", channel_id=A)
    before = snapshot(svc)
    with pytest.raises(ModelScopeError):
        await svc.change(scope="channel", model="haiku", channel_id=A)
    assert snapshot(svc) == before


# ── 永続化・競合 ─────────────────────────────────────────────────────────────


async def test_everything_survives_a_restart(svc):
    await session(svc, 1, A)
    await svc.change(scope="session", model="haiku", thread_id=1, channel_id=A)
    await svc.change(scope="channel", effort="low", channel_id=A)
    await svc.change(scope="global", model="sonnet")
    reborn = ModelScopeService(svc.db_path, svc.env)
    v = await reborn.view(channel_id=A, thread_id=1)
    assert v.session_explicit == ("haiku", None)
    assert v.channel == (None, "low") and v.global_ == ("sonnet", None)
    assert v.session_saved is not None
    assert (v.session_saved.model, v.applied) == ("haiku", ("opus", "high"))  # pending kept
    nxt = await reborn.begin_turn(1, A)
    assert nxt.needs_restart


async def test_a_turn_never_sees_a_half_applied_pair(svc):
    await session(svc, 1, A)
    seen: set[tuple] = set()

    async def turns():
        for _ in range(25):
            t = await svc.begin_turn(1, A)
            seen.add((t.model, t.effort))
            await asyncio.sleep(0)

    async def flips():
        for i in range(25):
            if i % 2:
                await svc.change(
                    scope="session", model="opus", effort="high", thread_id=1, channel_id=A
                )
            else:
                await svc.change(
                    scope="session", model="sonnet", effort="low", thread_id=1, channel_id=A
                )
            await asyncio.sleep(0)

    await asyncio.gather(turns(), flips())
    assert seen <= {("opus", "high"), ("sonnet", "low")}


async def test_sessions_from_before_the_feature_get_a_row_when_reflected(svc):
    # A live session with no settings row (created before #918).
    await session(svc, 7, A, turn=False)
    await svc.change(
        scope="channel",
        model="sonnet",
        channel_id=A,
        include_existing=True,
        channel_of=lambda tid: A,
    )
    assert (await saved(svc, 7))[0] == "sonnet"
