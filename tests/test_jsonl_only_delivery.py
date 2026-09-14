"""Issue #712: jsonl is the only delivery path, and the REST API is unconditional.

Two things are asserted here:

* the legacy skill-push bridge (#53) is gone — no env var can bring it back, and
  the modules that rendered ``discord-reply`` / ``discord-prompt-choice``
  SKILL.md no longer exist (``discord-read`` is a different skill and stays,
  #259);
* the REST API control plane starts regardless of any delivery setting, which is
  what #543 was about — ``main.py`` used to gate it on ``skills_enabled()``.
"""

from __future__ import annotations

import logging
import socket
from pathlib import Path
from unittest.mock import AsyncMock, MagicMock

import pytest
from aiohttp.test_utils import TestClient, TestServer

from c_lord.legacy_env import warn_removed_delivery_env
from c_lord.main import build_api_server, start_api_server


@pytest.fixture(autouse=True)
def _clean_delivery_env(monkeypatch: pytest.MonkeyPatch) -> None:
    """Start every test from the zero-config state (nothing set)."""
    for key in ("CLORD_BRIDGE_MODE", "USE_SKILL_REPLY", "CLORD_API_PORT", "CLORD_API_HOST"):
        monkeypatch.delenv(key, raising=False)


@pytest.fixture
def bot() -> MagicMock:
    b = MagicMock()
    channel = MagicMock()
    channel.send = AsyncMock()
    b.get_channel.return_value = channel
    return b


class TestSkillBridgeIsGone:
    """AC1 / AC2 — the skill-push path is not reachable by any means."""

    def test_reply_skill_module_removed(self) -> None:
        with pytest.raises(ImportError):
            import c_lord.skills.discord_reply  # noqa: F401

    def test_prompt_choice_skill_module_removed(self) -> None:
        with pytest.raises(ImportError):
            import c_lord.skills.discord_prompt_choice  # noqa: F401

    def test_injector_has_no_skill_push_entry_points(self) -> None:
        from c_lord.skills import injector

        assert not hasattr(injector, "inject_skills")
        assert not hasattr(injector, "skills_enabled")

    def test_discord_read_skill_survives(self) -> None:
        """#259: discord-read curls the Discord API, not c-lord's — it stays."""
        from c_lord.skills import inject_read_skill, render_discord_read_skill

        assert callable(inject_read_skill)
        assert callable(render_discord_read_skill)

    def test_transcript_mirror_has_no_bridge_mode_gate(self) -> None:
        from c_lord.transcript import mirror

        assert not hasattr(mirror, "bridge_mode_jsonl")


class TestLegacyEnvWarning:
    """AC5 — an operator who still sets the removed vars is told, not surprised."""

    def test_warns_when_skill_mode_still_requested(
        self, monkeypatch: pytest.MonkeyPatch, caplog: pytest.LogCaptureFixture
    ) -> None:
        monkeypatch.setenv("CLORD_BRIDGE_MODE", "skill")
        with caplog.at_level(logging.WARNING, logger="c_lord.legacy_env"):
            warned = warn_removed_delivery_env()

        assert warned == ["CLORD_BRIDGE_MODE"]
        assert any("CLORD_BRIDGE_MODE" in r.message for r in caplog.records)
        assert any(r.levelno == logging.WARNING for r in caplog.records)

    def test_warns_when_use_skill_reply_still_enabled(
        self, monkeypatch: pytest.MonkeyPatch, caplog: pytest.LogCaptureFixture
    ) -> None:
        monkeypatch.setenv("USE_SKILL_REPLY", "true")
        with caplog.at_level(logging.WARNING, logger="c_lord.legacy_env"):
            warned = warn_removed_delivery_env()

        assert warned == ["USE_SKILL_REPLY"]

    def test_leftover_jsonl_value_is_not_a_warning(
        self, monkeypatch: pytest.MonkeyPatch, caplog: pytest.LogCaptureFixture
    ) -> None:
        """Every live deployment has ``CLORD_BRIDGE_MODE=jsonl`` in its .env — it
        asked for what it gets, so this is an INFO nudge to delete the line."""
        monkeypatch.setenv("CLORD_BRIDGE_MODE", "jsonl")
        with caplog.at_level(logging.INFO, logger="c_lord.legacy_env"):
            warned = warn_removed_delivery_env()

        assert warned == []
        assert any(r.levelno == logging.INFO for r in caplog.records)

    def test_silent_when_nothing_is_set(self, caplog: pytest.LogCaptureFixture) -> None:
        with caplog.at_level(logging.INFO, logger="c_lord.legacy_env"):
            assert warn_removed_delivery_env() == []
        assert caplog.records == []

    def test_empty_value_is_treated_as_unset(self, monkeypatch: pytest.MonkeyPatch) -> None:
        monkeypatch.setenv("CLORD_BRIDGE_MODE", "")
        assert warn_removed_delivery_env() == []


