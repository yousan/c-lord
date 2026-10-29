"""Layout / pixel-level pins for the design-comp renderer (#316, #588, #623).

``conversation_renderer`` has **no production caller inside the bot**: the only
caller is the dev CLI ``scripts/discord_mockup.py`` (#316 — "c-lord-internal dev
tool, not a shipped consumer feature"). What "the user sees" is therefore the PNG
a developer attaches to an Issue as a design comp, so these tests pin what ends up
in that picture: the header row, code-block cards, embeds, chips, wrapping and
the overall canvas size.

Inputs come from the tool's own real-world shapes, never invented ones:

* ``scripts/examples/mockup_spec.json`` — the bundled sample spec (#316),
* the spec in ``docs/design-comp-mockup.md`` (table + "最小例"),
* the repro spec from #623 (Japanese inside a code fence) and the
  ``status_view``-style table it was found on,
* the six-field notification embed from #588/#571.

The existing suites (``test_conversation_renderer*.py``) skip their whole render
class unless the optional ``emoji`` library is installed — CI never installs it
(``uv sync --dev``) — so almost none of the drawing path was exercised. Here the
emoji guard is bypassed explicitly where a test is about layout rather than about
emoji, and a minimal stand-in for ``emoji.emoji_list`` is used where the emoji
tile path itself is under test.
"""

from __future__ import annotations

import json
import sys
import types
from io import BytesIO
from pathlib import Path

import pytest

import c_lord.discord_ui.conversation_renderer as cr
from c_lord.discord_ui.conversation_renderer import (
    ConvAttachment,
    ConvEmbed,
    ConvField,
    ConvMessage,
    ConvReaction,
    _messages_contain_emoji,
    _split_code_blocks,
    _to_int_color,
    conversation_from_spec,
    load_spec_file,
    render_conversation_png,
)
from c_lord.discord_ui.fonts import load_mono_font, load_text_font

_REPO = Path(__file__).resolve().parent.parent
_EXAMPLE_SPEC = _REPO / "scripts" / "examples" / "mockup_spec.json"

_HAS_FONT = load_text_font(cr.BODY_SIZE) is not None and load_mono_font(cr.MONO_SIZE) is not None
needs_font = pytest.mark.skipif(not _HAS_FONT, reason="Pillow / a usable font not installed")


def _img(png: bytes | None):
    from PIL import Image

    assert png is not None
    return Image.open(BytesIO(png)).convert("RGB")


def _has_color(png: bytes | None, rgb: tuple[int, int, int]) -> bool:
    return rgb in {c for _n, c in _img(png).getcolors(maxcolors=1 << 20)}


@pytest.fixture
def allow_non_emoji_lib(monkeypatch: pytest.MonkeyPatch) -> None:
    """Bypass the #588 guard: layout tests are about geometry, not emoji glyphs.

    Without the ``emoji`` library ``_segment_runs`` classifies everything as
    text, so the picture is drawn with the body/CJK faces — exactly the path a
    Japanese-only spec takes.
    """
    monkeypatch.setattr(cr, "emoji_support_available", lambda: True)


# Clusters that actually occur in scripts/examples/mockup_spec.json (+ the
# 📎 the renderer adds to attachment chips).
_FAKE_EMOJI = ("🛠️", "🧠", "✅", "⏳", "🙏", "📎")


@pytest.fixture
def fake_emoji_lib(monkeypatch: pytest.MonkeyPatch) -> None:
    """Minimal stand-in for ``emoji.emoji_list`` (same return shape as emoji>=2.0).

    Only the clusters present in the bundled spec are recognised — enough to
    drive the emoji-tile branch of ``_draw_rich`` / ``_measure_rich``.
    """

    def emoji_list(text: str) -> list[dict]:
        out: list[dict] = []
        i = 0
        while i < len(text):
            for e in _FAKE_EMOJI:
                if text.startswith(e, i):
                    out.append({"match_start": i, "match_end": i + len(e), "emoji": e})
                    i += len(e)
                    break
            else:
                i += 1
        return out

    mod = types.ModuleType("emoji")
    mod.emoji_list = emoji_list  # type: ignore[attr-defined]
    monkeypatch.setitem(sys.modules, "emoji", mod)


