"""#911: a menu Claude Code draws itself must not stay at ⏳ 確認中 forever.

Production, 2026-10-13 (W14 AirTag). The CLI's own safeguard menu — "Model
switch": ``1. Stay on Opus 5.5 / 2. Switch to Sonnet 5.5`` — is drawn with the
same "Chat about this" layout as an AskUserQuestion, so the bridge put it on
Discord and answered it. Then it waited for the transcript to record "the next
AskUserQuestion written". The CLI writes no AskUserQuestion for its own menus,
so nothing ever came: ``⏳ 確認中 … 確認でき次第この表示が更新されます`` stayed
on the thread for days, over an answer the CLI had taken at once.

When the transcript has nothing to say, the pane does: the menu closed and
stayed closed, and no ask was written for it. That is the evidence these menus
have (the same evidence plan approval and transcript-less runners already use).
"""

from __future__ import annotations

import asyncio
import json
from pathlib import Path
from unittest.mock import AsyncMock, MagicMock

import pytest

from c_lord.claude.types import AskOption, AskQuestion
from c_lord.discord_ui import ask_handler
from c_lord.discord_ui.ask_bus import ask_bus
from c_lord.discord_ui.ask_handler import bridge_pane_ask

_OLD_ID = "toolu_01OLDxxxxxxxxxxxxxxxxxxxx"
_ANSWERED_RESULT = (
    'Your questions have been answered: "old?"="x". '
    "You can now continue with the user's answers in mind."
)


def _model_switch() -> AskQuestion:
    return AskQuestion(
        question="Opus 5.5's safeguards stopped the response above",
        header="Model switch",
        options=[AskOption("Stay on Opus 5.5"), AskOption("Switch to Sonnet 5.5")],
    )


def _write_finished_ask(project_dir: Path) -> None:
    """An earlier AskUserQuestion, long resolved — the newest ask in the file."""
    project_dir.mkdir(parents=True, exist_ok=True)
    events = [
        {
            "timestamp": "2026-10-13T00:01:00.000Z",
            "message": {
                "content": [
                    {"type": "tool_use", "id": _OLD_ID, "name": "AskUserQuestion", "input": {}}
                ]
            },
        },
        {
            "timestamp": "2026-10-13T00:02:00.000Z",
            "message": {
                "content": [
                    {"type": "tool_result", "tool_use_id": _OLD_ID, "content": _ANSWERED_RESULT}
                ]
            },
        },
    ]
    with (project_dir / "s.jsonl").open("w", encoding="utf-8") as fh:
        for e in events:
            fh.write(json.dumps(e) + "\n")


def _runner(project_dir: Path, *, closes_to: bool | None = True) -> MagicMock:
    """A pane whose menu is answered by the click. After it, the pane reports
    *closes_to* through ``peek_menu_closed`` (True: nothing open; False: the
    next question of the same ask is on screen)."""
    runner = MagicMock()
    state = {"answered": False}

    async def _peek_state():
        if state["answered"] and closes_to is True:
            return (None, True)
        return (_model_switch(), True)

    async def _closed():
        return closes_to if state["answered"] else False

    async def _answer(*_args, **_kwargs):
        state["answered"] = True
        return True

    async def _peek():
        return (await _peek_state())[0]

    runner.peek_pending_ask = _peek
    runner.peek_menu_state = _peek_state
    runner.peek_menu_closed = _closed
    runner.answer_menu = _answer
    runner.answer_menu_multi = _answer
    runner.answer_menu_text = _answer
    runner.cancel_menu = AsyncMock()
    runner.transcript_project_dir = AsyncMock(return_value=project_dir)
    return runner


def _thread(thread_id: int) -> tuple[MagicMock, MagicMock]:
    thread = MagicMock()
    thread.id = thread_id
    msg = MagicMock()
    msg.id = thread_id + 1
    msg.edit = AsyncMock()
    thread.send = AsyncMock(return_value=msg)
    return thread, msg


def _title(msg: MagicMock) -> str:
    assert msg.edit.await_args is not None, "the menu message was never edited"
    embed = msg.edit.await_args.kwargs.get("embed")
    return str(embed.title or "") if embed is not None else ""


