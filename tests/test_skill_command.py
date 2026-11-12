"""``/skill`` and ``!skill``: what a skill directory yields, and where a skill runs.

Three layers, each tested from the outside:

* reading ``SKILL.md`` frontmatter (``_parse_skill_meta`` / ``_load_skills``),
  including the reader's long-standing quirks — they decide which names show up
  in Discord, so they are pinned rather than left to chance;
* the autocomplete list Discord shows while the user types;
* the command itself: who may run it, which names are refused before anything
  reaches Claude, and whether it runs in the current thread or opens a new one.
"""

from __future__ import annotations

import logging
import time
from pathlib import Path
from unittest.mock import AsyncMock, MagicMock, patch

import discord
import pytest

from c_lord.cogs import skill_command
from c_lord.cogs.skill_command import (
    SKILL_RELOAD_INTERVAL,
    SkillCommandCog,
    _load_skills,
    _parse_skill_meta,
)
from c_lord.discord_ui.authorization import Authorizer

CLAUDE_CHANNEL_ID = 999
_PATCH_RUN = "c_lord.cogs.skill_command.run_claude_with_config"


# ---------------------------------------------------------------------------
# Builders
# ---------------------------------------------------------------------------


def _write_skill(skills_dir: Path, dirname: str, content: str | None) -> Path:
    """Create ``<skills_dir>/<dirname>/`` and, unless *content* is None, its SKILL.md."""
    skill_dir = skills_dir / dirname
    skill_dir.mkdir(parents=True)
    if content is not None:
        # newline="" so the bytes on disk are exactly the ones in the test.
        (skill_dir / "SKILL.md").write_text(content, encoding="utf-8", newline="")
    return skill_dir


def _make_cog(
    skills: list[dict[str, str]] | None = None,
    allowed_user_ids: set[int] | None = None,
) -> SkillCommandCog:
    """A cog on a mocked bot whose skill list is exactly *skills*."""
    bot = MagicMock()
    # No settings repo → thread auto-archive resolver uses the 3-day default.
    bot.settings_repo = None
    repo = MagicMock()
    repo.get = AsyncMock(return_value=None)
    runner = MagicMock()
    runner.model = "sonnet"
    runner.working_dir = None
    runner.timeout_seconds = 300
    runner.effort = None
    cog = SkillCommandCog(
        bot=bot,
        repo=repo,
        runner=runner,
        claude_channel_id=CLAUDE_CHANNEL_ID,
        skills_dir="/nonexistent/skills",
        allowed_user_ids=allowed_user_ids,
        # #713: the shipped default is owner-only, resolved from Discord at
        # on_ready — which a unit test never reaches. Tests that pass an
        # allowlist keep it; the rest are not about who may run /skill.
        authorizer=None if allowed_user_ids else Authorizer(allow_anyone=True),
    )
    cog._skills = list(skills or [])
    return cog


def _make_thread(parent_id: int = CLAUDE_CHANNEL_ID, thread_id: int = 5555) -> MagicMock:
    thread = MagicMock(spec=discord.Thread)
    thread.id = thread_id
    thread.parent_id = parent_id
    thread.mention = f"<#{thread_id}>"
    thread.send = AsyncMock()
    return thread


def _make_interaction(channel: object | None = None, user_id: int = 1) -> MagicMock:
    """A slash interaction; without *channel* it comes from an ordinary text channel."""
    interaction = MagicMock(spec=discord.Interaction)
    interaction.user = MagicMock(spec=discord.Member)
    interaction.user.id = user_id
    interaction.channel = channel if channel is not None else MagicMock(spec=discord.TextChannel)
    interaction.extras = {}
    interaction.response = MagicMock()
    interaction.response.send_message = AsyncMock()
    interaction.response.defer = AsyncMock()
    interaction.followup = MagicMock()
    interaction.followup.send = AsyncMock()
    interaction.delete_original_response = AsyncMock()
    return interaction


def _claude_channel_with_new_thread(cog: SkillCommandCog, thread_id: int = 777) -> MagicMock:
    """Make the bot find the Claude channel; return the thread it will create there."""
    new_thread = _make_thread(thread_id=thread_id)
    channel = MagicMock(spec=discord.TextChannel)
    channel.id = CLAUDE_CHANNEL_ID
    channel.create_thread = AsyncMock(return_value=new_thread)
    cog.bot.get_channel = MagicMock(return_value=channel)
    return new_thread


def _session_record(session_id: str = "abc-123") -> MagicMock:
    record = MagicMock()
    record.session_id = session_id
    record.working_dir = None
    record.trigger_message_id = None
    return record


async def _slash(
    cog: SkillCommandCog, interaction: MagicMock, name: str, args: str | None = None
) -> AsyncMock:
    """Invoke ``/skill`` with Claude's run replaced; return that replacement."""
    with patch(_PATCH_RUN, new_callable=AsyncMock) as mock_run:
        await cog.run_skill.callback(cog, interaction, name=name, args=args)
    return mock_run


def _replies(interaction: MagicMock) -> list[str]:
    """Everything the interaction was answered with, first response then followups."""
    calls = interaction.response.send_message.call_args_list + (
        interaction.followup.send.call_args_list
    )
    return [c.args[0] for c in calls]


# ---------------------------------------------------------------------------
# SKILL.md frontmatter
# ---------------------------------------------------------------------------


