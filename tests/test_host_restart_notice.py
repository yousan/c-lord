"""#807: a host reboot kills every Claude mid-turn — say so, once, from the jsonl.

c-lord-only restarts leave Claude running in tmux (#406), but a host reboot
takes tmux down with it, and nothing runs ``cog_unload`` on the way out. A
thread that was mid-turn then looks like it is still working, forever.

On startup c-lord posts one line into each such thread. The decision is made
from Claude Code's own transcript — its **last entry** — and nothing else: no
DB state, not even for "already told them" (yousan: tracking it in the DB is
how the bugs keep coming back).
"""

from __future__ import annotations

import json
from pathlib import Path
from subprocess import CompletedProcess
from unittest.mock import AsyncMock, MagicMock, patch

import discord
import pytest

from c_lord.host_restart_notice import (
    HOST_RESTART_NOTICE,
    notify_host_restart_stops,
    stopped_mid_turn,
    transcript_stopped_mid_turn,
)
from c_lord.tmux import LiveClaude, live_claude_panes
from c_lord.transcript.claim import write_claim

SID = "0f0e0d0c-0b0a-4908-8706-050403020100"


# ── transcript shapes ────────────────────────────────────────────────────


def _assistant(stop_reason: str | None, block: dict, **extra) -> dict:
    return {
        "type": "assistant",
        "uuid": "a",
        "message": {"role": "assistant", "stop_reason": stop_reason, "content": [block]},
        **extra,
    }


def _user(content, **extra) -> dict:
    return {"type": "user", "uuid": "u", "message": {"role": "user", "content": content}, **extra}


TEXT = {"type": "text", "text": "done"}
TOOL_USE = {"type": "tool_use", "id": "t1", "name": "Bash", "input": {"command": "ls"}}
TOOL_RESULT = [{"type": "tool_result", "tool_use_id": "t1", "content": "ok"}]
# What Claude Code appends after the last message — bookkeeping, not a turn boundary.
TURN_END = {"type": "system", "subtype": "turn_duration", "durationMs": 10}
META = [
    {"type": "attachment", "attachment": {}},
    {"type": "last-prompt", "lastPrompt": "x"},
    {"type": "cost-state"},
    {"type": "ai-title", "title": "t"},
    {"type": "file-history-snapshot", "snapshot": {}},
]


def _write(path: Path, events: list[dict]) -> Path:
    path.write_text("".join(json.dumps(e, ensure_ascii=False) + "\n" for e in events), "utf-8")
    return path


@pytest.mark.parametrize(
    ("events", "mid_turn"),
    [
        # Finished and waiting — the usual case: say nothing.
        ([_user("hi"), _assistant("end_turn", TEXT)], False),
        ([_user("hi"), _assistant("end_turn", TEXT), TURN_END, *META], False),
        # A synthetic API-error reply also ends the turn; Claude is waiting.
        ([_user("hi"), _assistant("stop_sequence", TEXT, isApiErrorMessage=True), *META], False),
        # Cut off while working.
        ([_user("hi"), _assistant("tool_use", TOOL_USE)], True),
        ([_user("hi"), _assistant("tool_use", TOOL_USE), _user(TOOL_RESULT), *META], True),
        ([_user("hi"), *META], True),  # a prompt Claude never got to answer
        ([_user([{"type": "text", "text": "hi"}])], True),
        ([_user("hi"), _assistant(None, {"type": "thinking", "thinking": "…"})], True),
        # The user stopped it themselves (Esc) — nothing to report.
        (
            [_user("hi"), _assistant("tool_use", TOOL_USE), _user("[Request interrupted by user]")],
            False,
        ),
        (
            [
                _user("hi"),
                _assistant("tool_use", TOOL_USE),
                _user([{"type": "text", "text": "[Request interrupted by user for tool use]"}]),
            ],
            False,
        ),
        # A slash command is not a turn.
        ([_user("<command-name>/clear</command-name>"), {"type": "system", "subtype": "x"}], False),
        ([_user("<local-command-stdout>ok</local-command-stdout>")], False),
        # Meta / sidechain user rows are not the last *message*.
        (
            [
                _user("hi"),
                _assistant("end_turn", TEXT),
                _user("caveat", isMeta=True),
                _user("side", isSidechain=True),
            ],
            False,
        ),
        # Claude Code closes every finished turn with a turn-end marker; one
        # after the last message settles it whatever that message looks like.
        (
            [_user("hi"), _assistant(None, TEXT), {"type": "system", "subtype": "turn_duration"}],
            False,
        ),
        ([_user("hi"), _assistant(None, TEXT), {"type": "result"}], False),
        # Nothing to judge.
        ([], False),
        (META, False),
    ],
)
def test_last_entry_decides(tmp_path: Path, events: list[dict], mid_turn: bool) -> None:
    assert transcript_stopped_mid_turn(_write(tmp_path / "t.jsonl", events)) is mid_turn


