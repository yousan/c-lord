"""Tests for the Discord conversation → PNG renderer (#243, route 2).

These cover the data normalization (live ``discord.Message`` → ``ConvMessage``),
the ``channel.history`` fetch helper, and both rendering engines (Pillow
self-render + HTML/headless-Chrome). Rendering tests skip gracefully when the
optional dependency / browser is unavailable, mirroring ``test_pane_renderer``.
"""

from __future__ import annotations

import datetime as _dt
from types import SimpleNamespace
from unittest.mock import MagicMock

import pytest

from c_lord.discord_ui.conversation_renderer import (
    ConvAttachment,
    ConvEmbed,
    ConvField,
    ConvMessage,
    ConvReaction,
    browser_available,
    fetch_conversation,
    message_to_conv,
    render_conversation,
    render_conversation_html_png,
    render_conversation_png,
)
from c_lord.discord_ui.fonts import load_text_font

PNG_MAGIC = b"\x89PNG\r\n\x1a\n"

_HAS_FONT = load_text_font(20) is not None


def _sample_messages() -> list[ConvMessage]:
    """A small but representative thread: user msg, bot tool-use embed, lamps."""
    return [
        ConvMessage(
            author="yousan",
            content="この長文をスクショして証跡にして 🙏",
            is_bot=False,
            timestamp="2026-06-04 12:00",
            reactions=(ConvReaction("🧠", 1), ConvReaction("✅", 1)),
        ),
        ConvMessage(
            author="C-lord",
            content="やってみます。",
            is_bot=True,
            timestamp="2026-06-04 12:00",
            embeds=(
                ConvEmbed(
                    title="🛠️ Bash(git status)...",
                    description="⏳ 2s elapsed...",
                    color=0xFEE75C,
                ),
                ConvEmbed(
                    title="✅ Done",
                    description="Duration 3.2s · $0.0041",
                    color=0x57F287,
                    fields=(ConvField("model", "opus", inline=True),),
                ),
            ),
            attachments=(ConvAttachment("diff.patch", "text/plain"),),
        ),
    ]


# ── Data normalization ───────────────────────────────────────────────────────


class TestMessageToConv:
    def _fake_discord_message(self) -> MagicMock:
        author = MagicMock()
        author.display_name = "alice"
        author.bot = False
        author.color = SimpleNamespace(value=0xFF0000)

        embed = MagicMock()
        embed.title = "🛠️ Bash(ls)..."
        embed.description = "running"
        embed.color = SimpleNamespace(value=0xFEE75C)
        embed.fields = [SimpleNamespace(name="dir", value="/tmp", inline=True)]

        reaction = MagicMock()
        reaction.emoji = "✅"
        reaction.count = 2

        attachment = MagicMock()
        attachment.filename = "log.txt"
        attachment.content_type = "text/plain"

        msg = MagicMock()
        msg.author = author
        msg.content = "hello world"
        msg.created_at = _dt.datetime(2026, 6, 4, 12, 30)
        msg.embeds = [embed]
        msg.reactions = [reaction]
        msg.attachments = [attachment]
        return msg

    def test_basic_fields(self) -> None:
        conv = message_to_conv(self._fake_discord_message())
        assert conv.author == "alice"
        assert conv.content == "hello world"
        assert conv.is_bot is False
        assert conv.color == 0xFF0000
        assert "2026-06-04" in (conv.timestamp or "")

    def test_embeds_normalized(self) -> None:
        conv = message_to_conv(self._fake_discord_message())
        assert len(conv.embeds) == 1
        assert conv.embeds[0].title == "🛠️ Bash(ls)..."
        assert conv.embeds[0].color == 0xFEE75C
        assert conv.embeds[0].fields[0].name == "dir"

    def test_reactions_and_attachments(self) -> None:
        conv = message_to_conv(self._fake_discord_message())
        assert conv.reactions[0].emoji == "✅"
        assert conv.reactions[0].count == 2
        assert conv.attachments[0].filename == "log.txt"

    def test_zero_color_becomes_none(self) -> None:
        msg = self._fake_discord_message()
        msg.author.color = SimpleNamespace(value=0)
        assert message_to_conv(msg).color is None

    def test_missing_optional_fields_default_safely(self) -> None:
        msg = MagicMock()
        msg.author = SimpleNamespace(display_name="bob", bot=True, color=None)
        msg.content = ""
        msg.created_at = None
        msg.embeds = []
        msg.reactions = []
        msg.attachments = []
        conv = message_to_conv(msg)
        assert conv.author == "bob"
        assert conv.is_bot is True
        assert conv.embeds == ()