class TestParseSkillMeta:
    """``_parse_skill_meta`` turns one skill directory into ``{"name", "description"}``."""

    def test_name_and_description_come_from_the_frontmatter(self, tmp_path: Path) -> None:
        skill_dir = _write_skill(
            tmp_path,
            "dir-name",
            "---\nname: goodmorning\ndescription: Start the day\n---\n\n# Body\n",
        )
        assert _parse_skill_meta(skill_dir) == {
            "name": "goodmorning",
            "description": "Start the day",
        }

    def test_a_missing_name_falls_back_to_the_directory_name(self, tmp_path: Path) -> None:
        skill_dir = _write_skill(tmp_path, "todoist", "---\ndescription: Tasks\n---\n")
        assert _parse_skill_meta(skill_dir) == {"name": "todoist", "description": "Tasks"}

    def test_a_missing_description_is_an_empty_string(self, tmp_path: Path) -> None:
        skill_dir = _write_skill(tmp_path, "x", "---\nname: only-name\n---\n")
        assert _parse_skill_meta(skill_dir) == {"name": "only-name", "description": ""}

    def test_a_directory_without_skill_md_is_not_a_skill(self, tmp_path: Path) -> None:
        skill_dir = _write_skill(tmp_path, "empty", None)
        assert _parse_skill_meta(skill_dir) is None

    def test_a_file_without_frontmatter_is_not_a_skill(self, tmp_path: Path) -> None:
        skill_dir = _write_skill(tmp_path, "plain", "# Just a heading\n\nname: nope\n")
        assert _parse_skill_meta(skill_dir) is None

    def test_an_empty_frontmatter_block_still_counts(self, tmp_path: Path) -> None:
        """``---`` directly followed by ``---``: a skill named after its directory."""
        skill_dir = _write_skill(tmp_path, "bare", "---\n---\nbody\n")
        assert _parse_skill_meta(skill_dir) == {"name": "bare", "description": ""}

    def test_an_unreadable_file_is_skipped_with_a_warning(
        self, tmp_path: Path, caplog: pytest.LogCaptureFixture
    ) -> None:
        """A permission problem on one skill must not take the whole list down."""
        skill_dir = _write_skill(tmp_path, "locked", "---\nname: locked\n---\n")
        with (
            patch.object(Path, "read_text", side_effect=PermissionError("denied")),
            caplog.at_level(logging.WARNING, logger="c_lord.cogs.skill_command"),
        ):
            assert _parse_skill_meta(skill_dir) is None
        assert "Failed to read" in caplog.text

    def test_a_file_that_is_not_utf8_is_not_swallowed(self, tmp_path: Path) -> None:
        """Only ``OSError`` is treated as "unreadable"; a decoding error surfaces."""
        skill_dir = _write_skill(tmp_path, "binary", None)
        (skill_dir / "SKILL.md").write_bytes(b"---\nname: \xff\xfe\n---\n")
        with pytest.raises(UnicodeDecodeError):
            _parse_skill_meta(skill_dir)

    def test_surrounding_whitespace_is_dropped_from_values(self, tmp_path: Path) -> None:
        skill_dir = _write_skill(
            tmp_path, "ws", "---\nname:    padded  \ndescription:\tTabbed\t\n---\n"
        )
        assert _parse_skill_meta(skill_dir) == {"name": "padded", "description": "Tabbed"}

    def test_quotes_are_kept_as_written(self, tmp_path: Path) -> None:
        """The reader is not a YAML parser: quotes are part of the value."""
        skill_dir = _write_skill(tmp_path, "q", '---\nname: "quoted"\n---\n')
        meta = _parse_skill_meta(skill_dir)
        assert meta is not None and meta["name"] == '"quoted"'

    def test_a_yaml_block_scalar_is_not_interpreted(self, tmp_path: Path) -> None:
        """``description: >`` yields the marker itself; the indented lines are ignored."""
        skill_dir = _write_skill(
            tmp_path, "folded", "---\nname: folded\ndescription: >\n  spans\n  lines\n---\n"
        )
        assert _parse_skill_meta(skill_dir) == {"name": "folded", "description": ">"}

    def test_the_last_of_repeated_keys_wins(self, tmp_path: Path) -> None:
        skill_dir = _write_skill(tmp_path, "dup", "---\nname: first\nname: second\n---\n")
        meta = _parse_skill_meta(skill_dir)
        assert meta is not None and meta["name"] == "second"

    @pytest.mark.parametrize(
        "line",
        ["  name: indented", "name : spaced-colon", "\tname: tabbed"],
        ids=["indented", "space-before-colon", "tab-indented"],
    )
    def test_only_keys_at_the_start_of_a_line_are_read(self, tmp_path: Path, line: str) -> None:
        skill_dir = _write_skill(tmp_path, "fallback", f"---\n{line}\n---\n")
        meta = _parse_skill_meta(skill_dir)
        assert meta is not None and meta["name"] == "fallback"

    def test_a_hyphenated_key_is_an_entry_like_any_other(self, tmp_path: Path) -> None:
        """``allowed-tools:`` is read as a key, hyphen and all.

        Only ``name`` and ``description`` come back, so the one place this
        shows is the empty-value rule below: an entry with nothing after its
        colon takes the next line as its value. A hyphenated key does exactly
        that — the ``name:`` line under it is its value, not an entry — where
        a line that was not read as a key at all would have left ``name`` alone.
        """
        skill_dir = _write_skill(
            tmp_path, "h", "---\nallowed-tools:\nname: hy\ndescription: D\n---\n"
        )
        assert _parse_skill_meta(skill_dir) == {"name": "h", "description": "D"}

    def test_a_hyphenated_key_with_a_value_leaves_the_others_alone(self, tmp_path: Path) -> None:
        skill_dir = _write_skill(
            tmp_path, "h", "---\nallowed-tools: Bash\nname: hy\ndescription: D\n---\n"
        )
        assert _parse_skill_meta(skill_dir) == {"name": "hy", "description": "D"}

    def test_an_empty_name_swallows_the_whole_next_line(self, tmp_path: Path) -> None:
        """Pinned quirk: ``name:`` with nothing after it takes the next line as its value.

        The line it swallowed is then no longer read as an entry of its own, so
        the description is lost. Not pretty, but it is what decides the name
        Discord shows — a change here must be a decision, not an accident.
        """
        skill_dir = _write_skill(tmp_path, "quirk", "---\nname:\ndescription: Lost\n---\n")
        assert _parse_skill_meta(skill_dir) == {"name": "description: Lost", "description": ""}

    def test_an_empty_name_skips_blank_lines_to_find_its_value(self, tmp_path: Path) -> None:
        skill_dir = _write_skill(tmp_path, "quirk2", "---\nname:\n\n  taken\n---\n")
        meta = _parse_skill_meta(skill_dir)
        assert meta is not None and meta["name"] == "taken"

    @pytest.mark.parametrize(
        "opening", ["---", "---   ", "---\t", "--- \t "], ids=["bare", "spaces", "tab", "mixed"]
    )
    def test_spaces_and_tabs_may_trail_the_opening_fence(
        self, tmp_path: Path, opening: str
    ) -> None:
        skill_dir = _write_skill(tmp_path, "fence", f"{opening}\nname: ok\n---\n")
        meta = _parse_skill_meta(skill_dir)
        assert meta is not None and meta["name"] == "ok"

    def test_the_closing_fence_only_has_to_start_with_three_dashes(self, tmp_path: Path) -> None:
        skill_dir = _write_skill(
            tmp_path, "close", "---\nname: before\n--- end\nname: after\n---\n"
        )
        meta = _parse_skill_meta(skill_dir)
        assert meta is not None and meta["name"] == "before"

    def test_windows_line_endings_read_like_unix_ones(self, tmp_path: Path) -> None:
        """A SKILL.md saved with CRLF is the same skill — no stray ``\\r`` in its name."""
        skill_dir = _write_skill(
            tmp_path, "win", "---\r\nname: crlf\r\ndescription: From Windows\r\n---\r\nbody\r\n"
        )
        assert _parse_skill_meta(skill_dir) == {"name": "crlf", "description": "From Windows"}

    @pytest.mark.parametrize(
        "content",
        [
            "﻿---\nname: bom\n---\n",
            "\n---\nname: late\n---\n",
            " ---\nname: indented\n---\n",
            "--- yaml\nname: decorated\n---\n",
            "---\nname: never-closed\n",
            "---",
            "",
        ],
        ids=[
            "bom",
            "leading-blank-line",
            "indented-fence",
            "text-after-fence",
            "no-closing-fence",
            "fence-only",
            "empty-file",
        ],
    )
    def test_what_does_not_count_as_frontmatter(self, tmp_path: Path, content: str) -> None:
        """The block must open on the file's very first line with a bare ``---`` + LF,
        and must be closed. Anything else reads as "no frontmatter" — not as an error.
        """
        skill_dir = _write_skill(tmp_path, "nope", content)
        assert _parse_skill_meta(skill_dir) is None


