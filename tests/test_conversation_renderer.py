"""Tests for the design-comp conversation renderer + spec loader (#316).

These cover the hand-authored spec → ``ConvMessage`` loader and the Pillow
mockup renderer. Rendering tests skip gracefully when the optional ``[table]``
extra (Pillow) / a usable font is unavailable, mirroring ``test_pane_renderer``.
"""

from __future__ import annotations

import json

import pytest

from c_lord.discord_ui.conversation_renderer import (
    ConvAttachment,
    ConvEmbed,
    ConvField,
    ConvMessage,
    ConvReaction,
    conversation_from_spec,
    emoji_support_available,
    load_spec_file,
    render_conversation_png,
)
from c_lord.discord_ui.fonts import load_text_font

PNG_MAGIC = b"\x89PNG\r\n\x1a\n"
_HAS_FONT = load_text_font(20) is not None

# #588: the renderer now refuses to draw a spec containing emoji when the
# optional ``emoji`` library is absent — the old behaviour was to draw them with
# the body font (i.e. tofu) and report success. The specs below do carry emoji,
# so they need the whole ``[table]`` extra, not just a usable font.
_HAS_EMOJI = emoji_support_available()
_RENDERABLE = _HAS_FONT and _HAS_EMOJI
_RENDER_SKIP = "needs the optional [table] extra (font + emoji): uv sync --extra table"


def _sample_messages() -> list[ConvMessage]:
    """A small but representative design-comp: user msg, bot tool-use embeds, lamps."""
    return [
        ConvMessage(
            author="yousan",
            content="この機能のゴールはこう見えてほしい 🙏",
            timestamp="2026-06-04 12:00",
            reactions=(ConvReaction("🧠", 1), ConvReaction("✅", 1)),
        ),
        ConvMessage(
            author="C-lord",
            is_bot=True,
            timestamp="2026-06-04 12:00",
            color=0x5865F2,
            content="やってみます。",
            embeds=(
                ConvEmbed(
                    title="🛠️ Bash(git status)...", description="⏳ 2s elapsed...", color=0xFEE75C
                ),
                ConvEmbed(
                    title="✅ Done",
                    description="Duration 3.2s",
                    color=0x57F287,
                    fields=(ConvField("model", "opus", inline=True),),
                ),
            ),
            attachments=(ConvAttachment("diff.patch", "text/plain"),),
        ),
    ]


# ── Hand-authored spec loader ────────────────────────────────────────────────


class TestSpecLoader:
    def test_minimal(self) -> None:
        msgs = conversation_from_spec([{"author": "a", "content": "hi"}])
        assert len(msgs) == 1
        assert msgs[0].author == "a"
        assert msgs[0].content == "hi"

    def test_messages_key_wrapper(self) -> None:
        msgs = conversation_from_spec({"messages": [{"author": "a"}, {"author": "b"}]})
        assert [m.author for m in msgs] == ["a", "b"]

    def test_color_hex_string(self) -> None:
        assert conversation_from_spec([{"author": "a", "color": "#5865F2"}])[0].color == 0x5865F2

    def test_color_int(self) -> None:
        assert conversation_from_spec([{"author": "a", "color": 0xFEE75C}])[0].color == 0xFEE75C

    def test_embeds_and_fields(self) -> None:
        m = conversation_from_spec(
            [
                {
                    "author": "b",
                    "is_bot": True,
                    "embeds": [
                        {
                            "title": "🛠️ Bash(ls)",
                            "description": "run",
                            "color": "#FEE75C",
                            "fields": [{"name": "dir", "value": "/tmp", "inline": True}],
                        }
                    ],
                }
            ]
        )[0]
        assert m.is_bot is True
        assert m.embeds[0].title == "🛠️ Bash(ls)"
        assert m.embeds[0].color == 0xFEE75C
        assert m.embeds[0].fields[0].name == "dir"

    def test_reactions_shorthand_and_dict(self) -> None:
        m = conversation_from_spec(
            [{"author": "a", "reactions": ["🧠", {"emoji": "✅", "count": 2}]}]
        )[0]
        assert m.reactions[0] == ConvReaction("🧠", 1)
        assert m.reactions[1] == ConvReaction("✅", 2)

    def test_attachments_shorthand_and_dict(self) -> None:
        m = conversation_from_spec(
            [
                {
                    "author": "a",
                    "attachments": [
                        "diff.patch",
                        {"filename": "log.txt", "content_type": "text/plain"},
                    ],
                }
            ]
        )[0]
        assert m.attachments[0].filename == "diff.patch"
        assert m.attachments[1].content_type == "text/plain"

    def test_empty(self) -> None:
        assert conversation_from_spec([]) == []
        assert conversation_from_spec({"messages": []}) == []

    def test_numeric_optional_fields_are_str_coerced(self) -> None:
        # A bare number where a string is expected (a plausible JSON slip) must
        # be coerced, not passed through raw (which would crash the renderer).
        m = conversation_from_spec(
            [{"author": "a", "timestamp": 1200, "embeds": [{"title": 1, "description": 2}]}]
        )[0]
        assert m.timestamp == "1200"
        assert m.embeds[0].title == "1"
        assert m.embeds[0].description == "2"

    def test_omitted_optional_fields_stay_none(self) -> None:
        m = conversation_from_spec([{"author": "a", "embeds": [{"color": "#fff"}]}])[0]
        assert m.timestamp is None
        assert m.embeds[0].title is None
        assert m.embeds[0].description is None

    def test_load_spec_file(self, tmp_path) -> None:
        p = tmp_path / "spec.json"
        p.write_text(json.dumps({"messages": [{"author": "a", "content": "hi"}]}), encoding="utf-8")
        assert load_spec_file(str(p))[0].author == "a"


