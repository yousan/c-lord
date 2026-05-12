"""Tests for c_lord.skills — discord-reply skill injection (#52)."""

from __future__ import annotations

from pathlib import Path

import pytest

from c_lord.skills import inject_skills, render_discord_reply_skill
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
        body = render_discord_reply_skill(
            thread_id=1, api_url="http://x", api_secret="s3cret-xyz"
        )
        assert "Authorization: Bearer s3cret-xyz" in body

    def test_omits_bearer_header_when_no_secret(self) -> None:
        body = render_discord_reply_skill(thread_id=1, api_url="http://x")
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


class TestSkillsEnabled:
    @pytest.mark.parametrize("value", ["1", "true", "TRUE", "Yes", "on"])
    def test_truthy_values(self, value: str, monkeypatch) -> None:
        monkeypatch.setenv("USE_SKILL_REPLY", value)
        assert skills_enabled() is True

    @pytest.mark.parametrize("value", ["0", "false", "no", "off", ""])
    def test_falsy_values(self, value: str, monkeypatch) -> None:
        monkeypatch.setenv("USE_SKILL_REPLY", value)
        assert skills_enabled() is False

    def test_unset(self, monkeypatch) -> None:
        monkeypatch.delenv("USE_SKILL_REPLY", raising=False)
        assert skills_enabled() is False