class TestLoadSkills:
    """``_load_skills`` lists every skill directly under the skills directory."""

    def test_every_skill_directory_is_listed(self, tmp_path: Path) -> None:
        _write_skill(tmp_path, "alpha", "---\nname: alpha\ndescription: A\n---\n")
        _write_skill(tmp_path, "beta", "---\nname: beta\ndescription: B\n---\n")
        assert _load_skills(tmp_path) == [
            {"name": "alpha", "description": "A"},
            {"name": "beta", "description": "B"},
        ]

    def test_order_follows_directory_names_not_skill_names(self, tmp_path: Path) -> None:
        _write_skill(tmp_path, "b-dir", "---\nname: aaa\n---\n")
        _write_skill(tmp_path, "a-dir", "---\nname: zzz\n---\n")
        assert [s["name"] for s in _load_skills(tmp_path)] == ["zzz", "aaa"]

    def test_entries_that_are_not_skills_are_skipped(self, tmp_path: Path) -> None:
        _write_skill(tmp_path, "good", "---\nname: good\n---\n")
        _write_skill(tmp_path, "no-skill-md", None)
        _write_skill(tmp_path, "no-frontmatter", "just text\n")
        (tmp_path / "README.md").write_text("---\nname: a-file\n---\n", encoding="utf-8")
        assert [s["name"] for s in _load_skills(tmp_path)] == ["good"]

    def test_two_directories_with_the_same_skill_name_both_appear(self, tmp_path: Path) -> None:
        _write_skill(tmp_path, "one", "---\nname: same\n---\n")
        _write_skill(tmp_path, "two", "---\nname: same\n---\n")
        assert [s["name"] for s in _load_skills(tmp_path)] == ["same", "same"]

    def test_a_missing_directory_is_an_empty_list_and_a_warning(
        self, tmp_path: Path, caplog: pytest.LogCaptureFixture
    ) -> None:
        with caplog.at_level(logging.WARNING, logger="c_lord.cogs.skill_command"):
            assert _load_skills(tmp_path / "does-not-exist") == []
        assert "Skills directory not found" in caplog.text

    def test_a_file_in_place_of_the_directory_is_an_empty_list(self, tmp_path: Path) -> None:
        not_a_dir = tmp_path / "skills"
        not_a_dir.write_text("", encoding="utf-8")
        assert _load_skills(not_a_dir) == []

    def test_the_count_is_logged(self, tmp_path: Path, caplog: pytest.LogCaptureFixture) -> None:
        _write_skill(tmp_path, "one", "---\nname: one\n---\n")
        with caplog.at_level(logging.INFO, logger="c_lord.cogs.skill_command"):
            _load_skills(tmp_path)
        assert "Loaded 1 skills" in caplog.text


class TestCogConstruction:
    """What the cog reads and remembers when it is built."""

    def test_skills_are_read_once_at_construction(self, tmp_path: Path) -> None:
        _write_skill(tmp_path, "one", "---\nname: one\ndescription: First\n---\n")
        cog = SkillCommandCog(
            bot=MagicMock(), repo=MagicMock(), runner=MagicMock(), claude_channel_id=1,
            skills_dir=tmp_path,
        )  # fmt: skip
        assert cog._skills == [{"name": "one", "description": "First"}]

    def test_a_string_path_is_accepted(self, tmp_path: Path) -> None:
        cog = SkillCommandCog(
            bot=MagicMock(), repo=MagicMock(), runner=MagicMock(), claude_channel_id=1,
            skills_dir=str(tmp_path),
        )  # fmt: skip
        assert cog._skills_dir == tmp_path

    def test_the_default_directory_is_the_users_claude_skills(self) -> None:
        """Zero-config: with no ``skills_dir`` the cog reads ``~/.claude/skills``."""
        with patch("c_lord.cogs.skill_command._load_skills", return_value=[]) as mock_load:
            cog = SkillCommandCog(
                bot=MagicMock(), repo=MagicMock(), runner=MagicMock(), claude_channel_id=1
            )
        assert cog._skills_dir == Path.home() / ".claude" / "skills"
        mock_load.assert_called_once_with(cog._skills_dir)

    def test_the_registry_defaults_to_the_bots(self) -> None:
        bot = MagicMock()
        cog = SkillCommandCog(
            bot=bot, repo=MagicMock(), runner=MagicMock(), claude_channel_id=1,
            skills_dir="/nonexistent",
        )  # fmt: skip
        assert cog._registry is bot.session_registry

    def test_an_explicit_registry_wins(self) -> None:
        registry = MagicMock()
        cog = SkillCommandCog(
            bot=MagicMock(), repo=MagicMock(), runner=MagicMock(), claude_channel_id=1,
            skills_dir="/nonexistent", registry=registry,
        )  # fmt: skip
        assert cog._registry is registry

    def test_the_commands_are_both_called_skill(self) -> None:
        cog = _make_cog()
        assert cog.run_skill.name == "skill"
        assert cog.run_skill.description == "Run a Claude Code skill"
        assert cog.run_skill_text.name == "skill"


# ---------------------------------------------------------------------------
# Autocomplete and the lazy re-read behind it
# ---------------------------------------------------------------------------


