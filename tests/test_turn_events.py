"""Tests for the turn-end event outlet (#917).

The outlet tells whoever is outside Discord — a dispatching agent, CI, a
dashboard — that a turn is over, from the server's own knowledge instead of a
guess made by reading the thread.
"""

from __future__ import annotations

import asyncio
import hashlib
import hmac
import json
import socket
from pathlib import Path
from types import SimpleNamespace
from unittest.mock import AsyncMock, MagicMock

import discord
import pytest
from aiohttp import web
from aiohttp.test_utils import TestServer

from c_lord import turn_events
from c_lord.skills import reply_tracker
from c_lord.turn_events import StartedBy

THREAD_ID = 4242


@pytest.fixture(autouse=True)
def _clean(monkeypatch):
    for var in (
        turn_events.ENV_LOG,
        turn_events.ENV_URL,
        turn_events.ENV_SECRET,
        turn_events.ENV_DEFER_MAX,
    ):
        monkeypatch.delenv(var, raising=False)
    monkeypatch.setattr(turn_events, "_RETRY_DELAYS", (0.0, 0.0))
    turn_events.reset()
    reply_tracker.reset_tracker()
    yield
    turn_events.reset()
    reply_tracker.reset_tracker()


def _refused_url() -> str:
    """A local address nothing listens on — refuses at once, unlike a black-holed port."""
    with socket.socket() as sock:
        sock.bind(("127.0.0.1", 0))
        port = sock.getsockname()[1]
    return f"http://127.0.0.1:{port}/hook"


def _thread(thread_id: int = THREAD_ID):
    return SimpleNamespace(id=thread_id, parent_id=777, guild=SimpleNamespace(id=555))


def _reply(message_id: int) -> discord.Message:
    message = MagicMock(spec=discord.Message)
    message.id = message_id
    return message


def _lines(path: Path) -> list[dict]:
    if not path.exists():
        return []
    return [json.loads(line) for line in path.read_text().splitlines()]


@pytest.fixture
def log(tmp_path, monkeypatch) -> Path:
    path = tmp_path / "events" / "turn-events.jsonl"
    monkeypatch.setenv(turn_events.ENV_LOG, str(path))
    return path


class TestDisabledByDefault:
    async def test_nothing_is_begun_or_written(self, tmp_path):
        assert not turn_events.enabled()
        assert turn_events.begin_turn(_thread(), StartedBy("user", "1")) is None
        turn_events.emit_blocked(_thread(), message_id=1)
        turn_events.note_turn_end(THREAD_ID, 0)
        await turn_events.drain()
        assert list(tmp_path.iterdir()) == []


class TestCompletion:
    async def test_one_completion_with_the_last_reply_id(self, log):
        info = turn_events.begin_turn(_thread(), StartedBy("user", "99"))
        reply_tracker.record_reply_message(THREAD_ID, _reply(1001))

        turn_events.end_turn(info)
        turn_events.finish_turn(info)
        await turn_events.drain()

        (event,) = _lines(log)
        assert event["v"] == 1
        assert event["kind"] == "completion"
        assert event["guild_id"] == "555"
        assert event["channel_id"] == "777"
        assert event["thread_id"] == str(THREAD_ID)
        assert event["last_message_id"] == "1001"
        assert event["started_by"] == {"type": "user", "id": "99", "name": None}
        assert event["turn_started_at"] <= event["ended_at"]
        assert event["event_id"]

    async def test_a_reply_from_an_earlier_turn_is_not_this_turns(self, log):
        reply_tracker.record_reply_message(THREAD_ID, _reply(900))
        info = turn_events.begin_turn(_thread(), StartedBy("user", "99"))

        turn_events.end_turn(info)
        turn_events.finish_turn(info)
        await turn_events.drain()

        (event,) = _lines(log)
        assert event["last_message_id"] is None

    async def test_webhook_origin_carries_id_and_display_name(self, log):
        info = turn_events.begin_turn(_thread(), StartedBy("webhook", "31337", "Captain Hook"))
        turn_events.end_turn(info)
        turn_events.finish_turn(info)
        await turn_events.drain()

        (event,) = _lines(log)
        assert event["started_by"] == {
            "type": "webhook",
            "id": "31337",
            "name": "Captain Hook",
        }

    async def test_waits_for_the_mirror_to_post_the_held_final_answer(self, log, monkeypatch):
        """No turn-end marker: the answer is still held for the idle flush."""
        from c_lord.transcript import mirror as mirror_mod

        pending = {"held": True}
        monkeypatch.setattr(mirror_mod, "has_pending_final", lambda _tid: pending["held"])
        monkeypatch.setattr(turn_events, "_SETTLE_POLL", 0.01)
        info = turn_events.begin_turn(_thread(), StartedBy("user", "99"))

        turn_events.end_turn(info)
        turn_events.finish_turn(info)
        await asyncio.sleep(0.05)
        assert _lines(log) == []  # not before the answer is out

        reply_tracker.record_reply_message(THREAD_ID, _reply(2002))
        pending["held"] = False
        await turn_events.drain()

        (event,) = _lines(log)
        assert event["last_message_id"] == "2002"


