"""#746: an answer confirmed AFTER the 12s window must still end up ✅.

Production, 2026-09-14. One AskUserQuestion carried three questions. The CLI
writes the ``tool_result`` only once the LAST question is answered, so the first
two answers could not possibly be confirmed within c-lord's 12-second window —
the transcript showed the result 198 seconds after the ``tool_use`` (and 53
minutes in another thread). Both menus were finalised as
**「回答は送りましたが、Claude が受け取ったかどうかを確認できませんでした。
続きが返ってこないときは、同じ内容をスレッドにもう一度送ってください。」** — over
answers Claude had received and acted on. Doing what that text says interrupts
the running turn (#631's shape).

The window itself is not the bug: the bridge holds the thread's menu claim while
it waits, so the next question of the same ask cannot be bridged until it
returns. What was missing is the second half — noticing the result when it does
arrive, and correcting the menu. These tests pin that:

* the window still ends on time (the thread is released for the next question);
* the menu then reads "確認中", never "送り直してください";
* a result that lands later turns the menu ✅ (or ⚠️ for "no answer");
* ``ask answer outcome=unknown`` is logged only when the answer really could not
  be confirmed.
"""

from __future__ import annotations

import asyncio
import json
import logging
from pathlib import Path
from unittest.mock import AsyncMock, MagicMock

import pytest

from c_lord.claude.types import AskOption, AskQuestion
from c_lord.discord_ui import ask_handler
from c_lord.discord_ui.ask_bus import ask_bus
from c_lord.discord_ui.ask_handler import bridge_pane_ask
from c_lord.discord_ui.embeds import ask_unconfirmed_embed

_TOOL_USE_ID = "toolu_01W3124Zj3oHyGmcu9Q8NP4Y"

# Measured 2026-09-14 (CLI 2.1.270) — the multi-question success wording.
_ANSWERED_RESULT = (
    'Your questions have been answered: "#686 罫線"="長すぎたら畳む", '
    '"#525 通知"="blocked に戻す". You can now continue with the user\'s answers in mind.'
)
_REJECTED_RESULT = (
    "The user doesn't want to proceed with this tool use. The tool use was rejected. "
    "To tell you how to proceed, the user said:\n"
    "The user wants to clarify these questions.\n"
    "    Questions asked:\n"
    '- "repro?"\n'
    "  (No answer provided)"
)


def _question() -> AskQuestion:
    return AskQuestion(
        question="repro?",
        header="テスト",
        options=[AskOption("A1", "one"), AskOption("A2", "two"), AskOption("A3", "three")],
    )


def _session_file(project_dir: Path) -> Path:
    return project_dir / "s.jsonl"


def _write_tool_use(project_dir: Path) -> None:
    """The ask is in the transcript; its result is not (the CLI is still waiting)."""
    project_dir.mkdir(parents=True, exist_ok=True)
    event = {
        "timestamp": "2026-09-14T03:02:09.286Z",
        "message": {
            "content": [
                {"type": "tool_use", "id": _TOOL_USE_ID, "name": "AskUserQuestion", "input": {}}
            ]
        },
    }
    _session_file(project_dir).write_text(json.dumps(event) + "\n", encoding="utf-8")


def _append_tool_result(project_dir: Path, text: str) -> None:
    """What the CLI writes once the LAST question of the ask is answered."""
    event = {
        "timestamp": "2026-09-14T03:05:27.700Z",
        "message": {
            "content": [{"type": "tool_result", "tool_use_id": _TOOL_USE_ID, "content": text}]
        },
    }
    with _session_file(project_dir).open("a", encoding="utf-8") as fh:
        fh.write(json.dumps(event, ensure_ascii=False) + "\n")


def _thread(thread_id: int) -> tuple[MagicMock, MagicMock]:
    thread = MagicMock()
    thread.id = thread_id
    msg = MagicMock()
    msg.id = thread_id + 1
    msg.edit = AsyncMock()
    thread.send = AsyncMock(return_value=msg)
    return thread, msg


