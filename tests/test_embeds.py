"""The embeds c-lord posts: what each one says, and how long it may get."""

from __future__ import annotations

import pytest

from c_lord.claude.types import AskOption
from c_lord.discord_ui.embeds import (
    COLOR_ERROR,
    COLOR_INFO,
    ask_embed,
    error_embed,
    stopped_embed,
    timeout_embed,
)


class TestAskEmbedLegend:
    """#169: option descriptions must reach Discord.  Buttons (≤4 options) can't
    show descriptions, so ask_embed appends a `**label** — description` legend.
    """

    def _opts(self) -> list[AskOption]:
        return [
            AskOption(label="Production", description="本番"),
            AskOption(label="Staging", description="検証"),
        ]

    def test_button_mode_appends_legend(self) -> None:
        e = ask_embed("Which env?", "Deploy", self._opts(), multi_select=False)
        assert "**Production** — 本番" in e.description
        assert "**Staging** — 検証" in e.description
        assert e.description.startswith("Which env?")

    def test_select_mode_no_legend(self) -> None:
        """≥5 options → Select menu shows descriptions itself; no embed legend."""
        opts = [AskOption(label=f"O{i}", description=f"d{i}") for i in range(5)]
        e = ask_embed("Pick", "H", opts, multi_select=False)
        assert e.description == "Pick"

    def test_multiselect_no_legend(self) -> None:
        """multi_select renders a Select menu → no legend."""
        e = ask_embed("Pick", "H", self._opts(), multi_select=True)
        assert e.description == "Pick"

    def test_options_without_descriptions_no_legend(self) -> None:
        opts = [AskOption(label="Yes"), AskOption(label="No")]
        e = ask_embed("OK?", "H", opts)
        assert e.description == "OK?"

    def test_no_options_unchanged(self) -> None:
        e = ask_embed("Just a question", "H")
        assert e.description == "Just a question"


class TestSessionStartEmbed:
    """Tests for session_start_embed tmux/CLI/None display."""

    def test_tmux_session_shows_tmux_footer(self) -> None:
        from c_lord.discord_ui.embeds import session_start_embed

        embed = session_start_embed(session_id="tmux-1477909096400294011")
        assert embed.title == "🤖 Claude Code session started"
        assert embed.footer.text == "Session: tmux"

    def test_tmux_session_with_window_name(self) -> None:
        from c_lord.discord_ui.embeds import session_start_embed

        embed = session_start_embed(session_id="tmux-1477909096400294011", window_name="work3")
        assert embed.color.value == COLOR_INFO
        assert embed.footer.text == "Session: work3"

    def test_cli_session_shows_short_id_footer(self) -> None:
        from c_lord.discord_ui.embeds import session_start_embed

        embed = session_start_embed(session_id="abcdef12-3456-7890")
        assert embed.footer.text is not None
        assert embed.footer.text == "Session: abcdef12..."

    def test_no_session_id_no_footer(self) -> None:
        from c_lord.discord_ui.embeds import session_start_embed

        embed = session_start_embed(session_id=None)
        assert embed.title == "🤖 Claude Code session started"
        assert embed.footer.text is None


class TestEmbedLimits:
    """Discord rejects an oversized embed outright, so every builder cuts its text."""

    def test_error_text_is_cut_to_4000_characters(self) -> None:
        embed = error_embed("x" * 4001)
        assert embed.description == "x" * 4000

    def test_error_text_at_the_limit_is_kept_whole(self) -> None:
        text = "e" * 3999 + "!"
        embed = error_embed(text)
        assert embed.description == text
        assert embed.title == "❌ Error"
        assert embed.color.value == COLOR_ERROR

    @pytest.mark.parametrize(("header_len", "title_len"), [(254, 256), (255, 256), (300, 256)])
    def test_ask_title_is_cut_to_256_characters(self, header_len: int, title_len: int) -> None:
        """The title is "❓ " + header; the cut applies to the whole title."""
        embed = ask_embed("q", "h" * header_len)
        assert len(embed.title) == title_len
        assert embed.title.startswith("❓ h")

    def test_ask_body_is_cut_to_4096_characters_legend_included(self) -> None:
        options = [AskOption(label="A", description="d" * 200)]
        embed = ask_embed("q" * 4000, "H", options)
        assert len(embed.description) == 4096
        assert embed.description.startswith("q" * 4000 + "\n\n**A** — d")

    def test_timeout_names_the_seconds_and_the_way_forward(self) -> None:
        embed = timeout_embed(300)
        assert embed.title == "⏱️ Session timed out"
        assert embed.description.startswith("No response received for 300 seconds.")
        assert "`/clear`" in embed.description
        assert embed.color.value == COLOR_ERROR


class TestStoppedEmbed:
    """A stop the user asked for: says the session survives, and what else ended (#878)."""

    def test_plain_stop_does_not_mention_commands(self) -> None:
        embed = stopped_embed()
        assert embed.title == "⏹️ Session stopped"
        assert "The session is preserved" in embed.description
        assert "running command" not in embed.description

    def test_stopped_commands_are_counted(self) -> None:
        embed = stopped_embed(shells_stopped=2)
        assert "Also stopped 2 running command(s), including background ones." in embed.description

    def test_a_stop_is_not_painted_as_an_error(self) -> None:
        assert stopped_embed().color.value != COLOR_ERROR
