"""#631 AC7-AC9: the mirror folds Claude's rate-limit banner into one line.

When the account hits a plan limit, Claude Code writes the refusal into the
transcript as an ordinary assistant message — so the mirror posted it verbatim.
On 2026-09-04 that meant six copies of

    You've hit your session limit · resets 3pm (Asia/Tokyo)

in one thread, with no explanation and no Japanese, and in the two threads where
c-lord *did* detect the limit the same sentence arrived again in English one
second after c-lord's own notice.
"""

from __future__ import annotations

import asyncio
import json
from pathlib import Path

import pytest

from c_lord.transcript.mirror import TranscriptMirror
from c_lord.usage_limit import usage_limit_notices

from .helpers import clord_transcript

_FIXTURE = Path(__file__).parents[1] / "fixtures" / "transcripts" / "i631_usage_limit_banners.jsonl"


def _fixture_event(uuid: str) -> dict:
    for line in _FIXTURE.read_text("utf-8").splitlines():
        if line.strip():
            event = json.loads(line)
            if event["uuid"] == uuid:
                return event
    raise AssertionError(f"fixture has no event {uuid}")


def _write_event(path: Path, payload: dict) -> None:
    with path.open("a", encoding="utf-8") as f:
        f.write(json.dumps(payload, ensure_ascii=False) + "\n")


def _turn_end() -> dict:
    return {"type": "system", "subtype": "turn_duration"}


class _Harness:
    """One mirror wired to list sinks, in the production (minimal) verbosity."""

    def __init__(self, tmp_path: Path, thread_id: int) -> None:
        self.posted: list[str] = []
        self.replied: list[str] = []
        project = tmp_path / "proj"
        project.mkdir()
        self.jsonl = project / "s.jsonl"
        clord_transcript(self.jsonl)

        async def sink(text: str) -> None:
            self.posted.append(text)

        async def reply_sink(text: str) -> None:
            self.replied.append(text)

        self.mirror = TranscriptMirror(
            thread_id=thread_id,
            project_dir=project,
            sink=sink,
            reply_sink=reply_sink,
            verbosity="minimal",
            poll_interval=0.05,
        )

    @property
    def all_text(self) -> str:
        return "\n".join(self.posted + self.replied)


@pytest.fixture
def clean_notices():
    usage_limit_notices.clear_all()
    yield
    usage_limit_notices.clear_all()


# ---------------------------------------------------------------------------
# AC7 — the raw English never reaches Discord; a Japanese line with the reset
# time does.  RED before the fix: `posted` held the banner verbatim.
# ---------------------------------------------------------------------------


async def test_banner_is_replaced_by_a_japanese_line(tmp_path: Path, clean_notices) -> None:
    h = _Harness(tmp_path, thread_id=6311)
    h.mirror.start()
    try:
        await asyncio.sleep(0.1)
        _write_event(h.jsonl, _fixture_event("i631-session"))
        _write_event(h.jsonl, _turn_end())
        await asyncio.sleep(0.3)
    finally:
        await h.mirror.stop()

    assert "You've hit your session limit" not in h.all_text
    assert "⏳" in h.all_text
    assert "2:20pm (Asia/Tokyo)" in h.all_text


async def test_folded_banner_is_not_the_turns_final_answer(tmp_path: Path, clean_notices) -> None:
    """The banner is not an answer, so it must not arrive as the pinging reply.

    The CLI often retries internally and the turn then runs anyway (six of the
    eight threads on 2026-09-04); a real answer still has to be the one that
    pings.
    """
    h = _Harness(tmp_path, thread_id=6312)
    h.mirror.start()
    try:
        await asyncio.sleep(0.1)
        _write_event(h.jsonl, _fixture_event("i631-session"))
        await asyncio.sleep(0.2)
        _write_event(
            h.jsonl,
            {
                "type": "assistant",
                "uuid": "real-answer",
                "message": {
                    "role": "assistant",
                    "content": [{"type": "text", "text": "リトライが通ったので続けます。"}],
                },
            },
        )
        _write_event(h.jsonl, _turn_end())
        await asyncio.sleep(0.3)
    finally:
        await h.mirror.stop()

    assert any("⏳" in p for p in h.posted)
    assert not any("⏳" in r for r in h.replied)
    assert any("リトライが通ったので続けます。" in r for r in h.replied)


async def test_quoted_banner_inside_an_answer_is_left_alone(tmp_path: Path, clean_notices) -> None:
    """c-lord's own threads discuss this banner — quoting it is not hitting it."""
    h = _Harness(tmp_path, thread_id=6313)
    h.mirror.start()
    try:
        await asyncio.sleep(0.1)
        _write_event(h.jsonl, _fixture_event("i631-quoted"))
        _write_event(h.jsonl, _turn_end())
        await asyncio.sleep(0.3)
    finally:
        await h.mirror.stop()

    assert "You've hit your weekly limit" in h.all_text
    assert "⏳" not in h.all_text


# ---------------------------------------------------------------------------
# AC8 — c-lord already said it in Japanese, so the mirror says nothing.
# ---------------------------------------------------------------------------


async def test_nothing_is_posted_when_clord_already_announced(
    tmp_path: Path, clean_notices
) -> None:
    h = _Harness(tmp_path, thread_id=6314)
    usage_limit_notices.note(6314)
    h.mirror.start()
    try:
        await asyncio.sleep(0.1)
        _write_event(h.jsonl, _fixture_event("i631-weekly"))
        _write_event(h.jsonl, _turn_end())
        await asyncio.sleep(0.3)
    finally:
        await h.mirror.stop()

    assert h.all_text.strip() == ""


# ---------------------------------------------------------------------------
# AC9 — one line per turn, however many times the banner is written.
# ---------------------------------------------------------------------------


async def test_repeated_banners_in_one_turn_post_once(tmp_path: Path, clean_notices) -> None:
    h = _Harness(tmp_path, thread_id=6315)
    h.mirror.start()
    try:
        await asyncio.sleep(0.1)
        for i in range(6):
            event = _fixture_event("i631-session")
            event["uuid"] = f"i631-session-{i}"
            _write_event(h.jsonl, event)
        _write_event(h.jsonl, _turn_end())
        await asyncio.sleep(0.4)
    finally:
        await h.mirror.stop()

    assert sum(1 for p in h.posted if "⏳" in p) == 1


async def test_a_new_turn_may_report_the_limit_again(tmp_path: Path, clean_notices) -> None:
    """Per turn, not per thread — the next turn is a new fact about the limit."""
    h = _Harness(tmp_path, thread_id=6316)
    h.mirror.start()
    try:
        await asyncio.sleep(0.1)
        _write_event(h.jsonl, _fixture_event("i631-session"))
        _write_event(h.jsonl, _turn_end())
        await asyncio.sleep(0.3)
        event = _fixture_event("i631-session")
        event["uuid"] = "i631-session-turn2"
        _write_event(h.jsonl, event)
        _write_event(h.jsonl, _turn_end())
        await asyncio.sleep(0.3)
    finally:
        await h.mirror.stop()

    assert sum(1 for p in h.posted if "⏳" in p) == 2
