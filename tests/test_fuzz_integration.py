"""End-to-end wiring test for the fuzz loop with a faked Discord client (#377).

This exercises the real ``inject_and_observe`` polling/filtering loop +
``detect_anomalies`` + ``build_report`` + ``render_discord_summary`` together,
without touching the network. It complements (does not replace) the live staging
smoke: it proves the pipeline wiring is correct deterministically, so a green
here + a passing live run together cover the loop.
"""

from __future__ import annotations

from scripts.fuzz.discord_io import inject_and_observe
from scripts.fuzz.oracle import detect_anomalies
from scripts.fuzz.report import build_report, render_discord_summary
from scripts.fuzz.scenarios import Scenario

BOT = "42"


class FakeClient:
    """Minimal stand-in for FuzzClient with canned messages/health."""

    def __init__(self, messages, *, health=True, spawn=("555", None)):
        self._messages = messages
        self._health = health
        self._spawn = spawn

    def spawn(self, prompt, channel_id):
        return self._spawn

    def webhook_post(self, content, thread_id):
        return ("999", None)

    def fetch_messages(self, channel_id, *, limit=100):
        return self._messages

    def health(self):
        return self._health


def _bot_msg(mid, content, *, reactions=None):
    m = {"id": mid, "author": {"id": BOT}, "content": content}
    if reactions:
        m["reactions"] = [{"emoji": {"name": n}} for n in reactions]
    return m


def _observe(client, text, **kw):
    scenario = Scenario("s01", "cat", text, "intent")
    return inject_and_observe(
        client, scenario, mode="spawn", channel_id="c", bot_id=BOT, timeout=0.3, poll=0.05, **kw
    )


def test_full_loop_flags_chrome_and_error_reaction() -> None:
    text = "please render this"
    messages = [
        _bot_msg("2", "sure!\nModel: claude-x\ndone"),  # bot answer with chrome leak
        _bot_msg("1", text, reactions=["❌"]),  # seed, marked error
    ]
    obs = _observe(FakeClient(messages), text)
    assert obs.injected and obs.replied
    assert obs.thread_id == "555"
    kinds = {a.kind for a in detect_anomalies(obs)}
    assert {"CHROME_LEAK", "ERROR_REACTION"} <= kinds

    report = build_report(
        run_id="r",
        started_at="a",
        finished_at="b",
        branch="x",
        inject_mode="spawn",
        scenarios=[Scenario("s01", "cat", text, "intent")],
        observations=[obs],
        anomalies=detect_anomalies(obs),
        generation_raw_path="g",
    )
    summary = render_discord_summary(report, guild_id="7")
    assert "CHROME_LEAK" in summary
    assert "discord.com/channels/7/555" in summary  # clickable thread link


def test_full_loop_clean_reply_no_anomaly() -> None:
    text = "hi"
    messages = [
        _bot_msg("2", "Hello! Here is your answer."),
        _bot_msg("1", text, reactions=["🟡"]),
    ]
    obs = _observe(FakeClient(messages), text)
    assert obs.replied
    assert detect_anomalies(obs) == []


def test_full_loop_no_response_when_only_seed() -> None:
    text = "stuck please"
    messages = [_bot_msg("1", text, reactions=["🟢"])]  # seed only, lamp still running
    obs = _observe(FakeClient(messages), text)
    assert not obs.replied
    assert "NO_RESPONSE" in {a.kind for a in detect_anomalies(obs)}


def test_full_loop_spawn_failure_is_flagged() -> None:
    client = FakeClient([], spawn=(None, "/api/spawn returned 503"))
    obs = _observe(client, "anything")
    assert not obs.injected
    assert [a.kind for a in detect_anomalies(obs)] == ["SPAWN_FAILED"]


def test_full_loop_webhook_mode_observes_reply() -> None:
    # Webhook mode injects into a pre-attached thread and observes the bot reply
    # (no /api/spawn). The faked webhook_post returns trigger id "999".
    messages = [_bot_msg("2", "Hi there, all good.")]
    obs = inject_and_observe(
        FakeClient(messages),
        Scenario("s01", "cat", "hello", "intent"),
        mode="webhook",
        channel_id="c",
        bot_id=BOT,
        webhook_thread_id="thr",
        timeout=0.3,
        poll=0.05,
    )
    assert obs.injected and obs.replied
    assert obs.reply_text == "Hi there, all good."
    assert detect_anomalies(obs) == []