class TestBlockedAndFailure:
    async def test_blocked_names_the_menu_message(self, log):
        turn_events.begin_turn(_thread(), StartedBy("webhook", "7", "Captain Hook"))

        turn_events.emit_blocked(_thread(), message_id=3003)
        await turn_events.drain()

        (event,) = _lines(log)
        assert event["kind"] == "blocked"
        assert event["last_message_id"] == "3003"
        assert event["started_by"]["type"] == "webhook"

    async def test_blocked_outside_a_run_still_goes_out(self, log):
        turn_events.emit_blocked(_thread(), message_id=3004)
        await turn_events.drain()

        (event,) = _lines(log)
        assert event["kind"] == "blocked"
        assert event["started_by"]["type"] == "user"

    async def test_failure_carries_a_coarse_reason_and_no_error_text(self, log):
        info = turn_events.begin_turn(_thread(), StartedBy("scheduler", "12", "nightly"))
        turn_events.end_turn(info)
        turn_events.finish_turn(info, failure_reason="timeout")
        await turn_events.drain()

        (event,) = _lines(log)
        assert event["kind"] == "failure"
        assert event["reason"] == "timeout"
        assert event["started_by"]["type"] == "scheduler"


class TestBackgroundAgents:
    """The decided rule: a turn that ended with background sub-agents still
    running is not finished — its completion goes out when the turn that the
    last agent's report starts has ended."""

    async def test_completion_waits_for_the_follow_up_turn(self, log):
        info = turn_events.begin_turn(_thread(), StartedBy("webhook", "7", "Captain Hook"))
        turn_events.note_turn_end(THREAD_ID, 2)  # transcript: ended, 2 agents running

        turn_events.end_turn(info)
        turn_events.finish_turn(info)
        await turn_events.drain()
        assert _lines(log) == []  # held back

        turn_events.note_turn_end(THREAD_ID, 1)  # a report arrived, one still running
        await turn_events.drain()
        assert _lines(log) == []

        reply_tracker.record_reply_message(THREAD_ID, _reply(5005))
        turn_events.note_turn_end(THREAD_ID, 0)  # the follow-up turn ended, none left
        await turn_events.drain()

        (event,) = _lines(log)
        assert event["kind"] == "completion"
        assert event["started_by"]["type"] == "webhook"
        assert event["last_message_id"] == "5005"

    async def test_without_a_marker_the_completion_goes_out_at_once(self, log):
        """Cannot tell → emit (the documented limit)."""
        info = turn_events.begin_turn(_thread(), StartedBy("user", "1"))

        turn_events.end_turn(info)
        turn_events.finish_turn(info)
        await turn_events.drain()

        assert [e["kind"] for e in _lines(log)] == ["completion"]

    async def test_a_marker_from_an_earlier_turn_is_ignored(self, log):
        turn_events.note_turn_end(THREAD_ID, 3)  # last turn's snapshot
        info = turn_events.begin_turn(_thread(), StartedBy("user", "1"))

        turn_events.end_turn(info)
        turn_events.finish_turn(info)
        await turn_events.drain()

        assert [e["kind"] for e in _lines(log)] == ["completion"]

    async def test_a_failure_is_never_held_back(self, log):
        info = turn_events.begin_turn(_thread(), StartedBy("user", "1"))
        turn_events.note_turn_end(THREAD_ID, 2)

        turn_events.end_turn(info)
        turn_events.finish_turn(info, failure_reason="error")
        await turn_events.drain()

        assert [e["kind"] for e in _lines(log)] == ["failure"]

    async def test_agents_that_never_report_do_not_hold_it_forever(self, log, monkeypatch):
        monkeypatch.setenv(turn_events.ENV_DEFER_MAX, "0.05")
        info = turn_events.begin_turn(_thread(), StartedBy("user", "1"))
        turn_events.note_turn_end(THREAD_ID, 1)
        turn_events.end_turn(info)
        turn_events.finish_turn(info)

        await asyncio.sleep(0.2)
        await turn_events.drain()

        (event,) = _lines(log)
        assert event["kind"] == "completion"
        assert event["reason"] == "background_agents_unresolved"

    async def test_the_next_message_takes_over_a_held_completion(self, log):
        first = turn_events.begin_turn(_thread(), StartedBy("webhook", "7", "Captain Hook"))
        turn_events.note_turn_end(THREAD_ID, 1)
        turn_events.end_turn(first)
        turn_events.finish_turn(first)

        second = turn_events.begin_turn(_thread(), StartedBy("user", "1"))
        turn_events.end_turn(second)
        turn_events.finish_turn(second)
        await turn_events.drain()

        # one event for the thread's next end, not a stale one for the first
        events = _lines(log)
        assert [e["kind"] for e in events] == ["completion"]
        assert events[0]["started_by"]["type"] == "user"
        turn_events.note_turn_end(THREAD_ID, 0)
        await turn_events.drain()
        assert len(_lines(log)) == 1