# ── spec coercion edge cases (authoring slips in hand-written JSON) ──────────


class TestColorCoercion:
    """``color`` is documented as ``"#5865F2"`` / ``0x5865F2`` (docs/design-comp-mockup.md)."""

    def test_documented_forms(self) -> None:
        assert _to_int_color("#5865F2") == 0x5865F2
        assert _to_int_color("5865F2") == 0x5865F2
        assert _to_int_color(" #57F287 ") == 0x57F287
        assert _to_int_color(0xFEE75C) == 0xFEE75C

    def test_black_and_zero_mean_no_color(self) -> None:
        """0 is Discord's "no colour", so it falls back to the default bar/name colour."""
        assert _to_int_color(0) is None
        assert _to_int_color("#000000") is None

    def test_bool_is_not_a_color(self) -> None:
        """``"color": true`` (JSON slip) must not become 0x000001."""
        assert _to_int_color(True) is None
        assert _to_int_color(False) is None

    def test_unparseable_values_are_dropped_not_raised(self) -> None:
        assert _to_int_color("blurple") is None
        assert _to_int_color(1.5) is None
        assert _to_int_color(None) is None

    def test_css_shorthand_is_read_literally_not_expanded(self) -> None:
        """Pin: ``"#fff"`` is 0x000FFF (a dark blue), not white — only #RRGGBB is documented."""
        assert _to_int_color("#fff") == 0x000FFF


class TestSpecShapes:
    def test_reaction_list_and_tuple_forms(self) -> None:
        m = conversation_from_spec([{"author": "a", "reactions": [["✅", 3], ["🧠"]]}])[0]
        assert m.reactions == (ConvReaction("✅", 3), ConvReaction("🧠", 1))

    def test_attachment_dict_without_filename_defaults_to_file(self) -> None:
        m = conversation_from_spec([{"author": "a", "attachments": [{"content_type": "x"}]}])[0]
        assert m.attachments == (ConvAttachment("file", "x"),)

    def test_missing_author_is_unknown_and_null_lists_are_empty(self) -> None:
        m = conversation_from_spec(
            [{"content": "hi", "embeds": None, "reactions": None, "attachments": None}]
        )[0]
        assert m.author == "unknown"
        assert (m.embeds, m.reactions, m.attachments) == ((), (), ())

    def test_null_messages_key_is_empty(self) -> None:
        assert conversation_from_spec({"messages": None}) == []
        assert conversation_from_spec(None) == []

    def test_bundled_example_spec_loads_as_documented(self) -> None:
        """scripts/examples/mockup_spec.json — 3 messages, bot card with 2 embeds."""
        msgs = load_spec_file(str(_EXAMPLE_SPEC))
        assert [m.author for m in msgs] == ["yousan", "C-lord", "yousan"]
        bot = msgs[1]
        assert bot.is_bot and bot.color == 0x5865F2
        assert [e.title for e in bot.embeds] == ["🛠️ Bash(git status)...", "✅ Done"]
        assert bot.embeds[1].fields == (ConvField("model", "opus 4.8", inline=True),)
        assert bot.attachments == (ConvAttachment("diff.patch"),)
        assert bot.reactions == (ConvReaction("✅", 1),)


# ── code fences ───────────────────────────────────────────────────────────────