COMPACT_BOUNDARY = {"type": "system", "subtype": "compact_boundary"}
COMPACT_SUMMARY = _user(
    "This session is being continued from a previous conversation that ran out of context.",
    isCompactSummary=True,
    isVisibleInTranscriptOnly=True,
)
TASK_NOTIFICATION = _user(
    "<task-notification>\n<task-id>b1</task-id>\n<status>completed</status>\n"
    "<summary>Background command done</summary>\n</task-notification>"
)


@pytest.mark.parametrize(
    ("events", "mid_turn"),
    [
        # #895: rows nobody typed are not a request — judge the row before them.
        (
            [
                _user("hi"),
                _assistant("end_turn", TEXT),
                TURN_END,
                COMPACT_BOUNDARY,
                COMPACT_SUMMARY,
                *META,
            ],
            False,
        ),
        ([_user("hi"), _assistant("end_turn", TEXT), COMPACT_SUMMARY], False),
        (
            [
                _user("hi"),
                _assistant("end_turn", TEXT),
                TURN_END,
                COMPACT_BOUNDARY,
                COMPACT_SUMMARY,
                _user("<command-name>/compact</command-name>"),
                _user("<local-command-stdout>Compacted</local-command-stdout>"),
            ],
            False,
        ),
        ([_user("hi"), _assistant("end_turn", TEXT), TURN_END, TASK_NOTIFICATION], False),
        ([_user("hi"), _assistant("end_turn", TEXT), _user("<bash-input>ls</bash-input>")], False),
        # …but a turn that really was cut off stays cut off underneath them.
        ([_user("hi"), _assistant("tool_use", TOOL_USE), COMPACT_BOUNDARY, COMPACT_SUMMARY], True),
        (
            [_user("hi"), _assistant("tool_use", TOOL_USE), _user(TOOL_RESULT), TASK_NOTIFICATION],
            True,
        ),
        ([_user("hi"), COMPACT_SUMMARY], True),
    ],
)
def test_rows_nobody_typed_are_looked_past(
    tmp_path: Path, events: list[dict], mid_turn: bool
) -> None:
    assert transcript_stopped_mid_turn(_write(tmp_path / "t.jsonl", events)) is mid_turn


_FIXTURES = Path(__file__).parent / "fixtures" / "transcripts"
# The transcript #895 misread (こぷー管理, 2026-10-13). Read only; absent off the host.
_KOPUU = Path(
    "/home/yousan/.claude/projects/-home-yousan-c-lord-sessions-1504670029923352597-"
    "1530372563858096321/9efbdfaa-5df4-46a2-855a-84fc9edb2c72.jsonl"
)


def test_compact_summary_last_is_not_mid_turn_895() -> None:
    """The last 40 rows of the misread transcript, contents redacted, shape kept."""
    fixture = _FIXTURES / "i895_compact_summary_last.jsonl"
    assert transcript_stopped_mid_turn(fixture) is False


@pytest.mark.skipif(not _KOPUU.exists(), reason="the production transcript lives on the bot host")
def test_the_real_kopuu_transcript_is_not_mid_turn_895() -> None:
    assert transcript_stopped_mid_turn(_KOPUU) is False