class TestApiServerStartsByDefault:
    """AC3 / #543 — the control plane no longer depends on the delivery path."""

    @pytest.mark.asyncio
    async def test_built_with_zero_config(self, bot: MagicMock, tmp_path: Path) -> None:
        api = await build_api_server(bot, default_channel_id=12345, data_dir=tmp_path)

        assert api is not None
        assert api.port == 8080
        assert api.host == "127.0.0.1"

    @pytest.mark.asyncio
    async def test_built_even_when_legacy_skill_mode_is_set(
        self, bot: MagicMock, tmp_path: Path, monkeypatch: pytest.MonkeyPatch
    ) -> None:
        """The removed env must not be able to switch the control plane off."""
        monkeypatch.setenv("CLORD_BRIDGE_MODE", "jsonl")
        monkeypatch.setenv("USE_SKILL_REPLY", "0")
        monkeypatch.setenv("CLORD_API_PORT", "8087")

        api = await build_api_server(bot, default_channel_id=12345, data_dir=tmp_path)

        assert api is not None
        assert api.port == 8087

    @pytest.mark.asyncio
    async def test_start_returns_true_and_binds(self, bot: MagicMock, tmp_path: Path) -> None:
        with socket.socket() as probe:
            probe.bind(("127.0.0.1", 0))
            free_port = probe.getsockname()[1]

        api = await build_api_server(
            bot, default_channel_id=1, data_dir=tmp_path, port_override=free_port
        )
        assert api is not None
        try:
            assert await start_api_server(api) is True
        finally:
            await api.stop()

    @pytest.mark.asyncio
    async def test_bind_failure_warns_instead_of_killing_the_bot(
        self, bot: MagicMock, tmp_path: Path, caplog: pytest.LogCaptureFixture
    ) -> None:
        """#543 AC4: a taken port must not crash startup — and must not be silent.

        Zero-config now means every bot tries to bind 8080 by default, so a
        second bot on one host is an ordinary case, not an exceptional one.
        """
        with socket.socket() as taken:
            taken.bind(("127.0.0.1", 0))
            taken.listen(1)
            port = taken.getsockname()[1]

            api = await build_api_server(
                bot, default_channel_id=1, data_dir=tmp_path, port_override=port
            )
            assert api is not None
            with caplog.at_level(logging.WARNING, logger="c_lord.main"):
                started = await start_api_server(api)

        assert started is False
        assert any(
            r.levelno == logging.WARNING and "CLORD_API_PORT" in r.getMessage()
            for r in caplog.records
        )


class TestControlPlaneEndpoints:
    """#543 AC2 — /api/health, /api/spawn and /api/tasks answer 2xx by default."""

    @pytest.fixture
    async def client(self, bot: MagicMock, tmp_path: Path) -> TestClient:
        from c_lord.database.task_repo import TaskRepository

        api = await build_api_server(bot, default_channel_id=12345, data_dir=tmp_path)
        assert api is not None

        task_repo = TaskRepository(str(tmp_path / "tasks.db"))
        await task_repo.init_db()
        api.task_repo = task_repo

        import discord

        thread = MagicMock()
        thread.id = 999
        thread.name = "spawned"
        cog = MagicMock()
        cog.spawn_session = AsyncMock(return_value=thread)
        bot.cogs = {"ClaudeChatCog": cog}
        bot.get_channel.return_value = MagicMock(spec=discord.TextChannel)

        server = TestServer(api.app)
        client = TestClient(server)
        await client.start_server()
        yield client
        await client.close()

    @pytest.mark.asyncio
    async def test_health(self, client: TestClient) -> None:
        resp = await client.get("/api/health")
        assert resp.status == 200

    @pytest.mark.asyncio
    async def test_spawn(self, client: TestClient) -> None:
        resp = await client.post("/api/spawn", json={"prompt": "hello"})
        assert 200 <= resp.status < 300

    @pytest.mark.asyncio
    async def test_tasks(self, client: TestClient) -> None:
        resp = await client.post(
            "/api/tasks",
            json={
                "name": "nightly",
                "prompt": "check the queue",
                "interval_seconds": 3600,
                "channel_id": 12345,
            },
        )
        assert 200 <= resp.status < 300

        listed = await client.get("/api/tasks")
        assert listed.status == 200