class TestSplitCodeBlocks:
    def test_bundled_example_fence_with_language_tag(self) -> None:
        """The language tag on the opening fence (```python) is not drawn."""
        bot = json.loads(_EXAMPLE_SPEC.read_text(encoding="utf-8"))["messages"][1]
        blocks = _split_code_blocks(bot["content"])
        assert blocks[0] == ("text", "了解です。実装イメージはこんな見え方です:")
        assert blocks[1][0] == "code"
        assert blocks[1][1].startswith("def _split_reply(text):")
        assert "```" not in blocks[1][1]
        assert blocks[2] == ("text", "")

    def test_issue_623_repro_spec(self) -> None:
        """#623's repro content: one fenced line of Japanese."""
        assert _split_code_blocks("```\n日本語のテーブル\n```") == [
            ("text", ""),
            ("code", "日本語のテーブル"),
            ("text", ""),
        ]

    def test_unclosed_fence_runs_to_the_end_as_code(self) -> None:
        assert _split_code_blocks("before\n```\nx = 1") == [("text", "before"), ("code", "x = 1")]

    def test_indented_fence_still_toggles(self) -> None:
        assert _split_code_blocks("  ```\ncode\n  ```") == [
            ("text", ""),
            ("code", "code"),
            ("text", ""),
        ]

    def test_whitespace_only_code_block_is_dropped(self) -> None:
        assert _split_code_blocks("a\n```\n   \n```\nb") == [("text", "a"), ("text", "b")]

    def test_fence_in_mid_line_is_plain_text(self) -> None:
        assert _split_code_blocks("run ```ls``` now") == [("text", "run ```ls``` now")]

    def test_empty_content(self) -> None:
        assert _split_code_blocks("") == [("text", "")]


# ── emoji detection for the #588 guard ───────────────────────────────────────


class TestMessagesContainEmoji:
    """Every place a design comp can carry an emoji must be inspected (#588)."""

    def test_ascii_only_spec_is_clean(self) -> None:
        msg = ConvMessage(
            author="C-lord",
            content="plain",
            attachments=(ConvAttachment("diff.patch"),),
            reactions=(ConvReaction("+1"),),
            embeds=(ConvEmbed(title="Done", description="3.2s", fields=(ConvField("a", "b"),)),),
        )
        assert _messages_contain_emoji([msg]) is False
        assert _messages_contain_emoji([]) is False

    @pytest.mark.parametrize(
        "msg",
        [
            ConvMessage(author="🤖 bot"),
            ConvMessage(author="a", attachments=(ConvAttachment("🧠.png"),)),
            ConvMessage(author="a", reactions=(ConvReaction("✅"),)),
            ConvMessage(author="a", embeds=(ConvEmbed(title="✅ Done"),)),
            ConvMessage(author="a", embeds=(ConvEmbed(description="⏳ 2s elapsed..."),)),
            ConvMessage(author="a", embeds=(ConvEmbed(fields=(ConvField("🛠️", "x"),)),)),
            ConvMessage(author="a", embeds=(ConvEmbed(fields=(ConvField("x", "🧠"),)),)),
        ],
        ids=["author", "attachment", "reaction", "title", "description", "f-name", "f-value"],
    )
    def test_each_slot_is_inspected(self, msg: ConvMessage) -> None:
        assert _messages_contain_emoji([msg]) is True


# ── full render: canvas, header, cards ───────────────────────────────────────


@needs_font
class TestCanvas:
    def test_empty_conversation_is_a_minimal_canvas(self) -> None:
        img = _img(render_conversation_png([]))
        assert img.size == (cr.WIDTH, cr.MARGIN * 2 + cr.AVATAR)
        assert img.getpixel((0, 0)) == cr.BG

    def test_title_adds_exactly_one_header_row(self, allow_non_emoji_lib: None) -> None:
        """``--title "#feature"`` (docs) prepends a header row and a divider."""
        msgs = [ConvMessage(author="yousan", content="いいね、これがゴール。")]
        plain = _img(render_conversation_png(msgs))
        titled_png = render_conversation_png(msgs, title="#new-feature")
        titled = _img(titled_png)
        name_font = load_text_font(cr.NAME_SIZE)
        assert titled.height - plain.height == cr._line_h(name_font, 10)
        assert _has_color(titled_png, cr.EMBED_BG)  # the divider line

    def test_every_message_occupies_at_least_the_avatar(self) -> None:
        one = _img(render_conversation_png([ConvMessage(author="a")]))
        two = _img(render_conversation_png([ConvMessage(author="a"), ConvMessage(author="b")]))
        assert two.height - one.height >= cr.AVATAR + 4 + cr.MSG_GAP

    def test_longer_content_wraps_into_a_taller_image(self, allow_non_emoji_lib: None) -> None:
        """The bundled spec's first line, repeated: wraps instead of running off the canvas."""
        line = "新機能のゴール: スレッドに送った長文が、途中で切れずに最後まで届いてほしい"
        short = _img(render_conversation_png([ConvMessage(author="yousan", content=line)]))
        long = _img(render_conversation_png([ConvMessage(author="yousan", content=line * 6)]))
        assert long.width == short.width == cr.WIDTH
        assert long.height > short.height

    def test_blank_lines_in_text_take_vertical_space(self) -> None:
        a = _img(render_conversation_png([ConvMessage(author="a", content="x\ny")]))
        b = _img(render_conversation_png([ConvMessage(author="a", content="x\n\n\n\ny")]))
        assert b.height > a.height


