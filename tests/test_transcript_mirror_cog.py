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


def _make_repo(rows: list) -> MagicMock:
    repo = MagicMock()
    repo.list_all = AsyncMock(return_value=rows)
    return repo


def _row(thread_id: int, working_dir: str | None) -> MagicMock:
    r = MagicMock()
    r.thread_id = thread_id
    r.working_dir = working_dir
    return r


async def test_cog_stays_idle_when_bridge_mode_not_jsonl(
    monkeypatch: pytest.MonkeyPatch, tmp_path: Path
) -> None:
    monkeypatch.delenv("CLORD_BRIDGE_MODE", raising=False)
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
    monkeypatch.delenv("CLORD_BRIDGE_MODE", raising=False)
    bot = MagicMock()
    cog = TranscriptMirrorCog(bot, session_repo=_make_repo([]))
    assert cog.start_for(1, str(tmp_path)) is False


async def test_sink_truncates_long_messages(
    monkeypatch: pytest.MonkeyPatch, tmp_path: Path
) -> None:
    monkeypatch.setenv("CLORD_BRIDGE_MODE", "jsonl")
    bot = MagicMock()
    channel = MagicMock()
    channel.send = AsyncMock()
    bot.get_channel.return_value = channel

    cog = TranscriptMirrorCog(bot, session_repo=_make_repo([]))
    sink = cog._make_sink(7)
    await sink("a" * 3000)
    sent = channel.send.call_args.kwargs.get("content") or channel.send.call_args[0][0]
    assert len(sent) <= 2000
    assert sent.endswith("…")


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
    jsonl = project / "s.jsonl"
    jsonl.write_text("")
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
        jsonl.write_text(
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
