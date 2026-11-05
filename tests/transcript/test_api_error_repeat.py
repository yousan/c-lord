"""#1251 — the same safeguards API error was posted once per refusal.

Real shape (thread=1559358447357526148, 2026-11-04 22:01:38 / 45 / 50 / 22:02:01 JST,
CLI transcript structure only — content redacted): inside ONE turn the CLI writes,
for each refused request, ``system/model_refusal_no_fallback`` + a synthetic
``assistant`` row (``isApiErrorMessage: true``, ``error: invalid_request``,
model ``<synthetic>``), and the turn keeps going (tool_result → next request).
After the 4th refusal comes ``turn_duration``. The journal shows four
``folded API error`` lines; the thread got four identical ``-# ⚠️ API Error: …``
lines in 23 seconds — nothing new for the reader, just a thread filled up.

docs/specs/turn-end-notification.md — the mirror shows the first sentence of the
English error as one small line. Within one turn the same line is posted once; a
different error in the same turn is still posted, and the next turn posts again.
A line whose post failed does not count as posted, and a prompt typed straight into
the pane after an interrupted turn (no ``turn_duration``) starts afresh.
"""

from __future__ import annotations

import asyncio
import json
import os
from pathlib import Path

import pytest

from c_lord.transcript.mirror import TranscriptMirror

from .helpers import clord_transcript

THREAD_ID = 1559358447357526148
_AUP = (
    "API Error: Opus 5.5's safeguards flagged this session "
    "(https://www.anthropic.com/legal/aup). <text>"
)
_MID = "API Error: Connection closed mid-response. Double press esc to edit your last message."
_TIMES = (
    "2026-11-04T13:01:38.362Z",
    "2026-11-04T13:01:45.966Z",
    "2026-11-04T13:01:50.255Z",
    "2026-11-04T13:02:01.003Z",
)
_TURN = "TURN"  # stands for ``note_turn_started`` between rows


def _base(kind: str, ts: str, uuid: str, parent: str) -> dict:
    return {
        "type": kind,
        "timestamp": ts,
        "uuid": uuid,
        "parentUuid": parent,
        "isSidechain": False,
        "sessionId": "s",
    }


def _refusal(ts: str, n: int | str, text: str = _AUP) -> list[dict]:
    """One refused request, as the CLI wrote it 4 times in 23 s."""
    tool = _base("assistant", ts, f"tool{n}", f"think{n}")
    tool["message"] = {
        "role": "assistant",
        "model": "claude-opus-5-5",
        "content": [{"type": "tool_use", "id": f"tu{n}", "name": "Bash", "input": {}}],
    }
    system = _base("system", ts, f"ref{n}", f"tool{n}")
    system.update(
        subtype="model_refusal_no_fallback", content="<text>", level="warning", isMeta=True
    )
    synthetic = _base("assistant", ts, f"err{n}", f"ref{n}")
    synthetic.update(
        isApiErrorMessage=True,
        error="invalid_request",
        message={
            "role": "assistant",
            "model": "<synthetic>",
            "type": "message",
            "content": [{"type": "text", "text": text}],
        },
    )
    result = _base("user", ts, f"res{n}", f"tool{n}")
    result["message"] = {
        "role": "user",
        "content": [{"type": "tool_result", "tool_use_id": f"tu{n}", "content": "<text>"}],
    }
    return [tool, system, synthetic, result]


def _turn_end(uuid: str, parent: str) -> dict:
    end = _base("system", "2026-11-04T13:02:01.023Z", uuid, parent)
    end.update(subtype="turn_duration", durationMs=50000, isMeta=True)
    return end


def _pane_prompt(uuid: str, parent: str) -> dict:
    """A prompt a person typed straight into the pane — no ``note_turn_started``."""
    prompt = _base("user", "2026-11-04T13:01:44.000Z", uuid, parent)
    prompt["message"] = {"role": "user", "content": "続けて"}
    return prompt