def _runner(project_dir: Path) -> MagicMock:
    """A pane whose menu stays open until answered, reading *project_dir*.

    The menu must stay open until the click lands, or the bridge takes the
    "answered in the pane" path and never sends keystrokes at all.
    """
    runner = MagicMock()
    state = {"answered": False}

    async def _peek():
        return None if state["answered"] else _question()

    async def _peek_state():
        return (await _peek(), True)

    async def _answer(*_args, **_kwargs):
        state["answered"] = True
        return True

    runner.peek_pending_ask = _peek
    runner.peek_menu_state = _peek_state
    runner.answer_menu = _answer
    runner.answer_menu_multi = _answer
    runner.answer_menu_text = _answer
    runner.cancel_menu = AsyncMock()
    runner.transcript_project_dir = AsyncMock(return_value=project_dir)
    return runner


def _text_of(call) -> str:
    kwargs = call.kwargs
    parts = [str(kwargs.get("content") or "")]
    embed = kwargs.get("embed")
    if embed is not None:
        parts += [str(embed.title or ""), str(embed.description or "")]
    return "\n".join(parts)


def _last_text(msg: MagicMock) -> str:
    assert msg.edit.await_args is not None, "the menu message was never edited"
    return _text_of(msg.edit.await_args)


def _answered(msg: MagicMock) -> bool:
    """The menu's headline is ✅ — the interim body may mention ✅ as a promise."""
    if msg.edit.await_args is None:
        return False
    embed = msg.edit.await_args.kwargs.get("embed")
    return embed is not None and str(embed.title or "").startswith("✅")


def _fast(monkeypatch, *, late_timeout: float = 5.0) -> None:
    monkeypatch.setattr(ask_handler, "_PANE_RESOLVE_POLL", 0.01)
    monkeypatch.setattr(ask_handler, "_PANE_RESOLVE_MISSES", 2)
    monkeypatch.setattr(ask_handler, "_ANSWER_CONFIRM_TIMEOUT", 0.1)
    monkeypatch.setattr(ask_handler, "_ANSWER_CONFIRM_POLL", 0.01)
    # raising=False: before #746 these knobs do not exist, and the tests must
    # then fail on the BEHAVIOUR (the menu stays ❔), not on a missing name.
    monkeypatch.setattr(ask_handler, "_LATE_CONFIRM_TIMEOUT", late_timeout, raising=False)
    monkeypatch.setattr(ask_handler, "_LATE_CONFIRM_POLL_MIN", 0.01, raising=False)
    monkeypatch.setattr(ask_handler, "_LATE_CONFIRM_POLL_MAX", 0.02, raising=False)


async def _answer(thread: MagicMock, runner: MagicMock, answer: str = "A1") -> None:
    async def _click_soon() -> None:
        await asyncio.sleep(0.05)
        ask_bus.post_answer(thread.id, [answer])

    await asyncio.gather(
        asyncio.wait_for(bridge_pane_ask(thread, _question(), runner), timeout=5.0),
        _click_soon(),
    )


async def _until(predicate, timeout: float = 3.0) -> bool:
    loop = asyncio.get_running_loop()
    deadline = loop.time() + timeout
    while loop.time() < deadline:
        if predicate():
            return True
        await asyncio.sleep(0.01)
    return predicate()


@pytest.fixture(autouse=True)
async def _drain_late_watchers():
    """Never let one test's background watcher edit another test's mocks."""
    yield
    tasks = list(getattr(ask_handler, "_late_confirmations", ()))
    for task in tasks:
        task.cancel()
    await asyncio.gather(*tasks, return_exceptions=True)


# -- AC1 / AC4: a result that lands after the window still turns the menu ✅ ---