class TestHttp:
    SECRET = "s3cret"

    async def _serve(self, monkeypatch, handler):
        app = web.Application()
        app.router.add_post("/hook", handler)
        server = TestServer(app)
        await server.start_server()
        monkeypatch.setenv(turn_events.ENV_URL, str(server.make_url("/hook")))
        monkeypatch.setenv(turn_events.ENV_SECRET, self.SECRET)
        return server

    async def _one_turn(self):
        info = turn_events.begin_turn(_thread(), StartedBy("user", "1"))
        turn_events.end_turn(info)
        turn_events.finish_turn(info)
        await turn_events.drain()

    async def test_body_is_signed_with_hmac_sha256(self, monkeypatch):
        seen: list[tuple[bytes, str, str]] = []

        async def handler(request: web.Request) -> web.Response:
            seen.append(
                (
                    await request.read(),
                    request.headers.get(turn_events.SIGNATURE_HEADER, ""),
                    request.headers.get("Content-Type", ""),
                )
            )
            return web.Response(status=204)

        server = await self._serve(monkeypatch, handler)
        try:
            await self._one_turn()
        finally:
            await server.close()

        ((body, signature, content_type),) = seen
        expected = hmac.new(self.SECRET.encode(), body, hashlib.sha256).hexdigest()
        assert signature == f"sha256={expected}"
        assert content_type.startswith("application/json")
        assert json.loads(body)["kind"] == "completion"

    async def test_a_5xx_is_retried_a_few_times_then_given_up_without_raising(self, monkeypatch):
        calls = 0

        async def handler(request: web.Request) -> web.Response:
            nonlocal calls
            calls += 1
            return web.Response(status=503)

        server = await self._serve(monkeypatch, handler)
        try:
            await self._one_turn()  # must not raise
        finally:
            await server.close()

        assert calls == 1 + len(turn_events._RETRY_DELAYS)

    async def test_recovers_when_a_retry_succeeds(self, monkeypatch):
        calls = 0

        async def handler(request: web.Request) -> web.Response:
            nonlocal calls
            calls += 1
            return web.Response(status=500 if calls == 1 else 200)

        server = await self._serve(monkeypatch, handler)
        try:
            await self._one_turn()
        finally:
            await server.close()

        assert calls == 2

    async def test_a_4xx_is_not_retried(self, monkeypatch):
        calls = 0

        async def handler(request: web.Request) -> web.Response:
            nonlocal calls
            calls += 1
            return web.Response(status=400)

        server = await self._serve(monkeypatch, handler)
        try:
            await self._one_turn()
        finally:
            await server.close()

        assert calls == 1

    async def test_a_dead_endpoint_does_not_raise(self, monkeypatch):
        monkeypatch.setenv(turn_events.ENV_URL, _refused_url())  # discard port
        monkeypatch.setenv(turn_events.ENV_SECRET, self.SECRET)
        await self._one_turn()  # must not raise

    async def test_a_down_endpoint_does_not_stop_the_jsonl(self, monkeypatch, log):
        monkeypatch.setenv(turn_events.ENV_URL, _refused_url())
        monkeypatch.setenv(turn_events.ENV_SECRET, self.SECRET)
        await self._one_turn()
        assert [e["kind"] for e in _lines(log)] == ["completion"]

    async def test_no_secret_means_nothing_is_sent(self, monkeypatch):
        calls = 0

        async def handler(request: web.Request) -> web.Response:
            nonlocal calls
            calls += 1
            return web.Response(status=200)

        server = await self._serve(monkeypatch, handler)
        monkeypatch.delenv(turn_events.ENV_SECRET)
        try:
            await self._one_turn()
        finally:
            await server.close()

        assert calls == 0

    async def test_non_http_urls_are_refused(self, monkeypatch):
        monkeypatch.setenv(turn_events.ENV_URL, "file:///etc/passwd")
        monkeypatch.setenv(turn_events.ENV_SECRET, self.SECRET)
        assert turn_events._http_target() is None