def test_notice_does_not_blame_a_host_reboot_895() -> None:
    """A c-lord-only restart can post it too, so it must not claim the host rebooted."""
    assert "ホストの再起動" not in HOST_RESTART_NOTICE
    assert "止まっていました" in HOST_RESTART_NOTICE
    assert "続けて" in HOST_RESTART_NOTICE


def test_only_the_last_entry_matters(tmp_path: Path) -> None:
    """An old mid-turn stretch earlier in the file must not count."""
    events = [_user("a"), _assistant("tool_use", TOOL_USE), _user(TOOL_RESULT)]
    events += [_assistant("end_turn", TEXT)]
    assert transcript_stopped_mid_turn(_write(tmp_path / "t.jsonl", events)) is False


def test_huge_last_entry_is_still_found(tmp_path: Path) -> None:
    """The last message can be bigger than one read window (a large tool output)."""
    big = [{"type": "tool_result", "tool_use_id": "t1", "content": "x" * 600_000}]
    path = _write(tmp_path / "t.jsonl", [_user("a"), _assistant("tool_use", TOOL_USE), _user(big)])
    assert transcript_stopped_mid_turn(path) is True


def test_torn_last_line_is_skipped(tmp_path: Path) -> None:
    path = _write(tmp_path / "t.jsonl", [_user("a"), _assistant("end_turn", TEXT)])
    with path.open("a", encoding="utf-8") as f:
        f.write('{"type": "user", "mess')
    assert transcript_stopped_mid_turn(path) is False


def test_missing_file_is_not_mid_turn(tmp_path: Path) -> None:
    assert transcript_stopped_mid_turn(tmp_path / "nope.jsonl") is False


def test_uses_the_claimed_transcript(tmp_path: Path) -> None:
    """Same ownership rule as the mirror (#773): the jsonl c-lord named wins,
    even when a stranger's mid-turn transcript is newer."""
    project = tmp_path / "proj"
    project.mkdir()
    write_claim(project, SID)
    _write(project / f"{SID}.jsonl", [_user("hi"), _assistant("end_turn", TEXT)])
    _write(project / "stranger.jsonl", [_user("hi"), _assistant("tool_use", TOOL_USE)])
    assert stopped_mid_turn(project) is False

    _write(project / f"{SID}.jsonl", [_user("hi"), _assistant("tool_use", TOOL_USE)])
    assert stopped_mid_turn(project) is True


def test_no_owned_transcript_is_not_mid_turn(tmp_path: Path) -> None:
    project = tmp_path / "proj"
    project.mkdir()
    _write(project / "stranger.jsonl", [_user("hi"), _assistant("tool_use", TOOL_USE)])
    assert stopped_mid_turn(project) is False


# ── which Claudes are alive ──────────────────────────────────────────────


def _cp(rc: int, out: str = "", err: str = "") -> CompletedProcess[str]:
    return CompletedProcess(["tmux"], rc, out, err)


def test_live_claude_panes_reads_tags_and_paths() -> None:
    out = "11\tclaude\t/w/a\n\tclaude\t/w/b\n12\tzsh\t/w/c\n"
    with (
        patch("c_lord.tmux._tmux_available", return_value=True),
        patch("c_lord.tmux._run", return_value=_cp(0, out)),
    ):
        live = live_claude_panes()
    assert live is not None
    assert live.covers(11, "/elsewhere")
    assert live.covers(99, "/w/b")  # untagged pane, matched by its cwd
    assert not live.covers(12, "/w/c")  # a shell is not Claude


def test_no_tmux_server_means_nobody_is_alive() -> None:
    """After a host reboot there is no server at all — that is the case to catch."""
    with (
        patch("c_lord.tmux._tmux_available", return_value=True),
        patch(
            "c_lord.tmux._run",
            return_value=_cp(1, err="no server running on /tmp/tmux-1000/default\n"),
        ),
    ):
        live = live_claude_panes()
    assert live == LiveClaude(frozenset(), frozenset())