@pytest.mark.asyncio
async def test_a_result_written_after_the_window_turns_the_menu_answered(monkeypatch, tmp_path):
    """AC1/AC4 — the production shape: 198s (or 53 min) after the tool_use.

    RED before #746: the menu was finalised as ❔「確認できませんでした」at the
    end of the window and nothing ever looked again.
    """
    _fast(monkeypatch)
    _write_tool_use(tmp_path)
    thread, msg = _thread(746_0001)

    await _answer(thread, _runner(tmp_path))
    assert not _answered(msg), f"nothing is confirmed yet — ✅ is a guess: {_last_text(msg)!r}"

    # The last question of the ask is answered only now.
    _append_tool_result(tmp_path, _ANSWERED_RESULT)

    assert await _until(lambda: _answered(msg)), (
        f"the late tool_result never reached the menu; it still reads: {_last_text(msg)!r}"
    )
    final = _last_text(msg)
    assert "repro?" in final and "A1" in final, f"question and answer must survive: {final!r}"


@pytest.mark.asyncio
async def test_the_bridge_releases_the_thread_at_the_end_of_the_window(monkeypatch, tmp_path):
    """The window stays bounded: the next question of the same ask is drawn in
    the pane right after this answer, and it can only be bridged once this
    bridge lets go of the thread. Waiting for the late result inside the bridge
    would trade the wrong ❔ for a question that never reaches Discord.
    """
    _fast(monkeypatch, late_timeout=60.0)
    _write_tool_use(tmp_path)
    thread, _msg = _thread(746_0002)

    await _answer(thread, _runner(tmp_path))  # would time out (5s) if it held on

    assert ask_bus.register(thread.id) is not None, "the thread's menu claim was not released"
    ask_bus.unregister(thread.id)


# -- AC2: while unconfirmed, say "checking" — never "send it again" ----------


@pytest.mark.asyncio
async def test_while_waiting_the_menu_says_checking_not_send_again(monkeypatch, tmp_path):
    """AC2: re-sending the answer interrupts the running turn (#631's shape).

    RED before #746: 「…同じ内容をスレッドにもう一度送ってください。」
    """
    _fast(monkeypatch, late_timeout=60.0)
    _write_tool_use(tmp_path)
    thread, msg = _thread(746_0003)

    await _answer(thread, _runner(tmp_path))

    interim = _last_text(msg)
    assert "もう一度" not in interim, f"must not tell the user to send it again: {interim!r}"
    assert "確認中" in interim, f"should say it is still being checked: {interim!r}"
    assert "A1" in interim, f"the answer that was sent must stay readable: {interim!r}"


def test_the_give_up_wording_never_asks_for_a_resend() -> None:
    """AC2 — even when confirmation finally gives up, re-sending is not the
    advice: the answer most likely landed, and a re-send is an interrupt."""
    embed = ask_unconfirmed_embed("repro?", "テスト", ["A1"])
    text = f"{embed.title}\n{embed.description}"
    assert "もう一度" not in text, text
    assert "A1" in text


# -- AC3: "(No answer provided)" is still reported as not delivered ----------


@pytest.mark.asyncio
async def test_a_late_no_answer_result_still_reads_as_undelivered(monkeypatch, tmp_path):
    """AC3: late is not the same as successful — the late result is classified
    exactly as an on-time one would be."""
    _fast(monkeypatch)
    _write_tool_use(tmp_path)
    thread, msg = _thread(746_0004)

    await _answer(thread, _runner(tmp_path))
    _append_tool_result(tmp_path, _REJECTED_RESULT)

    assert await _until(lambda: "伝わっていません" in _last_text(msg)), (
        f"a late '(No answer provided)' must read as undelivered: {_last_text(msg)!r}"
    )
    assert not _answered(msg)


@pytest.mark.asyncio
async def test_an_on_time_no_answer_result_still_reads_as_undelivered(monkeypatch, tmp_path):
    """AC3: the in-window path is untouched by the late watcher."""
    _fast(monkeypatch)
    _write_tool_use(tmp_path)
    thread, msg = _thread(746_0005)
    runner = _flushing_runner(
        tmp_path, flush=[_tool_result(_TOOL_USE_ID, "2026-09-14T03:02:12.000Z", _REJECTED_RESULT)]
    )

    await _answer(thread, runner)

    final = _last_text(msg)
    assert "伝わっていません" in final and "✅" not in final, final


