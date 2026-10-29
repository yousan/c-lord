"""c-lord must never obey the webhook it speaks for sub-agents through (#971)."""

from __future__ import annotations

import json
from pathlib import Path
from unittest.mock import AsyncMock, MagicMock

import discord
import pytest

from c_lord import voice_webhooks
from c_lord.bot import ClaudeDiscordBot
from c_lord.command_gate import is_message_authorized

VOICE = 4242
DISPATCH = 999


@pytest.fixture(autouse=True)
def _registry(tmp_path: Path, monkeypatch: pytest.MonkeyPatch):
    monkeypatch.setattr(voice_webhooks, "registry_path", lambda: tmp_path / "voice-webhooks.json")
    voice_webhooks._reset_for_tests()
    yield
    voice_webhooks._reset_for_tests()


def _message(webhook_id: int | None, channel: object | None = None) -> MagicMock:
    msg = MagicMock(spec=discord.Message)
    msg.webhook_id = webhook_id
    msg.author = MagicMock()
    msg.author.bot = True
    msg.author.id = webhook_id or 7
    msg.channel = channel if channel is not None else MagicMock(spec=discord.Thread)
    return msg


def test_registered_id_survives_a_restart(tmp_path: Path) -> None:
    voice_webhooks.register(VOICE)
    assert json.loads((tmp_path / "voice-webhooks.json").read_text()) == [VOICE]
    voice_webhooks._reset_for_tests()  # a new process
    assert voice_webhooks.is_voice_webhook(VOICE)
    assert not voice_webhooks.is_voice_webhook(DISPATCH)
    assert not voice_webhooks.is_voice_webhook(None)


def test_voice_message_is_not_authorized_but_dispatch_still_is() -> None:
    voice_webhooks.register(VOICE)
    assert is_message_authorized(_message(VOICE), lambda _u: True) is False
    assert is_message_authorized(_message(DISPATCH), lambda _u: False) is True


async def test_voice_message_runs_no_text_command() -> None:
    voice_webhooks.register(VOICE)
    bot = ClaudeDiscordBot(channel_id=1)
    bot.get_context = AsyncMock()
    await bot.process_commands(_message(VOICE))
    bot.get_context.assert_not_called()


async def test_on_message_drops_a_voice_before_anything_else() -> None:
    from c_lord.cogs.claude_chat import ClaudeChatCog

    voice_webhooks.register(VOICE)
    cog = MagicMock()
    cog._note_delivered = MagicMock(return_value=True)
    cog._denied_notifier = MagicMock()
    cog._denied_notifier.report = AsyncMock()
    cog._is_message_authorized = MagicMock(return_value=False)
    await ClaudeChatCog.on_message(cog, _message(VOICE))  # type: ignore[arg-type]
    cog._note_delivered.assert_not_called()
    cog._denied_notifier.report.assert_not_called()


def test_voice_message_fires_no_webhook_trigger() -> None:
    from c_lord.cogs.webhook_trigger import WebhookTrigger, WebhookTriggerCog

    voice_webhooks.register(VOICE)
    cog = WebhookTriggerCog(
        MagicMock(), MagicMock(), triggers={"🔧 go": WebhookTrigger(prompt="x", working_dir="/")}
    )
    msg = _message(VOICE)
    msg.content = "🔧 go now"
    assert cog._match(msg) is None
    msg.webhook_id = DISPATCH
    assert cog._match(msg) is not None


# --- #1024: every instance on the host shares one file ----------------------


def _write_as_another_instance(path: Path, ids: list[int]) -> None:
    """What another c-lord process's ``register`` leaves on disk."""
    path.write_text(json.dumps(ids), encoding="utf-8")


def test_two_instances_registering_in_turn_keep_both_ids(tmp_path: Path) -> None:
    path = tmp_path / "voice-webhooks.json"
    assert not voice_webhooks.is_voice_webhook(DISPATCH)  # this process has read the file
    _write_as_another_instance(path, [222])  # instance B registers 222
    voice_webhooks.register(111)  # this one registers 111
    assert sorted(json.loads(path.read_text())) == [111, 222]


def test_an_id_another_instance_registered_is_ours_too(tmp_path: Path) -> None:
    path = tmp_path / "voice-webhooks.json"
    voice_webhooks.register(111)
    _write_as_another_instance(path, [111, 222])
    assert voice_webhooks.is_voice_webhook(222)


def test_two_real_processes_registering_concurrently_lose_nothing(tmp_path: Path) -> None:
    """Separate interpreters, each with its own in-memory set, racing on one file."""
    import subprocess
    import sys

    path = tmp_path / "voice-webhooks.json"
    script = (
        "import sys\n"
        "from pathlib import Path\n"
        "from c_lord import voice_webhooks as v\n"
        "v.registry_path = lambda: Path(sys.argv[1])\n"
        "v.is_voice_webhook(1)\n"
        "base = int(sys.argv[2])\n"
        "for i in range(40):\n"
        "    v.register(base + i)\n"
    )
    procs = [
        subprocess.Popen([sys.executable, "-c", script, str(path), str(base)])
        for base in (1000, 2000, 3000)
    ]
    assert [p.wait(timeout=60) for p in procs] == [0, 0, 0]
    want = {b + i for b in (1000, 2000, 3000) for i in range(40)}
    assert set(json.loads(path.read_text())) == want
