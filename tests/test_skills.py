"""Tests for c_lord.skills — discord-reply skill injection (#52)."""

from __future__ import annotations

from pathlib import Path

import pytest

from c_lord.skills import (
    inject_read_skill,
    inject_skills,
    remove_injected_skills,
    render_discord_prompt_choice_skill,
    render_discord_read_skill,
    render_discord_reply_skill,
)
from c_lord.skills.injector import skills_enabled


class TestRenderDiscordReplySkill:
    def test_substitutes_thread_id_and_api_url(self) -> None:
        body = render_discord_reply_skill(thread_id=987654321, api_url="http://x:9999")
        assert "987654321" in body
        assert "http://x:9999" in body
        # YAML frontmatter is present
        assert body.startswith("---\nname: discord-reply")
        # No unfilled placeholders
        assert "{thread_id}" not in body
        assert "{api_url}" not in body

    def test_curl_payload_is_valid_template(self) -> None:
        """The JSON curl example must include the actual thread_id literal."""
        body = render_discord_reply_skill(thread_id=42, api_url="http://x")
        assert '"thread_id": 42' in body

    def test_no_multipart_examples(self) -> None:
        """The endpoint is JSON-only — SKILL.md must not show -F multipart."""
        body = render_discord_reply_skill(thread_id=1, api_url="http://x")
        # `-F` would mean multipart form-data which /api/reply does NOT accept.
        # `-d` (data) is JSON which it does accept.
        assert " -F " not in body, "SKILL.md must not advertise multipart form-data"

    def test_progress_file_uses_absolute_path(self) -> None:
        """`progress_file` is an absolute server-side path, not @local-file."""
        body = render_discord_reply_skill(thread_id=7, api_url="http://x")
        assert "progress_file" in body
        # Make sure we don't suggest the multipart `@path` upload syntax.
        assert '"progress_file": "@' not in body
        assert '"progress_file": "/' in body

    def test_renders_bearer_header_when_secret_set(self) -> None:
        body = render_discord_reply_skill(thread_id=1, api_url="http://x", api_secret="s3cret-xyz")
        assert "Authorization: Bearer s3cret-xyz" in body

    def test_omits_bearer_header_when_no_secret(self) -> None:
        body = render_discord_reply_skill(thread_id=1, api_url="http://x")
        assert "Bearer" not in body
        assert "Authorization" not in body


class TestRenderDiscordPromptChoiceSkill:
    """Issue #63: Skill that lets Claude post a choice prompt to Discord."""

    def test_substitutes_thread_id_and_api_url(self) -> None:
        body = render_discord_prompt_choice_skill(thread_id=123456, api_url="http://x:1111")
        assert "123456" in body
        assert "http://x:1111" in body
        assert body.startswith("---\nname: discord-prompt-choice")
        assert "{thread_id}" not in body
        assert "{api_url}" not in body

    def test_curl_targets_prompt_choice_endpoint(self) -> None:
        body = render_discord_prompt_choice_skill(thread_id=1, api_url="http://x")
        assert "/api/prompt-choice" in body
        assert "Content-Type: application/json" in body
        assert " -F " not in body  # JSON only, no multipart

    def test_documents_question_and_choices_fields(self) -> None:
        body = render_discord_prompt_choice_skill(thread_id=1, api_url="http://x")
        assert "question" in body
        assert "choices" in body
        # Each choice has label + description (numbered options pattern)
        assert "label" in body
        assert "description" in body

    def test_documents_when_to_invoke(self) -> None:
        """Skill description must direct Claude to use it for choice prompts."""
        body = render_discord_prompt_choice_skill(thread_id=1, api_url="http://x")
        # The frontmatter description must clearly signal *when* to invoke.
        # We deliberately don't pin exact wording — just key concepts.
        head = body.split("---\n", 2)[1].lower()
        assert "choice" in head or "選択" in head or "option" in head

    def test_renders_bearer_header_when_secret_set(self) -> None:
        body = render_discord_prompt_choice_skill(
            thread_id=1, api_url="http://x", api_secret="sk-cc"
        )
        assert "Authorization: Bearer sk-cc" in body

    def test_omits_bearer_header_when_no_secret(self) -> None:
        body = render_discord_prompt_choice_skill(thread_id=1, api_url="http://x")
        assert "Bearer" not in body
        assert "Authorization" not in body


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


