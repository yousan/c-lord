"""Tests for c_lord.cogs.transcript_mirror — Cog lifecycle and gating."""

from __future__ import annotations

import json
import logging
import os
from pathlib import Path
from unittest.mock import AsyncMock, MagicMock

import discord
import pytest

from c_lord.cogs.transcript_mirror import TranscriptMirrorCog

from .transcript.helpers import clord_marker_event, clord_transcript


def _make_repo(rows: list) -> MagicMock:
    repo = MagicMock()
    repo.list_all = AsyncMock(return_value=rows)
    return repo


def _row(thread_id: int, working_dir: str | None, *, closed_at: str | None = None) -> MagicMock:
    r = MagicMock()
    r.thread_id = thread_id
    r.working_dir = working_dir
    r.closed_at = closed_at
    return r


async def test_cog_stays_idle_when_bridge_mode_not_jsonl(
    monkeypatch: pytest.MonkeyPatch, tmp_path: Path
) -> None:
    # #492: jsonl is now the default, so "not jsonl" must be pinned explicitly.
    monkeypatch.setenv("CLORD_BRIDGE_MODE", "skill")
    bot = MagicMock()
    repo = _make_repo([_row(1, str(tmp_path))])
    cog = TranscriptMirrorCog(bot, session_repo=repo)
    await cog.on_ready()
    # list_all is not consulted, no mirrors registered.
    repo.list_all.assert_not_called()
    assert cog._mirrors == {}


async def test_cog_starts_mirrors_from_existing_sessions(
    monkeypatch: pytest.MonkeyPatch, tmp_path: Path
) -> None:
    monkeypatch.setenv("CLORD_BRIDGE_MODE", "jsonl")
    # Lie about the projects root so derive_project_dir lands inside tmp_path.
    monkeypatch.setenv("HOME", str(tmp_path))
    project = tmp_path / ".claude" / "projects" / "-some-cwd"
    project.mkdir(parents=True)

    bot = MagicMock()
    repo = _make_repo([_row(11, "/some/cwd"), _row(22, None)])
    cog = TranscriptMirrorCog(bot, session_repo=repo)
    try:
        await cog.on_ready()
        assert 11 in cog._mirrors
        assert 22 not in cog._mirrors  # working_dir None → skipped
    finally:
        await cog.cog_unload()


async def test_start_for_is_idempotent(monkeypatch: pytest.MonkeyPatch, tmp_path: Path) -> None:
    monkeypatch.setenv("CLORD_BRIDGE_MODE", "jsonl")
    monkeypatch.setenv("HOME", str(tmp_path))
    (tmp_path / ".claude" / "projects" / "-cwd").mkdir(parents=True)

    bot = MagicMock()
    repo = _make_repo([])
    cog = TranscriptMirrorCog(bot, session_repo=repo)
    try:
        assert cog.start_for(99, "/cwd") is True
        assert cog.start_for(99, "/cwd") is False  # already running
    finally:
        await cog.cog_unload()


async def test_start_for_noop_when_not_jsonl_mode(
    monkeypatch: pytest.MonkeyPatch, tmp_path: Path
) -> None:
    # #492: jsonl is now the default, so "not jsonl" must be pinned explicitly.
    monkeypatch.setenv("CLORD_BRIDGE_MODE", "skill")
    bot = MagicMock()
    cog = TranscriptMirrorCog(bot, session_repo=_make_repo([]))
    assert cog.start_for(1, str(tmp_path)) is False


async def test_sink_chunks_long_messages_without_truncation(
    monkeypatch: pytest.MonkeyPatch, tmp_path: Path
) -> None:
    """Issue #235: long bodies must be split into multiple sends, not
    truncated to 1985 chars + '…'."""
    monkeypatch.setenv("CLORD_BRIDGE_MODE", "jsonl")
    bot = MagicMock()
    channel = MagicMock()
    channel.send = AsyncMock()
    bot.get_channel.return_value = channel

    cog = TranscriptMirrorCog(bot, session_repo=_make_repo([]))
    sink = cog._make_sink(7)
    await sink("a" * 3000)
    assert channel.send.call_count >= 2
    bodies = []
    for call in channel.send.call_args_list:
        body = call.kwargs.get("content") or (call.args[0] if call.args else "")
        assert len(body) <= 2000
        assert not body.endswith("…")  # no lossy truncation
        bodies.append(body)
    assert sum(len(b) for b in bodies) == 3000  # full content preserved


async def test_reply_sink_chunks_long_messages(
    monkeypatch: pytest.MonkeyPatch, tmp_path: Path
) -> None:
    """Issue #235: final assistant text longer than 2000 chars is chunked."""
    monkeypatch.setenv("CLORD_BRIDGE_MODE", "jsonl")
    bot = MagicMock()
    channel = MagicMock()
    channel.send = AsyncMock()
    bot.get_channel.return_value = channel

    cog = TranscriptMirrorCog(bot, session_repo=_make_repo([]))
    reply_sink = cog._make_reply_sink(7)
    text = "\n".join(f"answer line {i}" for i in range(300))
    assert len(text) > 2000
    await reply_sink(text)
    assert channel.send.call_count >= 2
    for call in channel.send.call_args_list:
        body = call.kwargs.get("content") or (call.args[0] if call.args else "")
        assert len(body) <= 2000
        assert not body.endswith("…")