class TestAutocomplete:
    """The choices Discord shows for ``/skill name:``."""

    SKILLS = [
        {"name": "goodmorning", "description": "Daily morning briefing"},
        {"name": "todoist", "description": "Manage tasks"},
        {"name": "recall", "description": "Search past conversations"},
    ]

    async def _choices(self, cog: SkillCommandCog, typed: str) -> list:
        return await cog._skill_name_autocomplete(MagicMock(), typed)

    @pytest.mark.asyncio
    async def test_matches_on_the_name(self) -> None:
        choices = await self._choices(_make_cog(skills=self.SKILLS), "todo")
        assert [c.value for c in choices] == ["todoist"]

    @pytest.mark.asyncio
    async def test_matches_on_the_description(self) -> None:
        choices = await self._choices(_make_cog(skills=self.SKILLS), "briefing")
        assert [c.value for c in choices] == ["goodmorning"]

    @pytest.mark.asyncio
    async def test_matching_ignores_case(self) -> None:
        cog = _make_cog(skills=[{"name": "ReCall", "description": "SEARCH things"}])
        assert [c.value for c in await self._choices(cog, "RECALL")] == ["ReCall"]
        assert [c.value for c in await self._choices(cog, "search")] == ["ReCall"]

    @pytest.mark.asyncio
    async def test_nothing_typed_offers_everything_in_list_order(self) -> None:
        choices = await self._choices(_make_cog(skills=self.SKILLS), "")
        assert [c.value for c in choices] == ["goodmorning", "todoist", "recall"]

    @pytest.mark.asyncio
    async def test_no_match_is_an_empty_list(self) -> None:
        assert await self._choices(_make_cog(skills=self.SKILLS), "zzz") == []

    @pytest.mark.asyncio
    async def test_at_most_25_choices_are_offered(self) -> None:
        """Discord rejects an autocomplete answer with more than 25 choices."""
        skills = [{"name": f"skill-{i:02d}", "description": ""} for i in range(40)]
        choices = await self._choices(_make_cog(skills=skills), "skill")
        assert [c.value for c in choices] == [f"skill-{i:02d}" for i in range(25)]

    @pytest.mark.asyncio
    async def test_the_limit_applies_after_filtering(self) -> None:
        """A match beyond the 25th skill is still offered when earlier ones do not match."""
        skills = [{"name": f"other-{i}", "description": ""} for i in range(30)]
        skills.append({"name": "needle", "description": ""})
        choices = await self._choices(_make_cog(skills=skills), "needle")
        assert [c.value for c in choices] == ["needle"]

    @pytest.mark.asyncio
    async def test_the_label_is_name_then_description(self) -> None:
        choices = await self._choices(_make_cog(skills=self.SKILLS), "todoist")
        assert choices[0].name == "todoist — Manage tasks"
        assert choices[0].value == "todoist"

    @pytest.mark.asyncio
    async def test_a_skill_without_description_is_labelled_by_name_alone(self) -> None:
        cog = _make_cog(skills=[{"name": "bare", "description": ""}])
        assert (await self._choices(cog, ""))[0].name == "bare"

    @pytest.mark.asyncio
    async def test_a_long_description_is_cut_at_60_characters_with_an_ellipsis(self) -> None:
        cog = _make_cog(skills=[{"name": "long", "description": "d" * 61}])
        assert (await self._choices(cog, ""))[0].name == "long — " + "d" * 60 + "…"

    @pytest.mark.asyncio
    async def test_a_description_of_exactly_60_characters_is_shown_whole(self) -> None:
        cog = _make_cog(skills=[{"name": "edge", "description": "d" * 60}])
        assert (await self._choices(cog, ""))[0].name == "edge — " + "d" * 60

    @pytest.mark.asyncio
    async def test_the_label_never_exceeds_discords_100_characters(self) -> None:
        """A long name plus its description must still be a label Discord accepts."""
        name = "n" * 80
        cog = _make_cog(skills=[{"name": name, "description": "d" * 200}])
        choice = (await self._choices(cog, ""))[0]
        assert len(choice.name) == 100
        assert choice.name.startswith(name + " — ")
        assert choice.value == name


class TestLazyReload:
    """The skill list is re-read from disk at most every ``SKILL_RELOAD_INTERVAL`` seconds."""

    @pytest.mark.parametrize(
        ("seconds_since_read", "reloads"), [(59.0, False), (60.0, True)], ids=["59s", "60s"]
    )
    def test_a_skill_added_on_disk_shows_up_within_a_minute(
        self, monkeypatch: pytest.MonkeyPatch, seconds_since_read: float, reloads: bool
    ) -> None:
        """The promise to the person who just wrote a SKILL.md, in seconds."""
        cog = _make_cog()
        cog._last_loaded = 5000.0
        clock = MagicMock()
        clock.monotonic.return_value = 5000.0 + seconds_since_read
        monkeypatch.setattr(skill_command, "time", clock)
        with patch("c_lord.cogs.skill_command._load_skills", return_value=[]) as mock_load:
            cog._maybe_reload_skills()
        assert mock_load.called is reloads

    def test_a_fresh_list_is_not_read_again(self) -> None:
        cog = _make_cog(skills=[{"name": "old", "description": ""}])
        cog._last_loaded = time.monotonic()
        with patch("c_lord.cogs.skill_command._load_skills") as mock_load:
            cog._maybe_reload_skills()
        mock_load.assert_not_called()
        assert [s["name"] for s in cog._skills] == ["old"]

    def test_an_aged_list_is_replaced_from_disk(self) -> None:
        cog = _make_cog(skills=[{"name": "old", "description": ""}])
        aged = time.monotonic() - SKILL_RELOAD_INTERVAL - 1
        cog._last_loaded = aged
        new_skills = [{"name": "new", "description": "Just added"}]
        with patch("c_lord.cogs.skill_command._load_skills", return_value=new_skills) as mock_load:
            cog._maybe_reload_skills()
        mock_load.assert_called_once_with(cog._skills_dir)
        assert cog._skills == new_skills
        assert cog._last_loaded > aged

    def test_a_reload_restarts_the_clock(self) -> None:
        """Right after a re-read, the next call must not read again."""
        cog = _make_cog()
        cog._last_loaded = time.monotonic() - SKILL_RELOAD_INTERVAL - 1
        with patch("c_lord.cogs.skill_command._load_skills", return_value=[]) as mock_load:
            cog._maybe_reload_skills()
            cog._maybe_reload_skills()
        mock_load.assert_called_once()

    @pytest.mark.parametrize(
        ("age", "reloads"),
        [(SKILL_RELOAD_INTERVAL - 0.5, False), (SKILL_RELOAD_INTERVAL, True)],
        ids=["just-under", "exactly-the-interval"],
    )
    def test_the_list_ages_out_at_the_interval_itself(
        self, monkeypatch: pytest.MonkeyPatch, age: float, reloads: bool
    ) -> None:
        cog = _make_cog()
        cog._last_loaded = 1000.0
        clock = MagicMock()
        clock.monotonic.return_value = 1000.0 + age
        monkeypatch.setattr(skill_command, "time", clock)
        with patch("c_lord.cogs.skill_command._load_skills", return_value=[]) as mock_load:
            cog._maybe_reload_skills()
        assert mock_load.called is reloads

    @pytest.mark.asyncio
    async def test_typing_in_the_autocomplete_picks_up_a_new_skill(self) -> None:
        """A skill added on disk shows up without restarting the bot."""
        cog = _make_cog(skills=[])
        cog._last_loaded = time.monotonic() - SKILL_RELOAD_INTERVAL - 1
        new_skills = [{"name": "fresh", "description": "Brand new"}]
        with patch("c_lord.cogs.skill_command._load_skills", return_value=new_skills):
            choices = await cog._skill_name_autocomplete(MagicMock(), "")
        assert [c.value for c in choices] == ["fresh"]

    @pytest.mark.asyncio
    async def test_running_a_skill_picks_up_a_new_skill_too(self) -> None:
        """The run path re-reads as well — a new skill is runnable, not just listed."""
        cog = _make_cog(skills=[])
        cog._last_loaded = time.monotonic() - SKILL_RELOAD_INTERVAL - 1
        cog.bot.get_channel = MagicMock(return_value=None)
        interaction = _make_interaction()
        with patch(
            "c_lord.cogs.skill_command._load_skills",
            return_value=[{"name": "fresh", "description": ""}],
        ):
            await _slash(cog, interaction, "fresh")
        # It got past the "is this a skill?" check and failed only on the channel.
        assert _replies(interaction) == ["Claude channel not found."]


