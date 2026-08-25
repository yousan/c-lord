"""Attachments reach Claude as files, and a dropped one is never silent (#528).

The reported symptom was "マークダウンを添付したら見つからないと言われる": the file
was inlined into the prompt when it was small enough and thrown away when it
was not, and either way Claude was never handed something it could ``Read``.
Nothing in Discord said a file had been dropped.

So: every attachment is written next to the checkout and the prompt carries its
path — and anything that could not be handed over is named, with a reason, in
the thread.
"""

from __future__ import annotations

from pathlib import Path
from unittest.mock import AsyncMock, MagicMock

import discord
import pytest

from c_lord.cogs.claude_chat import ClaudeChatCog


def _make_cog() -> ClaudeChatCog:
    bot = MagicMock()
    bot.channel_id = 999
    bot.settings_repo = None
    bot.get_cog = MagicMock(return_value=None)
    repo = MagicMock()
    repo.get = AsyncMock(return_value=None)
    runner = MagicMock()
    return ClaudeChatCog(bot=bot, repo=repo, runner=runner)


def _attachment(
    filename: str = "notes.txt",
    content_type: str = "text/plain",
    content: bytes = b"hello",
    size: int | None = None,
) -> MagicMock:
    att = MagicMock(spec=discord.Attachment)
    att.filename = filename
    att.content_type = content_type
    att.size = len(content) if size is None else size
    att.url = f"https://cdn.discordapp.com/attachments/1/2/{filename}?ex=a&is=b&hm=c"
    att.read = AsyncMock(return_value=content)
    return att


def _message(attachments: list, message_id: int = 555) -> MagicMock:
    msg = MagicMock(spec=discord.Message)
    msg.id = message_id
    msg.content = "見てほしい"
    msg.attachments = attachments
    return msg


def _thread() -> MagicMock:
    thread = MagicMock(spec=discord.Thread)
    thread.send = AsyncMock()
    return thread


def _notices(thread: MagicMock) -> str:
    return "\n".join(str(call.args[0]) for call in thread.send.await_args_list if call.args)


# ── the happy path ──────────────────────────────────────────────────


@pytest.mark.asyncio
async def test_attachment_is_written_to_disk_and_referenced_by_path(tmp_path: Path) -> None:
    cog, thread = _make_cog(), _thread()
    att = _attachment(filename="spec.md", content=b"# spec\nbody\n")

    section = await cog._stage_attachments(_message([att]), str(tmp_path), thread)

    saved = tmp_path / ".clord" / "attachments" / "555" / "spec.md"
    assert saved.read_bytes() == b"# spec\nbody\n"
    assert str(saved.resolve()) in section, "the prompt must name the path Claude should Read"
    assert "spec.md" in section
    thread.send.assert_not_awaited()


@pytest.mark.asyncio
async def test_file_contents_are_not_pasted_into_the_prompt(tmp_path: Path) -> None:
    """Inlining is what blew past tmux's input cap (#527) — it must be gone."""
    cog, thread = _make_cog(), _thread()
    att = _attachment(filename="spec.md", content=b"SECRET-BODY-MARKER")

    section = await cog._stage_attachments(_message([att]), str(tmp_path), thread)

    assert "SECRET-BODY-MARKER" not in section


@pytest.mark.asyncio
async def test_images_are_saved_as_files_not_cdn_urls(tmp_path: Path) -> None:
    """A local path beats a signed CDN URL: it cannot expire and cannot be
    mangled on the way through the shell (#529)."""
    cog, thread = _make_cog(), _thread()
    att = _attachment(filename="shot.png", content_type="image/png", content=b"\x89PNG\r\n")

    section = await cog._stage_attachments(_message([att]), str(tmp_path), thread)

    assert (tmp_path / ".clord" / "attachments" / "555" / "shot.png").exists()
    assert "cdn.discordapp.com" not in section


@pytest.mark.asyncio
async def test_no_attachments_produces_nothing(tmp_path: Path) -> None:
    cog, thread = _make_cog(), _thread()
    assert await cog._stage_attachments(_message([]), str(tmp_path), thread) == ""
    thread.send.assert_not_awaited()


