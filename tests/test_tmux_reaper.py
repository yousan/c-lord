"""Tests for the tmux orphan reaper — Issue #570.

The reaper existed (``TmuxSessionManager.cleanup_orphaned``) but was never
called, and had two structural gaps that made it unsafe to simply wire up:

1. it killed a window purely on ``@thread_id`` membership, **without checking
   whether Claude was still running there** — so wiring it with the
   ``active_thread_ids=set()`` that ``bot.py`` passes at startup would have
   killed every live session, and
2. it only ever looked at its own ``session_name``, while real windows live in
   per-repo sessions resolved by ``resolve_tmux_manager`` (#427).
"""

from __future__ import annotations

from unittest.mock import MagicMock, patch

import pytest

from c_lord.tmux import TmuxSessionManager


class FakeTmux:
    """Dispatches ``_run`` by tmux subcommand instead of a rigid call sequence.

    The sequence-based ``side_effect`` used by the older reaper tests breaks
    whenever a probe is added, which is exactly the change this issue makes.
    """

    def __init__(
        self,
        *,
        sessions: list[str] | None = None,
        windows: dict[str, list[str]] | None = None,
        thread_ids: dict[str, str] | None = None,
        pane_commands: dict[str, str] | None = None,
        pane_paths: dict[str, str] | None = None,
    ) -> None:
        self.sessions = sessions if sessions is not None else ["clord"]
        self.windows = windows or {}
        self.thread_ids = thread_ids or {}
        self.pane_commands = pane_commands or {}
        # #677: the reaper must not use the pane's cwd as a kill signal, so the
        # fixtures need to be able to put a *hand-made* window inside a c-lord
        # session dir (``tachikoma1:factorio3``) and a *c-lord* window outside
        # one (every orphan on the production host, after resurrect restored it
        # into tmux's default dir).
        self.pane_paths = pane_paths or {}
        self.killed: list[str] = []
        # #649: tmux targets are ``window_id``s now. Test data stays keyed by the
        # readable ``session:name``; this gives every window a stable id and
        # resolves either form back to that key.
        self._ids: dict[str, str] = {}
        n = 0
        for session, names in self.windows.items():
            for name in names:
                n += 1
                self._ids[f"@{n}"] = f"{session}:{name}"

    def _key(self, target: str) -> str:
        """``@3`` or ``session:name`` → the ``session:name`` key of the test data."""
        return self._ids.get(target, target)

    def __call__(self, argv: list[str], **kwargs: object) -> MagicMock:
        if "list-sessions" in argv:
            return MagicMock(returncode=0, stdout="\n".join(self.sessions) + "\n")

        if "list-windows" in argv:
            session = argv[argv.index("-t") + 1]
            fmt = argv[argv.index("-F") + 1] if "-F" in argv else "#{window_name}"
            body = ""
            for name in self.windows.get(session, []):
                key = f"{session}:{name}"
                wid = next((i for i, k in self._ids.items() if k == key), "")
                row = fmt
                for token, value in (
                    ("#{window_id}", wid),
                    ("#{window_name}", name),
                    ("#{@thread_id}", self.thread_ids.get(key, "")),
                    ("#{pane_current_path}", self.pane_paths.get(key, f"/work/{name}")),
                ):
                    row = row.replace(token, value)
                body += row + "\n"
            return MagicMock(returncode=0, stdout=body)

        if "show-option" in argv:
            tid = self.thread_ids.get(self._key(argv[argv.index("-t") + 1]))
            if tid is None:
                return MagicMock(returncode=1, stdout="")
            return MagicMock(returncode=0, stdout=f"{tid}\n")

        if "list-panes" in argv:
            key = self._key(argv[argv.index("-t") + 1])
            return MagicMock(returncode=0, stdout=self.pane_commands.get(key, "zsh") + "\n")

        if "kill-window" in argv:
            self.killed.append(self._key(argv[argv.index("-t") + 1]))
            return MagicMock(returncode=0, stdout="")

        return MagicMock(returncode=0, stdout="")


def _manager(session_name: str = "clord") -> TmuxSessionManager:
    mgr = TmuxSessionManager(session_name=session_name, mapping_path="")
    mgr._available = True
    return mgr


