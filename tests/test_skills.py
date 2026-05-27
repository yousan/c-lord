"""Tests for c_lord.skills — discord-reply skill injection (#52)."""

from __future__ import annotations

from pathlib import Path

import pytest

from c_lord.skills import (
    inject_skills,
    remove_injected_skills,
    render_discord_prompt_choice_skill,
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
        assert reply.exists() and choice.exists()

        removed = remove_injected_skills(session_dir)

        assert not reply.exists()
        assert not choice.exists()
        assert str(reply) in removed
        assert str(choice) in removed

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
