"""Tests for the Cog side of ``SendUserFile`` delivery (Issue #233).

What the mirror detects, this sink has to actually put on Discord: real
filenames, the caption as the body, every file delivered even when one of them
is broken — and above all, a file that could **not** be delivered has to say so
in the thread.  The bug being fixed is not "files fail", it is "files fail in
silence while the tool reports success".
"""

from __future__ import annotations

import logging
from pathlib import Path
from unittest.mock import AsyncMock, MagicMock

import discord
import pytest

from c_lord.cogs.transcript_mirror import TranscriptMirrorCog
from c_lord.transcript.mirror import UserFileRequest


def _make_repo() -> MagicMock:
    repo = MagicMock()
    repo.list_all = AsyncMock(return_value=[])
    return repo


def _cog_with_channel(monkeypatch: pytest.MonkeyPatch) -> tuple[TranscriptMirrorCog, MagicMock]:
    monkeypatch.setenv("CLORD_BRIDGE_MODE", "jsonl")
    bot = MagicMock()
    channel = MagicMock()
    channel.send = AsyncMock()
    bot.get_channel.return_value = channel
    return TranscriptMirrorCog(bot, session_repo=_make_repo()), channel


def _sink(cog: TranscriptMirrorCog):
    """The SendUserFile sink, asserted present (it is opt-out, not opt-in)."""
    sink = cog._make_user_file_sink(7)
    assert sink is not None
    return sink


def _png(tmp_path: Path, name: str, size: int = 32) -> str:
    path = tmp_path / name
    path.write_bytes(b"\x89PNG" + b"0" * size)
    return str(path)


def _sent_filenames(channel: MagicMock) -> list[str]:
    names: list[str] = []
    for call in channel.send.call_args_list:
        for f in call.kwargs.get("files") or []:
            names.append(f.filename)
    return names


def _sent_bodies(channel: MagicMock) -> list[str]:
    return [
        call.kwargs.get("content") or (call.args[0] if call.args else "")
        for call in channel.send.call_args_list
    ]


async def test_files_are_attached_with_their_real_names(
    monkeypatch: pytest.MonkeyPatch, tmp_path: Path
) -> None:
    cog, channel = _cog_with_channel(monkeypatch)
    sink = _sink(cog)

    await sink(
        UserFileRequest(
            tool_use_id="t1",
            paths=[_png(tmp_path, "02-same-image-detail-loss.png"), _png(tmp_path, "report.pdf")],
            caption="差③の比較です",
        )
    )

    assert _sent_filenames(channel) == ["02-same-image-detail-loss.png", "report.pdf"]
    assert any("差③の比較です" in body for body in _sent_bodies(channel))


async def test_no_caption_sends_the_files_alone(
    monkeypatch: pytest.MonkeyPatch, tmp_path: Path
) -> None:
    cog, channel = _cog_with_channel(monkeypatch)
    sink = _sink(cog)

    await sink(UserFileRequest(tool_use_id="t1", paths=[_png(tmp_path, "a.png")], caption=None))

    channel.send.assert_called_once()
    assert _sent_filenames(channel) == ["a.png"]
    assert not (channel.send.call_args.kwargs.get("content") or "").strip()


async def test_more_than_ten_files_are_split_and_none_are_lost(
    monkeypatch: pytest.MonkeyPatch, tmp_path: Path
) -> None:
    """Discord takes 10 attachments per message; 12 files must still all arrive."""
    cog, channel = _cog_with_channel(monkeypatch)
    sink = _sink(cog)
    paths = [_png(tmp_path, f"shot-{i:02d}.png") for i in range(12)]

    await sink(UserFileRequest(tool_use_id="t1", paths=paths, caption="12枚"))

    assert channel.send.call_count == 2
    per_message = [len(c.kwargs.get("files") or []) for c in channel.send.call_args_list]
    assert per_message == [10, 2]
    assert _sent_filenames(channel) == [f"shot-{i:02d}.png" for i in range(12)]


async def test_missing_file_is_reported_in_the_thread(
    monkeypatch: pytest.MonkeyPatch, tmp_path: Path, caplog: pytest.LogCaptureFixture
) -> None:
    """The whole point of #233: a file that did not make it must say so."""
    cog, channel = _cog_with_channel(monkeypatch)
    sink = _sink(cog)
    good = _png(tmp_path, "good.png")

    with caplog.at_level(logging.INFO, logger="c_lord.cogs.transcript_mirror"):
        await sink(
            UserFileRequest(
                tool_use_id="t1",
                paths=[good, str(tmp_path / "gone.png")],
                caption="1枚だけ実在します",
            )
        )

    # The good one still arrives.
    assert _sent_filenames(channel) == ["good.png"]
    # And the missing one is named, with a reason, in the thread.
    warning = "\n".join(_sent_bodies(channel))
    assert "gone.png" in warning
    assert "⚠️" in warning
    # Never only in DEBUG (#678).
    assert any("gone.png" in r.getMessage() for r in caplog.records)