class TestReaperNeverKillsLiveClaude:
    def test_window_running_claude_survives_even_when_not_active(self) -> None:
        """A pane running Claude must never be reaped.

        ``bot.py`` passes ``active_thread_ids=set()`` at startup, so membership
        alone cannot be the guard — the pane's foreground command is what tells
        a live session apart from a leftover shell.
        """
        fake = FakeTmux(
            windows={"clord": ["w1"]},
            thread_ids={"clord:w1": "111"},
            pane_commands={"clord:w1": "claude"},
        )
        mgr = _manager()

        with patch("c_lord.tmux._run", side_effect=fake):
            killed = mgr.cleanup_orphaned(active_thread_ids=set())

        assert killed == 0
        assert fake.killed == []

    def test_dead_window_is_reaped(self) -> None:
        fake = FakeTmux(
            windows={"clord": ["w1"]},
            thread_ids={"clord:w1": "111"},
            pane_commands={"clord:w1": "zsh"},
        )
        mgr = _manager()

        with patch("c_lord.tmux._run", side_effect=fake):
            killed = mgr.cleanup_orphaned(active_thread_ids=set())

        assert killed == 1
        assert fake.killed == ["clord:w1"]

    def test_active_thread_is_kept_even_when_pane_is_dead(self) -> None:
        """``active_thread_ids`` still protects a thread the bot is using."""
        fake = FakeTmux(
            windows={"clord": ["w1"]},
            thread_ids={"clord:w1": "111"},
            pane_commands={"clord:w1": "zsh"},
        )
        mgr = _manager()

        with patch("c_lord.tmux._run", side_effect=fake):
            killed = mgr.cleanup_orphaned(active_thread_ids={111})

        assert killed == 0

    def test_hand_named_window_without_thread_id_is_never_touched(self) -> None:
        """Manually created windows (``factorio-server-1`` etc.) carry no
        ``@thread_id`` **and** no c-lord-generated name, so they stay invisible
        to the reaper even after #677 taught it to reap untagged windows."""
        fake = FakeTmux(
            windows={"clord": ["factorio-server-1"]},
            thread_ids={},
            pane_commands={"clord:factorio-server-1": "zsh"},
        )
        mgr = _manager()

        with patch("c_lord.tmux._run", side_effect=fake):
            killed = mgr.cleanup_orphaned(active_thread_ids=set())

        assert killed == 0
        assert fake.killed == []


class TestReaperCoversEverySession:
    def test_reaps_windows_in_per_repo_sessions(self) -> None:
        """#427: real windows live in per-repo sessions, not the default one.

        A reaper bound to ``SESSION_NAME`` alone covers zero of them.
        """
        from c_lord.tmux import cleanup_orphaned_all_sessions

        fake = FakeTmux(
            sessions=["clord", "c-lord", "project_30_ehon-ya"],
            windows={
                "clord": [],
                "c-lord": ["w1", "w2"],
                "project_30_ehon-ya": ["w1"],
            },
            thread_ids={
                "c-lord:w1": "111",
                "c-lord:w2": "222",
                "project_30_ehon-ya:w1": "333",
            },
            pane_commands={
                "c-lord:w1": "zsh",
                "c-lord:w2": "claude",
                "project_30_ehon-ya:w1": "zsh",
            },
        )

        with patch("c_lord.tmux._run", side_effect=fake):
            killed = cleanup_orphaned_all_sessions(active_thread_ids=set())

        assert killed == 2
        assert sorted(fake.killed) == ["c-lord:w1", "project_30_ehon-ya:w1"]

    def test_returns_zero_when_tmux_unavailable(self) -> None:
        from c_lord.tmux import cleanup_orphaned_all_sessions

        with patch("c_lord.tmux._tmux_available", return_value=False):
            assert cleanup_orphaned_all_sessions(active_thread_ids=set()) == 0


class TestBotWiring:
    """#570: the reaper existed but ``grep`` found zero call sites.

    These tests pin the call site so it cannot silently rot back into dead code.
    """

    def test_bot_reaper_uses_all_sessions(self) -> None:
        """The bot must sweep every tmux session, not just ``bot.tmux_manager``'s.

        ``bot.tmux_manager`` points at the default ``clord`` session, which on a
        real host holds none of the windows (#427).
        """
        import asyncio

        from c_lord.bot import ClaudeDiscordBot

        bot = ClaudeDiscordBot(channel_id=123)

        with patch("c_lord.tmux.cleanup_orphaned_all_sessions", return_value=7) as mock_reap:
            asyncio.run(bot._cleanup_orphaned_tmux_sessions())

        assert mock_reap.called, "the bot must reap across all tmux sessions"

    def test_bot_reaper_protects_threads_with_an_in_flight_turn(self) -> None:
        """Threads currently running a turn are passed as ``active_thread_ids``.

        The pane check is the primary guard, but a turn that is mid-startup can
        briefly show a shell rather than ``claude`` in the pane, so the set of
        in-flight threads is a necessary second belt.
        """
        import asyncio

        from c_lord.bot import ClaudeDiscordBot

        bot = ClaudeDiscordBot(channel_id=123)
        cog = MagicMock()
        cog._active_tasks = {111: object(), 222: object()}
        bot.get_cog = MagicMock(return_value=cog)  # type: ignore[method-assign]

        with patch("c_lord.tmux.cleanup_orphaned_all_sessions", return_value=0) as mock_reap:
            asyncio.run(bot._cleanup_orphaned_tmux_sessions())

        assert mock_reap.call_args is not None
        args, kwargs = mock_reap.call_args
        passed = args[0] if args else kwargs["active_thread_ids"]
        assert {111, 222} <= set(passed)

    def test_on_ready_schedules_the_reaper(self) -> None:
        """``on_ready`` must actually start the sweep — the #570 root cause was
        that it never did."""
        import asyncio
        import inspect

        from c_lord.bot import ClaudeDiscordBot

        src = inspect.getsource(ClaudeDiscordBot.on_ready)
        assert "_cleanup_orphaned_tmux_sessions" in src, (
            "on_ready does not start the tmux reaper — it is dead code again"
        )
        assert asyncio  # keep the import meaningful for linters


