"""Tests for the ANSI → PNG pane renderer (tmux screenshot, #285)."""

from __future__ import annotations

import pytest

from c_lord.discord_ui.fonts import first_existing
from c_lord.discord_ui.pane_renderer import (
    ANSI_16,
    Style,
    color_256,
    parse_ansi,
    render_pane_png,
)

PNG_MAGIC = b"\x89PNG\r\n\x1a\n"


def _last_run(line: list[tuple[str, Style]]) -> tuple[str, Style]:
    """Return the last non-empty run on a parsed line."""
    runs = [r for r in line if r[0]]
    return runs[-1]


class TestParseAnsi:
    def test_plain_text_single_run(self) -> None:
        assert parse_ansi("hello") == [[("hello", Style())]]

    def test_splits_lines(self) -> None:
        lines = parse_ansi("a\nb")
        assert len(lines) == 2
        assert "".join(r[0] for r in lines[0]) == "a"
        assert "".join(r[0] for r in lines[1]) == "b"

    def test_standard_fg_color(self) -> None:
        line = parse_ansi("\x1b[31mRED\x1b[0m")[0]
        assert _last_run(line) == ("RED", Style(fg=ANSI_16[1]))

    def test_bright_fg_color(self) -> None:
        line = parse_ansi("\x1b[91mX")[0]
        assert _last_run(line)[1].fg == ANSI_16[9]

    def test_bold_and_color(self) -> None:
        style = _last_run(parse_ansi("\x1b[1;32mok")[0])[1]
        assert style.bold is True
        assert style.fg == ANSI_16[2]

    def test_dim_flag(self) -> None:
        assert _last_run(parse_ansi("\x1b[2mfaint")[0])[1].dim is True

    def test_background_color(self) -> None:
        assert _last_run(parse_ansi("\x1b[44mB")[0])[1].bg == ANSI_16[4]

    def test_bright_background_color(self) -> None:
        assert _last_run(parse_ansi("\x1b[104mB")[0])[1].bg == ANSI_16[12]

    def test_256_color_fg(self) -> None:
        assert _last_run(parse_ansi("\x1b[38;5;5mX")[0])[1].fg == color_256(5)

    def test_256_color_bg(self) -> None:
        assert _last_run(parse_ansi("\x1b[48;5;200mX")[0])[1].bg == color_256(200)

    def test_truecolor_fg(self) -> None:
        assert _last_run(parse_ansi("\x1b[38;2;10;20;30mX")[0])[1].fg == (10, 20, 30)

    def test_truecolor_bg(self) -> None:
        assert _last_run(parse_ansi("\x1b[48;2;1;2;3mX")[0])[1].bg == (1, 2, 3)

    def test_inverse(self) -> None:
        assert _last_run(parse_ansi("\x1b[7mX")[0])[1].inverse is True

    def test_default_fg_reset(self) -> None:
        line = parse_ansi("\x1b[31mA\x1b[39mB")[0]
        a = next(r for r in line if r[0] == "A")
        b = next(r for r in line if r[0] == "B")
        assert a[1].fg == ANSI_16[1]
        assert b[1].fg is None

    def test_reset_clears_style(self) -> None:
        line = parse_ansi("\x1b[1;31mA\x1b[0mB")[0]
        a = next(r for r in line if r[0] == "A")
        b = next(r for r in line if r[0] == "B")
        assert a[1] == Style(fg=ANSI_16[1], bold=True)
        assert b[1] == Style()

    def test_strips_osc8_hyperlink_keeps_text(self) -> None:
        s = "\x1b]8;;https://example.com\x1b\\link\x1b]8;;\x1b\\"
        line = parse_ansi(s)[0]
        assert "".join(r[0] for r in line) == "link"

    def test_strips_osc8_bel_terminated(self) -> None:
        s = "\x1b]8;;https://x\x07link\x1b]8;;\x07"
        line = parse_ansi(s)[0]
        assert "".join(r[0] for r in line) == "link"

    def test_strips_non_sgr_csi(self) -> None:
        line = parse_ansi("\x1b[2J\x1b[Khi")[0]
        assert "".join(r[0] for r in line) == "hi"

    def test_empty_string(self) -> None:
        assert parse_ansi("") == []


class TestColor256:
    def test_base_16(self) -> None:
        assert color_256(0) == ANSI_16[0]
        assert color_256(15) == ANSI_16[15]

    def test_cube_corners(self) -> None:
        assert color_256(16) == (0, 0, 0)
        assert color_256(231) == (255, 255, 255)

    def test_grayscale_ramp(self) -> None:
        g = color_256(240)
        assert g[0] == g[1] == g[2]


class TestRenderPanePng:
    def test_returns_png_bytes(self) -> None:
        pytest.importorskip("PIL")
        png = render_pane_png("hello world")
        assert png is not None
        assert png[:8] == PNG_MAGIC

    def test_renders_color_and_emoji(self) -> None:
        pytest.importorskip("PIL")
        text = "\x1b[32m\U0001f7e2 ok\x1b[0m\n\x1b[31m\U0001f534 bad\x1b[0m"
        png = render_pane_png(text)
        assert png is not None
        assert png[:8] == PNG_MAGIC

    def test_renders_cjk(self) -> None:
        pytest.importorskip("PIL")
        png = render_pane_png("状態ランプ 緑")
        assert png is not None
        assert png[:8] == PNG_MAGIC

    def test_empty_returns_png(self) -> None:
        pytest.importorskip("PIL")
        png = render_pane_png("")
        assert png is not None
        assert png[:8] == PNG_MAGIC

    def test_no_font_returns_none(self, monkeypatch: pytest.MonkeyPatch) -> None:
        import c_lord.discord_ui.pane_renderer as pr

        monkeypatch.setattr(pr, "load_mono_font", lambda size: None)
        assert render_pane_png("hello") is None


class TestFonts:
    def test_first_existing_none_for_missing(self) -> None:
        assert first_existing(("/no/such/font.ttf",)) is None

    def test_first_existing_returns_match(self, tmp_path) -> None:
        p = tmp_path / "f.ttf"
        p.write_bytes(b"x")
        assert first_existing((str(p),)) == str(p)