def _fast(monkeypatch, *, window: float = 1.0) -> None:
    monkeypatch.setattr(ask_handler, "_PANE_RESOLVE_POLL", 0.01)
    monkeypatch.setattr(ask_handler, "_PANE_RESOLVE_MISSES", 2)
    monkeypatch.setattr(ask_handler, "_ANSWER_CONFIRM_TIMEOUT", window)
    monkeypatch.setattr(ask_handler, "_ANSWER_CONFIRM_POLL", 0.01)
    monkeypatch.setattr(ask_handler, "_LATE_CONFIRM_TIMEOUT", 5.0)
    monkeypatch.setattr(ask_handler, "_LATE_CONFIRM_POLL_MIN", 0.01)
    monkeypatch.setattr(ask_handler, "_LATE_CONFIRM_POLL_MAX", 0.02)
    # raising=False: before #911 the knob does not exist, and the test must then
    # fail on the BEHAVIOUR (the menu stays ⏳), not on a missing name.
    monkeypatch.setattr(ask_handler, "_PANE_CLOSED_GRACE", 0.05, raising=False)


async def _answer(thread: MagicMock, question: AskQuestion, runner: MagicMock) -> None:
    async def _click_soon() -> None:
        await asyncio.sleep(0.05)
        ask_bus.post_answer(thread.id, [question.options[0].label])

    await asyncio.gather(
        asyncio.wait_for(bridge_pane_ask(thread, question, runner), timeout=5.0),
        _click_soon(),
    )


@pytest.fixture(autouse=True)
async def _drain_late_watchers():
    yield
    tasks = list(getattr(ask_handler, "_late_confirmations", ()))
    for task in tasks:
        task.cancel()
    await asyncio.gather(*tasks, return_exceptions=True)


@pytest.mark.asyncio
async def test_a_cli_menu_that_closes_turns_answered(monkeypatch, tmp_path):
    """AC1 — the production shape. RED before #911: ⏳ 確認中, watching for an
    AskUserQuestion the CLI never writes."""
    _fast(monkeypatch)
    _write_finished_ask(tmp_path)
    thread, msg = _thread(911_0001)

    await _answer(thread, _model_switch(), _runner(tmp_path))

    assert _title(msg).startswith("✅"), _title(msg)
    assert not ask_handler._late_confirmations, "nothing is left to watch"
    final = msg.edit.await_args.kwargs["embed"].description
    assert "Stay on Opus 5.5" in final


@pytest.mark.asyncio
async def test_a_cli_menu_in_an_empty_transcript_turns_answered(monkeypatch, tmp_path):
    """No ask in the file at all — a fresh session whose first menu is the CLI's."""
    _fast(monkeypatch)
    tmp_path.mkdir(exist_ok=True)
    thread, msg = _thread(911_0002)

    await _answer(thread, _model_switch(), _runner(tmp_path))

    assert _title(msg).startswith("✅"), _title(msg)


@pytest.mark.asyncio
async def test_a_menu_still_on_screen_is_not_taken_for_closed(monkeypatch, tmp_path):
    """AC2 — the next question of a real multi-question ask is on screen: its
    result is written only after the last answer, so keep waiting on the
    transcript (#746), never ✅ on the pane's say-so."""
    _fast(monkeypatch, window=0.2)
    _write_finished_ask(tmp_path)
    thread, msg = _thread(911_0003)

    await _answer(thread, _model_switch(), _runner(tmp_path, closes_to=False))

    assert _title(msg).startswith("⏳"), _title(msg)


@pytest.mark.asyncio
async def test_an_unreadable_pane_is_not_taken_for_closed(monkeypatch, tmp_path):
    """A pane that cannot be read (dead claude, empty capture) proves nothing."""
    _fast(monkeypatch, window=0.2)
    _write_finished_ask(tmp_path)
    thread, msg = _thread(911_0004)

    await _answer(thread, _model_switch(), _runner(tmp_path, closes_to=None))

    assert _title(msg).startswith("⏳"), _title(msg)


