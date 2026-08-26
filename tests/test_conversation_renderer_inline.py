"""Inline field layout and the emoji-extra guard — Issue #588.

``scripts/discord_mockup.py`` exists so a design can be *adjudicated* from a
picture (#316). Two defects made the picture disagree with what Discord would
actually show, which defeats the point:

* without the ``emoji`` library every emoji was drawn with the body font, i.e.
  as tofu, and the renderer reported success anyway, and
* ``ConvField.inline`` was parsed but never used, so fields Discord lays out
  three-to-a-row were stacked vertically — a 2-row embed rendered as 10 rows.
"""

from __future__ import annotations

import pytest

from c_lord.discord_ui.conversation_renderer import (
    ConvEmbed,
    ConvField,
    ConvMessage,
    group_fields_into_rows,
    render_conversation_png,
)
from c_lord.discord_ui.fonts import load_text_font

pytestmark = pytest.mark.skipif(
    load_text_font(16) is None, reason="optional [table] extra (Pillow/font) not installed"
)


def _f(name: str, *, inline: bool) -> ConvField:
    return ConvField(name=name, value="v", inline=inline)


class TestGrouping:
    def test_three_inline_fields_share_one_row(self) -> None:
        rows = group_fields_into_rows([_f(n, inline=True) for n in "abc"])
        assert [[f.name for f in r] for r in rows] == [["a", "b", "c"]]

    def test_a_fourth_inline_field_wraps_to_the_next_row(self) -> None:
        """Discord caps an inline row at three."""
        rows = group_fields_into_rows([_f(n, inline=True) for n in "abcd"])
        assert [[f.name for f in r] for r in rows] == [["a", "b", "c"], ["d"]]

    def test_non_inline_field_takes_a_whole_row(self) -> None:
        rows = group_fields_into_rows([_f("solo", inline=False)])
        assert [[f.name for f in r] for r in rows] == [["solo"]]

    def test_non_inline_field_breaks_the_inline_run(self) -> None:
        """``a b | wide | c`` — the wide field must not be absorbed into a row."""
        fields = [
            _f("a", inline=True),
            _f("b", inline=True),
            _f("wide", inline=False),
            _f("c", inline=True),
        ]
        rows = group_fields_into_rows(fields)
        assert [[f.name for f in r] for r in rows] == [["a", "b"], ["wide"], ["c"]]

    def test_no_fields_is_no_rows(self) -> None:
        assert group_fields_into_rows([]) == []


class TestInlineIsActuallyShorter:
    def _png_height(self, inline: bool) -> int:
        from io import BytesIO

        from PIL import Image

        fields = [ConvField(name=f"n{i}", value=f"v{i}", inline=inline) for i in range(6)]
        msg = ConvMessage(
            author="C-lord", content="x", embeds=(ConvEmbed(title="t", fields=tuple(fields)),)
        )
        png = render_conversation_png([msg])
        assert png is not None
        return Image.open(BytesIO(png)).height

    def test_inline_embed_is_shorter_than_stacked(self) -> None:
        """Six fields: 2 rows inline vs 6 rows stacked."""
        assert self._png_height(inline=True) < self._png_height(inline=False)


class TestEmojiExtraGuard:
    def test_emoji_spec_without_the_library_fails_loudly(self, monkeypatch) -> None:
        """Silently emitting tofu is worse than refusing: the PNG is evidence."""
        monkeypatch.setattr(
            "c_lord.discord_ui.conversation_renderer.emoji_support_available", lambda: False
        )
        msg = ConvMessage(author="C-lord", content="💤 スリープしました")

        assert render_conversation_png([msg]) is None

    def test_emoji_free_spec_still_renders_without_the_library(self, monkeypatch) -> None:
        """Only specs that actually contain emoji need the extra."""
        monkeypatch.setattr(
            "c_lord.discord_ui.conversation_renderer.emoji_support_available", lambda: False
        )
        msg = ConvMessage(author="C-lord", content="plain ascii only")

        assert render_conversation_png([msg]) is not None