# ── Pillow mockup renderer ───────────────────────────────────────────────────


@pytest.mark.skipif(not _RENDERABLE, reason=_RENDER_SKIP)
class TestRenderPillow:
    def test_returns_png_bytes(self) -> None:
        png = render_conversation_png(_sample_messages())
        assert png is not None
        assert png.startswith(PNG_MAGIC)

    def test_empty_messages(self) -> None:
        png = render_conversation_png([])
        assert png is None or png.startswith(PNG_MAGIC)

    def test_renders_from_spec(self) -> None:
        msgs = conversation_from_spec([{"author": "a", "content": "hi 🧠", "reactions": ["✅"]}])
        png = render_conversation_png(msgs)
        assert png is not None
        assert png.startswith(PNG_MAGIC)

    def test_no_font_returns_none(self, monkeypatch) -> None:
        import c_lord.discord_ui.conversation_renderer as cr

        monkeypatch.setattr(cr, "load_text_font", lambda size: None)
        monkeypatch.setattr(cr, "load_mono_font", lambda size: None)
        assert render_conversation_png(_sample_messages()) is None

    def test_numeric_spec_fields_render_without_crash(self) -> None:
        # Regression: numeric timestamp / embed title-desc used to crash Pillow.
        msgs = conversation_from_spec(
            [{"author": "a", "timestamp": 1200, "embeds": [{"title": 1, "description": 2}]}]
        )
        png = render_conversation_png(msgs)
        assert png is not None
        assert png.startswith(PNG_MAGIC)


# ── CLI (scripts/discord_mockup.py) ──────────────────────────────────────────


def _load_cli():
    import importlib.util
    from pathlib import Path

    path = Path(__file__).resolve().parent.parent / "scripts" / "discord_mockup.py"
    spec = importlib.util.spec_from_file_location("discord_mockup", path)
    assert spec and spec.loader
    mod = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(mod)
    return mod


@pytest.mark.skipif(not _RENDERABLE, reason=_RENDER_SKIP)
class TestCli:
    def test_writes_png_from_spec(self, tmp_path) -> None:
        cli = _load_cli()
        spec_file = tmp_path / "s.json"
        spec_file.write_text('[{"author":"a","content":"hi 🧠"}]', encoding="utf-8")
        out = tmp_path / "o.png"
        rc = cli.main([str(spec_file), "-o", str(out), "--title", "#x"])
        assert rc == 0
        assert out.read_bytes().startswith(PNG_MAGIC)

    def test_bundled_example_spec_renders(self, tmp_path) -> None:
        from pathlib import Path

        cli = _load_cli()
        example = (
            Path(__file__).resolve().parent.parent / "scripts" / "examples" / "mockup_spec.json"
        )
        out = tmp_path / "example.png"
        assert cli.main([str(example), "-o", str(out)]) == 0
        assert out.read_bytes().startswith(PNG_MAGIC)