class TestReaperReclaimsUntaggedClordWindows:
    """#677: a window that *lost* its ``@thread_id`` must still be reclaimable.

    ``@thread_id`` is a tmux **window option**, and tmux-resurrect's save format
    stores no window options at all — so every host reboot returns c-lord's
    windows with their names intact and their tag gone. Requiring the tag put
    those windows permanently outside the reaper: 15 had piled up by 2026-09-02
    and 18 were back by 2026-09-04.

    The name is what survives, and ``w{N}`` / ``work{N}`` is a name only
    :meth:`TmuxSessionManager._next_window_name` hands out.
    """

    def test_untagged_window_with_clord_name_is_reaped(self) -> None:
        """The production case: ``welovefactorio:work1``, tag gone, bare zsh."""
        fake = FakeTmux(
            sessions=["welovefactorio"],
            windows={"welovefactorio": ["work1"]},
            thread_ids={},
            pane_commands={"welovefactorio:work1": "zsh"},
        )
        mgr = _manager("welovefactorio")

        with patch("c_lord.tmux._run", side_effect=fake):
            killed = mgr.cleanup_orphaned(active_thread_ids=set())

        assert killed == 1
        assert fake.killed == ["welovefactorio:work1"]

    def test_untagged_short_prefix_window_is_reaped(self) -> None:
        """Both naming generations are c-lord's: ``w{N}`` (#356) and ``work{N}``."""
        fake = FakeTmux(
            sessions=["claude_base"],
            windows={"claude_base": ["w6", "w8"]},
            thread_ids={},
            pane_commands={"claude_base:w6": "zsh", "claude_base:w8": "-bash"},
        )
        mgr = _manager("claude_base")

        with patch("c_lord.tmux._run", side_effect=fake):
            killed = mgr.cleanup_orphaned(active_thread_ids=set())

        assert killed == 2
        assert sorted(fake.killed) == ["claude_base:w6", "claude_base:w8"]

    def test_session_shell_window_is_not_reaped(self) -> None:
        """A session's own initial window (``zsh``) is not a c-lord work window."""
        fake = FakeTmux(
            sessions=["games"],
            windows={"games": ["zsh"]},
            thread_ids={},
            pane_commands={"games:zsh": "zsh"},
        )
        mgr = _manager("games")

        with patch("c_lord.tmux._run", side_effect=fake):
            killed = mgr.cleanup_orphaned(active_thread_ids=set())

        assert killed == 0
        assert fake.killed == []

    def test_untagged_window_running_a_program_is_not_reaped(self) -> None:
        """AC3-adjacent: only an *idle shell* is a corpse.

        ``not claude`` is too weak a guard for the untagged branch — a human's
        ``work3`` running a dev server would pass it. Every pane must be sitting
        at a plain shell.
        """
        fake = FakeTmux(
            sessions=["games"],
            windows={"games": ["w4", "w5"]},
            thread_ids={},
            pane_commands={"games:w4": "node", "games:w5": "zsh\nnode"},
        )
        mgr = _manager("games")

        with patch("c_lord.tmux._run", side_effect=fake):
            killed = mgr.cleanup_orphaned(active_thread_ids=set())

        assert killed == 0
        assert fake.killed == []

    def test_untagged_window_running_claude_is_not_reaped(self) -> None:
        """AC3: a live Claude is untouchable, tagged or not.

        ``i633rig:rig`` on the production host is exactly this — a Claude a
        human started by hand, with no ``@thread_id``.
        """
        fake = FakeTmux(
            sessions=["c-lord"],
            windows={"c-lord": ["w7"]},
            thread_ids={},
            pane_commands={"c-lord:w7": "claude"},
        )
        mgr = _manager("c-lord")

        with patch("c_lord.tmux._run", side_effect=fake):
            killed = mgr.cleanup_orphaned(active_thread_ids=set())

        assert killed == 0
        assert fake.killed == []