# ---------------------------------------------------------------------------
# /skill — refusals before anything runs
# ---------------------------------------------------------------------------


class TestRunSkillValidation:
    """What ``/skill`` refuses outright, and how it says so."""

    @pytest.mark.asyncio
    async def test_a_user_outside_the_allowlist_is_refused(self) -> None:
        cog = _make_cog(skills=[{"name": "test", "description": ""}], allowed_user_ids={1})
        interaction = _make_interaction(user_id=99)

        mock_run = await _slash(cog, interaction, "test")

        interaction.response.send_message.assert_called_once()
        call = interaction.response.send_message.call_args
        assert "permission" in call.args[0].lower()
        assert call.kwargs["ephemeral"] is True
        interaction.response.defer.assert_not_called()
        mock_run.assert_not_called()

    @pytest.mark.asyncio
    @pytest.mark.parametrize(
        "name",
        [
            "bad;name",
            "two words",
            "../etc",
            "a/b",
            "$(whoami)",
            "tick`",
            "",
            "line\nbreak",
            "x\n\n",
        ],
        ids=[
            "semicolon",
            "space",
            "path",
            "slash",
            "subshell",
            "backtick",
            "empty",
            "inner-newline",
            "two-trailing-newlines",
        ],
    )
    async def test_a_name_with_anything_but_word_characters_and_hyphens_is_refused(
        self, name: str
    ) -> None:
        """Security boundary: the name is typed into Claude's prompt as ``/<name>``."""
        cog = _make_cog(skills=[{"name": name, "description": ""}])
        interaction = _make_interaction()

        mock_run = await _slash(cog, interaction, name)

        call = interaction.response.send_message.call_args
        assert call.args[0] == f"Invalid skill name: `{name}`"
        assert call.kwargs["ephemeral"] is True
        mock_run.assert_not_called()

    @pytest.mark.asyncio
    async def test_a_single_trailing_newline_gets_past_the_name_check(self) -> None:
        """Pinned quirk: ``name\\n`` is not reported as invalid.

        It is stopped one step later instead — no skill is listed under a name
        ending in a newline (frontmatter values are stripped) — so it still
        never reaches Claude.
        """
        cog = _make_cog(skills=[{"name": "test", "description": ""}])
        interaction = _make_interaction()

        mock_run = await _slash(cog, interaction, "test\n")

        reply = interaction.response.send_message.call_args.args[0]
        assert "Invalid" not in reply
        assert "not found" in reply
        mock_run.assert_not_called()

    @pytest.mark.asyncio
    async def test_an_unknown_skill_is_refused(self) -> None:
        cog = _make_cog(skills=[{"name": "known", "description": ""}])
        interaction = _make_interaction()

        mock_run = await _slash(cog, interaction, "unknown")

        call = interaction.response.send_message.call_args
        assert call.args[0] == "Skill `unknown` not found. Use `/skill` with autocomplete."
        assert call.kwargs["ephemeral"] is True
        interaction.response.defer.assert_not_called()
        mock_run.assert_not_called()

    @pytest.mark.asyncio
    async def test_the_name_must_match_exactly_not_as_a_prefix(self) -> None:
        cog = _make_cog(skills=[{"name": "todoist", "description": ""}])
        interaction = _make_interaction()

        await _slash(cog, interaction, "todo")

        assert "not found" in interaction.response.send_message.call_args.args[0]


# ---------------------------------------------------------------------------
# /skill outside a Claude thread — a new thread is opened
# ---------------------------------------------------------------------------


