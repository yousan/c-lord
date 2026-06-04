"""Tests for the manual /discord-screenshot command (#243).

The manual twin of the auto-capture: renders the thread to a PNG on demand.
Rendering is monkeypatched; pixel output is covered by test_conversation_renderer.
"""

from __future__ import annotations

from unittest.mock import AsyncMock, MagicMock

import discord

from c_lord.discord_ui.conversation_renderer import ConvMessage


def _make_cog():
    from c_lord.cogs.session_manage import SessionManageCog

    bot = MagicMock()
    bot.channel_id = 999
    repo = MagicMock()
    repo.get = AsyncMock(return_value=None)
    repo.list_all = AsyncMock(return_value=[])
    return SessionManageCog(bot=bot, repo=repo)


def _capture_responder():
    sent: list[dict] = []

    async def respond(content=None, *, embed=None, file=None, ephemeral=False):
        sent.append({"content": content, "embed": embed, "file": file, "ephemeral": ephemeral})

    async def ack(*, ephemeral=False):
        return None

    return respond, ack, sent


def _thread(thread_id: int = 321) -> MagicMock:
    t = MagicMock(spec=discord.Thread)
    t.id = thread_id
    t.name = "evi-thread"
    return t


def _channel_interaction() -> MagicMock:
    interaction = MagicMock(spec=discord.Interaction)
    interaction.channel = MagicMock(spec=discord.TextChannel)
    interaction.response = MagicMock()
    interaction.response.send_message = AsyncMock()
    return interaction


class TestDiscordScreenshot:
    async def test_outside_thread_sends_ephemeral(self) -> None:
        cog = _make_cog()
        interaction = _channel_interaction()
        await cog.discord_screenshot.callback(cog, interaction)
        call_args = interaction.response.send_message.call_args
        assert call_args.kwargs.get("ephemeral") is True

    async def test_impl_sends_png_file(self, monkeypatch) -> None:
        import c_lord.cogs.session_manage as sm

        cog = _make_cog()
        monkeypatch.setattr(
            sm, "fetch_conversation", AsyncMock(return_value=[ConvMessage(author="a")])
        )
        captured = {}

        def fake_render(messages, *, engine="pillow", title=None):
            captured["engine"] = engine
            captured["title"] = title
            return b"\x89PNG\r\n\x1a\nDATA"

        monkeypatch.setattr(sm, "render_conversation", fake_render)

        respond, ack, sent = _capture_responder()
        await cog._discord_screenshot_impl(
            channel=_thread(), respond=respond, ack=ack, engine="html"
        )

        files = [s["file"] for s in sent if s["file"] is not None]
        assert len(files) == 1
        assert isinstance(files[0], discord.File)
        assert files[0].filename == "discord-321.png"
        assert captured["engine"] == "html"
        assert "evi-thread" in captured["title"]

    async def test_impl_empty_thread_sends_ephemeral(self, monkeypatch) -> None:
        import c_lord.cogs.session_manage as sm

        cog = _make_cog()
        monkeypatch.setattr(sm, "fetch_conversation", AsyncMock(return_value=[]))

        respond, ack, sent = _capture_responder()
        await cog._discord_screenshot_impl(channel=_thread(), respond=respond, ack=ack)

        assert sent and sent[-1]["ephemeral"] is True
        assert all(s["file"] is None for s in sent)

    async def test_impl_render_unavailable_sends_ephemeral(self, monkeypatch) -> None:
        import c_lord.cogs.session_manage as sm

        cog = _make_cog()
        monkeypatch.setattr(
            sm, "fetch_conversation", AsyncMock(return_value=[ConvMessage(author="a")])
        )
        monkeypatch.setattr(sm, "render_conversation", lambda messages, **kw: None)

        respond, ack, sent = _capture_responder()
        await cog._discord_screenshot_impl(channel=_thread(), respond=respond, ack=ack)

        assert sent and sent[-1]["ephemeral"] is True
        assert all(s["file"] is None for s in sent)
        assert "c-lord[table]" in (sent[-1]["content"] or "")