class TestReaperNeverKillsHandMadeWindows:
    """AC2 — the three windows this fix must not be able to touch.

    ``tachikoma1``'s ``factorio2-discord`` / ``factorio3`` / ``factorio3-discord``
    are hand-made Factorio ops windows whose panes happen to sit **inside a
    c-lord session directory**. They are the reason the cwd-based candidate (a)
    was rejected: on the production host it matched *zero* of the 18 orphans
    (resurrect had dropped them into tmux's default dir) and *all three* of
    these.
    """

    HAND_MADE = ["factorio2-discord", "factorio3", "factorio3-discord"]
    SESSION_DIR = "/home/yousan/c-lord-sessions/1506819883797712998/1507245403215499304"

    def test_hand_made_windows_in_a_session_dir_survive(self) -> None:
        fake = FakeTmux(
            sessions=["tachikoma1"],
            windows={"tachikoma1": self.HAND_MADE},
            thread_ids={},
            pane_commands={f"tachikoma1:{n}": "zsh" for n in self.HAND_MADE},
            pane_paths={f"tachikoma1:{n}": self.SESSION_DIR for n in self.HAND_MADE},
        )
        mgr = _manager("tachikoma1")

        with patch("c_lord.tmux._run", side_effect=fake):
            killed = mgr.cleanup_orphaned(active_thread_ids=set())

        assert killed == 0
        assert fake.killed == []

    def test_reaping_is_decided_by_name_not_by_cwd(self) -> None:
        """The discriminator must be the window name, in both directions.

        Same session, same bare-zsh panes, same c-lord session dir: only the
        ``w{N}``-named window goes.
        """
        fake = FakeTmux(
            sessions=["tachikoma1"],
            windows={"tachikoma1": ["factorio3", "w9"]},
            thread_ids={},
            pane_commands={"tachikoma1:factorio3": "zsh", "tachikoma1:w9": "zsh"},
            pane_paths={
                "tachikoma1:factorio3": self.SESSION_DIR,
                "tachikoma1:w9": self.SESSION_DIR,
            },
        )
        mgr = _manager("tachikoma1")

        with patch("c_lord.tmux._run", side_effect=fake):
            killed = mgr.cleanup_orphaned(active_thread_ids=set())

        assert killed == 1
        assert fake.killed == ["tachikoma1:w9"]

    def test_untagged_orphan_outside_any_session_dir_is_still_reaped(self) -> None:
        """The mirror image: the real orphans' cwd is tmux's default dir.

        A cwd-based rule would have spared every one of them.
        """
        fake = FakeTmux(
            sessions=["c-lord-parallel-3"],
            windows={"c-lord-parallel-3": ["work5"]},
            thread_ids={},
            pane_commands={"c-lord-parallel-3:work5": "zsh"},
            pane_paths={"c-lord-parallel-3:work5": "/home/yousan/c-lord"},
        )
        mgr = _manager("c-lord-parallel-3")

        with patch("c_lord.tmux._run", side_effect=fake):
            killed = mgr.cleanup_orphaned(active_thread_ids=set())

        assert killed == 1
        assert fake.killed == ["c-lord-parallel-3:work5"]


class TestReaperLogsWhatItKilled:
    """AC5 — a count alone cannot be audited after the fact."""

    def test_kill_is_logged_with_session_window_and_reason(
        self, caplog: pytest.LogCaptureFixture
    ) -> None:
        fake = FakeTmux(
            sessions=["welovefactorio"],
            windows={"welovefactorio": ["work1"]},
            thread_ids={},
            pane_commands={"welovefactorio:work1": "zsh"},
            pane_paths={"welovefactorio:work1": "/home/yousan/c-lord"},
        )
        mgr = _manager("welovefactorio")

        with (
            caplog.at_level("INFO", logger="c_lord.tmux"),
            patch("c_lord.tmux._run", side_effect=fake),
        ):
            mgr.cleanup_orphaned(active_thread_ids=set())

        line = "\n".join(r.getMessage() for r in caplog.records)
        assert "welovefactorio" in line
        assert "work1" in line
        assert "/home/yousan/c-lord" in line
        assert "no @thread_id" in line

    def test_tagged_kill_is_logged_with_session_window_and_reason(
        self, caplog: pytest.LogCaptureFixture
    ) -> None:
        fake = FakeTmux(
            sessions=["c-lord"],
            windows={"c-lord": ["w1"]},
            thread_ids={"c-lord:w1": "111"},
            pane_commands={"c-lord:w1": "zsh"},
            pane_paths={"c-lord:w1": "/work/w1"},
        )
        mgr = _manager("c-lord")

        with (
            caplog.at_level("INFO", logger="c_lord.tmux"),
            patch("c_lord.tmux._run", side_effect=fake),
        ):
            mgr.cleanup_orphaned(active_thread_ids=set())

        line = "\n".join(r.getMessage() for r in caplog.records)
        assert "c-lord" in line
        assert "w1" in line
        assert "111" in line