# -- AC6 (unit half): outcome=unknown only when it really is unknown ---------


@pytest.mark.asyncio
async def test_a_late_confirmation_logs_no_outcome_unknown(monkeypatch, tmp_path, caplog):
    """AC6: production grep for ``ask answer outcome=unknown`` must stop hitting
    answers that arrived. RED before #746: the line was logged at the end of
    the window regardless of what the transcript said a minute later."""
    _fast(monkeypatch)
    _write_tool_use(tmp_path)
    thread, msg = _thread(746_0006)

    with caplog.at_level(logging.INFO, logger="c_lord.discord_ui.ask_handler"):
        await _answer(thread, _runner(tmp_path))
        _append_tool_result(tmp_path, _ANSWERED_RESULT)
        assert await _until(lambda: _answered(msg))

    assert "ask answer outcome=unknown" not in caplog.text, caplog.text


@pytest.mark.asyncio
async def test_a_result_that_never_comes_is_finally_reported_unconfirmed(
    monkeypatch, tmp_path, caplog
):
    """When the result really never arrives, say so — once, and truthfully:
    ❔ (neither ✅ nor ⚠️), no resend advice, and the WARNING that grep finds."""
    _fast(monkeypatch, late_timeout=0.2)
    _write_tool_use(tmp_path)
    thread, msg = _thread(746_0007)

    with caplog.at_level(logging.INFO, logger="c_lord.discord_ui.ask_handler"):
        await _answer(thread, _runner(tmp_path))
        assert await _until(lambda: "確認中" not in _last_text(msg)), _last_text(msg)

    final = _last_text(msg)
    assert "✅" not in final and "伝わっていません" not in final, final
    assert "もう一度" not in final, final
    assert "ask answer outcome=unknown" in caplog.text


# -- the restart-recovery path (#671) answers the same way -------------------


@pytest.mark.asyncio
async def test_a_restored_menu_is_also_corrected_by_a_late_result(monkeypatch, tmp_path):
    """#671's re-armed menus type into the pane and confirm exactly like a live
    bridge — so they carried the same 12-second blind spot."""
    from c_lord.ask_menu_recovery import PaneMenuAnswerer

    _fast(monkeypatch)
    _write_tool_use(tmp_path)
    runner = _runner(tmp_path)
    _thread_mock, msg = _thread(746_0008)

    async def _factory(_thread_id: int):
        return runner

    answerer = PaneMenuAnswerer(thread_id=746_0008, question=_question(), runner_factory=_factory)
    ok, _reason = await asyncio.wait_for(answerer(["A1"], msg), timeout=3.0)
    assert ok
    assert not _answered(msg)

    _append_tool_result(tmp_path, _ANSWERED_RESULT)

    assert await _until(lambda: _answered(msg)), _last_text(msg)


# -- the menu on screen is not in the transcript yet (found on staging) -------
#
# staging 2026-09-23 21:04: 案B was Esc'd (its tool_result says "rejected"), then
# 案C was asked and answered. The CLI had not written 案C's tool_use yet when the
# bridge looked it up — it lands together with the result — so "the newest
# AskUserQuestion" was 案B, and 案C's answer was judged by 案B's rejection:
# ``outcome=unknown`` over an answer Claude had received. With the late watcher
# that became worse: it would watch 案B's result, which never changes, for 25h.

_OLD_ID = "toolu_01OLDxxxxxxxxxxxxxxxxxxxx"
_NEW_ID = "toolu_01NEWxxxxxxxxxxxxxxxxxxxx"
_PLAIN_REJECTION = (
    "The user doesn't want to proceed with this tool use. The tool use was rejected "
    "(eg. if it was a file edit, the new_string was NOT written to the file). STOP what "
    "you are doing and wait for the user to tell you how to proceed."
)