def test_unreadable_tmux_is_unknown() -> None:
    """ "Couldn't ask" is not "everyone is dead" (fleet-tmux-restart.md)."""
    with patch("c_lord.tmux._tmux_available", return_value=False):
        assert live_claude_panes() is None
    with (
        patch("c_lord.tmux._tmux_available", return_value=True),
        patch("c_lord.tmux._run", return_value=_cp(1, err="permission denied\n")),
    ):
        assert live_claude_panes() is None


# ── posting ──────────────────────────────────────────────────────────────


def _message(author_id: int, content: str) -> MagicMock:
    m = MagicMock()
    m.author.id = author_id
    m.content = content
    return m


def _thread(history: list[MagicMock] | None = None, *, locked: bool = False) -> MagicMock:
    thread = MagicMock(spec=discord.Thread)
    thread.locked = locked
    thread.send = AsyncMock()
    newest_first = list(reversed(history or []))

    def _history(limit: int = 100):
        async def gen():
            for m in newest_first[:limit]:
                yield m

        return gen()

    thread.history = MagicMock(side_effect=_history)
    return thread


def _bot(threads: dict[int, object]) -> MagicMock:
    bot = MagicMock()
    bot.user.id = 1
    bot.get_channel = MagicMock(side_effect=lambda tid: threads.get(tid))
    bot.fetch_channel = AsyncMock(side_effect=discord.NotFound(MagicMock(status=404), "gone"))
    return bot


