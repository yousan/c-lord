"""The ❌ line for a self-started turn that stopped on an API error (#1131/#1149).

#1178: the spec says it pings the thread's last requester. With no turn-event
outlet configured — the default, for OSS users and staging — the requester was
never recorded (``begin_turn`` returned before it), so the owner was pinged,
or nobody when no owner is set.

#1180: the line already opens with "Claude Code が API エラーで止まりました。", and
the generic advice repeated that very sentence (W9's real error, below).
"""

from __future__ import annotations

import json
from pathlib import Path
from types import SimpleNamespace
from unittest.mock import AsyncMock, MagicMock

from c_lord import turn_events
from c_lord.api_error import advice
from c_lord.cogs.transcript_mirror import TranscriptMirrorCog
from c_lord.turn_events import StartedBy

from .test_transcript_mirror_cog import _make_repo

W9_ROW = json.loads(
    (Path(__file__).parent / "fixtures/transcripts/i1131_api_error_row.json").read_text()
)
W9_TEXT = W9_ROW["message"]["content"][0]["text"]


def _bot() -> tuple[MagicMock, MagicMock]:
    bot = MagicMock()
    bot.owner_id = 1
    channel = MagicMock()
    channel.send = AsyncMock()
    bot.get_channel.return_value = channel
    return bot, channel


async def _ping_after_person_turn(monkeypatch, *, events_on: bool, tmp_path) -> str:  # noqa: ANN001
    if events_on:
        monkeypatch.setenv(turn_events.ENV_LOG, str(tmp_path / "ev.jsonl"))
    else:
        monkeypatch.delenv(turn_events.ENV_LOG, raising=False)
        monkeypatch.delenv(turn_events.ENV_URL, raising=False)
    bot, channel = _bot()
    turn_events.reset()
    try:
        # What claude_chat calls for a person's turn.
        thread = SimpleNamespace(id=42, parent_id=1, guild=None)
        info = turn_events.begin_turn(thread, StartedBy("user", "777"))
        turn_events.end_turn(info)
        cog = TranscriptMirrorCog(bot, session_repo=_make_repo([]))
        await cog._make_api_error_sink(42)("API Error: The response stopped arriving.")
    finally:
        turn_events.reset()
    return channel.send.call_args.args[0]


async def test_ping_with_turn_events_on(monkeypatch, tmp_path) -> None:  # noqa: ANN001
    body = await _ping_after_person_turn(monkeypatch, events_on=True, tmp_path=tmp_path)
    assert body.endswith("<@777>")


async def test_ping_with_turn_events_off(monkeypatch, tmp_path) -> None:  # noqa: ANN001
    """#1178: no CLORD_EVENT_LOG / CLORD_EVENT_WEBHOOK_URL (the default)."""
    body = await _ping_after_person_turn(monkeypatch, events_on=False, tmp_path=tmp_path)
    assert body.endswith("<@777>"), body


def test_turn_events_off_still_sends_nothing() -> None:
    """#1178: recording the requester must not turn the outlet on."""
    turn_events.reset()
    assert not turn_events.enabled()
    thread = SimpleNamespace(id=43, parent_id=1, guild=None)
    assert turn_events.begin_turn(thread, StartedBy("user", "5")) is None
    assert turn_events.last_started_by(43) == StartedBy("user", "5")
    turn_events.reset()


async def test_notice_on_the_w9_error_does_not_repeat_itself(monkeypatch) -> None:  # noqa: ANN001
    """#1180: W9's real row (i1131_api_error_row.json) — the sentence once."""
    monkeypatch.delenv(turn_events.ENV_LOG, raising=False)
    bot, channel = _bot()
    cog = TranscriptMirrorCog(bot, session_repo=_make_repo([]))
    await cog._make_api_error_sink(42)(W9_TEXT)
    body = channel.send.call_args.args[0]
    assert body.count("API エラーで止まりました") == 1, body


def test_the_runner_line_does_not_repeat_itself() -> None:
    """#1180: the runner's #908 line is built the same way."""
    from c_lord.discord_ui.thread_dashboard import _completion_text

    line = _completion_text(777, no_response=False, api_error=advice(W9_TEXT))
    assert line.count("API エラーで止まりました") == 1, line
