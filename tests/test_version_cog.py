"""Tests for VersionCog (/version slash command + !version text twin).

Discord-dependent code — uses mocks (30%+ coverage target per CLAUDE.md).
Verifies the command is registered and that the shared core posts the
article-format version string via an embed.
"""

from __future__ import annotations

from unittest.mock import AsyncMock, MagicMock

import pytest

from c_lord.cogs.version_cmd import VersionCog, version_embed


def test_version_embed_contains_version() -> None:
    embed = version_embed("v1.4.0-b599631-20251203")
    assert embed.description is not None
    assert "v1.4.0-b599631-20251203" in embed.description


def test_cog_registers_slash_and_text_commands() -> None:
    cog = VersionCog(MagicMock())
    # Slash command present
    slash_names = {c.name for c in cog.get_app_commands()}
    assert "version" in slash_names
    # Text command twin present (webhook-invokable for E2E)
    text_names = {c.name for c in cog.get_commands()}
    assert "version" in text_names


@pytest.mark.asyncio
async def test_version_impl_posts_embed(monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.setattr(
        "c_lord.cogs.version_cmd.resolve_version",
        lambda: "v9.9.9-bdeadbee-20260529",
    )
    cog = VersionCog(MagicMock())
    sent: dict[str, object] = {}

    async def respond(content: str | None = None, *, embed=None, ephemeral: bool = False) -> None:
        sent["embed"] = embed

    await cog._version_impl(respond=respond)
    embed = sent["embed"]
    assert embed is not None
    assert "v9.9.9-bdeadbee-20260529" in embed.description  # type: ignore[union-attr]


@pytest.mark.asyncio
async def test_slash_io_sends_embed(monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.setattr(
        "c_lord.cogs.version_cmd.resolve_version",
        lambda: "v1.0.0-bcafef00-20260529",
    )
    cog = VersionCog(MagicMock())
    interaction = MagicMock()
    send_message = AsyncMock()
    interaction.response.send_message = send_message

    # Drive the shared core through the real slash I/O adapter.
    await cog._version_impl(respond=cog._slash_io(interaction))

    send_message.assert_awaited_once()
    call = send_message.await_args
    assert call is not None
    assert "v1.0.0-bcafef00-20260529" in call.kwargs["embed"].description