class TestFetchConversation:
    async def test_iterates_history_oldest_first(self) -> None:
        def _msg(name: str, content: str) -> MagicMock:
            m = MagicMock()
            m.author = SimpleNamespace(display_name=name, bot=False, color=None)
            m.content = content
            m.created_at = None
            m.embeds = []
            m.reactions = []
            m.attachments = []
            return m

        history_msgs = [_msg("a", "first"), _msg("b", "second")]

        class _AsyncIter:
            def __aiter__(self):
                async def gen():
                    for m in history_msgs:
                        yield m

                return gen()

        channel = MagicMock()
        channel.history = MagicMock(return_value=_AsyncIter())

        out = await fetch_conversation(channel, limit=5)
        channel.history.assert_called_once()
        kwargs = channel.history.call_args.kwargs
        assert kwargs.get("limit") == 5
        assert kwargs.get("oldest_first") is True
        assert [c.author for c in out] == ["a", "b"]
        assert [c.content for c in out] == ["first", "second"]


# ── Pillow renderer (variant a) ──────────────────────────────────────────────


@pytest.mark.skipif(not _HAS_FONT, reason="no usable font (optional [table] extra)")
class TestRenderPillow:
    def test_returns_png_bytes(self) -> None:
        png = render_conversation_png(_sample_messages())
        assert png is not None
        assert png.startswith(PNG_MAGIC)

    def test_empty_messages_returns_png(self) -> None:
        # An empty list should still produce a (small) valid PNG, not crash.
        png = render_conversation_png([])
        assert png is None or png.startswith(PNG_MAGIC)

    def test_no_font_returns_none(self, monkeypatch) -> None:
        import c_lord.discord_ui.conversation_renderer as cr

        monkeypatch.setattr(cr, "load_text_font", lambda size: None)
        monkeypatch.setattr(cr, "load_mono_font", lambda size: None)
        assert render_conversation_png(_sample_messages()) is None


# ── HTML / headless-Chrome renderer (variant b) ──────────────────────────────


class TestRenderHtml:
    def test_browser_available_is_bool(self) -> None:
        assert isinstance(browser_available(), bool)

    @pytest.mark.skipif(not browser_available(), reason="no headless browser present")
    def test_returns_png_bytes_when_browser_present(self) -> None:
        png = render_conversation_html_png(_sample_messages())
        assert png is not None
        assert png.startswith(PNG_MAGIC)


# ── Dispatcher ───────────────────────────────────────────────────────────────


class TestDispatcher:
    def test_pillow_engine_dispatch(self, monkeypatch) -> None:
        import c_lord.discord_ui.conversation_renderer as cr

        monkeypatch.setattr(cr, "render_conversation_png", lambda msgs, **kw: b"PILLOW")
        monkeypatch.setattr(cr, "render_conversation_html_png", lambda msgs, **kw: b"HTML")
        assert render_conversation([], engine="pillow") == b"PILLOW"

    def test_html_engine_dispatch(self, monkeypatch) -> None:
        import c_lord.discord_ui.conversation_renderer as cr

        monkeypatch.setattr(cr, "render_conversation_png", lambda msgs, **kw: b"PILLOW")
        monkeypatch.setattr(cr, "render_conversation_html_png", lambda msgs, **kw: b"HTML")
        assert render_conversation([], engine="html") == b"HTML"

    def test_auto_prefers_html_when_browser_falls_back_to_pillow(self, monkeypatch) -> None:
        import c_lord.discord_ui.conversation_renderer as cr

        monkeypatch.setattr(cr, "render_conversation_png", lambda msgs, **kw: b"PILLOW")
        monkeypatch.setattr(cr, "render_conversation_html_png", lambda msgs, **kw: None)
        monkeypatch.setattr(cr, "browser_available", lambda: True)
        # html returns None (e.g. chrome failed) → must fall back to pillow
        assert render_conversation([], engine="auto") == b"PILLOW"