@needs_font
class TestHeaderRow:
    def test_bot_gets_the_badge_and_users_do_not(self) -> None:
        bot = render_conversation_png([ConvMessage(author="C-lord", is_bot=True, content="ok")])
        user = render_conversation_png([ConvMessage(author="yousan", content="ok")])
        assert _has_color(bot, cr.BADGE_BG)
        # The avatar palette also contains (88,101,242); "yousan" maps elsewhere.
        assert not _has_color(user, cr.BADGE_BG)

    def test_timestamp_is_drawn_to_the_right_of_the_name(self) -> None:
        """``"timestamp": "2026-06-04 12:00"`` (bundled spec) lands on the header row."""
        with_ts = _img(
            render_conversation_png([ConvMessage(author="yousan", timestamp="2026-06-04 12:00")])
        )
        without = _img(render_conversation_png([ConvMessage(author="yousan")]))
        assert with_ts.size == without.size  # the timestamp never adds a row
        header = (cr.GUTTER + 80, cr.MARGIN, cr.WIDTH - cr.MARGIN, cr.MARGIN + cr.NAME_SIZE)
        assert with_ts.crop(header).tobytes() != without.crop(header).tobytes()
        body = (0, cr.MARGIN + cr.AVATAR, cr.WIDTH, with_ts.height)
        assert with_ts.crop(body).tobytes() == without.crop(body).tobytes()

    def test_avatar_colour_is_stable_per_author(self) -> None:
        name = "C-lord"
        expected = cr._AVATAR_COLORS[sum(ord(c) for c in name) % len(cr._AVATAR_COLORS)]
        img = _img(render_conversation_png([ConvMessage(author=name)]))
        cx = cy = cr.MARGIN + cr.AVATAR // 2
        # Sample off the initial letter, inside the disc.
        assert img.getpixel((cr.MARGIN + 6, cy)) == expected
        assert img.getpixel((cx, cr.MARGIN + 3)) == expected

    def test_blank_author_still_renders(self) -> None:
        """``_draw_avatar`` falls back to "?" for an empty / whitespace name."""
        assert render_conversation_png([ConvMessage(author="   ")]) is not None