class TestNewThreadMode:
    """Used from an ordinary channel, ``/skill`` opens a thread in the Claude channel."""

    def _setup_bound_channel(self, cog: SkillCommandCog) -> None:
        """Set up ChannelRepoCog mock so the channel appears bound."""
        cc = _make_channel_cog_mock(session_dir_manager=MagicMock(), tmux_manager=MagicMock())
        cog.bot.get_cog = MagicMock(return_value=cc)

    @pytest.mark.asyncio
    async def test_a_thread_is_created_and_the_skill_runs_in_it(self) -> None:
        cog = _make_cog(skills=[{"name": "goodmorning", "description": "Morning"}])
        self._setup_bound_channel(cog)
        new_thread = _claude_channel_with_new_thread(cog)
        interaction = _make_interaction(user_id=31)

        with patch(
            "c_lord.cogs.skill_command.run_claude_with_config", new_callable=AsyncMock
        ) as mock_run:
            await cog.run_skill.callback(cog, interaction, name="goodmorning", args=None)

        interaction.response.defer.assert_called_once()
        mock_run.assert_called_once()
        config = mock_run.call_args[0][0]
        assert config.thread is new_thread
        assert config.prompt == "/goodmorning"
        assert config.session_id is None
        assert config.repo is cog.repo
        assert config.registry is cog._registry
        assert config.notify_user_id == 31
        assert f"Running `/goodmorning` → {new_thread.mention}" in _replies(interaction)

    @pytest.mark.asyncio
    async def test_arguments_follow_the_skill_name_in_the_prompt(self) -> None:
        cog = _make_cog(skills=[{"name": "todoist", "description": ""}])
        self._setup_bound_channel(cog)
        new_thread = _claude_channel_with_new_thread(cog)
        interaction = _make_interaction()

        with patch(
            "c_lord.cogs.skill_command.run_claude_with_config", new_callable=AsyncMock
        ) as mock_run:
            await cog.run_skill.callback(cog, interaction, name="todoist", args='filter "today"')

        assert mock_run.call_args[0][0].prompt == '/todoist filter "today"'
        assert f'Running `/todoist filter "today"` → {new_thread.mention}' in _replies(interaction)

    @pytest.mark.asyncio
    async def test_the_thread_is_named_after_the_command_line(self) -> None:
        cog = _make_cog(skills=[{"name": "todoist", "description": ""}])
        self._setup_bound_channel(cog)
        _claude_channel_with_new_thread(cog)
        interaction = _make_interaction()

        with patch("c_lord.cogs.skill_command.run_claude_with_config", new_callable=AsyncMock):
            await cog.run_skill.callback(cog, interaction, name="todoist", args="today")

        call_kwargs = cog.bot.get_channel.return_value.create_thread.call_args.kwargs
        assert call_kwargs["name"] == "/todoist today"
        assert call_kwargs["type"] == discord.ChannelType.public_thread
        # Defaults to 3 days (4320 min) when no setting is configured.
        assert call_kwargs["auto_archive_duration"] == 4320

    @pytest.mark.asyncio
    async def test_a_long_command_line_is_cut_to_discords_thread_name_limit(self) -> None:
        cog = _make_cog(skills=[{"name": "todoist", "description": ""}])
        self._setup_bound_channel(cog)
        _claude_channel_with_new_thread(cog)
        interaction = _make_interaction()
        args = "x" * 300

        mock_run = await _slash(cog, interaction, "todoist", args)

        name = cog.bot.get_channel.return_value.create_thread.call_args.kwargs["name"]
        assert name == f"/todoist {args}"[:100]
        # Only the thread's name is cut — Claude still gets every argument.
        assert mock_run.call_args[0][0].prompt == f"/todoist {args}"

    @pytest.mark.asyncio
    async def test_a_missing_claude_channel_is_reported(self) -> None:
        cog = _make_cog(skills=[{"name": "test", "description": ""}])
        cog.bot.get_channel = MagicMock(return_value=None)
        interaction = _make_interaction()

        mock_run = await _slash(cog, interaction, "test")

        call = interaction.followup.send.call_args
        assert call.args[0] == "Claude channel not found."
        assert call.kwargs["ephemeral"] is True
        mock_run.assert_not_called()

    @pytest.mark.asyncio
    async def test_a_channel_that_is_not_a_text_channel_is_reported_the_same_way(self) -> None:
        cog = _make_cog(skills=[{"name": "test", "description": ""}])
        cog.bot.get_channel = MagicMock(return_value=MagicMock(spec=discord.VoiceChannel))
        interaction = _make_interaction()

        mock_run = await _slash(cog, interaction, "test")

        assert "Claude channel not found." in _replies(interaction)
        mock_run.assert_not_called()

    @pytest.mark.asyncio
    async def test_the_runner_is_built_for_a_slash_command(self) -> None:
        """#762: the prompt is ``/<name> …`` — the runner must send it as a command."""
        cog = _make_cog(skills=[{"name": "test", "description": ""}])
        tmux = MagicMock()
        cc = _make_channel_cog_mock(session_dir_manager=None, tmux_manager=tmux)
        cog.bot.get_cog = MagicMock(return_value=cc)
        new_thread = _claude_channel_with_new_thread(cog)
        interaction = _make_interaction()

        with patch("c_lord.cogs.skill_command.TmuxClaudeRunner") as runner_cls:
            mock_run = await _slash(cog, interaction, "test")

        kwargs = runner_cls.call_args.kwargs
        assert kwargs["tmux_manager"] is tmux
        assert kwargs["thread_id"] == new_thread.id
        assert kwargs["model"] == "sonnet"
        assert kwargs["timeout_seconds"] == 300
        assert kwargs["slash_command"] is True
        assert mock_run.call_args[0][0].runner is runner_cls.return_value


# ---------------------------------------------------------------------------
# /skill inside a Claude thread — the thread's own session
# ---------------------------------------------------------------------------


