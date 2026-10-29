"""thread_state_sync: decision paths ``tests/test_thread_state_sync.py`` leaves open.

Each scenario comes from an Issue (cited per test): the #930 network-failure
rename path, the #618 session-label lookup through ChannelRepoCog, the #359
unparsable-menu capture, the #438 ownership filter when the cog breaks, and
the loop lifecycle that keeps one bad tick from ending the sweep.

No real tmux: ``subprocess.run`` / ``_capture_pane_text`` / ``_list_all_windows``
are patched in every test that would reach them.
"""

from __future__ import annotations

import asyncio
import logging
import socket
from dataclasses import dataclass
from pathlib import Path
from unittest.mock import AsyncMock, MagicMock, patch

import aiohttp
import discord
import pytest

from c_lord import thread_state_sync
from c_lord.cogs.channel_repo import ChannelRepoCog
from c_lord.database.channel_repo import ChannelRepository
from c_lord.database.thread_repo import ThreadRepository
from c_lord.thread_state_sync import (
    MenuWatchdogLoop,
    ThreadStateSyncLoop,
    _capture_pane_text,
    _list_all_windows,
    _pane_foreground_command,
    record_unparsable_menu,
)


@dataclass
class _Rec:
    """Same shape as the helper in tests/test_thread_state_sync.py."""

    thread_id: int
    topic: str | None = "T"
    state: str | None = "waiting"
    tmux_window_id: str | None = None
    rename_backoff_until: str | None = None
    issue_ref: str | None = None
    origin_issue_ref: str | None = None
    closed_at: str | None = None


def _repo() -> MagicMock:
    repo = MagicMock()
    repo.set_state = AsyncMock()
    repo.set_tmux_window_id = AsyncMock()
    repo.set_rename_backoff_until = AsyncMock()
    return repo


def _thread(thread_id: int, *, name: str = "old name", parent_id: int | None = 999) -> MagicMock:
    t = MagicMock(spec=discord.Thread)
    t.id = thread_id
    t.name = name
    t.parent_id = parent_id
    t.edit = AsyncMock()
    return t


# ---------------------------------------------------------------------------
# tmux helpers: "could not tell" is never "dead" / never a crash
# ---------------------------------------------------------------------------


class TestTmuxHelpers:
    def test_capture_without_target_does_not_shell_out(self) -> None:
        with patch.object(thread_state_sync.subprocess, "run") as run:
            assert _capture_pane_text("", "w1") == ""
            assert _capture_pane_text("clord", "") == ""
        run.assert_not_called()

    def test_capture_returns_empty_when_tmux_missing_or_fails(self) -> None:
        with patch.object(thread_state_sync.subprocess, "run", side_effect=FileNotFoundError):
            assert _capture_pane_text("clord", "w1") == ""
        with patch.object(
            thread_state_sync.subprocess, "run", return_value=MagicMock(returncode=1, stdout="x")
        ):
            assert _capture_pane_text("clord", "w1") == ""

    def test_capture_targets_session_colon_window(self) -> None:
        """The pane is addressed by the swept ``session:window`` — not via a
        channel-resolved manager (#427: the two can disagree)."""
        with patch.object(
            thread_state_sync.subprocess, "run", return_value=MagicMock(returncode=0, stdout="❯\n")
        ) as run:
            assert _capture_pane_text("qiita-article", "w1", 120) == "❯\n"
        argv = run.call_args.args[0]
        assert argv[argv.index("-t") + 1] == "qiita-article:w1"
        assert argv[argv.index("-S") + 1] == "-120"

    def test_foreground_command_unknown_is_none_not_dead(self) -> None:
        """#510: None means "could not tell"; the watchdog must not read it as
        a dead pane and drop a live menu."""
        assert _pane_foreground_command("", "w1") is None
        with patch.object(thread_state_sync.subprocess, "run", side_effect=FileNotFoundError):
            assert _pane_foreground_command("s", "w1") is None
        with patch.object(
            thread_state_sync.subprocess, "run", return_value=MagicMock(returncode=1, stdout="")
        ):
            assert _pane_foreground_command("s", "w1") is None
        with patch.object(
            thread_state_sync.subprocess, "run", return_value=MagicMock(returncode=0, stdout="\n")
        ):
            assert _pane_foreground_command("s", "w1") is None
        with patch.object(
            thread_state_sync.subprocess,
            "run",
            return_value=MagicMock(returncode=0, stdout="claude\n"),
        ):
            assert _pane_foreground_command("s", "w1") == "claude"

    def test_list_all_windows_skips_malformed_lines(self) -> None:
        fake = MagicMock(returncode=0, stdout="garbage\nclord|@1|0\n")
        with patch.object(thread_state_sync.subprocess, "run", return_value=fake):
            out = _list_all_windows()
        assert out == [
            {
                "session_name": "clord",
                "window_id": "@1",
                "window_index": "0",
                "thread_id": "",
                "window_name": "",
            }
        ]