@needs_font
class TestCodeBlocks:
    def test_bundled_example_code_block_card(self, allow_non_emoji_lib: None) -> None:
        bot = json.loads(_EXAMPLE_SPEC.read_text(encoding="utf-8"))["messages"][1]
        png = render_conversation_png([ConvMessage(author="C-lord", content=bot["content"])])
        assert _has_color(png, cr.CODE_BG)

    def test_issue_623_japanese_code_block_renders(self, allow_non_emoji_lib: None) -> None:
        """#623 repro spec — drawn on the mono grid with the CJK fallback face."""
        msgs = conversation_from_spec(
            {
                "messages": [
                    {"author": "C-lord", "is_bot": True, "content": "```\n日本語のテーブル\n```"}
                ]
            }
        )
        png = render_conversation_png(msgs)
        assert _has_color(png, cr.CODE_BG)

    def test_code_block_height_grows_one_mono_line_per_row(self, allow_non_emoji_lib: None) -> None:
        """The status_view-style table from #623: one row = one mono line."""
        rows = [
            "#   status  topic                   size    used",
            "1   wait    OpenClawのQiita記事化      412 MB  2h",
            "9   wait    pt-jp 移行                88 MB   1d",
        ]

        def h(n: int) -> int:
            body = "```\n" + "\n".join(rows[:n]) + "\n```"
            return _img(
                render_conversation_png([ConvMessage(author="C-lord", content=body)])
            ).height

        mono_h = cr._line_h(load_mono_font(cr.MONO_SIZE), 4)
        assert h(3) - h(2) == mono_h
        assert h(2) - h(1) == mono_h

    def test_blank_line_inside_code_block_keeps_its_row(self, allow_non_emoji_lib: None) -> None:
        def h(body: str) -> int:
            return _img(render_conversation_png([ConvMessage(author="a", content=body)])).height

        mono_h = cr._line_h(load_mono_font(cr.MONO_SIZE), 4)
        assert h("```\na\n\nb\n```") - h("```\na\nb\n```") == mono_h

    def test_missing_cjk_face_falls_back_without_crashing(
        self, allow_non_emoji_lib: None, monkeypatch: pytest.MonkeyPatch
    ) -> None:
        """#623 AC4: no CJK face → keep the mono face (tofu beats crashing)."""
        real = cr.load_text_font
        calls: list[int] = []

        def fake(size: int):
            # Call order in render_conversation_png: body, then the two CJK
            # fallback attempts (2-cell size, MONO_SIZE), then name/time/badge.
            calls.append(size)
            return None if len(calls) in (2, 3) else real(size)

        monkeypatch.setattr(cr, "load_text_font", fake)
        png = render_conversation_png([ConvMessage(author="a", content="```\n日本語\n```")])
        assert _has_color(png, cr.CODE_BG)


@needs_font
class TestEmbeds:
    def test_embed_bar_uses_the_embed_colour(self, allow_non_emoji_lib: None) -> None:
        """Bundled spec's "Bash(git status)" card is #FEE75C; "Done" is #57F287."""
        bot = load_spec_file(str(_EXAMPLE_SPEC))[1]
        png = render_conversation_png([ConvMessage(author="C-lord", embeds=bot.embeds)])
        assert _has_color(png, (0xFE, 0xE7, 0x5C))
        assert _has_color(png, (0x57, 0xF2, 0x87))
        assert _has_color(png, cr.EMBED_BG)

    def test_colourless_embed_uses_the_default_bar(self) -> None:
        png = render_conversation_png(
            [ConvMessage(author="a", embeds=(ConvEmbed(description="Duration 3.2s"),))]
        )
        assert _has_color(png, cr.EMBED_BAR_DEFAULT)

    def test_description_only_embed_is_shorter_than_titled(self) -> None:
        def h(e: ConvEmbed) -> int:
            return _img(render_conversation_png([ConvMessage(author="a", embeds=(e,))])).height

        assert h(ConvEmbed(title="Done", description="3.2s")) > h(ConvEmbed(description="3.2s"))

    def test_588_notification_six_inline_fields_take_two_rows(self) -> None:
        """#588/#571: six inline fields → 2 rows; height difference = 4 field rows."""

        def h(inline: bool) -> int:
            fields = tuple(ConvField(f"n{i}", f"v{i}", inline=inline) for i in range(6))
            e = ConvEmbed(title="t", fields=fields)
            return _img(render_conversation_png([ConvMessage(author="a", embeds=(e,))])).height

        name_h = cr._line_h(load_text_font(cr.NAME_SIZE), 2)
        body_h = cr._line_h(load_text_font(cr.BODY_SIZE))
        assert h(False) - h(True) == 4 * (name_h + body_h)

    def test_empty_embed_is_still_a_card(self) -> None:
        """A spec embed with only a colour (``{"color": "#fff"}``) draws an empty card."""
        png = render_conversation_png([ConvMessage(author="a", embeds=(ConvEmbed(),))])
        assert _has_color(png, cr.EMBED_BG)