# ── the size cap that used to eat files ─────────────────────────────


@pytest.mark.asyncio
async def test_a_file_past_the_old_50kb_cap_is_still_delivered(tmp_path: Path) -> None:
    """The regression that started this: 50KB+ used to vanish without a word."""
    cog, thread = _make_cog(), _thread()
    body = ("あ" * 40_000).encode()  # 120KB — well past the old caps
    att = _attachment(filename="big.md", content=body)

    section = await cog._stage_attachments(_message([att]), str(tmp_path), thread)

    assert (tmp_path / ".clord" / "attachments" / "555" / "big.md").read_bytes() == body
    assert "big.md" in section
    thread.send.assert_not_awaited()


# ── nothing is ever dropped in silence ──────────────────────────────


@pytest.mark.asyncio
async def test_an_oversized_file_is_reported_by_name_and_reason(tmp_path: Path) -> None:
    cog, thread = _make_cog(), _thread()
    huge = _attachment(filename="huge.bin", content=b"x", size=200 * 1024 * 1024)

    section = await cog._stage_attachments(_message([huge]), str(tmp_path), thread)

    assert "huge.bin" not in section
    notice = _notices(thread)
    assert "huge.bin" in notice, "a dropped attachment must be named in the thread"
    assert "⚠️" in notice


@pytest.mark.asyncio
async def test_extra_attachments_beyond_the_cap_are_reported(tmp_path: Path) -> None:
    cog, thread = _make_cog(), _thread()
    atts = [_attachment(filename=f"f{i}.txt", content=b"x") for i in range(12)]

    section = await cog._stage_attachments(_message(atts), str(tmp_path), thread)

    saved = sorted(p.name for p in (tmp_path / ".clord" / "attachments" / "555").iterdir())
    assert len(saved) == 10
    assert "f10.txt" not in section
    notice = _notices(thread)
    assert "f10.txt" in notice and "f11.txt" in notice


@pytest.mark.asyncio
async def test_a_download_failure_is_reported(tmp_path: Path) -> None:
    cog, thread = _make_cog(), _thread()
    att = _attachment(filename="flaky.txt")
    att.read = AsyncMock(side_effect=discord.HTTPException(MagicMock(status=500), "boom"))

    section = await cog._stage_attachments(_message([att]), str(tmp_path), thread)

    assert section == ""
    assert "flaky.txt" in _notices(thread)


@pytest.mark.asyncio
async def test_one_bad_attachment_does_not_lose_the_good_ones(tmp_path: Path) -> None:
    cog, thread = _make_cog(), _thread()
    bad = _attachment(filename="huge.bin", content=b"x", size=200 * 1024 * 1024)
    good = _attachment(filename="ok.md", content=b"fine")

    section = await cog._stage_attachments(_message([bad, good]), str(tmp_path), thread)

    assert "ok.md" in section
    assert (tmp_path / ".clord" / "attachments" / "555" / "ok.md").exists()
    assert "huge.bin" in _notices(thread)


# ── the checkout stays clean ────────────────────────────────────────


@pytest.mark.asyncio
async def test_saved_attachments_are_git_excluded(tmp_path: Path) -> None:
    import subprocess

    subprocess.run(["git", "init", "-q", str(tmp_path)], check=True)
    cog, thread = _make_cog(), _thread()

    await cog._stage_attachments(_message([_attachment()]), str(tmp_path), thread)

    status = subprocess.run(
        ["git", "-C", str(tmp_path), "status", "--porcelain"],
        capture_output=True,
        text=True,
        check=True,
    )
    assert ".clord" not in status.stdout


# ── a thread with nowhere to write must not crash the turn ──────────


@pytest.mark.asyncio
async def test_missing_work_dir_reports_instead_of_raising() -> None:
    cog, thread = _make_cog(), _thread()

    section = await cog._stage_attachments(_message([_attachment()]), None, thread)

    assert section == ""
    assert "notes.txt" in _notices(thread)