async def _api_lines(
    tmp_path: Path, items: list[dict | str], fail_first: str | None = None
) -> list[str]:
    """The API-error lines the thread got.

    *fail_first* makes the first API-error post fail: ``"raise"`` (the sink
    raised) or ``"none"`` (the sink returned no message). Those are not counted.
    """
    project = tmp_path / "proj"
    project.mkdir()
    jsonl = project / "s.jsonl"
    clord_transcript(jsonl)
    os.utime(jsonl, (1, 1))
    posted: list[str] = []
    failed = [fail_first]

    async def sink(text: str) -> object:
        if text.startswith("-# ⚠️ API Error") and failed[0] is not None:
            how, failed[0] = failed[0], None
            if how == "raise":
                raise RuntimeError("Discord 503")
            return None
        posted.append(text)
        return object()  # the posted message, as the cog's sink returns it

    async def reply_sink(text: str, **_: object) -> None:
        posted.append(text)

    async def file_sink(text: str, _path: str, **_: object) -> None:
        posted.append(text)

    async def api_error_sink(text: str) -> None:
        pass

    mirror = TranscriptMirror(
        thread_id=THREAD_ID,
        project_dir=project,
        sink=sink,
        reply_sink=reply_sink,
        file_sink=file_sink,
        poll_interval=0.02,
        idle_flush_seconds=0,
        api_error_sink=api_error_sink,
    )
    mirror.start()
    try:
        await asyncio.sleep(0.1)
        for item in items:
            if item == _TURN:
                await asyncio.sleep(0.15)  # the previous rows are read first
                mirror.note_turn_started(trigger_id=42)
                continue
            with jsonl.open("a", encoding="utf-8") as f:
                f.write(json.dumps(item, ensure_ascii=False) + "\n")
            await asyncio.sleep(0.03)
        await asyncio.sleep(0.3)
    finally:
        await mirror.stop()
    return [p for p in posted if p.startswith("-# ⚠️ API Error")]


async def test_four_refusals_in_one_turn_post_the_api_error_line_once(tmp_path: Path) -> None:
    rows: list[dict | str] = [_TURN]
    for n, ts in enumerate(_TIMES):
        rows += _refusal(ts, n)
    rows.pop()  # the 4th refusal has no tool_result: the turn ends there
    rows.append(_turn_end("end", "err3"))

    api_lines = await _api_lines(tmp_path, rows)

    assert len(api_lines) == 1, f"posted {len(api_lines)} API-error lines: {api_lines}"


async def test_a_different_error_in_the_same_turn_is_still_posted(tmp_path: Path) -> None:
    rows: list[dict | str] = [_TURN]
    rows += _refusal(_TIMES[0], 0)
    rows += _refusal(_TIMES[1], 1, _MID)
    rows += _refusal(_TIMES[2], 2)
    rows.append(_turn_end("end", "err2"))

    api_lines = await _api_lines(tmp_path, rows)

    assert len(api_lines) == 2, api_lines
    assert "safeguards" in api_lines[0] and "mid-response" in api_lines[1]


async def test_the_next_turn_posts_the_same_error_again(tmp_path: Path) -> None:
    rows: list[dict | str] = [_TURN]
    rows += _refusal(_TIMES[0], 0)
    rows.append(_turn_end("end0", "err0"))
    rows.append(_TURN)
    rows += _refusal(_TIMES[1], 1)
    rows.append(_turn_end("end1", "err1"))

    api_lines = await _api_lines(tmp_path, rows)

    assert len(api_lines) == 2, api_lines


@pytest.mark.parametrize("fail_first", ["raise", "none"])
async def test_a_line_whose_post_failed_is_posted_on_the_next_repeat(
    tmp_path: Path, fail_first: str
) -> None:
    rows: list[dict | str] = [_TURN]
    for n, ts in enumerate(_TIMES[:3]):
        rows += _refusal(ts, n)
    rows.append(_turn_end("end", "res2"))

    api_lines = await _api_lines(tmp_path, rows, fail_first=fail_first)

    assert len(api_lines) == 1, api_lines


async def test_a_pane_typed_prompt_after_an_interrupted_turn_posts_again(
    tmp_path: Path,
) -> None:
    rows: list[dict | str] = [_TURN]
    rows += _refusal(_TIMES[0], 0)
    # interrupted: no turn_duration; the next prompt is typed into the pane
    rows.append(_pane_prompt("typed", "res0"))
    rows += _refusal(_TIMES[1], 1)
    rows.append(_turn_end("end", "res1"))

    api_lines = await _api_lines(tmp_path, rows)

    assert len(api_lines) == 2, api_lines