@pytest.fixture
def home(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> Path:
    monkeypatch.setenv("HOME", str(tmp_path))
    return tmp_path


def _workspace(home: Path, name: str, events: list[dict]) -> str:
    """A working dir whose claimed transcript holds ``events``."""
    cwd = f"/w/{name}"
    project = home / ".claude" / "projects" / cwd.replace("/", "-")
    project.mkdir(parents=True)
    write_claim(project, SID)
    _write(project / f"{SID}.jsonl", events)
    return cwd


MID = [_user("hi"), _assistant("tool_use", TOOL_USE), _user(TOOL_RESULT)]
DONE = [_user("hi"), _assistant("end_turn", TEXT)]
NOBODY = LiveClaude(frozenset(), frozenset())


async def test_posts_once_into_a_dead_mid_turn_thread(home: Path) -> None:
    thread = _thread([_message(2, "やって")])
    bot = _bot({10: thread})
    sent = await notify_host_restart_stops(bot, [(10, _workspace(home, "a", MID))], live=NOBODY)
    assert sent == 1
    thread.send.assert_awaited_once()
    args, kwargs = thread.send.call_args
    assert args == (HOST_RESTART_NOTICE,)
    # No ping, no button.
    assert kwargs["allowed_mentions"].to_dict()["parse"] == []
    assert "view" not in kwargs and "components" not in kwargs
    assert "<@" not in HOST_RESTART_NOTICE


async def test_finished_thread_gets_nothing(home: Path) -> None:
    thread = _thread()
    bot = _bot({10: thread})
    assert (
        await notify_host_restart_stops(bot, [(10, _workspace(home, "a", DONE))], live=NOBODY) == 0
    )
    thread.send.assert_not_awaited()


async def test_live_claude_gets_nothing(home: Path) -> None:
    """c-lord-only restart: Claude is still running in tmux, mid-turn is real work."""
    thread = _thread()
    bot = _bot({10: thread})
    cwd = _workspace(home, "a", MID)
    live = LiveClaude(frozenset({10}), frozenset())
    assert await notify_host_restart_stops(bot, [(10, cwd)], live=live) == 0
    live = LiveClaude(frozenset(), frozenset({cwd}))
    assert await notify_host_restart_stops(bot, [(10, cwd)], live=live) == 0
    thread.send.assert_not_awaited()


async def test_unknown_tmux_says_nothing(home: Path) -> None:
    thread = _thread()
    bot = _bot({10: thread})
    assert await notify_host_restart_stops(bot, [(10, _workspace(home, "a", MID))], live=None) == 0
    thread.send.assert_not_awaited()


async def test_repeated_restarts_post_once(home: Path) -> None:
    """Dedup is read off the thread itself: our notice since the last human message."""
    thread = _thread([_message(2, "やって"), _message(1, HOST_RESTART_NOTICE)])
    bot = _bot({10: thread})
    assert (
        await notify_host_restart_stops(bot, [(10, _workspace(home, "a", MID))], live=NOBODY) == 0
    )
    thread.send.assert_not_awaited()


async def test_the_old_wording_still_counts_as_told_895(home: Path) -> None:
    """Rewording the notice must not post the same stop again under the new text."""
    old = (
        "⚠️ ホストの再起動で、作業の途中で Claude が止まりました。"
        "続けるなら、このスレに「続けて」と投稿してください。"
    )
    thread = _thread([_message(2, "やって"), _message(1, old)])
    bot = _bot({10: thread})
    assert (
        await notify_host_restart_stops(bot, [(10, _workspace(home, "a", MID))], live=NOBODY) == 0
    )
    thread.send.assert_not_awaited()


async def test_other_bot_lines_after_the_notice_still_dedup(home: Path) -> None:
    thread = _thread(
        [_message(2, "やって"), _message(1, HOST_RESTART_NOTICE), _message(1, "何か別の案内")]
    )
    bot = _bot({10: thread})
    assert (
        await notify_host_restart_stops(bot, [(10, _workspace(home, "a", MID))], live=NOBODY) == 0
    )


async def test_a_new_stop_after_resuming_is_reported_again(home: Path) -> None:
    """Posted 続けて, it resumed, and the host went down again mid-turn."""
    thread = _thread([_message(1, HOST_RESTART_NOTICE), _message(2, "続けて")])
    bot = _bot({10: thread})
    assert (
        await notify_host_restart_stops(bot, [(10, _workspace(home, "a", MID))], live=NOBODY) == 1
    )


async def test_deleted_and_locked_threads_are_skipped(home: Path) -> None:
    locked = _thread(locked=True)
    bot = _bot({20: locked})  # 10 is not cached and fetch → NotFound (deleted)
    rows = [(10, _workspace(home, "a", MID)), (20, _workspace(home, "b", MID))]
    assert await notify_host_restart_stops(bot, rows, live=NOBODY) == 0
    locked.send.assert_not_awaited()


async def test_one_failing_thread_does_not_stop_the_rest(home: Path) -> None:
    bad = _thread()
    bad.send = AsyncMock(side_effect=discord.HTTPException(MagicMock(status=500), "boom"))
    good = _thread()
    bot = _bot({10: bad, 20: good})
    rows = [(10, _workspace(home, "a", MID)), (20, _workspace(home, "b", MID))]
    assert await notify_host_restart_stops(bot, rows, live=NOBODY) == 1
    good.send.assert_awaited_once()


# ── wired into startup ───────────────────────────────────────────────────


async def test_on_ready_posts_the_notice(home: Path) -> None:
    from c_lord.cogs.transcript_mirror import TranscriptMirrorCog

    cwd = _workspace(home, "a", MID)
    closed_cwd = _workspace(home, "b", MID)
    thread = _thread([_message(2, "やって")])
    closed_thread = _thread([_message(2, "やって")])
    bot = _bot({10: thread, 20: closed_thread})

    def row(tid: int, wd: str, closed: str | None = None) -> MagicMock:
        r = MagicMock()
        r.thread_id, r.working_dir, r.closed_at = tid, wd, closed
        r.mirror_replied_uuid = "a"
        return r

    repo = MagicMock()
    repo.list_all = AsyncMock(return_value=[row(10, cwd), row(20, closed_cwd, "2026-09-01")])
    repo.set_mirror_replied_uuid = AsyncMock()
    cog = TranscriptMirrorCog(bot, session_repo=repo)
    try:
        with patch("c_lord.cogs.transcript_mirror.live_claude_panes", return_value=NOBODY):
            await cog.on_ready()
        thread.send.assert_awaited_once()
        assert thread.send.call_args.args == (HOST_RESTART_NOTICE,)
        closed_thread.send.assert_not_awaited()
    finally:
        await cog.cog_unload()