@pytest.mark.asyncio
async def test_a_real_ask_written_when_its_menu_closes_is_judged_by_the_transcript(
    monkeypatch, tmp_path
):
    """The CLI may write an ask together with its result as the menu closes —
    the transcript, not the pane, gives that one its verdict."""
    _fast(monkeypatch)
    monkeypatch.setattr(ask_handler, "_PANE_CLOSED_GRACE", 0.5)
    _write_finished_ask(tmp_path)
    thread, msg = _thread(911_0005)
    runner = _runner(tmp_path)
    inner = runner.answer_menu

    new_id = "toolu_01NEWxxxxxxxxxxxxxxxxxxxx"
    rejected = (
        "The user doesn't want to proceed with this tool use. The tool use was rejected.\n"
        "The user wants to clarify these questions.\n"
        '- "x"\n  (No answer provided)'
    )

    async def _answer_and_flush(*args, **kwargs):
        await inner(*args, **kwargs)

        async def _flush_later() -> None:
            await asyncio.sleep(0.1)
            with (tmp_path / "s.jsonl").open("a", encoding="utf-8") as fh:
                for e in (
                    {
                        "timestamp": "2026-10-13T00:16:47.000Z",
                        "message": {
                            "content": [
                                {
                                    "type": "tool_use",
                                    "id": new_id,
                                    "name": "AskUserQuestion",
                                    "input": {},
                                }
                            ]
                        },
                    },
                    {
                        "timestamp": "2026-10-13T00:16:47.100Z",
                        "message": {
                            "content": [
                                {"type": "tool_result", "tool_use_id": new_id, "content": rejected}
                            ]
                        },
                    },
                ):
                    fh.write(json.dumps(e) + "\n")

        asyncio.ensure_future(_flush_later())
        return True

    runner.answer_menu = _answer_and_flush

    await _answer(thread, _model_switch(), runner)

    assert not _title(msg).startswith("✅"), _title(msg)
    assert "伝わっていません" in msg.edit.await_args.kwargs["embed"].description


@pytest.mark.asyncio
async def test_a_cli_menu_left_confirming_by_a_restart_is_corrected(monkeypatch, tmp_path):
    """AC3 — a ⏳ left by the previous process: the recovered watch asks the pane
    too, so a CLI menu that has long closed turns ✅ on startup."""
    import datetime as dt

    import discord

    from c_lord.ask_confirm_recovery import resume_confirming_menus
    from c_lord.database.repository import SessionRecord
    from c_lord.discord_ui.embeds import ask_confirming_embed
    from c_lord.transcript.resolver import derive_project_dir

    _fast(monkeypatch)
    utc = dt.timezone.utc  # noqa: UP017 — dt.UTC is 3.11+
    menu_at = dt.datetime(2026, 10, 13, 0, 14, 9, tzinfo=utc)
    # Within the (shortened) watch lifetime, so the menu is still looked at.
    started = menu_at + dt.timedelta(seconds=2)
    workdir = "/work/thread-911"
    project_dir = derive_project_dir(workdir, projects_root=tmp_path)
    _write_finished_ask(project_dir)

    q = _model_switch()
    thread = MagicMock(spec=discord.Thread)
    thread.id = 911
    menu = MagicMock(spec=discord.Message)
    menu.id = discord.utils.time_snowflake(menu_at)
    menu.created_at = menu_at
    menu.author = MagicMock()
    menu.author.id = 4242
    menu.embeds = [ask_confirming_embed(q.question, q.header, ["Stay on Opus 5.5"])]
    menu.edit = AsyncMock()
    thread.last_message_id = menu.id

    def _history(**_kwargs: object) -> object:
        async def _gen():  # type: ignore[no-untyped-def]
            yield menu

        return _gen()

    thread.history = MagicMock(side_effect=_history)
    guild = MagicMock()
    guild.active_threads = AsyncMock(return_value=[thread])
    bot = MagicMock()
    bot.user = MagicMock()
    bot.user.id = 4242
    bot.guilds = [guild]
    record = SessionRecord(
        thread_id=911,
        session_id="s",
        working_dir=workdir,
        model=None,
        origin="discord",
        summary=None,
        created_at="2026-10-13 00:00:00",
        last_used_at="2026-10-13 00:00:00",
    )
    repo = MagicMock()
    repo.get = AsyncMock(return_value=record)

    pane = MagicMock()
    pane.peek_menu_closed = AsyncMock(return_value=True)

    async def _factory(_bot, _thread_id):
        return pane

    assert (
        await resume_confirming_menus(
            bot, repo, projects_root=tmp_path, started_at=started, runner_factory=_factory
        )
        == 1
    )
    pending = list(ask_handler._late_confirmations)
    await asyncio.wait_for(asyncio.gather(*pending), timeout=5)

    menu.edit.assert_awaited()
    assert menu.edit.await_args.kwargs["embed"].title.startswith("✅")