# ---------------------------------------------------------------------------
# #359: unparsable menu capture store
# ---------------------------------------------------------------------------


class TestRecordUnparsableMenu:
    def test_writes_each_distinct_frame_once(self, tmp_path: Path) -> None:
        """#359 / #579: the sweep revisits a stuck menu every 60s — the same
        frame must not be written again."""
        first = record_unparsable_menu(
            thread_id=1, session_name="s", window_name="w1", pane_text="A", directory=tmp_path
        )
        assert first is not None and first.read_text() == "A"
        again = record_unparsable_menu(
            thread_id=1, session_name="s", window_name="w1", pane_text="A", directory=tmp_path
        )
        assert again is None
        assert len(list(tmp_path.glob("*.txt"))) == 1

    def test_store_is_bounded(self, tmp_path: Path) -> None:
        for i in range(thread_state_sync._MAX_UNPARSABLE_CAPTURES):
            (tmp_path / f"pre{i}.txt").write_text(str(i))
        assert (
            record_unparsable_menu(
                thread_id=1, session_name="s", window_name="w1", pane_text="new", directory=tmp_path
            )
            is None
        )

    def test_env_var_sets_directory(self, tmp_path: Path, monkeypatch) -> None:
        target = tmp_path / "captures"
        monkeypatch.setenv(thread_state_sync._UNPARSABLE_DIR_ENV, str(target))
        path = record_unparsable_menu(
            thread_id=7, session_name="s", window_name="w1", pane_text="menu"
        )
        assert path is not None and path.parent == target

    def test_write_failure_never_raises(self, tmp_path: Path) -> None:
        """A diagnostic must not be able to take the sweep down."""
        blocker = tmp_path / "file"
        blocker.write_text("not a dir")
        assert (
            record_unparsable_menu(
                thread_id=1, session_name="s", window_name="w1", pane_text="x", directory=blocker
            )
            is None
        )


# ---------------------------------------------------------------------------
# ThreadStateSyncLoop._sync_one: rename-side decisions
# ---------------------------------------------------------------------------


class TestSyncOneRenamePaths:
    async def test_get_channel_raising_keeps_state_sync(self) -> None:
        """The DB state is persisted before any Discord lookup, so a failing
        cache lookup cannot lose it."""
        repo = _repo()
        bot = MagicMock()
        bot.get_channel.side_effect = RuntimeError("cache gone")
        loop = ThreadStateSyncLoop(bot, repo, interval_seconds=999)
        await loop._sync_one(_Rec(thread_id=1, state="waiting"), by_tid={})
        repo.set_state.assert_awaited_once_with(1, "dead")

    async def test_non_thread_channel_is_never_renamed(self) -> None:
        repo = _repo()
        text_channel = MagicMock(spec=discord.TextChannel)
        text_channel.edit = AsyncMock()
        bot = MagicMock()
        bot.get_channel.return_value = text_channel
        loop = ThreadStateSyncLoop(bot, repo, interval_seconds=999)
        await loop._sync_one(_Rec(thread_id=2), by_tid={})
        text_channel.edit.assert_not_awaited()

    async def test_network_failure_on_rename_sets_no_backoff(self, caplog) -> None:
        """#930: DNS/connection failure is not a rate limit. It is logged and
        retried next sweep — no 600s backoff, no persisted deadline."""
        repo = _repo()
        thread = _thread(3)
        thread.edit = AsyncMock(side_effect=socket.gaierror(-3, "Temporary failure"))
        bot = MagicMock()
        bot.get_channel.return_value = thread
        bot.get_cog.return_value = None
        loop = ThreadStateSyncLoop(bot, repo, interval_seconds=999)
        with caplog.at_level(logging.WARNING, logger="c_lord.thread_state_sync"):
            await loop._sync_one(_Rec(thread_id=3), by_tid={})
        assert 3 not in loop._rename_backoff
        repo.set_rename_backoff_until.assert_not_awaited()
        assert "rename failed for thread 3" in caplog.text

    async def test_aiohttp_client_error_on_rename_is_contained(self) -> None:
        """#930: the other half of DISCORD_CALL_FAILURES."""
        repo = _repo()
        thread = _thread(4)
        thread.edit = AsyncMock(side_effect=aiohttp.ClientConnectionError("reset"))
        bot = MagicMock()
        bot.get_channel.return_value = thread
        bot.get_cog.return_value = None
        loop = ThreadStateSyncLoop(bot, repo, interval_seconds=999)
        await loop._sync_one(_Rec(thread_id=4), by_tid={})  # must not raise
        assert 4 not in loop._rename_backoff

    async def test_non_429_http_error_sets_no_backoff(self) -> None:
        """Only a 429 means "wait"; e.g. a 403 on an archived thread does not."""
        repo = _repo()
        thread = _thread(5)
        resp = MagicMock(status=403, reason="Forbidden")
        thread.edit = AsyncMock(side_effect=discord.Forbidden(resp, "Missing Permissions"))
        bot = MagicMock()
        bot.get_channel.return_value = thread
        bot.get_cog.return_value = None
        loop = ThreadStateSyncLoop(bot, repo, interval_seconds=999)
        await loop._sync_one(_Rec(thread_id=5), by_tid={})
        assert 5 not in loop._rename_backoff
        repo.set_rename_backoff_until.assert_not_awaited()

    async def test_repo_without_backoff_setter_still_renames(self) -> None:
        """#281: ``set_rename_backoff_until`` is optional for older consumers."""
        repo = MagicMock(spec=["set_state", "set_tmux_window_id", "list_all"])
        repo.set_state = AsyncMock()
        repo.set_tmux_window_id = AsyncMock()
        thread = _thread(6)
        bot = MagicMock()
        bot.get_channel.return_value = thread
        bot.get_cog.return_value = None
        loop = ThreadStateSyncLoop(bot, repo, interval_seconds=999)
        await loop._sync_one(_Rec(thread_id=6), by_tid={})
        thread.edit.assert_awaited_once()