class TestInjectSkills:
    def test_writes_discord_reply_skill_md(self, tmp_path: Path) -> None:
        session_dir = tmp_path / "12345"
        session_dir.mkdir()

        paths = inject_skills(session_dir, thread_id=42, api_url="http://x:1234")

        skill_md = session_dir / ".claude" / "skills" / "discord-reply" / "SKILL.md"
        assert skill_md.exists()
        assert str(skill_md) in paths
        body = skill_md.read_text()
        assert "42" in body
        assert "http://x:1234" in body

    def test_idempotent_overwrites(self, tmp_path: Path) -> None:
        session_dir = tmp_path / "99"
        session_dir.mkdir()
        inject_skills(session_dir, thread_id=1, api_url="http://a")
        inject_skills(session_dir, thread_id=1, api_url="http://b")
        body = (session_dir / ".claude" / "skills" / "discord-reply" / "SKILL.md").read_text()
        assert "http://b" in body
        assert "http://a" not in body

    def test_strips_trailing_slash_from_api_url(self, tmp_path: Path) -> None:
        session_dir = tmp_path / "1"
        session_dir.mkdir()
        inject_skills(session_dir, thread_id=1, api_url="http://x:9/")
        body = (session_dir / ".claude" / "skills" / "discord-reply" / "SKILL.md").read_text()
        assert "http://x:9/api/reply" in body

    def test_falls_back_to_env_var(self, tmp_path: Path, monkeypatch) -> None:
        monkeypatch.setenv("CLORD_API_URL", "http://envset:7")
        session_dir = tmp_path / "1"
        session_dir.mkdir()
        inject_skills(session_dir, thread_id=1)
        body = (session_dir / ".claude" / "skills" / "discord-reply" / "SKILL.md").read_text()
        assert "http://envset:7" in body

    def test_default_url_when_no_env(self, tmp_path: Path, monkeypatch) -> None:
        monkeypatch.delenv("CLORD_API_URL", raising=False)
        session_dir = tmp_path / "1"
        session_dir.mkdir()
        inject_skills(session_dir, thread_id=1)
        body = (session_dir / ".claude" / "skills" / "discord-reply" / "SKILL.md").read_text()
        assert "http://127.0.0.1:8080" in body

    def test_reads_api_secret_from_env(self, tmp_path: Path, monkeypatch) -> None:
        monkeypatch.setenv("CLORD_API_SECRET", "env-secret-99")
        session_dir = tmp_path / "1"
        session_dir.mkdir()
        inject_skills(session_dir, thread_id=1, api_url="http://x")
        body = (session_dir / ".claude" / "skills" / "discord-reply" / "SKILL.md").read_text()
        assert "Authorization: Bearer env-secret-99" in body

    def test_explicit_api_secret_overrides_env(self, tmp_path: Path, monkeypatch) -> None:
        monkeypatch.setenv("CLORD_API_SECRET", "from-env")
        session_dir = tmp_path / "1"
        session_dir.mkdir()
        inject_skills(session_dir, thread_id=1, api_url="http://x", api_secret="explicit")
        body = (session_dir / ".claude" / "skills" / "discord-reply" / "SKILL.md").read_text()
        assert "Bearer explicit" in body
        assert "from-env" not in body

    def test_no_auth_header_when_no_secret(self, tmp_path: Path, monkeypatch) -> None:
        monkeypatch.delenv("CLORD_API_SECRET", raising=False)
        session_dir = tmp_path / "1"
        session_dir.mkdir()
        inject_skills(session_dir, thread_id=1, api_url="http://x")
        body = (session_dir / ".claude" / "skills" / "discord-reply" / "SKILL.md").read_text()
        assert "Authorization" not in body

    def test_also_writes_prompt_choice_skill_md(self, tmp_path: Path) -> None:
        """Issue #63: prompt-choice skill is injected alongside discord-reply."""
        session_dir = tmp_path / "thr"
        session_dir.mkdir()
        paths = inject_skills(session_dir, thread_id=88, api_url="http://x:2222")

        choice_md = session_dir / ".claude" / "skills" / "discord-prompt-choice" / "SKILL.md"
        assert choice_md.exists()
        assert str(choice_md) in paths
        body = choice_md.read_text()
        assert "88" in body
        assert "http://x:2222" in body
        assert "/api/prompt-choice" in body

    def test_also_writes_discord_read_skill_md(self, tmp_path: Path) -> None:
        """Issue #259: discord-read skill is injected alongside the others."""
        session_dir = tmp_path / "thr"
        session_dir.mkdir()
        paths = inject_skills(session_dir, thread_id=88, api_url="http://x:2222")

        read_md = session_dir / ".claude" / "skills" / "discord-read" / "SKILL.md"
        assert read_md.exists()
        assert str(read_md) in paths
        body = read_md.read_text()
        assert "discord.com/api/" in body
        assert "mcp" in body.lower()

    def test_inject_read_uses_env_path_arg(self, tmp_path: Path) -> None:
        session_dir = tmp_path / "1"
        session_dir.mkdir()
        inject_skills(session_dir, thread_id=1, api_url="http://x", env_path="/opt/clord/.env")
        body = (session_dir / ".claude" / "skills" / "discord-read" / "SKILL.md").read_text()
        assert "/opt/clord/.env" in body

    def test_inject_read_falls_back_to_env_var(self, tmp_path: Path, monkeypatch) -> None:
        monkeypatch.setenv("CLORD_ENV_PATH", "/from/env/.env")
        session_dir = tmp_path / "1"
        session_dir.mkdir()
        inject_skills(session_dir, thread_id=1, api_url="http://x")
        body = (session_dir / ".claude" / "skills" / "discord-read" / "SKILL.md").read_text()
        assert "/from/env/.env" in body


