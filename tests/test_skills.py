"""Tests for c_lord.skills — the ``discord-read`` skill (#259) and the scrub of
the retired skill-push bundle (#712)."""

from __future__ import annotations

from pathlib import Path

from c_lord.skills import (
    inject_read_skill,
    remove_legacy_skills,
    render_discord_read_skill,
)


class TestRenderDiscordReadSkill:
    """Issue #259: spawn-read skill — tells Claude to read other Discord
    channels/threads via curl + the c-lord bot token (read at runtime from
    the c-lord .env), NOT via the MCP plugin. The token is never baked into
    the SKILL.md (it lives in the user repo working tree → git-leak risk);
    Claude reads it into a shell variable at runtime."""

    def test_frontmatter_name(self) -> None:
        body = render_discord_read_skill(env_path="/srv/c-lord/.env")
        assert body.startswith("---\nname: discord-read")

    def test_includes_env_path_when_given(self) -> None:
        body = render_discord_read_skill(env_path="/srv/c-lord/.env")
        assert "/srv/c-lord/.env" in body
        assert "{env_path}" not in body

    def test_reads_token_into_variable_not_literal(self) -> None:
        """The token must be read at runtime via a shell var, never printed
        as a literal — otherwise it leaks into the (#71-mirrored) transcript."""
        body = render_discord_read_skill(env_path="/srv/c-lord/.env")
        # token is pulled from .env into a variable then used as $VAR
        assert "DISCORD_BOT_TOKEN" in body
        assert "grep" in body
        # curl uses the bot auth scheme via a variable expansion ($...)
        assert "Authorization: Bot $" in body

    def test_points_at_discord_rest_api(self) -> None:
        body = render_discord_read_skill(env_path="/x/.env")
        assert "discord.com/api/" in body
        assert "/messages" in body

    def test_forbids_mcp_and_mandates_fallback(self) -> None:
        """The whole point of #454: don't use the MCP plugin; if it returns
        Missing Access / not allowlisted, fall back to curl instead of giving
        up."""
        low = render_discord_read_skill(env_path="/x/.env").lower()
        assert "mcp" in low
        # must reference the give-up trigger and a fallback instruction
        assert "missing access" in low or "allowlist" in low
        assert "fall back" in low or "fallback" in low

    def test_does_not_leak_a_literal_token(self) -> None:
        """No raw token value is ever substituted into the body."""
        body = render_discord_read_skill(env_path="/x/.env")
        # A real bot token never appears because we only know the .env path.
        assert "Bot " in body  # the scheme word is fine
        # but there must be a variable expansion, proving runtime read
        assert "$" in body

    def test_usable_without_env_path(self) -> None:
        """When the .env path is unknown, the skill still instructs Claude to
        read DISCORD_BOT_TOKEN from c-lord's .env — no unfilled placeholder."""
        body = render_discord_read_skill()
        assert "{env_path}" not in body
        assert "DISCORD_BOT_TOKEN" in body


class TestInjectReadSkill:
    """Issue #259: discord-read is injected into every session dir."""

    def test_writes_read_skill_md(self, tmp_path: Path) -> None:
        session_dir = tmp_path / "1"
        session_dir.mkdir()
        path = inject_read_skill(session_dir, env_path="/opt/clord/.env")
        read_md = session_dir / ".claude" / "skills" / "discord-read" / "SKILL.md"
        assert read_md.exists()
        assert str(read_md) == path
        body = read_md.read_text()
        assert "/opt/clord/.env" in body
        assert "discord.com/api/" in body

    def test_falls_back_to_env_var(self, tmp_path: Path, monkeypatch) -> None:
        monkeypatch.setenv("CLORD_ENV_PATH", "/from/env/.env")
        session_dir = tmp_path / "1"
        session_dir.mkdir()
        inject_read_skill(session_dir)
        body = (session_dir / ".claude" / "skills" / "discord-read" / "SKILL.md").read_text()
        assert "/from/env/.env" in body

    def test_idempotent_overwrites(self, tmp_path: Path) -> None:
        session_dir = tmp_path / "1"
        session_dir.mkdir()
        inject_read_skill(session_dir, env_path="/a/.env")
        inject_read_skill(session_dir, env_path="/b/.env")
        body = (session_dir / ".claude" / "skills" / "discord-read" / "SKILL.md").read_text()
        assert "/b/.env" in body
        assert "/a/.env" not in body

    def test_survives_the_legacy_scrub(self, tmp_path: Path) -> None:
        """#259: discord-read curls Discord's API, not c-lord's — it stays."""
        session_dir = tmp_path / "1"
        session_dir.mkdir()
        inject_read_skill(session_dir, env_path="/x/.env")
        remove_legacy_skills(session_dir)
        assert (session_dir / ".claude" / "skills" / "discord-read" / "SKILL.md").exists()


def _plant_legacy_skills(session_dir: Path) -> tuple[Path, Path]:
    """Write the SKILL.md files an older c-lord injected for the skill-push path."""
    skills = session_dir / ".claude" / "skills"
    reply = skills / "discord-reply"
    choice = skills / "discord-prompt-choice"
    for d in (reply, choice):
        d.mkdir(parents=True)
        (d / "SKILL.md").write_text("legacy")
    return reply, choice


class TestRemoveLegacySkills:
    """#712: a session dir created before the upgrade still carries the skill
    telling Claude to POST its answer to the REST API — which is listening again
    — so the answer would land twice. Every turn scrubs it."""

    def test_removes_both_legacy_skill_dirs(self, tmp_path: Path) -> None:
        session_dir = tmp_path / "1"
        session_dir.mkdir()
        reply, choice = _plant_legacy_skills(session_dir)
        inject_read_skill(session_dir, env_path="/x/.env")
        read = session_dir / ".claude" / "skills" / "discord-read"

        removed = remove_legacy_skills(session_dir)

        assert not reply.exists()
        assert not choice.exists()
        assert str(reply) in removed
        assert str(choice) in removed
        assert read.exists()
        assert str(read) not in removed

    def test_idempotent_when_absent(self, tmp_path: Path) -> None:
        session_dir = tmp_path / "1"
        session_dir.mkdir()
        assert remove_legacy_skills(session_dir) == []

    def test_leaves_user_skills_intact(self, tmp_path: Path) -> None:
        session_dir = tmp_path / "1"
        session_dir.mkdir()
        _plant_legacy_skills(session_dir)
        other = session_dir / ".claude" / "skills" / "my-custom-skill"
        other.mkdir(parents=True)
        (other / "SKILL.md").write_text("custom")

        remove_legacy_skills(session_dir)

        assert other.exists()
        assert (other / "SKILL.md").read_text() == "custom"