def _write_events(project_dir: Path, events: list[dict], *, append: bool = False) -> None:
    project_dir.mkdir(parents=True, exist_ok=True)
    mode = "a" if append else "w"
    with _session_file(project_dir).open(mode, encoding="utf-8") as fh:
        for e in events:
            fh.write(json.dumps(e, ensure_ascii=False) + "\n")


def _tool_use(tool_id: str, ts: str) -> dict:
    return {
        "timestamp": ts,
        "message": {
            "content": [{"type": "tool_use", "id": tool_id, "name": "AskUserQuestion", "input": {}}]
        },
    }


def _tool_result(tool_id: str, ts: str, text: str) -> dict:
    return {
        "timestamp": ts,
        "message": {"content": [{"type": "tool_result", "tool_use_id": tool_id, "content": text}]},
    }


def _flushing_runner(project_dir: Path, *, flush: list[dict], closes: bool = True) -> MagicMock:
    """A pane whose answer makes the CLI write *flush* (tool_use + result at once)."""
    runner = _runner(project_dir)
    state = {"answered": False}

    async def _peek():
        return None if (state["answered"] and closes) else _question()

    async def _peek_state():
        return (await _peek(), True)

    async def _answer(*_args, **_kwargs):
        state["answered"] = True
        _write_events(project_dir, flush, append=True)
        return True

    runner.peek_pending_ask = _peek
    runner.peek_menu_state = _peek_state
    runner.answer_menu = _answer
    runner.answer_menu_multi = _answer
    runner.answer_menu_text = _answer
    return runner


@pytest.mark.asyncio
async def test_an_unwritten_ask_is_not_judged_by_the_previous_one(monkeypatch, tmp_path, caplog):
    """RED before the fix: 案C read 案B's rejection → ⏳ forever, outcome=unknown."""
    _fast(monkeypatch)
    _write_events(
        tmp_path,
        [
            _tool_use(_OLD_ID, "2026-09-23T11:54:12.683Z"),
            _tool_result(_OLD_ID, "2026-09-23T12:02:50.959Z", _PLAIN_REJECTION),
        ],
    )
    runner = _flushing_runner(
        tmp_path,
        flush=[
            _tool_use(_NEW_ID, "2026-09-23T12:02:56.631Z"),
            _tool_result(_NEW_ID, "2026-09-23T12:04:39.715Z", _ANSWERED_RESULT),
        ],
    )
    thread, msg = _thread(746_0101)

    with caplog.at_level(logging.INFO, logger="c_lord.discord_ui.ask_handler"):
        await _answer(thread, runner)
        assert await _until(lambda: _answered(msg)), _last_text(msg)

    assert "ask answer outcome=unknown" not in caplog.text, caplog.text


@pytest.mark.asyncio
async def test_a_plan_menu_is_not_watched_through_an_old_ask(monkeypatch, tmp_path):
    """Plan approval (#251, ``allow_other=False``) raises no AskUserQuestion at
    all, so "the newest ask" is always some earlier, finished one. Judging by
    it — and, since #746, watching it for a day — is wrong either way; the pane
    closing is the evidence for these menus, exactly as when no ask exists."""
    _fast(monkeypatch)
    _write_events(
        tmp_path,
        [
            _tool_use(_OLD_ID, "2026-09-23T11:54:12.683Z"),
            _tool_result(_OLD_ID, "2026-09-23T12:02:50.959Z", _PLAIN_REJECTION),
        ],
    )
    runner = _flushing_runner(tmp_path, flush=[])
    thread, msg = _thread(746_0102)
    plan = _question()
    plan.allow_other = False

    async def _click_soon() -> None:
        await asyncio.sleep(0.05)
        ask_bus.post_answer(thread.id, ["A1"])

    await asyncio.gather(
        asyncio.wait_for(bridge_pane_ask(thread, plan, runner), timeout=5.0), _click_soon()
    )

    assert _answered(msg), _last_text(msg)
    assert not ask_handler._late_confirmations, "a watcher was left on an unrelated ask"