# ---------------------------------------------------------------------------
# #618: session label resolved through the real ChannelRepoCog
# ---------------------------------------------------------------------------


@pytest.fixture
async def channel_cog(tmp_path) -> ChannelRepoCog:
    repo = ChannelRepository(str(tmp_path / "channel.db"))
    await repo.init_db()
    threads = ThreadRepository(str(tmp_path / "thread.db"))
    await threads.init_db()
    # #618 real case: channel #claude_base, thread working in qiita-article.
    await repo.save(channel_id=999, source_repo="https://github.com/yousan/claude_base.git")
    await threads.save(
        thread_id=333, source_repo="https://github.com/yousan/qiita-article.git", channel_id=999
    )
    return ChannelRepoCog(MagicMock(), repo=repo, thread_repo=threads)


class TestDivergentSessionLabelViaCog:
    async def test_thread_in_another_repos_session_gets_the_label(
        self, channel_cog: ChannelRepoCog
    ) -> None:
        """#618 end-to-end: the sweep asks the real cog, and the rename carries
        ``qiita-article:W1`` so ``tmux attach`` lands on the right window."""
        bot = MagicMock()
        bot.get_cog.return_value = channel_cog
        thread = _thread(333, parent_id=999)
        bot.get_channel.return_value = thread
        loop = ThreadStateSyncLoop(bot, _repo(), interval_seconds=999)
        by_tid = {
            333: {
                "window_id": "@9",
                "window_index": "0",
                "thread_id": "333",
                "session_name": "qiita-article",
                "window_name": "w1",
            }
        }
        with patch.object(thread_state_sync, "_capture_pane_text", return_value="❯ "):
            await loop._sync_one(_Rec(thread_id=333, topic="Qiita記事"), by_tid)
        assert "qiita-article:W1 │ " in thread.edit.await_args.kwargs["name"]

    async def test_thread_in_its_channels_session_gets_no_label(
        self, channel_cog: ChannelRepoCog
    ) -> None:
        bot = MagicMock()
        bot.get_cog.return_value = channel_cog
        loop = ThreadStateSyncLoop(bot, _repo(), interval_seconds=999)
        assert await loop._divergent_session_name(_thread(444, parent_id=999)) is None

    async def test_no_parent_or_no_cog_gives_no_label(self) -> None:
        bot = MagicMock()
        bot.get_cog.return_value = None
        loop = ThreadStateSyncLoop(bot, _repo(), interval_seconds=999)
        assert await loop._divergent_session_name(_thread(1, parent_id=None)) is None
        assert await loop._divergent_session_name(_thread(1, parent_id=5)) is None

    async def test_cog_failure_gives_no_label_instead_of_raising(self) -> None:
        """A broken lookup must cost the label, not the lamp sweep."""
        cog = MagicMock(spec=ChannelRepoCog)
        cog.divergent_session_name = AsyncMock(side_effect=RuntimeError("db locked"))
        bot = MagicMock()
        bot.get_cog.return_value = cog
        loop = ThreadStateSyncLoop(bot, _repo(), interval_seconds=999)
        assert await loop._divergent_session_name(_thread(1, parent_id=5)) is None