# ---------------------------------------------------------------------------
# #623: CJK inside fenced code blocks must not render as tofu
# ---------------------------------------------------------------------------


class TestCodeBlockCJK:
    """Code blocks are drawn with a monospace font that has no CJK glyphs.

    Before #623 the renderer drew every code-block glyph with that font, so a
    Japanese table — the thing design comps are actually for — came out as
    ``□□□□``. Wide glyphs must fall back to the CJK text face while still
    occupying exactly two monospace cells, so table columns stay aligned.
    """

    def test_wide_runs_splits_by_east_asian_width(self) -> None:
        from c_lord.discord_ui.conversation_renderer import _wide_runs

        assert _wide_runs("ab") == [("ab", False)]
        assert _wide_runs("あい") == [("あい", True)]
        assert _wide_runs("w1 記事 x") == [
            ("w1 ", False),
            ("記事", True),
            (" x", False),
        ]
        assert _wide_runs("") == []

    def test_wide_glyphs_drawn_with_cjk_face_on_a_two_cell_grid(self) -> None:
        """Wide chars use the CJK face; each advances exactly 2 monospace cells."""
        from c_lord.discord_ui.conversation_renderer import _draw_rich, _MonoGrid

        mono, cjk = object(), object()
        calls: list[tuple[float, str, object]] = []

        class _Draw:
            def text(self, xy, s, font=None, fill=None, **kw):  # noqa: ANN001
                calls.append((xy[0], s, font))

            def textlength(self, s, font=None):  # noqa: ANN001
                return 10.0 * len(s)  # mono cell = 10px

        fonts = _FakeFonts(mono=mono)
        grid = _MonoGrid(cjk=cjk, cell_w=10.0)
        end_x = _draw_rich(
            None, _Draw(), 0.0, 0.0, "a記b", mono, fonts, (0, 0, 0), 16, {}, grid=grid
        )

        by_font = {s: f for _x, s, f in calls}
        assert by_font["a"] is mono, "narrow glyph must keep the monospace face"
        assert by_font["記"] is cjk, "wide glyph must use the CJK face, not tofu"
        # a(1 cell) + 記(2 cells) + b(1 cell) = 4 cells = 40px
        assert end_x == 40.0
        # ...and 記 must start right after 'a', at exactly one cell in.
        assert dict((s, x) for x, s, _f in calls)["記"] == 10.0
        assert dict((s, x) for x, s, _f in calls)["b"] == 30.0

    def test_measure_matches_what_is_drawn(self) -> None:
        """Wrapping must measure the same width the draw path advances (AC3)."""
        from c_lord.discord_ui.conversation_renderer import _measure_rich, _MonoGrid

        class _Draw:
            def textlength(self, s, font=None):  # noqa: ANN001
                return 10.0 * len(s)

        grid = _MonoGrid(cjk=object(), cell_w=10.0)
        assert _measure_rich(_Draw(), "a記b", object(), 16, grid=grid) == 40.0

    def test_missing_cjk_face_still_renders(self) -> None:
        """No CJK font available → keep drawing with mono instead of crashing (AC4)."""
        from c_lord.discord_ui.conversation_renderer import _draw_rich, _MonoGrid

        mono = object()
        seen: list[object] = []

        class _Draw:
            def text(self, xy, s, font=None, fill=None, **kw):  # noqa: ANN001
                seen.append(font)

            def textlength(self, s, font=None):  # noqa: ANN001
                return 10.0 * len(s)

        grid = _MonoGrid(cjk=None, cell_w=10.0)
        _draw_rich(
            None, _Draw(), 0.0, 0.0, "記", mono, _FakeFonts(mono=mono), (0, 0, 0), 16, {}, grid=grid
        )
        assert seen == [mono]


class _FakeFonts:
    """Minimal stand-in for ``_Fonts`` — only the fields ``_draw_rich`` reads."""

    def __init__(self, mono: object) -> None:
        self.mono = mono
        self.emoji = None
        self.emoji_color = False
