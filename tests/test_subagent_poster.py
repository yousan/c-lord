"""Discord side of the sub-agent log (#971): names via webhook, or in front."""

from __future__ import annotations

from pathlib import Path
from unittest.mock import AsyncMock, MagicMock

import discord
import pytest

from c_lord import voice_webhooks
from c_lord.discord_ui.subagent_poster import DiscordSubagentPoster, _WebhookCache
from c_lord.voice_webhooks import VOICE_WEBHOOK_NAME

BOT_ID = 1
THREAD = 77


@pytest.fixture(autouse=True)
def _registry(tmp_path: Path, monkeypatch: pytest.MonkeyPatch):
    monkeypatch.setattr(voice_webhooks, "registry_path", lambda: tmp_path / "vw.json")
    voice_webhooks._reset_for_tests()
    yield
    voice_webhooks._reset_for_tests()


async def _send_chunks(send, text: str, *, silent: bool = False):
    return await send(content=text, silent=silent)


def _thread(parent: MagicMock) -> MagicMock:
    thread = MagicMock(spec=discord.Thread)
    thread.id = THREAD
    thread.parent = parent
    thread.send = AsyncMock()
    return thread


def _poster(thread: MagicMock) -> DiscordSubagentPoster:
    bot = MagicMock()
    bot.user.id = BOT_ID

    async def resolve(_tid: int):
        return thread

    return DiscordSubagentPoster(
        bot=bot,
        thread_id=THREAD,
        resolve_channel=resolve,
        send_chunks=_send_chunks,
        cache=_WebhookCache(),
    )


def _hook(hook_id: int = 55) -> MagicMock:
    hook = MagicMock()
    hook.id = hook_id
    hook.name = VOICE_WEBHOOK_NAME
    hook.token = "t"
    hook.user.id = BOT_ID
    hook.send = AsyncMock()
    return hook


async def test_voice_goes_through_the_webhook_under_the_speakers_name() -> None:
    parent = MagicMock()
    parent.id = 9
    hook = _hook()
    parent.webhooks = AsyncMock(return_value=[])
    parent.create_webhook = AsyncMock(return_value=hook)
    thread = _thread(parent)

    await _poster(thread).post_voice("alice", "→ **bob**\nhi")

    hook.send.assert_awaited_once()
    kwargs = hook.send.await_args.kwargs
    assert kwargs["username"] == "alice"
    assert kwargs["content"] == "→ **bob**\nhi"
    assert kwargs["thread"] is thread
    thread.send.assert_not_awaited()
    # Registered, so c-lord will not take it as an order — also after a restart.
    assert voice_webhooks.is_voice_webhook(55)


async def test_existing_webhook_is_reused() -> None:
    parent = MagicMock()
    parent.id = 9
    hook = _hook()
    parent.webhooks = AsyncMock(return_value=[hook])
    parent.create_webhook = AsyncMock()
    poster = _poster(_thread(parent))
    await poster.post_voice("alice", "x")
    await poster.post_voice("bob", "y")
    parent.create_webhook.assert_not_awaited()
    parent.webhooks.assert_awaited_once()
    assert hook.send.await_count == 2


async def test_without_manage_webhooks_the_name_goes_in_front() -> None:
    parent = MagicMock()
    parent.id = 9
    parent.webhooks = AsyncMock(side_effect=discord.Forbidden(MagicMock(status=403), "no"))
    thread = _thread(parent)

    await _poster(thread).post_voice("alice", "→ **bob**\nhi")

    thread.send.assert_awaited_once()
    assert thread.send.await_args.kwargs["content"] == "**alice** → **bob**\nhi"


async def test_reserved_words_are_kept_out_of_the_username() -> None:
    parent = MagicMock()
    parent.id = 9
    hook = _hook()
    parent.webhooks = AsyncMock(return_value=[hook])
    await _poster(_thread(parent)).post_voice("Discord reader", "x")
    assert "discord" not in hook.send.await_args.kwargs["username"].lower()