class TestInThreadMode:
    """Used inside a thread of the Claude channel, ``/skill`` runs in that thread."""

    def _setup_in_thread_cog(self, cog: SkillCommandCog) -> None:
        """Mock tmux/session_dir resolution for in-thread mode."""
        cog._resolve_tmux_manager = AsyncMock(return_value=MagicMock())
        cog._resolve_session_dir_manager = AsyncMock(return_value=MagicMock())

    @pytest.mark.asyncio
    async def test_the_threads_recorded_session_is_resumed(self) -> None:
        cog = _make_cog(skills=[{"name": "recall", "description": ""}])
        self._setup_in_thread_cog(cog)
        cog.repo.get = AsyncMock(return_value=_session_record("abc-123"))
        thread = _make_thread()
        interaction = _make_interaction(channel=thread, user_id=31)

        with patch(
            "c_lord.cogs.skill_command.run_claude_with_config", new_callable=AsyncMock
        ) as mock_run:
            await cog.run_skill.callback(cog, interaction, name="recall", args=None)

        cog.repo.get.assert_any_call(thread.id)
        config = mock_run.call_args[0][0]
        assert config.thread is thread
        assert config.session_id == "abc-123"
        assert config.prompt == "/recall"
        assert config.repo is cog.repo
        assert config.notify_user_id == 31
        assert "Running `/recall` in this thread…" in _replies(interaction)
        # No second thread is opened for it.
        cog.bot.get_channel.assert_not_called()

    @pytest.mark.asyncio
    async def test_a_thread_with_no_session_yet_starts_a_new_one(self) -> None:
        cog = _make_cog(skills=[{"name": "recall", "description": ""}])
        self._setup_in_thread_cog(cog)
        thread = _make_thread()
        interaction = _make_interaction(channel=thread)

        with patch(
            "c_lord.cogs.skill_command.run_claude_with_config", new_callable=AsyncMock
        ) as mock_run:
            await cog.run_skill.callback(cog, interaction, name="recall", args=None)

        config = mock_run.call_args[0][0]
        assert config.thread is thread
        assert config.session_id is None

    @pytest.mark.asyncio
    async def test_other_clords_thread_is_not_taken_over(self) -> None:
        """#811: /skill picked from the wrong bot names the owner and runs nothing."""
        other = 1517328788164182116
        cog = _make_cog(skills=[{"name": "recall", "description": ""}])
        self._setup_in_thread_cog(cog)
        cog.bot.user.id = 1475105094071750818
        cog.bot.get_user = MagicMock(return_value=MagicMock(bot=True))
        thread = _make_thread()
        thread.owner_id = other
        interaction = _make_interaction(channel=thread)

        with patch(
            "c_lord.cogs.skill_command.run_claude_with_config", new_callable=AsyncMock
        ) as mock_run:
            await cog.run_skill.callback(cog, interaction, name="recall", args=None)
            mock_run.assert_not_called()
        sent = [str(c) for c in interaction.followup.send.call_args_list]
        sent += [str(c) for c in interaction.response.send_message.call_args_list]
        assert any(f"<@{other}>" in s for s in sent), sent

    @pytest.mark.asyncio
    async def test_a_thread_of_another_channel_gets_a_new_thread_instead(self) -> None:
        """Only threads of the Claude channel carry a session to run in."""
        cog = _make_cog(skills=[{"name": "recall", "description": ""}])
        # Set up ChannelRepoCog so channel appears bound
        cc = _make_channel_cog_mock(session_dir_manager=MagicMock(), tmux_manager=MagicMock())
        cog.bot.get_cog = MagicMock(return_value=cc)
        new_thread = _claude_channel_with_new_thread(cog)
        elsewhere = _make_thread(parent_id=12321, thread_id=4444)
        interaction = _make_interaction(channel=elsewhere)

        with patch(
            "c_lord.cogs.skill_command.run_claude_with_config", new_callable=AsyncMock
        ) as mock_run:
            await cog.run_skill.callback(cog, interaction, name="recall", args=None)

        cog.bot.get_channel.return_value.create_thread.assert_called_once()
        config = mock_run.call_args[0][0]
        assert config.thread is new_thread
        assert config.session_id is None

    @pytest.mark.asyncio
    async def test_arguments_are_passed_on_in_the_thread(self) -> None:
        cog = _make_cog(skills=[{"name": "todoist", "description": ""}])
        self._setup_in_thread_cog(cog)
        cog.repo.get = AsyncMock(return_value=_session_record("s-9"))
        interaction = _make_interaction(channel=_make_thread())

        with patch(
            "c_lord.cogs.skill_command.run_claude_with_config", new_callable=AsyncMock
        ) as mock_run:
            await cog.run_skill.callback(cog, interaction, name="todoist", args="filter today")

        assert mock_run.call_args[0][0].prompt == "/todoist filter today"
        assert "Running `/todoist filter today` in this thread…" in _replies(interaction)

    @pytest.mark.asyncio
    async def test_a_thread_without_tmux_is_reported_and_nothing_runs(self) -> None:
        cog = _make_cog(skills=[{"name": "recall", "description": ""}])
        cog._resolve_tmux_manager = AsyncMock(return_value=None)
        cog._resolve_session_dir_manager = AsyncMock(return_value=None)
        interaction = _make_interaction(channel=_make_thread())

        mock_run = await _slash(cog, interaction, "recall")

        assert any("tmux is not configured" in reply for reply in _replies(interaction))
        mock_run.assert_not_called()

    @pytest.mark.asyncio
    async def test_the_thread_is_resolved_by_its_own_id(self) -> None:
        """#427: a thread bound to its own repo must not get the parent channel's tmux."""
        cog = _make_cog(skills=[{"name": "recall", "description": ""}])
        self._setup_in_thread_cog(cog)
        thread = _make_thread(thread_id=6060)

        await _slash(cog, _make_interaction(channel=thread), "recall")

        cog._resolve_tmux_manager.assert_awaited_once_with(CLAUDE_CHANNEL_ID, thread_id=6060)
        cog._resolve_session_dir_manager.assert_awaited_once_with(CLAUDE_CHANNEL_ID, thread_id=6060)


class TestIsClaudeThread:
    """``_is_claude_thread`` — is this a thread of the Claude channel?"""

    def test_a_thread_of_the_claude_channel(self) -> None:
        assert _make_cog()._is_claude_thread(_make_thread(parent_id=CLAUDE_CHANNEL_ID)) is True

    def test_a_thread_of_some_other_channel(self) -> None:
        assert _make_cog()._is_claude_thread(_make_thread(parent_id=1)) is False

    def test_a_channel_that_is_not_a_thread(self) -> None:
        channel = MagicMock(spec=discord.TextChannel)
        channel.id = CLAUDE_CHANNEL_ID
        assert _make_cog()._is_claude_thread(channel) is False


# ---------------------------------------------------------------------------
# Per-channel resolution
# ---------------------------------------------------------------------------


def _make_channel_cog_mock(
    *,
    session_dir_manager: object = None,
    tmux_manager: object = None,
) -> MagicMock:
    """Return a mock ChannelRepoCog with configurable resolve methods."""
    from c_lord.cogs.channel_repo import ChannelRepoCog

    channel_cog = MagicMock(spec=ChannelRepoCog)
    channel_cog.resolve_manager = AsyncMock(return_value=session_dir_manager)
    channel_cog.resolve_tmux_manager = AsyncMock(return_value=tmux_manager)
    return channel_cog