class TestJsonlSink:
    async def test_an_unwritable_path_does_not_raise(self, tmp_path, monkeypatch):
        blocker = tmp_path / "file"
        blocker.write_text("x")
        monkeypatch.setenv(turn_events.ENV_LOG, str(blocker / "sub" / "events.jsonl"))
        info = turn_events.begin_turn(_thread(), StartedBy("user", "1"))
        turn_events.end_turn(info)
        turn_events.finish_turn(info)
        await turn_events.drain()  # must not raise

    async def test_events_append_one_line_each(self, log):
        for _ in range(2):
            info = turn_events.begin_turn(_thread(), StartedBy("user", "1"))
            turn_events.end_turn(info)
            turn_events.finish_turn(info)
        await turn_events.drain()

        events = _lines(log)
        assert len(events) == 2
        assert events[0]["event_id"] != events[1]["event_id"]


class TestStartedByFromMessage:
    def test_webhook_message(self):
        message = SimpleNamespace(webhook_id=31337, author=SimpleNamespace(id=31337, name="Hook"))
        got = turn_events.started_by_from_message(message, requester=None, bot_is_author=False)
        assert got == StartedBy("webhook", "31337", "Hook")

    def test_human_message(self):
        message = SimpleNamespace(webhook_id=None, author=SimpleNamespace(id=5, name="u"))
        got = turn_events.started_by_from_message(
            message, requester=message.author, bot_is_author=False
        )
        assert got == StartedBy("user", "5", None)

    def test_a_seed_message_c_lord_posted_itself_is_the_api(self):
        message = SimpleNamespace(webhook_id=None, author=SimpleNamespace(id=9, name="clord"))
        got = turn_events.started_by_from_message(message, requester=None, bot_is_author=True)
        assert got.type == "api"

    def test_slash_invoker_is_a_user_even_though_the_bot_posted_the_seed(self):
        message = SimpleNamespace(webhook_id=None, author=SimpleNamespace(id=9, name="clord"))
        invoker = SimpleNamespace(id=5, name="u")
        got = turn_events.started_by_from_message(message, requester=invoker, bot_is_author=True)
        assert got == StartedBy("user", "5", None)


