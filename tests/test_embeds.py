# <<REDACTED 4 lines>>
from c_lord.claude.types import AskOption
from c_lord.discord_ui.embeds import (
    ask_embed,
# <<REDACTED 3 lines>>
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
        # <<REDACTED 1 lines>>
        assert embed.footer.text == "Session: tmux"

    def test_tmux_session_with_window_name(self) -> None:
        from c_lord.discord_ui.embeds import session_start_embed

        embed = session_start_embed(session_id="tmux-1477909096400294011", window_name="work3")
        # <<REDACTED 1 lines>>
        assert embed.footer.text == "Session: work3"

    def test_cli_session_shows_short_id_footer(self) -> None:
        from c_lord.discord_ui.embeds import session_start_embed

        embed = session_start_embed(session_id="abcdef12-3456-7890")
        # <<REDACTED 1 lines>>
        assert embed.footer.text == "Session: abcdef12..."

    def test_no_session_id_no_footer(self) -> None:
        from c_lord.discord_ui.embeds import session_start_embed

        embed = session_start_embed(session_id=None)
        # <<REDACTED 1 lines>>
