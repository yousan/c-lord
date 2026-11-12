"""Tests for c_lord.discord_ref.enrich_discord_references (Issue #318, route-B Unit 1).

reactive "push": when a human pastes a Discord message link (or uses Discord's
reply feature), c-lord resolves the referenced content server-side (via the bot)
and inlines it VERBATIM into the prompt before it reaches Claude. The bot token is
never handled here — discord.py carries auth internally.
"""

from __future__ import annotations

from unittest.mock import AsyncMock, MagicMock

from c_lord.discord_ref import enrich_discord_references

GUILD = 111
CHAN = 222
MSG = 333
LINK = f"https://discord.com/channels/{GUILD}/{CHAN}/{MSG}"


class _AsyncIter:
    """Minimal async iterable to stand in for channel.history(...)."""

    def __init__(self, items: list) -> None:
        self._items = list(items)

    def __aiter__(self) -> _AsyncIter:
        return self

    async def __anext__(self):
        if not self._items:
            raise StopAsyncIteration
        return self._items.pop(0)


def _msg(content: str, author: str = "alice", ts: str = "2026-06-05T00:00:00") -> MagicMock:
    m = MagicMock()
    m.content = content
    m.author = MagicMock()
    m.author.display_name = author
    m.author.name = author
    m.created_at = ts
    return m


def _channel(name: str = "support", *, fetched: MagicMock | None = None) -> MagicMock:
    ch = MagicMock()
    ch.id = CHAN
    ch.name = name
    ch.fetch_message = AsyncMock(return_value=fetched)
    return ch


def _bot(channel: MagicMock | None) -> MagicMock:
    b = MagicMock()
    b.get_channel.return_value = channel
    b.fetch_channel = AsyncMock(return_value=channel)
    return b


def _message(*, reference: MagicMock | None = None, mid: int = 999) -> MagicMock:
    """The *incoming* Discord message (the human's turn)."""
    msg = MagicMock()
    msg.id = mid
    msg.reference = reference
    msg.channel = MagicMock()
    msg.channel.id = 555
    return msg


# --------------------------------------------------------------------------- #
# No-op cases
# --------------------------------------------------------------------------- #
async def test_no_links_no_reference_returns_prompt_unchanged() -> None:
    bot = _bot(None)
    out = await enrich_discord_references("just a normal message", _message(), bot, enabled=True)
    assert out == "just a normal message"
    bot.get_channel.assert_not_called()


async def test_disabled_is_total_noop_even_with_link() -> None:
    fetched = _msg("secret server is down")
    bot = _bot(_channel(fetched=fetched))
    prompt = f"look at this {LINK}"
    out = await enrich_discord_references(prompt, _message(), bot, enabled=False)
    assert out == prompt
    bot.get_channel.assert_not_called()


# --------------------------------------------------------------------------- #
# Core: message-link resolution (push)
# --------------------------------------------------------------------------- #
async def test_message_link_inlines_verbatim_content() -> None:
    fetched = _msg("the server is on fire", author="bob")
    bot = _bot(_channel(name="support", fetched=fetched))
    prompt = f"please look 👉 {LINK}"
    out = await enrich_discord_references(prompt, _message(), bot, enabled=True)

    assert out.startswith(prompt)  # original is preserved, content appended
    assert "the server is on fire" in out  # verbatim content
    assert "#support" in out  # channel label
    assert "bob" in out  # author
    bot.get_channel.assert_called_once_with(CHAN)


async def test_falls_back_to_fetch_channel_when_get_channel_none() -> None:
    fetched = _msg("hello from fetch")
    ch = _channel(fetched=fetched)
    bot = MagicMock()
    bot.get_channel.return_value = None
    bot.fetch_channel = AsyncMock(return_value=ch)
    out = await enrich_discord_references(f"x {LINK}", _message(), bot, enabled=True)
    assert "hello from fetch" in out
    bot.fetch_channel.assert_awaited_once_with(CHAN)


# --------------------------------------------------------------------------- #
# Discord reply reference
# --------------------------------------------------------------------------- #
async def test_reply_reference_is_resolved() -> None:
    fetched = _msg("original message text")
    bot = _bot(_channel(fetched=fetched))
    ref = MagicMock()
    ref.channel_id = CHAN
    ref.message_id = MSG
    out = await enrich_discord_references("my reply", _message(reference=ref), bot, enabled=True)
    assert "original message text" in out


# --------------------------------------------------------------------------- #
# Failure handling — NEVER raise
# --------------------------------------------------------------------------- #
async def test_fetch_message_failure_appends_note_never_raises() -> None:
    ch = _channel()
    ch.fetch_message = AsyncMock(side_effect=Exception("Forbidden"))
    bot = _bot(ch)
    out = await enrich_discord_references(f"x {LINK}", _message(), bot, enabled=True)
    assert "could not fetch" in out.lower()
    assert out.startswith("x ")  # original preserved


async def test_channel_not_found_appends_note() -> None:
    bot = MagicMock()
    bot.get_channel.return_value = None
    bot.fetch_channel = AsyncMock(side_effect=Exception("NotFound"))
    out = await enrich_discord_references(f"x {LINK}", _message(), bot, enabled=True)
    assert "could not fetch" in out.lower()


# --------------------------------------------------------------------------- #
# Caps / dedupe / truncation
# --------------------------------------------------------------------------- #
async def test_duplicate_link_and_reference_fetched_once() -> None:
    fetched = _msg("dedup me")
    ch = _channel(fetched=fetched)
    bot = _bot(ch)
    ref = MagicMock()
    ref.channel_id = CHAN
    ref.message_id = MSG
    out = await enrich_discord_references(f"see {LINK}", _message(reference=ref), bot, enabled=True)
    # same (channel, message) referenced by both link and reply → fetched once
    assert ch.fetch_message.await_count == 1
    assert out.count("dedup me") == 1