@needs_font
class TestChips:
    def test_attachment_and_reaction_chips(self, fake_emoji_lib: None) -> None:
        """Bundled spec message 2: 📎 diff.patch + ✅ 1."""
        bot = load_spec_file(str(_EXAMPLE_SPEC))[1]
        png = render_conversation_png(
            [ConvMessage(author="C-lord", attachments=bot.attachments, reactions=bot.reactions)]
        )
        assert _has_color(png, cr.CHIP_BG)

    def test_many_chips_wrap_onto_more_rows(self, allow_non_emoji_lib: None) -> None:
        def h(n: int) -> int:
            atts = tuple(ConvAttachment(f"evidence-{i:02d}-screenshot.png") for i in range(n))
            return _img(render_conversation_png([ConvMessage(author="a", attachments=atts)])).height

        chip_row = cr._line_h(load_text_font(cr.BODY_SIZE), 8) + 6
        assert h(1) == h(2)  # two short chips fit on one row
        assert h(12) - h(1) >= 2 * chip_row


@needs_font
class TestEmojiTiles:
    def test_bundled_example_spec_renders_with_emoji_tiles(self, fake_emoji_lib: None) -> None:
        """The whole bundled spec, with 🧠/✅/🛠️/⏳/🙏 classified as emoji clusters."""
        png = render_conversation_png(load_spec_file(str(_EXAMPLE_SPEC)), title="#new-feature")
        img = _img(png)
        assert img.width == cr.WIDTH
        assert _has_color(png, cr.CODE_BG)
        assert _has_color(png, cr.CHIP_BG)

    def test_emoji_cluster_measures_as_one_tile(self, fake_emoji_lib: None) -> None:
        from PIL import Image, ImageDraw

        draw = ImageDraw.Draw(Image.new("RGB", (4, 4)))
        font = load_text_font(cr.BODY_SIZE)
        assert cr._measure_rich(draw, "🧠", font, 22.0) == 22.0
        assert cr._measure_rich(draw, "🧠✅", font, 22.0) == 44.0

    def test_wrap_breaks_a_long_emoji_run_by_cluster(self, fake_emoji_lib: None) -> None:
        from PIL import Image, ImageDraw

        draw = ImageDraw.Draw(Image.new("RGB", (4, 4)))
        font = load_text_font(cr.BODY_SIZE)
        lines = cr._wrap_rich(draw, "🧠" * 10, font, 100.0, 22.0)
        assert lines == ["🧠" * 4, "🧠" * 4, "🧠" * 2]


# ── wrapping with real fonts ─────────────────────────────────────────────────


@needs_font
class TestWrap:
    @pytest.fixture
    def ctx(self):
        from PIL import Image, ImageDraw

        return ImageDraw.Draw(Image.new("RGB", (4, 4))), load_text_font(cr.BODY_SIZE)

    def test_every_wrapped_line_fits(self, ctx) -> None:
        draw, font = ctx
        text = "Duration 3.2s · $0.0041 " * 20
        width = cr.WIDTH - cr.MARGIN - cr.GUTTER
        lines = cr._wrap_rich(draw, text, font, width, 22.0)
        assert len(lines) > 1
        assert all(draw.textlength(ln, font=font) <= width for ln in lines)
        assert " ".join(ln.strip() for ln in lines).split() == text.split()

    def test_japanese_without_spaces_is_hard_broken_by_character(self, ctx) -> None:
        """Japanese has no spaces, so the whole paragraph is one overlong token."""
        draw, font = ctx
        text = "新機能のゴール:スレッドに送った長文が途中で切れずに最後まで届いてほしい" * 3
        lines = cr._wrap_rich(draw, text, font, 300.0, 22.0)
        assert len(lines) > 1
        assert "".join(lines) == text
        assert all(draw.textlength(ln, font=font) <= 300.0 for ln in lines)

    def test_overlong_path_token_is_flushed_then_char_broken(self, ctx) -> None:
        draw, font = ctx
        path = "/home/yousan/c-lord-sessions/1505747831447883806/1565189817178783864/c_lord"
        lines = cr._wrap_rich(draw, f"see {path}", font, 200.0, 22.0)
        assert lines[0] == "see"
        assert "".join(lines[1:]) == path

    def test_paragraph_breaks_are_preserved(self, ctx) -> None:
        draw, font = ctx
        assert cr._wrap_rich(draw, "a\n\nb", font, 500.0, 22.0) == ["a", "", "b"]