class TestPerChannelResolution:
    """SkillCommandCog should resolve managers per-channel via ChannelRepoCog."""

    @pytest.mark.asyncio
    async def test_in_thread_resolves_per_channel(self) -> None:
        """In-thread mode uses ChannelRepoCog to resolve managers."""
        sdm = MagicMock()
        tmux = MagicMock()
        cc = _make_channel_cog_mock(session_dir_manager=sdm, tmux_manager=tmux)

        cog = _make_cog(skills=[{"name": "test", "description": ""}])
        cog.bot.get_cog = MagicMock(return_value=cc)
        thread = _make_thread()
        interaction = _make_interaction(channel=thread)

        with patch(
            "c_lord.cogs.skill_command.run_claude_with_config", new_callable=AsyncMock
        ) as mock_run:
            await cog.run_skill.callback(cog, interaction, name="test", args=None)
            config = mock_run.call_args[0][0]
            assert config.session_dir_manager is sdm
            assert config.tmux_manager is tmux

    @pytest.mark.asyncio
    async def test_new_thread_resolves_per_channel(self) -> None:
        """New-thread mode uses ChannelRepoCog to resolve managers."""
        sdm = MagicMock()
        tmux = MagicMock()
        cc = _make_channel_cog_mock(session_dir_manager=sdm, tmux_manager=tmux)

        cog = _make_cog(skills=[{"name": "test", "description": ""}])
        cog.bot.get_cog = MagicMock(return_value=cc)

        mock_channel = MagicMock(spec=discord.TextChannel)
        mock_channel.id = 999
        mock_channel.create_thread = AsyncMock(return_value=_make_thread(thread_id=777))
        cog.bot.get_channel = MagicMock(return_value=mock_channel)

        interaction = _make_interaction()  # non-thread channel

        with patch(
            "c_lord.cogs.skill_command.run_claude_with_config", new_callable=AsyncMock
        ) as mock_run:
            await cog.run_skill.callback(cog, interaction, name="test", args=None)
            config = mock_run.call_args[0][0]
            assert config.session_dir_manager is sdm
            assert config.tmux_manager is tmux

    @pytest.mark.asyncio
    async def test_unbound_channel_returns_error(self) -> None:
        """Unbound channel (no /clord-init) returns an error."""
        # ChannelRepoCog returns None for both managers
        cc = _make_channel_cog_mock()
        cog = _make_cog(skills=[{"name": "test", "description": ""}])
        cog.bot.get_cog = MagicMock(return_value=cc)

        mock_channel = MagicMock(spec=discord.TextChannel)
        mock_channel.id = 777  # unbound
        mock_channel.create_thread = AsyncMock()
        cog.bot.get_channel = MagicMock(return_value=mock_channel)
        interaction = _make_interaction()  # non-thread channel

        await _slash(cog, interaction, "test")

        # Should have sent error mentioning /clord-init
        mock_channel.create_thread.assert_not_called()
        msg = interaction.followup.send.call_args.args[0]
        assert "/clord-init" in msg

    @pytest.mark.asyncio
    async def test_without_a_channel_repo_cog_the_channel_counts_as_unbound(self) -> None:
        """No ChannelRepoCog loaded → no tmux to resolve → the same /clord-init hint."""
        cog = _make_cog(skills=[{"name": "test", "description": ""}])
        cog.bot.get_cog = MagicMock(return_value=None)
        _claude_channel_with_new_thread(cog)
        interaction = _make_interaction()

        mock_run = await _slash(cog, interaction, "test")

        assert "/clord-init" in interaction.followup.send.call_args.args[0]
        mock_run.assert_not_called()

    @pytest.mark.asyncio
    async def test_a_new_thread_is_resolved_through_the_claude_channel(self) -> None:
        """No thread exists yet, so the lookup carries the channel and ``thread_id=None``."""
        cc = _make_channel_cog_mock(session_dir_manager=MagicMock(), tmux_manager=MagicMock())
        cog = _make_cog(skills=[{"name": "test", "description": ""}])
        cog.bot.get_cog = MagicMock(return_value=cc)
        _claude_channel_with_new_thread(cog)

        await _slash(cog, _make_interaction(), "test")

        cc.resolve_tmux_manager.assert_awaited_once_with(CLAUDE_CHANNEL_ID, thread_id=None)


# ---------------------------------------------------------------------------
# !skill text/mention twin (E2E-testable via webhook)
# ---------------------------------------------------------------------------


def _make_ctx(channel: MagicMock | None = None, author_id: int = 1) -> MagicMock:
    """Return a mocked commands.Context for the !skill text twin.

    ``ctx.message`` is a *human* message: since #508 the text twin authorizes on
    the message (webhook / trusted bot / allowlisted human), so leaving it an
    unconfigured mock would make every author look like a webhook.
    """
    ctx = MagicMock()
    ctx.send = AsyncMock()
    ctx.author = MagicMock()
    ctx.author.id = author_id
    ctx.message = MagicMock(spec=discord.Message)
    ctx.message.webhook_id = None
    ctx.message.author = ctx.author
    ctx.message.author.bot = False
    if channel is not None:
        ctx.channel = channel
    else:
        ctx.channel = MagicMock(spec=discord.TextChannel)
    return ctx


class TestSkillTextCommand:
    """The !skill text command mirrors /skill but is invokable from webhooks."""

    @pytest.mark.asyncio
    async def test_missing_name_shows_usage(self) -> None:
        cog = _make_cog(skills=[])
        ctx = _make_ctx()
        await cog.run_skill_text.callback(cog, ctx, name=None, args=None)
        ctx.send.assert_called_once()
        assert "Usage" in ctx.send.call_args.args[0]

    @pytest.mark.asyncio
    async def test_a_user_outside_the_allowlist_is_refused(self) -> None:
        cog = _make_cog(skills=[{"name": "test", "description": ""}], allowed_user_ids={1})
        ctx = _make_ctx(author_id=99)
        await cog.run_skill_text.callback(cog, ctx, name="test", args=None)
        ctx.send.assert_called_once()
        assert "permission" in ctx.send.call_args.args[0].lower()

    @pytest.mark.asyncio
    async def test_an_invalid_name_is_refused(self) -> None:
        cog = _make_cog(skills=[])
        ctx = _make_ctx()
        await cog.run_skill_text.callback(cog, ctx, name="bad;name", args=None)
        assert "Invalid" in ctx.send.call_args.args[0]

    @pytest.mark.asyncio
    async def test_an_unknown_skill_is_refused(self) -> None:
        cog = _make_cog(skills=[{"name": "known", "description": ""}])
        ctx = _make_ctx()
        await cog.run_skill_text.callback(cog, ctx, name="missing", args=None)
        assert "not found" in ctx.send.call_args.args[0]

    @pytest.mark.asyncio
    async def test_in_thread_runs_with_prompt_and_args(self) -> None:
        cog = _make_cog(skills=[{"name": "recall", "description": ""}])
        cog._resolve_tmux_manager = AsyncMock(return_value=MagicMock())
        cog._resolve_session_dir_manager = AsyncMock(return_value=MagicMock())
        cog._make_runner = MagicMock(return_value=MagicMock())
        thread = _make_thread()
        ctx = _make_ctx(channel=thread)

        with patch(
            "c_lord.cogs.skill_command.run_claude_with_config", new_callable=AsyncMock
        ) as mock_run:
            await cog.run_skill_text.callback(cog, ctx, name="recall", args="filter today")
            config = mock_run.call_args[0][0]
            assert config.prompt == "/recall filter today"
            assert config.thread is thread
            assert config.session_id is None