async def test_long_content_is_truncated() -> None:
    huge = "A" * 10_000
    bot = _bot(_channel(fetched=_msg(huge)))
    out = await enrich_discord_references(f"x {LINK}", _message(), bot, enabled=True)
    assert "truncated" in out.lower()
    assert len(out) < len(huge)  # not the full 10k inlined


async def test_more_than_max_refs_are_capped() -> None:
    # 7 distinct message links; only _MAX_REFS (5) should be resolved.
    ch = _channel(fetched=_msg("c"))
    bot = _bot(ch)
    links = " ".join(f"https://discord.com/channels/{GUILD}/{CHAN}/{1000 + i}" for i in range(7))
    out = await enrich_discord_references(links, _message(), bot, enabled=True)
    assert ch.fetch_message.await_count <= 5
    assert "more reference" in out.lower() or "limit" in out.lower()


# --------------------------------------------------------------------------- #
# Security — bot token must never leak into the prompt
# --------------------------------------------------------------------------- #
async def test_bot_token_never_leaks_into_prompt() -> None:
    fetched = _msg("benign content")
    ch = _channel(fetched=fetched)
    bot = _bot(ch)
    bot.http = MagicMock()
    bot.http.token = "SUPER_SECRET_BOT_TOKEN"  # noqa: S105 — test fixture
    out = await enrich_discord_references(f"x {LINK}", _message(), bot, enabled=True)
    assert "SUPER_SECRET_BOT_TOKEN" not in out


async def test_channel_only_link_inlines_recent_history() -> None:
    ch = _channel(name="general")
    ch.history = MagicMock(
        return_value=_AsyncIter([_msg("m1", author="a"), _msg("m2", author="b")])
    )
    bot = _bot(ch)
    chan_link = f"https://discord.com/channels/{GUILD}/{CHAN}"
    out = await enrich_discord_references(f"see {chan_link}", _message(), bot, enabled=True)
    assert "m1" in out and "m2" in out
    assert "#general" in out


async def test_self_link_to_trigger_message_is_skipped() -> None:
    # A link pointing at the incoming message itself should not be re-inlined.
    bot = _bot(_channel(fetched=_msg("self")))
    self_link = f"https://discord.com/channels/{GUILD}/{CHAN}/{777}"
    out = await enrich_discord_references(self_link, _message(mid=777), bot, enabled=True)
    # trigger message id == 777 → skip
    assert "self" not in out
    assert out == self_link


# --------------------------------------------------------------------------- #
# Env gate default-on path (enabled=None reads CLORD_RESOLVE_DISCORD_LINKS)
# --------------------------------------------------------------------------- #
async def test_env_gate_defaults_on_when_unset(monkeypatch) -> None:
    monkeypatch.delenv("CLORD_RESOLVE_DISCORD_LINKS", raising=False)
    bot = _bot(_channel(fetched=_msg("on by default")))
    out = await enrich_discord_references(f"x {LINK}", _message(), bot)  # enabled=None
    assert "on by default" in out


async def test_env_gate_disabled_via_env(monkeypatch) -> None:
    monkeypatch.setenv("CLORD_RESOLVE_DISCORD_LINKS", "0")
    bot = _bot(_channel(fetched=_msg("should not appear")))
    out = await enrich_discord_references(f"x {LINK}", _message(), bot)  # enabled=None
    assert out == f"x {LINK}"
    bot.get_channel.assert_not_called()


# --------------------------------------------------------------------------- #
# Channel-only link with empty history → silently skipped
# --------------------------------------------------------------------------- #
async def test_channel_only_link_with_empty_history_is_skipped() -> None:
    ch = _channel(name="general")
    ch.history = MagicMock(return_value=_AsyncIter([]))
    bot = _bot(ch)
    chan_link = f"https://discord.com/channels/{GUILD}/{CHAN}"
    out = await enrich_discord_references(f"see {chan_link}", _message(), bot, enabled=True)
    assert out == f"see {chan_link}"  # nothing appended


# --------------------------------------------------------------------------- #
# Distinct reply + link: the reply reference leads the appended blocks
# --------------------------------------------------------------------------- #
async def test_reply_reference_leads_pasted_links() -> None:
    ch = MagicMock()
    ch.id = CHAN
    ch.name = "support"

    async def _fetch(mid):
        return _msg(f"content-of-{mid}")

    ch.fetch_message = AsyncMock(side_effect=_fetch)
    bot = _bot(ch)
    ref = MagicMock()
    ref.channel_id = CHAN
    ref.message_id = 444
    link = f"https://discord.com/channels/{GUILD}/{CHAN}/555"
    out = await enrich_discord_references(
        f"reply {link}", _message(reference=ref), bot, enabled=True
    )
    assert "content-of-444" in out and "content-of-555" in out
    assert out.index("content-of-444") < out.index("content-of-555")  # reply leads


# --------------------------------------------------------------------------- #
# Total-size-limit early break (distinct from the _MAX_REFS cap)
# --------------------------------------------------------------------------- #
async def test_total_size_limit_stops_inlining() -> None:
    big = "B" * 1900  # under the per-ref cap (2000) but several blow the total (6000)

    async def _fetch(_mid):
        return _msg(big)

    ch = MagicMock()
    ch.id = CHAN
    ch.name = "support"
    ch.fetch_message = AsyncMock(side_effect=_fetch)
    bot = _bot(ch)
    links = " ".join(f"https://discord.com/channels/{GUILD}/{CHAN}/{1000 + i}" for i in range(5))
    out = await enrich_discord_references(links, _message(), bot, enabled=True)
    assert "total size limit" in out.lower()
    assert out.count(big) < 5  # not all five big blocks inlined