class TestInjectReadSkill:
    """Issue #259: discord-read has an independent, bridge-mode-agnostic
    lifecycle — it is injected in both skill-reply and jsonl modes."""

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

    def test_survives_remove_injected_skills(self, tmp_path: Path) -> None:
        """The jsonl-mode path: output skills are scrubbed but read stays."""
        session_dir = tmp_path / "1"
        session_dir.mkdir()
        inject_read_skill(session_dir, env_path="/x/.env")
        remove_injected_skills(session_dir)
        assert (session_dir / ".claude" / "skills" / "discord-read" / "SKILL.md").exists()


class TestRemoveInjectedSkills:
    """When skill reply is disabled (e.g. CLORD_BRIDGE_MODE=jsonl) a stale skill
    from a previous skill-mode session must be scrubbed so it can't point Claude
    at a now-dead REST API."""

    def test_removes_both_injected_skill_dirs(self, tmp_path: Path) -> None:
        session_dir = tmp_path / "1"
        session_dir.mkdir()
        inject_skills(session_dir, thread_id=1, api_url="http://x")
        reply = session_dir / ".claude" / "skills" / "discord-reply"
        choice = session_dir / ".claude" / "skills" / "discord-prompt-choice"
        read = session_dir / ".claude" / "skills" / "discord-read"
        assert reply.exists() and choice.exists() and read.exists()

        removed = remove_injected_skills(session_dir)

        assert not reply.exists()
        assert not choice.exists()
        assert str(reply) in removed
        assert str(choice) in removed
        # #259: discord-read is bridge-independent (curls Discord directly, not
        # the c-lord REST API), so it is NOT scrubbed by remove_injected_skills.
        assert read.exists()
        assert str(read) not in removed

    def test_idempotent_when_absent(self, tmp_path: Path) -> None:
        session_dir = tmp_path / "1"
        session_dir.mkdir()
        assert remove_injected_skills(session_dir) == []

    def test_leaves_user_skills_intact(self, tmp_path: Path) -> None:
        session_dir = tmp_path / "1"
        session_dir.mkdir()
        inject_skills(session_dir, thread_id=1, api_url="http://x")
        other = session_dir / ".claude" / "skills" / "my-custom-skill"
        other.mkdir(parents=True)
        (other / "SKILL.md").write_text("custom")

        remove_injected_skills(session_dir)

        assert other.exists()
        assert (other / "SKILL.md").read_text() == "custom"


class TestSkillsEnabled:
    """Issue #53: skill path is now the ONLY path → default ON.

    USE_SKILL_REPLY remains as an opt-out emergency switch. Empty string
    keeps the default (= ON) rather than disabling, so accidentally
    setting USE_SKILL_REPLY= does not silently kill Discord output.
    """

    @pytest.mark.parametrize("value", ["1", "true", "TRUE", "Yes", "on"])
    def test_truthy_values_enabled(self, value: str, monkeypatch) -> None:
        monkeypatch.setenv("USE_SKILL_REPLY", value)
        assert skills_enabled() is True

    @pytest.mark.parametrize("value", ["0", "false", "no", "off"])
    def test_explicit_disable(self, value: str, monkeypatch) -> None:
        monkeypatch.setenv("USE_SKILL_REPLY", value)
        assert skills_enabled() is False

    def test_empty_value_keeps_default(self, monkeypatch) -> None:
        monkeypatch.setenv("USE_SKILL_REPLY", "")
        assert skills_enabled() is True

    def test_unset_defaults_to_enabled(self, monkeypatch) -> None:
        monkeypatch.delenv("USE_SKILL_REPLY", raising=False)
        assert skills_enabled() is True