async def test_reply_sink_suppresses_url_embeds_by_default(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """#372: in jsonl bridge mode the final answer goes through reply_sink.
    By default (CLORD_SHOW_URL_EMBEDS unset) it must set suppress_embeds=True
    so a URL in Claude's reply doesn't expand into an OGP card."""
    monkeypatch.setenv("CLORD_BRIDGE_MODE", "jsonl")
    monkeypatch.delenv("CLORD_SHOW_URL_EMBEDS", raising=False)
    bot = MagicMock()
    channel = MagicMock()
    channel.send = AsyncMock()
    bot.get_channel.return_value = channel

    cog = TranscriptMirrorCog(bot, session_repo=_make_repo([]))
    reply_sink = cog._make_reply_sink(7)
    await reply_sink("see https://github.com/yousan/c-lord/issues for details")
    channel.send.assert_called_once()
    assert channel.send.call_args.kwargs.get("suppress_embeds") is True


async def test_reply_sink_shows_url_embeds_when_enabled(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """#372: CLORD_SHOW_URL_EMBEDS=true restores OGP previews on reply_sink."""
    monkeypatch.setenv("CLORD_BRIDGE_MODE", "jsonl")
    monkeypatch.setenv("CLORD_SHOW_URL_EMBEDS", "true")
    bot = MagicMock()
    channel = MagicMock()
    channel.send = AsyncMock()
    bot.get_channel.return_value = channel

    cog = TranscriptMirrorCog(bot, session_repo=_make_repo([]))
    reply_sink = cog._make_reply_sink(7)
    await reply_sink("see https://github.com/yousan/c-lord/issues for details")
    channel.send.assert_called_once()
    assert channel.send.call_args.kwargs.get("suppress_embeds") is False


async def test_file_sink_chunks_and_attaches_to_last_only(
    monkeypatch: pytest.MonkeyPatch, tmp_path: Path
) -> None:
    """Issue #235: when the final answer is chunked, the progress.txt
    attachment rides on the last message only."""
    monkeypatch.setenv("CLORD_BRIDGE_MODE", "jsonl")
    bot = MagicMock()
    channel = MagicMock()
    channel.send = AsyncMock()
    bot.get_channel.return_value = channel
    progress_file = tmp_path / "progress.txt"
    progress_file.write_text("tool output\n")

    cog = TranscriptMirrorCog(bot, session_repo=_make_repo([]))
    file_sink = cog._make_file_sink(7)
    text = "\n".join(f"answer line {i}" for i in range(300))
    assert len(text) > 2000
    await file_sink(text, str(progress_file))

    calls = channel.send.call_args_list
    assert len(calls) >= 2
    for call in calls[:-1]:
        assert "file" not in call.kwargs and "files" not in call.kwargs
        body = call.kwargs.get("content") or (call.args[0] if call.args else "")
        assert len(body) <= 2000
    assert "files" in calls[-1].kwargs or "file" in calls[-1].kwargs


async def test_sink_falls_back_to_fetch_channel(
    monkeypatch: pytest.MonkeyPatch, tmp_path: Path
) -> None:
    monkeypatch.setenv("CLORD_BRIDGE_MODE", "jsonl")
    bot = MagicMock()
    bot.get_channel.return_value = None
    fetched = MagicMock()
    fetched.send = AsyncMock()
    bot.fetch_channel = AsyncMock(return_value=fetched)

    cog = TranscriptMirrorCog(bot, session_repo=_make_repo([]))
    sink = cog._make_sink(7)
    await sink("hello")
    fetched.send.assert_called_once()
    assert fetched.send.call_args.kwargs.get("content") == "hello"


async def test_end_to_end_jsonl_event_posts_to_discord(
    monkeypatch: pytest.MonkeyPatch, tmp_path: Path
) -> None:
    """Write an event to the project's jsonl and confirm it lands on Discord."""
    import asyncio as _asyncio

    monkeypatch.setenv("CLORD_BRIDGE_MODE", "jsonl")
    monkeypatch.setenv("HOME", str(tmp_path))
    project = tmp_path / ".claude" / "projects" / "-some-cwd"
    project.mkdir(parents=True)
    jsonl = clord_transcript(project / "s.jsonl")
    os.utime(jsonl, (1, 1))

    bot = MagicMock()
    channel = MagicMock()
    channel.send = AsyncMock()
    bot.get_channel.return_value = channel

    repo = _make_repo([])
    cog = TranscriptMirrorCog(bot, session_repo=repo)
    cog.start_for(123, "/some/cwd")
    # Bump poll interval down for the test.
    cog._mirrors[123]._poll_interval = 0.05
    try:
        await _asyncio.sleep(0.15)
        with jsonl.open("a", encoding="utf-8") as f:
            f.write(
                json.dumps(
                    {
                        "type": "assistant",
                        "message": {
                            "role": "assistant",
                            "content": [{"type": "text", "text": "via cog"}],
                        },
                    }
                )
                + "\n"
            )
        await _asyncio.sleep(0.3)
    finally:
        await cog.cog_unload()

    assert channel.send.called
    sent_bodies = [
        c.kwargs.get("content") or (c.args[0] if c.args else "")
        for c in channel.send.call_args_list
    ]
    assert any("via cog" in b for b in sent_bodies)


# ---------------------------------------------------------------------------
# Issue #85: observability — sink failures must be logged, not suppressed
# ---------------------------------------------------------------------------


async def test_sink_logs_warning_on_http_exception(
    monkeypatch: pytest.MonkeyPatch, caplog: pytest.LogCaptureFixture
) -> None:
    """HTTPException in sink must produce a WARNING log, not be silently swallowed."""

    bot = MagicMock()
    channel = MagicMock()
    channel.send = AsyncMock(side_effect=discord.HTTPException(MagicMock(status=403), "Forbidden"))
    bot.get_channel.return_value = channel

    cog = TranscriptMirrorCog(bot, session_repo=_make_repo([]))
    sink = cog._make_sink(42)

    with caplog.at_level(logging.WARNING, logger="c_lord.cogs.transcript_mirror"):
        await sink("test message")

    assert any("42" in r.message for r in caplog.records), "thread_id missing from log"
    assert any(r.levelno >= logging.WARNING for r in caplog.records), "no WARNING logged"


async def test_sink_log_includes_body_length(
    monkeypatch: pytest.MonkeyPatch, caplog: pytest.LogCaptureFixture
) -> None:
    """Log message should include body length so we can triage truncation issues."""

    bot = MagicMock()
    channel = MagicMock()
    channel.send = AsyncMock(side_effect=discord.HTTPException(MagicMock(status=400), "Bad"))
    bot.get_channel.return_value = channel

    cog = TranscriptMirrorCog(bot, session_repo=_make_repo([]))
    sink = cog._make_sink(99)

    with caplog.at_level(logging.WARNING, logger="c_lord.cogs.transcript_mirror"):
        await sink("hello world")

    combined = " ".join(r.message for r in caplog.records)
    assert "99" in combined, "thread_id not in log"


async def test_file_sink_falls_back_to_plain_on_http_exception(
    monkeypatch: pytest.MonkeyPatch, tmp_path: Path
) -> None:
    """When file attachment send fails, file_sink must retry with plain text."""
    bot = MagicMock()
    channel = MagicMock()
    call_count = 0

    async def send_side_effect(*args, **kwargs):
        nonlocal call_count
        call_count += 1
        if "files" in kwargs or "file" in kwargs:
            raise discord.HTTPException(MagicMock(status=413), "Too large")
        # Plain text succeeds

    channel.send = AsyncMock(side_effect=send_side_effect)
    bot.get_channel.return_value = channel

    progress_file = tmp_path / "progress.txt"
    progress_file.write_text("tool output")

    cog = TranscriptMirrorCog(bot, session_repo=_make_repo([]))
    file_sink = cog._make_file_sink(10)
    await file_sink("final answer", str(progress_file))

    # send called twice: once with files (failed), once without (fallback)
    assert call_count == 2
    # Last call was plain text (no file kwarg)
    last_kwargs = channel.send.call_args_list[-1][1]
    assert "files" not in last_kwargs and "file" not in last_kwargs


async def test_file_sink_logs_warning_on_http_exception(
    monkeypatch: pytest.MonkeyPatch, tmp_path: Path, caplog: pytest.LogCaptureFixture
) -> None:
    """HTTPException in file_sink must produce a WARNING log."""

    bot = MagicMock()
    channel = MagicMock()
    channel.send = AsyncMock(side_effect=discord.HTTPException(MagicMock(status=500), "Error"))
    bot.get_channel.return_value = channel

    progress_file = tmp_path / "progress.txt"
    progress_file.write_text("some tool output")

    cog = TranscriptMirrorCog(bot, session_repo=_make_repo([]))
    file_sink = cog._make_file_sink(77)

    with caplog.at_level(logging.WARNING, logger="c_lord.cogs.transcript_mirror"):
        await file_sink("body", str(progress_file))

    assert any(r.levelno >= logging.WARNING for r in caplog.records)
    combined = " ".join(r.message for r in caplog.records)
    assert "77" in combined, "thread_id missing from log"


# ---------------------------------------------------------------------------
# Table image attachment tests
# ---------------------------------------------------------------------------

TABLE_CONTENT = """\
Here is some output:

| Name  | Score |
|-------|-------|
| Alice | 100   |
| Bob   | 85    |

That was the table.
"""

NO_TABLE_CONTENT = "Just plain text with no table here."


async def test_reply_sink_attaches_table_image_when_enabled(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """When CLORD_RENDER_TABLE_IMAGES=true, reply_sink sends with files= for table content.

    render_table_image is mocked so this test does not require matplotlib.
    reply_sink is used for final assistant_text responses.
    """
    from unittest.mock import patch

    monkeypatch.setenv("CLORD_RENDER_TABLE_IMAGES", "true")
    monkeypatch.delenv("CLORD_REPLY_TO_TRIGGER", raising=False)

    bot = MagicMock()
    channel = MagicMock()
    channel.send = AsyncMock()
    bot.get_channel.return_value = channel

    cog = TranscriptMirrorCog(bot, session_repo=_make_repo([]))
    reply_sink = cog._make_reply_sink(42)

    # Mock the renderer so the test works without matplotlib installed
    with patch(
        "c_lord.discord_ui.table_renderer.render_table_image",
        return_value=b"\x89PNG\r\n",
    ):
        await reply_sink(TABLE_CONTENT)

    channel.send.assert_called_once()
    call_kwargs = channel.send.call_args.kwargs
    assert "files" in call_kwargs or "file" in call_kwargs, "no file attachment"
    files = call_kwargs.get("files") or [call_kwargs.get("file")]
    assert len(files) >= 1
    assert any(getattr(f, "filename", "").startswith("table_") for f in files)


async def test_sink_no_attachment_when_disabled(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """When CLORD_RENDER_TABLE_IMAGES is not set, sink sends without files."""
    monkeypatch.delenv("CLORD_RENDER_TABLE_IMAGES", raising=False)

    bot = MagicMock()
    channel = MagicMock()
    channel.send = AsyncMock()
    bot.get_channel.return_value = channel

    cog = TranscriptMirrorCog(bot, session_repo=_make_repo([]))
    sink = cog._make_sink(42)
    await sink(TABLE_CONTENT)

    channel.send.assert_called_once()
    call_kwargs = channel.send.call_args.kwargs
    assert "files" not in call_kwargs
    assert "file" not in call_kwargs


async def test_sink_no_attachment_when_no_table(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """When content has no table, no file attachment even if rendering is enabled."""
    monkeypatch.setenv("CLORD_RENDER_TABLE_IMAGES", "true")

    bot = MagicMock()
    channel = MagicMock()
    channel.send = AsyncMock()
    bot.get_channel.return_value = channel

    cog = TranscriptMirrorCog(bot, session_repo=_make_repo([]))
    sink = cog._make_sink(42)
    await sink(NO_TABLE_CONTENT)

    channel.send.assert_called_once()
    call_kwargs = channel.send.call_args.kwargs
    assert "files" not in call_kwargs
    assert "file" not in call_kwargs


# ---------------------------------------------------------------------------
# file_sink table image attachment tests
# ---------------------------------------------------------------------------


async def test_file_sink_attaches_table_image_when_enabled(
    monkeypatch: pytest.MonkeyPatch, tmp_path: Path
) -> None:
    """file_sink sends table image alongside progress.txt when rendering is enabled."""
    from unittest.mock import patch

    monkeypatch.setenv("CLORD_RENDER_TABLE_IMAGES", "true")

    bot = MagicMock()
    channel = MagicMock()
    channel.send = AsyncMock()
    bot.get_channel.return_value = channel

    progress_file = tmp_path / "progress.txt"
    progress_file.write_text("tool output")

    cog = TranscriptMirrorCog(bot, session_repo=_make_repo([]))
    file_sink = cog._make_file_sink(42)

    with patch(
        "c_lord.discord_ui.table_renderer.render_table_image",
        return_value=b"\x89PNG\r\n",
    ):
        await file_sink(TABLE_CONTENT, str(progress_file))

    channel.send.assert_called_once()
    call_kwargs = channel.send.call_args.kwargs
    files = call_kwargs.get("files", [])
    filenames = [getattr(f, "filename", "") for f in files]
    assert any(n.startswith("table_") for n in filenames), f"no table image in {filenames}"
    assert any(n == "progress.txt" for n in filenames), f"no progress.txt in {filenames}"


async def test_file_sink_no_table_image_when_disabled(
    monkeypatch: pytest.MonkeyPatch, tmp_path: Path
) -> None:
    """file_sink sends only progress.txt when table rendering is disabled."""
    monkeypatch.delenv("CLORD_RENDER_TABLE_IMAGES", raising=False)

    bot = MagicMock()
    channel = MagicMock()
    channel.send = AsyncMock()
    bot.get_channel.return_value = channel

    progress_file = tmp_path / "progress.txt"
    progress_file.write_text("tool output")

    cog = TranscriptMirrorCog(bot, session_repo=_make_repo([]))
    file_sink = cog._make_file_sink(42)
    await file_sink(TABLE_CONTENT, str(progress_file))

    channel.send.assert_called_once()
    call_kwargs = channel.send.call_args
    # Only progress.txt, no table image
    files = call_kwargs.kwargs.get("files", [])
    filenames = [getattr(f, "filename", "") for f in files]
    assert not any(n.startswith("table_") for n in filenames)


# ---------------------------------------------------------------------------
# Issue #115: silent posts + reply threading
# ---------------------------------------------------------------------------


async def test_sink_sends_silent_by_default(monkeypatch: pytest.MonkeyPatch) -> None:
    """Intermediate (non-final) sends should be silent by default."""
    monkeypatch.delenv("CLORD_SILENT_POSTS", raising=False)

    bot = MagicMock()
    channel = MagicMock()
    channel.send = AsyncMock()
    bot.get_channel.return_value = channel

    cog = TranscriptMirrorCog(bot, session_repo=_make_repo([]))
    sink = cog._make_sink(1)
    await sink("hello")

    call_kwargs = channel.send.call_args.kwargs
    assert call_kwargs.get("silent") is True


async def test_sink_not_silent_when_disabled(monkeypatch: pytest.MonkeyPatch) -> None:
    """When CLORD_SILENT_POSTS=0, sink sends without silent flag."""
    monkeypatch.setenv("CLORD_SILENT_POSTS", "0")

    bot = MagicMock()
    channel = MagicMock()
    channel.send = AsyncMock()
    bot.get_channel.return_value = channel

    cog = TranscriptMirrorCog(bot, session_repo=_make_repo([]))
    sink = cog._make_sink(1)
    await sink("hello")

    call_kwargs = channel.send.call_args.kwargs
    assert call_kwargs.get("silent") is not True


async def test_reply_sink_uses_reference(monkeypatch: pytest.MonkeyPatch) -> None:
    """reply_sink uses reference to trigger message and mention_author=False."""
    monkeypatch.delenv("CLORD_REPLY_TO_TRIGGER", raising=False)

    bot = MagicMock()
    channel = MagicMock()
    channel.send = AsyncMock()
    bot.get_channel.return_value = channel

    cog = TranscriptMirrorCog(bot, session_repo=_make_repo([]))
    cog.set_trigger_message(42, 9999)
    reply_sink = cog._make_reply_sink(42)
    await reply_sink("final answer")

    call_kwargs = channel.send.call_args.kwargs
    assert call_kwargs.get("mention_author") is False
    ref = call_kwargs.get("reference")
    assert ref is not None
    assert ref.message_id == 9999


async def test_reply_sink_no_reference_when_disabled(monkeypatch: pytest.MonkeyPatch) -> None:
    """When CLORD_REPLY_TO_TRIGGER=0, reply_sink sends without reference."""
    monkeypatch.setenv("CLORD_REPLY_TO_TRIGGER", "0")

    bot = MagicMock()
    channel = MagicMock()
    channel.send = AsyncMock()
    bot.get_channel.return_value = channel

    cog = TranscriptMirrorCog(bot, session_repo=_make_repo([]))
    cog.set_trigger_message(42, 9999)
    reply_sink = cog._make_reply_sink(42)
    await reply_sink("final answer")

    call_kwargs = channel.send.call_args.kwargs
    assert "reference" not in call_kwargs


async def test_reply_sink_no_reference_when_no_trigger(monkeypatch: pytest.MonkeyPatch) -> None:
    """reply_sink works without reference if trigger message is unknown."""
    monkeypatch.delenv("CLORD_REPLY_TO_TRIGGER", raising=False)

    bot = MagicMock()
    channel = MagicMock()
    channel.send = AsyncMock()
    bot.get_channel.return_value = channel

    cog = TranscriptMirrorCog(bot, session_repo=_make_repo([]))
    # No set_trigger_message call
    reply_sink = cog._make_reply_sink(42)
    await reply_sink("final answer")

    channel.send.assert_called_once()
    call_kwargs = channel.send.call_args.kwargs
    assert "reference" not in call_kwargs


async def test_set_trigger_message_stores_per_thread(monkeypatch: pytest.MonkeyPatch) -> None:
    """set_trigger_message stores independently per thread."""
    bot = MagicMock()
    cog = TranscriptMirrorCog(bot, session_repo=_make_repo([]))
    cog.set_trigger_message(1, 111)
    cog.set_trigger_message(2, 222)
    assert cog._trigger_messages[1] == 111
    assert cog._trigger_messages[2] == 222


async def test_file_sink_uses_reference(monkeypatch: pytest.MonkeyPatch, tmp_path: Path) -> None:
    """file_sink (final answer with progress.txt) uses reference to trigger message."""
    monkeypatch.delenv("CLORD_REPLY_TO_TRIGGER", raising=False)

    bot = MagicMock()
    channel = MagicMock()
    channel.send = AsyncMock()
    bot.get_channel.return_value = channel

    progress_file = tmp_path / "progress.txt"
    progress_file.write_text("tool output")

    cog = TranscriptMirrorCog(bot, session_repo=_make_repo([]))
    cog.set_trigger_message(5, 8888)
    file_sink = cog._make_file_sink(5)
    await file_sink("final answer", str(progress_file))

    call_kwargs = channel.send.call_args.kwargs
    assert call_kwargs.get("mention_author") is False
    ref = call_kwargs.get("reference")
    assert ref is not None
    assert ref.message_id == 8888


# ---------------------------------------------------------------------------
# Issue #149: DB fallback for trigger_message_id when in-memory is absent
# ---------------------------------------------------------------------------


def _make_repo_with_trigger(trigger_message_id: int | None) -> MagicMock:
    repo = MagicMock()
    repo.list_all = AsyncMock(return_value=[])
    record = MagicMock()
    record.trigger_message_id = trigger_message_id
    repo.get = AsyncMock(return_value=record)
    return repo


async def test_reply_sink_falls_back_to_db_trigger_when_inmemory_missing(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """In-memory absent + DB has trigger_message_id → reply_sink attaches reference."""
    monkeypatch.delenv("CLORD_REPLY_TO_TRIGGER", raising=False)

    bot = MagicMock()
    channel = MagicMock()
    channel.send = AsyncMock()
    bot.get_channel.return_value = channel

    cog = TranscriptMirrorCog(bot, session_repo=_make_repo_with_trigger(7777))
    reply_sink = cog._make_reply_sink(42)
    await reply_sink("final answer")

    call_kwargs = channel.send.call_args.kwargs
    assert call_kwargs.get("mention_author") is False
    ref = call_kwargs.get("reference")
    assert ref is not None
    assert ref.message_id == 7777


async def test_reply_sink_inmemory_takes_priority_over_db(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """When in-memory is registered, it is used (DB is not consulted)."""
    monkeypatch.delenv("CLORD_REPLY_TO_TRIGGER", raising=False)

    bot = MagicMock()
    channel = MagicMock()
    channel.send = AsyncMock()
    bot.get_channel.return_value = channel

    repo = _make_repo_with_trigger(9999)
    cog = TranscriptMirrorCog(bot, session_repo=repo)
    cog.set_trigger_message(42, 1234)
    reply_sink = cog._make_reply_sink(42)
    await reply_sink("final answer")

    ref = channel.send.call_args.kwargs.get("reference")
    assert ref is not None
    assert ref.message_id == 1234
    repo.get.assert_not_called()


async def test_reply_sink_no_reference_when_db_also_empty(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """When in-memory is absent and DB has no trigger_message_id, sends normally."""
    monkeypatch.delenv("CLORD_REPLY_TO_TRIGGER", raising=False)

    bot = MagicMock()
    channel = MagicMock()
    channel.send = AsyncMock()
    bot.get_channel.return_value = channel

    cog = TranscriptMirrorCog(bot, session_repo=_make_repo_with_trigger(None))
    reply_sink = cog._make_reply_sink(42)
    await reply_sink("final answer")

    call_kwargs = channel.send.call_args.kwargs
    assert "reference" not in call_kwargs


async def test_reply_sink_no_reference_when_db_raises(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """DB error in fallback does not crash reply_sink — sends normally."""
    monkeypatch.delenv("CLORD_REPLY_TO_TRIGGER", raising=False)

    bot = MagicMock()
    channel = MagicMock()
    channel.send = AsyncMock()
    bot.get_channel.return_value = channel

    repo = MagicMock()
    repo.list_all = AsyncMock(return_value=[])
    repo.get = AsyncMock(side_effect=Exception("DB exploded"))
    cog = TranscriptMirrorCog(bot, session_repo=repo)
    reply_sink = cog._make_reply_sink(42)
    await reply_sink("final answer")

    channel.send.assert_called_once()
    call_kwargs = channel.send.call_args.kwargs
    assert "reference" not in call_kwargs


async def test_file_sink_falls_back_to_db_trigger_when_inmemory_missing(
    monkeypatch: pytest.MonkeyPatch, tmp_path: Path
) -> None:
    """In-memory absent + DB has trigger_message_id → file_sink attaches reference."""
    monkeypatch.delenv("CLORD_REPLY_TO_TRIGGER", raising=False)

    bot = MagicMock()
    channel = MagicMock()
    channel.send = AsyncMock()
    bot.get_channel.return_value = channel

    progress_file = tmp_path / "progress.txt"
    progress_file.write_text("tool output")

    cog = TranscriptMirrorCog(bot, session_repo=_make_repo_with_trigger(5555))
    file_sink = cog._make_file_sink(55)
    await file_sink("final answer", str(progress_file))

    call_kwargs = channel.send.call_args.kwargs
    assert call_kwargs.get("mention_author") is False
    ref = call_kwargs.get("reference")
    assert ref is not None
    assert ref.message_id == 5555


async def test_file_sink_no_reference_when_db_raises(
    monkeypatch: pytest.MonkeyPatch, tmp_path: Path
) -> None:
    """DB error in file_sink fallback does not crash — sends normally."""
    monkeypatch.delenv("CLORD_REPLY_TO_TRIGGER", raising=False)

    bot = MagicMock()
    channel = MagicMock()
    channel.send = AsyncMock()
    bot.get_channel.return_value = channel

    progress_file = tmp_path / "progress.txt"
    progress_file.write_text("tool output")

    repo = MagicMock()
    repo.list_all = AsyncMock(return_value=[])
    repo.get = AsyncMock(side_effect=Exception("DB exploded"))
    cog = TranscriptMirrorCog(bot, session_repo=repo)
    file_sink = cog._make_file_sink(55)
    await file_sink("final answer", str(progress_file))

    channel.send.assert_called_once()
    call_kwargs = channel.send.call_args.kwargs
    assert "reference" not in call_kwargs


# ── Issue #215: recover undelivered final answer on restart ───────────


def _recovery_repo(rows: list, *, trigger_message_id=None) -> MagicMock:
    """A repo with list_all + get + set_mirror_replied_uuid for recovery tests."""
    repo = MagicMock()
    repo.list_all = AsyncMock(return_value=rows)
    record = MagicMock()
    record.trigger_message_id = trigger_message_id
    repo.get = AsyncMock(return_value=record)
    repo.set_mirror_replied_uuid = AsyncMock()
    return repo


def _session_row(thread_id: int, working_dir: str, replied_uuid, *, closed_at: str | None = None):
    r = MagicMock()
    r.thread_id = thread_id
    r.working_dir = working_dir
    r.mirror_replied_uuid = replied_uuid
    r.closed_at = closed_at
    return r


async def test_on_ready_recovers_undelivered_final_answer(
    monkeypatch: pytest.MonkeyPatch, tmp_path: Path
) -> None:
    """Issue #215: a newer completed turn's final answer (uuid != the stored
    cursor) was written while the bot was down → re-delivered on on_ready."""
    monkeypatch.setenv("CLORD_BRIDGE_MODE", "jsonl")
    monkeypatch.setenv("HOME", str(tmp_path))
    project = tmp_path / ".claude" / "projects" / "-some-cwd"
    project.mkdir(parents=True)
    (project / "s.jsonl").write_text(
        "\n".join(
            json.dumps(e, ensure_ascii=False)
            # #627: the mirror only follows a transcript c-lord itself drove.
            for e in [
                clord_marker_event(),
                # Previous turn, already delivered (cursor points here).
                {
                    "type": "assistant",
                    "uuid": "u-old",
                    "message": {"content": [{"type": "text", "text": "previous answer"}]},
                },
                {"type": "system", "subtype": "turn_duration"},
                # The turn that completed while the bot was down (undelivered).
                {
                    "type": "assistant",
                    "uuid": "u-final",
                    "message": {"content": [{"type": "text", "text": "the dropped final answer"}]},
                },
                {"type": "system", "subtype": "turn_duration"},
            ]
        )
        + "\n"
    )

    bot = MagicMock()
    channel = MagicMock()
    channel.send = AsyncMock()
    bot.get_channel.return_value = channel

    repo = _recovery_repo([_session_row(11, "/some/cwd", "u-old")])
    cog = TranscriptMirrorCog(bot, session_repo=repo)
    try:
        await cog.on_ready()
    finally:
        await cog.cog_unload()

    # The dropped final answer was re-delivered exactly once...
    sent_bodies = [
        (c.kwargs.get("content") or (c.args[0] if c.args else ""))
        for c in channel.send.call_args_list
    ]
    assert any("the dropped final answer" in b for b in sent_bodies), sent_bodies
    assert not any("previous answer" in b for b in sent_bodies), sent_bodies
    # ...and the cursor was advanced so it won't be re-posted next restart.
    repo.set_mirror_replied_uuid.assert_awaited_once_with(11, "u-final")


async def test_on_ready_seeds_cursor_silently_when_null(
    monkeypatch: pytest.MonkeyPatch, tmp_path: Path
) -> None:
    """First time a session is tracked (cursor NULL, e.g. right after the
    migration added the column) the last answer must NOT be re-posted —
    otherwise every existing thread is spammed on the first deploy. The cursor
    is seeded silently instead."""
    monkeypatch.setenv("CLORD_BRIDGE_MODE", "jsonl")
    monkeypatch.setenv("HOME", str(tmp_path))
    project = tmp_path / ".claude" / "projects" / "-some-cwd"
    project.mkdir(parents=True)
    (project / "s.jsonl").write_text(
        "\n".join(
            json.dumps(e, ensure_ascii=False)
            # #627: the mirror only follows a transcript c-lord itself drove.
            for e in [
                clord_marker_event(),
                {
                    "type": "assistant",
                    "uuid": "u-final",
                    "message": {"content": [{"type": "text", "text": "pre-existing last answer"}]},
                },
                {"type": "system", "subtype": "turn_duration"},
            ]
        )
        + "\n"
    )

    bot = MagicMock()
    channel = MagicMock()
    channel.send = AsyncMock()
    bot.get_channel.return_value = channel

    repo = _recovery_repo([_session_row(11, "/some/cwd", None)])
    cog = TranscriptMirrorCog(bot, session_repo=repo)
    try:
        await cog.on_ready()
    finally:
        await cog.cog_unload()

    sent_bodies = [
        (c.kwargs.get("content") or (c.args[0] if c.args else ""))
        for c in channel.send.call_args_list
    ]
    assert not any("pre-existing last answer" in b for b in sent_bodies), sent_bodies
    # Cursor seeded so a genuine drop on the *next* restart can be detected.
    repo.set_mirror_replied_uuid.assert_awaited_once_with(11, "u-final")


async def test_on_ready_does_not_redeliver_when_uuid_matches(
    monkeypatch: pytest.MonkeyPatch, tmp_path: Path
) -> None:
    """If the stored uuid already matches the last final answer, no re-delivery."""
    monkeypatch.setenv("CLORD_BRIDGE_MODE", "jsonl")
    monkeypatch.setenv("HOME", str(tmp_path))
    project = tmp_path / ".claude" / "projects" / "-some-cwd"
    project.mkdir(parents=True)
    (project / "s.jsonl").write_text(
        "\n".join(
            json.dumps(e, ensure_ascii=False)
            # #627: the mirror only follows a transcript c-lord itself drove.
            for e in [
                clord_marker_event(),
                {
                    "type": "assistant",
                    "uuid": "u-final",
                    "message": {"content": [{"type": "text", "text": "already delivered"}]},
                },
                {"type": "system", "subtype": "turn_duration"},
            ]
        )
        + "\n"
    )

    bot = MagicMock()
    channel = MagicMock()
    channel.send = AsyncMock()
    bot.get_channel.return_value = channel

    repo = _recovery_repo([_session_row(11, "/some/cwd", "u-final")])
    cog = TranscriptMirrorCog(bot, session_repo=repo)
    try:
        await cog.on_ready()
    finally:
        await cog.cog_unload()

    sent_bodies = [
        (c.kwargs.get("content") or (c.args[0] if c.args else ""))
        for c in channel.send.call_args_list
    ]
    assert not any("already delivered" in b for b in sent_bodies), sent_bodies
    repo.set_mirror_replied_uuid.assert_not_awaited()


# ----------------------------------------------------------------------
# #553: the #215 rescue must fire ONLY for answers that were really dropped.
# ----------------------------------------------------------------------


def _write_transcript(project: Path, events: list[dict]) -> None:
    project.mkdir(parents=True, exist_ok=True)
    # #627: the rescue only reads a transcript c-lord itself drove, so the
    # fixture opens with one of c-lord's marked prompts as a real one does.
    # ensure_ascii=False because Claude Code writes non-ASCII raw.
    (project / "s.jsonl").write_text(
        "\n".join(json.dumps(e, ensure_ascii=False) for e in [clord_marker_event(), *events])
        + "\n",
        encoding="utf-8",
    )


def _assistant_ev(uuid: str, text: str) -> dict:
    return {
        "type": "assistant",
        "uuid": uuid,
        "message": {"content": [{"type": "text", "text": text}]},
    }


def _cog_with_recovery(monkeypatch: pytest.MonkeyPatch, project: Path, posted: list[str]):
    """A cog whose recovery reads *project* and records reply-sink posts."""
    monkeypatch.setattr("c_lord.cogs.transcript_mirror.derive_project_dir", lambda _wd: project)
    bot = MagicMock()
    cog = TranscriptMirrorCog(bot, session_repo=MagicMock())
    cog._session_repo.set_mirror_replied_uuid = AsyncMock()

    async def _reply(text: str) -> None:
        posted.append(text)

    monkeypatch.setattr(cog, "_make_reply_sink", lambda _tid: _reply)
    return cog


async def test_no_recovery_after_a_restart_mid_turn(
    monkeypatch: pytest.MonkeyPatch, tmp_path: Path
) -> None:
    """AC3: the #553 shape end-to-end — nothing is re-posted.

    The cursor is on an intermediate message of the turn that was interrupted,
    which sits AFTER the last completed turn's (already delivered) final answer.
    """
    project = tmp_path / "proj"
    _write_transcript(
        project,
        [
            _assistant_ev("u-final", "4点とも答えます。"),
            {"type": "system", "subtype": "turn_duration"},
            {"type": "user", "uuid": "u-task", "message": {"content": "task-notification"}},
            _assistant_ev("u-mid", "#534 も CI 全 pass。"),
        ],
    )
    posted: list[str] = []
    cog = _cog_with_recovery(monkeypatch, project, posted)
    row = _row(1, str(tmp_path))
    row.mirror_replied_uuid = "u-mid"

    recovered = await cog._recover_final_answer(1, str(tmp_path), row)

    assert recovered is False
    assert posted == [], f"re-posted an already-delivered answer: {posted!r}"


async def test_still_recovers_a_genuinely_dropped_answer(
    monkeypatch: pytest.MonkeyPatch, tmp_path: Path
) -> None:
    """AC4: killing the false positive must not kill the rescue.

    A whole turn completed while the mirror was down: its final answer is newer
    than the cursor and was never posted.
    """
    project = tmp_path / "proj"
    _write_transcript(
        project,
        [
            _assistant_ev("u-old", "first turn answer"),
            {"type": "system", "subtype": "turn_duration"},
            {"type": "user", "uuid": "u-ask", "message": {"content": "next please"}},
            _assistant_ev("u-new", "second turn answer — never delivered"),
            {"type": "system", "subtype": "turn_duration"},
        ],
    )
    posted: list[str] = []
    cog = _cog_with_recovery(monkeypatch, project, posted)
    row = _row(2, str(tmp_path))
    row.mirror_replied_uuid = "u-old"

    recovered = await cog._recover_final_answer(2, str(tmp_path), row)

    assert recovered is True
    assert posted == ["second turn answer — never delivered"]
    cog._session_repo.set_mirror_replied_uuid.assert_awaited_with(2, "u-new")


# ── Issue #537: closed sessions are not mirrored on startup ──────────────


async def test_on_ready_skips_closed_sessions(
    monkeypatch: pytest.MonkeyPatch, tmp_path: Path, caplog: pytest.LogCaptureFixture
) -> None:
    """Issue #537: a session closed with ``!close-workspace`` keeps its row and
    its (often huge) transcript forever.  Startup must not mirror it — and must
    not pay the Issue #215 recovery scan for it either, which is what made the
    on_ready walk cost ~1 GB of parsing on the production host."""
    monkeypatch.setenv("CLORD_BRIDGE_MODE", "jsonl")
    monkeypatch.setenv("HOME", str(tmp_path))
    project = tmp_path / ".claude" / "projects" / "-some-cwd"
    project.mkdir(parents=True)
    (project / "s.jsonl").write_text(
        "\n".join(
            json.dumps(e, ensure_ascii=False)
            # #627: the mirror only follows a transcript c-lord itself drove.
            for e in [
                clord_marker_event(),
                {
                    "type": "assistant",
                    "uuid": "u-final",
                    "message": {"content": [{"type": "text", "text": "answer in a closed thread"}]},
                },
                {"type": "system", "subtype": "turn_duration"},
            ]
        )
        + "\n"
    )

    bot = MagicMock()
    channel = MagicMock()
    channel.send = AsyncMock()
    bot.get_channel.return_value = channel

    repo = _recovery_repo(
        [
            _session_row(11, "/some/cwd", "u-old"),
            _session_row(22, "/some/cwd", "u-old", closed_at="2026-08-01 10:00:00"),
        ]
    )
    cog = TranscriptMirrorCog(bot, session_repo=repo)
    with caplog.at_level(logging.INFO, logger="c_lord.cogs.transcript_mirror"):
        try:
            await cog.on_ready()
            mirrored = set(cog._mirrors)
        finally:
            await cog.cog_unload()

    assert 11 in mirrored  # open session still mirrored
    assert 22 not in mirrored  # closed session skipped
    # The closed row was not scanned for recovery either.
    assert [c.args for c in repo.set_mirror_replied_uuid.await_args_list] == [(11, "u-final")]
    # AC3: the row counts are visible in the log.
    startup_lines = [r.getMessage() for r in caplog.records if "session row(s)" in r.getMessage()]
    assert startup_lines, caplog.text
    assert "1 closed" in startup_lines[-1], startup_lines[-1]


# -- #539: the silence filler is on by default and opt-out ------------------


async def test_progress_line_is_wired_by_default(
    monkeypatch: pytest.MonkeyPatch, tmp_path: Path
) -> None:
    """Zero-Config: upgrading the package alone must turn #539 on."""
    monkeypatch.delenv("CLORD_TURN_PROGRESS", raising=False)
    monkeypatch.setenv("CLORD_BRIDGE_MODE", "jsonl")
    cog = TranscriptMirrorCog(MagicMock(), session_repo=_make_repo([]))

    assert cog._make_progress(123) is not None


async def test_progress_line_can_be_opted_out(
    monkeypatch: pytest.MonkeyPatch, tmp_path: Path
) -> None:
    monkeypatch.setenv("CLORD_TURN_PROGRESS", "0")
    cog = TranscriptMirrorCog(MagicMock(), session_repo=_make_repo([]))

    assert cog._make_progress(123) is None


async def test_progress_line_posts_silently(
    monkeypatch: pytest.MonkeyPatch, tmp_path: Path
) -> None:
    """It is a hint, not a notification — it must never ping the thread."""
    monkeypatch.delenv("CLORD_TURN_PROGRESS", raising=False)
    channel = MagicMock()
    channel.send = AsyncMock(return_value=MagicMock())
    bot = MagicMock()
    bot.get_channel.return_value = channel

    cog = TranscriptMirrorCog(bot, session_repo=_make_repo([]))
    progress = cog._make_progress(123)
    assert progress is not None
    progress.begin_turn()
    progress._last_output -= 10_000  # pretend the thread has been quiet
    await progress.tick()

    channel.send.assert_awaited_once()
    assert channel.send.await_args.kwargs.get("silent") is True


async def test_progress_quiet_threshold_is_configurable(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    monkeypatch.delenv("CLORD_TURN_PROGRESS", raising=False)
    monkeypatch.setenv("CLORD_TURN_PROGRESS_QUIET_SECONDS", "45")
    cog = TranscriptMirrorCog(MagicMock(), session_repo=_make_repo([]))

    progress = cog._make_progress(123)
    assert progress is not None
    assert progress._quiet_seconds == 45.0