class TestRunHelperWiring:
    """``run_claude_with_config`` is where every turn — chat, webhook, scheduler,
    slash — ends, so it is where the one event per turn is sent."""

    def _config(self, events, *, started_by=None):
        from c_lord.claude.tmux_runner import TmuxClaudeRunner
        from c_lord.cogs.run_config import RunConfig

        async def run(prompt, session_id=None):
            for event in events:
                yield event

        runner = MagicMock(spec=TmuxClaudeRunner)
        runner.run = run
        runner.preempted = False
        runner.working_dir = None
        thread = MagicMock(spec=discord.Thread)
        thread.id = THREAD_ID
        thread.parent_id = 777
        thread.guild = SimpleNamespace(id=555)
        thread.send = AsyncMock(return_value=MagicMock(spec=discord.Message))
        return RunConfig(thread=thread, runner=runner, prompt="go", started_by=started_by)

    @staticmethod
    def _done(error: str | None = None):
        from c_lord.claude.types import MessageType, StreamEvent

        return [
            StreamEvent(message_type=MessageType.SYSTEM, session_id="s1"),
            StreamEvent(message_type=MessageType.RESULT, is_complete=True, error=error),
        ]

    async def test_a_finished_turn_sends_one_completion(self, log, monkeypatch):
        from c_lord.cogs import _run_helper

        monkeypatch.setattr(_run_helper, "_post_context_usage", AsyncMock())
        config = self._config(self._done(), started_by=StartedBy("webhook", "31", "Hook"))

        await _run_helper.run_claude_with_config(config)
        await turn_events.drain()

        (event,) = _lines(log)
        assert event["kind"] == "completion"
        assert event["started_by"]["type"] == "webhook"
        assert event["started_by"]["name"] == "Hook"

    async def test_an_errored_turn_sends_one_failure(self, log, monkeypatch):
        from c_lord.cogs import _run_helper

        monkeypatch.setattr(_run_helper, "_post_context_usage", AsyncMock())
        config = self._config(self._done(error="Timed out after 300 seconds"))

        await _run_helper.run_claude_with_config(config)
        await turn_events.drain()

        (event,) = _lines(log)
        assert event["kind"] == "failure"
        assert event["reason"] == "timeout"
        assert "300" not in json.dumps(event)

    async def test_a_runner_that_raises_is_a_failure(self, log, monkeypatch):
        from c_lord.cogs import _run_helper

        monkeypatch.setattr(_run_helper, "_post_context_usage", AsyncMock())
        config = self._config([])

        async def boom(prompt, session_id=None):
            raise RuntimeError("tmux gone")
            yield  # pragma: no cover

        config.runner.run = boom

        await _run_helper.run_claude_with_config(config)
        await turn_events.drain()

        assert [e["kind"] for e in _lines(log)] == ["failure"]

    async def test_a_preempted_turn_sends_nothing(self, log, monkeypatch):
        from c_lord.cogs import _run_helper

        monkeypatch.setattr(_run_helper, "_post_context_usage", AsyncMock())
        config = self._config(self._done())
        config.runner.preempted = True

        await _run_helper.run_claude_with_config(config)
        await turn_events.drain()

        assert _lines(log) == []

    async def test_nothing_is_written_when_no_outlet_is_configured(self, tmp_path, monkeypatch):
        from c_lord.cogs import _run_helper

        monkeypatch.setattr(_run_helper, "_post_context_usage", AsyncMock())
        config = self._config(self._done())

        await _run_helper.run_claude_with_config(config)
        await turn_events.drain()

        assert list(tmp_path.iterdir()) == []
        assert not turn_events._ACTIVE


def test_the_signing_secret_is_not_inherited_by_claude_panes():
    from c_lord.tmux import SENSITIVE_ENV_KEYS

    assert turn_events.ENV_SECRET in SENSITIVE_ENV_KEYS
    assert turn_events.ENV_URL in SENSITIVE_ENV_KEYS
