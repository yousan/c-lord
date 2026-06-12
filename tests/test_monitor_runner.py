"""Unit tests for scripts.monitor.runner — config, rendering, observe (#404)."""

from __future__ import annotations

from pathlib import Path

from scripts.monitor.runner import (
    MonitorClient,
    _csv,
    build_config,
    observe_thread,
    parse_args,
    render_summary,
    run,
    snowflake_age_s,
)


def test_csv_splits_and_strips() -> None:
    assert _csv("/a.log, /b.log ,") == ["/a.log", "/b.log"]
    assert _csv(None) == []


def test_build_config_precedence() -> None:
    env = {
        "DISCORD_BOT_TOKEN": "tok",
        "MONITOR_LOGS": "/x.log",
        "DISCORD_CHANNEL_ID": "999",
        "FUZZ_GUILD_ID": "guild1",
    }
    cfg = build_config(env, parse_args([]))
    assert cfg.bot_token == "tok"
    assert cfg.logs == ["/x.log"]
    assert cfg.report_channel == "999"  # falls back to DISCORD_CHANNEL_ID
    assert cfg.guild_id == "guild1"
    # CLI overrides env
    cfg2 = build_config(env, parse_args(["--logs", "/y.log", "--report-channel", "111"]))
    assert cfg2.logs == ["/y.log"]
    assert cfg2.report_channel == "111"


def test_snowflake_age_positive() -> None:
    # a snowflake created well before "now" → positive age
    created_ms = 1700000000000  # 2023-ish
    sid = str((created_ms - 1420070400000) << 22)
    age = snowflake_age_s(sid, now_ms=created_ms + 60_000)
    assert 59 < age < 61


def test_render_summary_clean() -> None:
    s = render_summary([], total=3, guild="g")
    assert "新規 0" in s and "✅" in s


def test_render_summary_with_anomaly_has_link_and_under_limit() -> None:
    from scripts.fuzz.oracle import Anomaly

    a = Anomaly("555", "THREAD_STUCK", "high", "stuck", "stuck", fields={"thread": "555"})
    s = render_summary([a], total=1, guild="42")
    assert len(s) <= 2000
    assert "THREAD_STUCK" in s
    assert "discord.com/channels/42/555" in s


class _FakeClient:
    def __init__(self, msgs):
        self._msgs = msgs

    def fetch_messages(self, channel_id, *, limit=50):
        return self._msgs


def _m(mid, *, author="bot", content="", webhook=False, reactions=None):
    d = {"id": mid, "author": {"id": author}, "content": content}
    if webhook:
        d["webhook_id"] = "w"
    if reactions:
        d["reactions"] = [{"emoji": {"name": n}} for n in reactions]
    return d


def test_observe_thread_picks_trigger_with_status_and_latest_reply() -> None:
    # newest-first: a clean bot reply, then the trigger (a webhook msg) carrying 🟢
    msgs = [
        _m("30", author="bot", content="here is your answer"),
        _m("20", author="x", webhook=True, content="user question", reactions=["🟢"]),
        _m("10", author="bot", content="-# 💻 status"),
    ]
    out = observe_thread(_FakeClient(msgs), "t", bot_id="bot", now_ms=4_000_000_000_000)
    assert out is not None
    reactions, reply, age = out
    assert reactions == ["🟢"]
    assert reply == "here is your answer"
    assert age > 0


def test_observe_thread_none_when_no_status_reaction() -> None:
    out = observe_thread(_FakeClient([_m("1", content="hi")]), "t", bot_id="bot", now_ms=1e12)
    assert out is None


def test_run_logscan_only_writes_report(tmp_path: Path) -> None:
    logf = tmp_path / "bot.log"
    logf.write_text(
        "2026-06-12 10:00:00 [INFO] ok\n"
        "2026-06-12 10:00:01 [ERROR] c_lord.x: boom [thread=77]\n"
        "Traceback (most recent call last):\n"
        "ValueError: bad\n"
    )
    args = parse_args(
        [
            "--logs",
            str(logf),
            "--state-file",
            str(tmp_path / "state.json"),
            "--out-dir",
            str(tmp_path / "out"),
            "--no-report",
        ]
    )
    cfg = build_config({}, args)  # no token → thread scan skipped
    rc = run(cfg, args)
    assert rc == 0
    outs = list((tmp_path / "out").glob("*.json"))
    assert outs, "a report json should be written"
    import json

    rep = json.loads(outs[0].read_text())
    kinds = {a["kind"] for a in rep["anomalies"]}
    assert {"LOG_ERROR", "LOG_TRACEBACK"} <= kinds


def test_monitor_client_constructs() -> None:
    MonitorClient("token")  # smoke: no network in ctor