# ---------------------------------------------------------------------------
# Loop lifecycle: one bad tick must not end the sweep
# ---------------------------------------------------------------------------


class TestLoopLifecycle:
    async def test_stop_before_start_is_a_noop(self) -> None:
        loop = ThreadStateSyncLoop(MagicMock(), _repo(), interval_seconds=999)
        await loop.stop()
        assert loop._task is None
        wd = MenuWatchdogLoop(MagicMock(), interval_seconds=999)
        await wd.stop()
        assert wd._task is None

    @pytest.mark.parametrize("cls", [ThreadStateSyncLoop, MenuWatchdogLoop])
    async def test_run_survives_a_raising_tick(self, cls, caplog) -> None:
        if cls is ThreadStateSyncLoop:
            loop = cls(MagicMock(), _repo(), interval_seconds=0.001)
        else:
            loop = cls(MagicMock(), interval_seconds=0.001)
        calls = 0
        survived = asyncio.Event()

        async def tick() -> None:
            nonlocal calls
            calls += 1
            if calls == 1:
                raise RuntimeError("tmux server gone")
            survived.set()

        loop.tick = tick  # type: ignore[method-assign]
        with caplog.at_level(logging.ERROR, logger="c_lord.thread_state_sync"):
            loop.start()
            await asyncio.wait_for(survived.wait(), timeout=2)
            await loop.stop()
        assert calls >= 2
        assert "tick raised" in caplog.text
        assert loop._task is None

    async def test_watchdog_start_is_idempotent_and_stop_cancels_bridges(self) -> None:
        wd = MenuWatchdogLoop(MagicMock(), interval_seconds=999)
        wd.start()
        first = wd._task
        wd.start()
        assert wd._task is first
        bridge = asyncio.create_task(asyncio.sleep(999))
        wd._ask_bridges[1] = bridge
        await wd.stop()
        await asyncio.sleep(0)
        assert bridge.cancelled()
        assert wd._ask_bridges == {}


# ---------------------------------------------------------------------------
# MenuWatchdogLoop.tick / ownership (#438) and bridge edge cases
# ---------------------------------------------------------------------------


class TestWatchdogTickPaths:
    async def test_windows_without_thread_id_are_ignored(self) -> None:
        """A plain tmux window (no ``@thread_id``) is not ours to bridge."""
        wd = MenuWatchdogLoop(MagicMock(), interval_seconds=999)
        wd._maybe_bridge_open_menu = AsyncMock()  # type: ignore[method-assign]
        windows = [{"thread_id": "", "session_name": "clord", "window_name": "zsh"}]
        with (
            patch.object(thread_state_sync, "_list_all_windows", return_value=windows),
            patch.object(thread_state_sync, "_capture_pane_text") as cap,
        ):
            await wd.tick()
        cap.assert_not_called()
        wd._maybe_bridge_open_menu.assert_not_awaited()

    async def test_one_failing_window_does_not_stop_the_sweep(self, caplog) -> None:
        bot = MagicMock()
        bot.get_cog.return_value = None
        wd = MenuWatchdogLoop(bot, interval_seconds=999)
        seen: list[int] = []

        async def bridge(tid, *_a) -> None:
            seen.append(tid)
            if tid == 1:
                raise RuntimeError("boom")

        wd._maybe_bridge_open_menu = bridge  # type: ignore[method-assign]
        windows = [
            {"thread_id": "1", "session_name": "clord", "window_name": "w1"},
            {"thread_id": "2", "session_name": "clord", "window_name": "w2"},
        ]
        with (
            patch.object(thread_state_sync, "_list_all_windows", return_value=windows),
            patch.object(thread_state_sync, "_capture_pane_text", return_value=""),
            caplog.at_level(logging.ERROR, logger="c_lord.thread_state_sync"),
        ):
            await wd.tick()
        assert seen == [1, 2]
        assert "menu watchdog failed for thread=1" in caplog.text

    async def test_managed_names_failure_falls_back_to_db_guard(self) -> None:
        """#438: if the cog cannot list sessions, AC1 is skipped (None) and
        ownership rests on AC2 alone — our own thread is still processed."""
        cog = MagicMock(spec=ChannelRepoCog)
        cog.managed_session_names = AsyncMock(side_effect=RuntimeError("db locked"))
        bot = MagicMock()
        bot.get_cog.return_value = cog
        repo = MagicMock()
        repo.get = AsyncMock(side_effect=lambda tid: object() if tid == 1 else None)
        wd = MenuWatchdogLoop(bot, interval_seconds=999, repo=repo)
        assert await wd._managed_session_names() is None
        assert await wd._owns_window(1, "any-session", None) is True
        assert await wd._owns_window(2, "any-session", None) is False