async def test_oversize_file_is_reported_not_uploaded(
    monkeypatch: pytest.MonkeyPatch, tmp_path: Path
) -> None:
    monkeypatch.setenv("CLORD_USER_FILE_MAX_BYTES", "1024")
    cog, channel = _cog_with_channel(monkeypatch)
    sink = _sink(cog)
    big = tmp_path / "huge.bin"
    big.write_bytes(b"0" * 4096)

    await sink(UserFileRequest(tool_use_id="t1", paths=[str(big)], caption=None))

    assert _sent_filenames(channel) == []
    body = "\n".join(_sent_bodies(channel))
    assert "huge.bin" in body and "⚠️" in body


async def test_one_rejected_file_does_not_take_the_others_down(
    monkeypatch: pytest.MonkeyPatch, tmp_path: Path
) -> None:
    """A batch Discord rejects is retried file by file, so the good ones land."""
    cog, channel = _cog_with_channel(monkeypatch)
    sink = _sink(cog)
    paths = [_png(tmp_path, "a.png"), _png(tmp_path, "b.png")]

    calls: list[list[str]] = []

    async def send(**kwargs):
        names = [f.filename for f in kwargs.get("files") or []]
        calls.append(names)
        if len(names) > 1 or names == ["b.png"]:
            raise discord.HTTPException(MagicMock(status=413), "Payload Too Large")
        return MagicMock()

    channel.send = AsyncMock(side_effect=send)

    await sink(UserFileRequest(tool_use_id="t1", paths=paths, caption=None))

    # batch of 2 rejected → retried individually: a.png lands, b.png reported.
    assert ["a.png", "b.png"] in calls
    assert ["a.png"] in calls
    body = "\n".join(
        (c.kwargs.get("content") or "")
        for c in channel.send.call_args_list
        if not c.kwargs.get("files")
    )
    assert "b.png" in body and "⚠️" in body


async def test_disabled_by_env_returns_no_sink(monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.setenv("CLORD_SEND_USER_FILE", "0")
    cog, _channel = _cog_with_channel(monkeypatch)
    assert cog._make_user_file_sink(7) is None


async def test_enabled_by_default(monkeypatch: pytest.MonkeyPatch) -> None:
    """Zero-Config: consumers get this by upgrading the package alone."""
    monkeypatch.delenv("CLORD_SEND_USER_FILE", raising=False)
    cog, _channel = _cog_with_channel(monkeypatch)
    assert cog._make_user_file_sink(7) is not None


async def test_display_name_is_sanitised(monkeypatch: pytest.MonkeyPatch, tmp_path: Path) -> None:
    """The attachment name shown in Discord is one harmless path component."""
    cog, channel = _cog_with_channel(monkeypatch)
    sink = _sink(cog)
    nested = tmp_path / "sub"
    nested.mkdir()
    path = _png(nested, ".hidden.png")

    await sink(UserFileRequest(tool_use_id="t1", paths=[path], caption=None))

    assert _sent_filenames(channel) == ["_.hidden.png"]


async def test_posts_are_silent(monkeypatch: pytest.MonkeyPatch, tmp_path: Path) -> None:
    """Files ride along a turn; the turn's final answer is what pings."""
    cog, channel = _cog_with_channel(monkeypatch)
    sink = _sink(cog)

    await sink(UserFileRequest(tool_use_id="t1", paths=[_png(tmp_path, "a.png")], caption="x"))

    assert channel.send.call_args.kwargs.get("silent") is True


async def test_a_directory_is_reported_rather_than_sent(
    monkeypatch: pytest.MonkeyPatch, tmp_path: Path
) -> None:
    cog, channel = _cog_with_channel(monkeypatch)
    sink = _sink(cog)
    d = tmp_path / "adir"
    d.mkdir()

    await sink(UserFileRequest(tool_use_id="t1", paths=[str(d)], caption=None))

    assert _sent_filenames(channel) == []
    assert "adir" in "\n".join(_sent_bodies(channel))


async def test_missing_channel_does_not_raise(
    monkeypatch: pytest.MonkeyPatch, tmp_path: Path
) -> None:
    monkeypatch.setenv("CLORD_BRIDGE_MODE", "jsonl")
    bot = MagicMock()
    bot.get_channel.return_value = None
    bot.fetch_channel = AsyncMock(side_effect=discord.NotFound(MagicMock(status=404), "gone"))
    cog = TranscriptMirrorCog(bot, session_repo=_make_repo())
    sink = _sink(cog)

    await sink(UserFileRequest(tool_use_id="t1", paths=[_png(tmp_path, "a.png")], caption=None))