def _fixture(name: str) -> str:
    return (Path(__file__).parent / "fixtures" / "panes" / name).read_text()


class TestWatchdogBridgeEdges:
    async def test_signature_without_parsable_menu_is_recorded(self) -> None:
        """#359: signature present, parser yields nothing → the frame is kept
        for diagnosis and nothing is bridged."""
        from c_lord.claude.tmux_runner import _ASK_SIGNATURE

        wd = MenuWatchdogLoop(MagicMock(), interval_seconds=999)
        pane = f"some output\n{_ASK_SIGNATURE}\n(garbled)\n"
        with (
            patch.object(thread_state_sync, "_capture_pane_text", return_value=pane),
            patch.object(thread_state_sync, "_pane_foreground_command", return_value="claude"),
            patch.object(thread_state_sync, "record_unparsable_menu") as rec,
        ):
            await wd._maybe_bridge_open_menu(359, "sess", "w1", pane)
        rec.assert_called_once()
        assert rec.call_args.kwargs["thread_id"] == 359
        assert 359 not in wd._ask_bridges

    async def test_uncached_thread_is_fetched_before_bridging(self) -> None:
        """After a restart the thread may not be in the cache — the watchdog
        must fetch it, since recovering pre-restart menus is its job (#359)."""
        bot = MagicMock()
        bot.get_cog.return_value = None
        bot.tmux_manager = MagicMock()
        bot.ask_repo = None
        bot.get_channel.return_value = None
        thread = MagicMock(spec=discord.Thread)
        bot.fetch_channel = AsyncMock(return_value=thread)
        wd = MenuWatchdogLoop(bot, interval_seconds=999)
        pane = _fixture("ask_rich_descriptions.txt")
        with (
            patch.object(thread_state_sync, "_capture_pane_text", return_value=pane),
            patch.object(thread_state_sync, "_pane_foreground_command", return_value="claude"),
            patch("c_lord.discord_ui.ask_handler.bridge_pane_ask", new=AsyncMock()) as bridge,
        ):
            await wd._maybe_bridge_open_menu(360, "sess", "w1", pane)
            await asyncio.gather(wd._ask_bridges[360])
        bot.fetch_channel.assert_awaited_once_with(360)
        assert bridge.await_args.args[0] is thread

    async def test_unreachable_thread_is_not_bridged(self) -> None:
        bot = MagicMock()
        bot.get_channel.return_value = None
        bot.fetch_channel = AsyncMock(side_effect=OSError("network down"))
        wd = MenuWatchdogLoop(bot, interval_seconds=999)
        pane = _fixture("ask_rich_descriptions.txt")
        with (
            patch.object(thread_state_sync, "_capture_pane_text", return_value=pane),
            patch.object(thread_state_sync, "_pane_foreground_command", return_value="claude"),
        ):
            await wd._maybe_bridge_open_menu(361, "sess", "w1", pane)
        assert 361 not in wd._ask_bridges

    async def test_cancelled_bridge_does_not_count_as_failure(self) -> None:
        """#579's failure budget counts real post failures; a cancellation
        (bot shutdown via stop()) must not burn it."""
        bot = MagicMock()
        bot.get_cog.return_value = None
        bot.tmux_manager = MagicMock()
        bot.ask_repo = None
        bot.get_channel.return_value = MagicMock(spec=discord.Thread)
        wd = MenuWatchdogLoop(bot, interval_seconds=999)
        pane = _fixture("ask_rich_descriptions.txt")
        started = asyncio.Event()

        async def hang(*_a, **_kw) -> None:
            started.set()
            await asyncio.sleep(999)

        with (
            patch.object(thread_state_sync, "_capture_pane_text", return_value=pane),
            patch.object(thread_state_sync, "_pane_foreground_command", return_value="claude"),
            patch("c_lord.discord_ui.ask_handler.bridge_pane_ask", new=hang),
        ):
            await wd._maybe_bridge_open_menu(362, "sess", "w1", pane)
            task = wd._ask_bridges[362]
            await started.wait()
            task.cancel()
            await asyncio.gather(task, return_exceptions=True)
        assert task.cancelled()
        assert wd._ask_bridge_failures.get(362, 0) == 0
