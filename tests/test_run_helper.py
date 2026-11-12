# <<REDACTED 10 lines>>
from c_lord.claude.types import (
    # <<REDACTED 3 lines>>
from c_lord.cogs._run_helper import (
    # <<REDACTED 3 lines>>
from c_lord.concurrency import SessionRegistry
# <<REDACTED 2 lines>>
class TestPersistedContextWindow:
    """#370: the window total is persisted per-model in the settings KV store.

    tier is stable per (host, account, model), so a value learned once survives
    bot restarts and other sessions on the same model — the /context probe
    (which renders into the human-visible tmux pane) then fires at most once per
    model per host, instead of once per session / bot-restart.
    """

    def _config(self, *, probe_total: int | None, settings_repo: object | None):
        cfg = MagicMock()
        cfg.runner.model = "opus"
        cfg.runner.probe_context_window = AsyncMock(return_value=probe_total)
        cfg.settings_repo = settings_repo
        return cfg

    def test_disk_hit_skips_probe(self) -> None:
        from c_lord.cogs import _run_helper

        _run_helper._context_window_cache.clear()
        settings = MagicMock()
        settings.get = AsyncMock(return_value="1000000")
        settings.set = AsyncMock()
        cfg = self._config(probe_total=1_000_000, settings_repo=settings)

        total = asyncio.run(
            _run_helper._resolve_context_window(cfg, "sess-disk", "claude-opus-4-8", 50_000)
        )

        assert total == 1_000_000
        settings.get.assert_awaited_once_with("context_window:claude-opus-4-8")
        cfg.runner.probe_context_window.assert_not_awaited()

    def test_disk_miss_probes_then_persists(self) -> None:
        from c_lord.cogs import _run_helper

        _run_helper._context_window_cache.clear()
        settings = MagicMock()
        settings.get = AsyncMock(return_value=None)
        settings.set = AsyncMock()
        cfg = self._config(probe_total=1_000_000, settings_repo=settings)

        total = asyncio.run(
            _run_helper._resolve_context_window(cfg, "sess-miss", "claude-opus-4-8", 50_000)
        )

        assert total == 1_000_000
        cfg.runner.probe_context_window.assert_awaited_once()
        settings.set.assert_awaited_once_with("context_window:claude-opus-4-8", "1000000")

    def test_probe_failure_is_not_persisted(self) -> None:
        from c_lord.cogs import _run_helper

        _run_helper._context_window_cache.clear()
        settings = MagicMock()
        settings.get = AsyncMock(return_value=None)
        settings.set = AsyncMock()
        cfg = self._config(probe_total=None, settings_repo=settings)

        total = asyncio.run(
            _run_helper._resolve_context_window(cfg, "sess-failpersist", "claude-opus-4-8", 20_000)
        )

        # Probe failed → fallback used, and nothing is written to disk (a
        # transient failure must not lock a wrong window in for every future
        # session — mirrors the in-memory "do not cache on failure" rule).
        assert total == 200_000
        settings.set.assert_not_awaited()


class TestPostContextUsage:
    """_post_context_usage posts the context line and caches the denominator."""

    def _config(
        self,
        *,
        working_dir: str | None = "/tmp/x",
        probe_total: int | None = 1_000_000,
        settings_repo: object | None = None,
    ):
        cfg = MagicMock()
        cfg.thread.id = 12345
        cfg.thread.send = AsyncMock()
        cfg.runner.working_dir = working_dir
        cfg.runner.model = "opus"
        cfg.runner.probe_context_window = AsyncMock(return_value=probe_total)
        # Default to None so existing tests exercise the no-persistence path; a
        # bare MagicMock would make ``await settings_repo.get(...)`` blow up.
        cfg.settings_repo = settings_repo
        return cfg

    def _patch_usage(self, monkeypatch, used: int | None):
        from pathlib import Path

        from c_lord.claude.context_usage import ContextUsage
        from c_lord.cogs import _run_helper

        _run_helper._context_window_cache.clear()
        usage = None if used is None else ContextUsage(input_tokens=used)
        monkeypatch.setattr(_run_helper, "read_latest_usage", lambda _p: usage)
        monkeypatch.setattr(_run_helper, "latest_session_jsonl", lambda _d: Path("/tmp/fake.jsonl"))
        return _run_helper

    def test_posts_line_with_probed_total(self, monkeypatch) -> None:
        rh = self._patch_usage(monkeypatch, used=60_000)
        cfg = self._config(probe_total=1_000_000)
        asyncio.run(rh._post_context_usage(cfg, "sess-1"))
        cfg.thread.send.assert_awaited_once()
        msg = cfg.thread.send.await_args.args[0]
        assert "6%" in msg and "1.0M" in msg

    def test_denominator_probed_once_then_cached(self, monkeypatch) -> None:
        rh = self._patch_usage(monkeypatch, used=10_000)
        cfg = self._config(probe_total=1_000_000)
        asyncio.run(rh._post_context_usage(cfg, "sess-2"))
        asyncio.run(rh._post_context_usage(cfg, "sess-2"))
        cfg.runner.probe_context_window.assert_awaited_once()

    def test_reprobes_when_model_changes(self, monkeypatch) -> None:
        from pathlib import Path

        from c_lord.claude.context_usage import ContextUsage
        from c_lord.cogs import _run_helper

        _run_helper._context_window_cache.clear()
        models = iter(["claude-opus-4-7", "claude-sonnet-4-6"])
        monkeypatch.setattr(
            _run_helper,
            "read_latest_usage",
            lambda _p: ContextUsage(input_tokens=10_000, model=next(models)),
        )
        monkeypatch.setattr(_run_helper, "latest_session_jsonl", lambda _d: Path("/tmp/fake.jsonl"))
        cfg = self._config(probe_total=1_000_000)
        asyncio.run(_run_helper._post_context_usage(cfg, "sess-m"))
        asyncio.run(_run_helper._post_context_usage(cfg, "sess-m"))
        # Model changed between turns → denominator must be re-learned.
        assert cfg.runner.probe_context_window.await_count == 2

    def test_falls_back_to_model_default_when_probe_fails(self, monkeypatch) -> None:
        rh = self._patch_usage(monkeypatch, used=20_000)
        cfg = self._config(probe_total=None)  # probe returns None → fallback 200k
        asyncio.run(rh._post_context_usage(cfg, "sess-3"))
        msg = cfg.thread.send.await_args.args[0]
        assert "10%" in msg  # 20k / 200k
        assert "200k" in msg

    def test_promotes_fallback_when_used_exceeds_default(self, monkeypatch) -> None:
        """#292: probe fails on a 1M-tier session (used > 200k default).

        Regression for the Factorio case: the fallback denominator must be
        promoted to a tier that contains ``used`` (1M), so the posted line shows
        the real ~31% rather than a false "100% full / auto-compact" warning.
        """
        rh = self._patch_usage(monkeypatch, used=313_779)
        cfg = self._config(probe_total=None)  # /context scrape failed → fallback
        asyncio.run(rh._post_context_usage(cfg, "sess-1m-fallback"))
        msg = cfg.thread.send.await_args.args[0]
        assert "1.0M" in msg
        assert "31%" in msg
        assert "⚠" not in msg
        assert "compact" not in msg.lower()

    def test_does_not_cache_when_probe_fails(self, monkeypatch) -> None:
        """A transient probe failure must NOT lock the fallback into the cache.

        Regression: caching the fallback caused a 1M-context user to be stuck
        on the 200k default for the rest of the session (showing "100% full"
        when actually at 34%).  The next turn must re-probe.
        """
        from pathlib import Path

        from c_lord.claude.context_usage import ContextUsage
        from c_lord.cogs import _run_helper

        _run_helper._context_window_cache.clear()
        monkeypatch.setattr(
            _run_helper,
            "read_latest_usage",
            lambda _p: ContextUsage(input_tokens=10_000),
        )
        monkeypatch.setattr(_run_helper, "latest_session_jsonl", lambda _d: Path("/tmp/fake.jsonl"))

        # First turn: probe fails → fallback used → must NOT cache.
        cfg = self._config(probe_total=None)
        asyncio.run(_run_helper._post_context_usage(cfg, "sess-flaky"))
        assert cfg.runner.probe_context_window.await_count == 1

        # Second turn: probe must be called again (cache was not poisoned).
        cfg.runner.probe_context_window.return_value = 1_000_000
        asyncio.run(_run_helper._post_context_usage(cfg, "sess-flaky"))
        assert cfg.runner.probe_context_window.await_count == 2
        # And this turn's posted line must use the (now-successful) 1M total.
        msg = cfg.thread.send.await_args.args[0]
        assert "1.0M" in msg

    def test_skips_when_no_working_dir(self, monkeypatch) -> None:
        rh = self._patch_usage(monkeypatch, used=60_000)
        cfg = self._config(working_dir=None)
        asyncio.run(rh._post_context_usage(cfg, "sess-4"))
        cfg.thread.send.assert_not_called()

    def test_sends_new_message_ignoring_reply_tracker(self, monkeypatch) -> None:
        """#455: footer is always a new message, never appended to the reply.

        Even when reply_tracker has recorded the assistant reply message, the
        footer must be sent as a standalone message so it appears AFTER
        progress.txt and before the 🟡 mention.
        """
        from pathlib import Path
        from unittest.mock import MagicMock

        from c_lord.claude.context_usage import ContextUsage
        from c_lord.cogs import _run_helper
        from c_lord.skills import reply_tracker

        _run_helper._context_window_cache.clear()
        reply_tracker.reset_tracker()

        # Even if the reply message is tracked, footer must NOT edit it.
        last_msg = MagicMock()
        last_msg.content = "Claude's answer"
        last_msg.edit = AsyncMock()
        reply_tracker.record_reply_message(12345, last_msg)

        monkeypatch.setattr(
            _run_helper,
            "read_latest_usage",
            lambda _p: ContextUsage(input_tokens=60_000),
        )
        monkeypatch.setattr(_run_helper, "latest_session_jsonl", lambda _d: Path("/tmp/fake.jsonl"))

        cfg = self._config(probe_total=1_000_000)
        asyncio.run(_run_helper._post_context_usage(cfg, "sess-new-msg"))

        # Must send a new message, must NOT edit the reply.
        cfg.thread.send.assert_awaited_once()
        last_msg.edit.assert_not_called()
        sent = cfg.thread.send.await_args.args[0]
        assert "6%" in sent
        assert "1.0M" in sent

    def test_send_suppresses_embeds_by_default(self, monkeypatch) -> None:
        """#455/#372: new-message footer must suppress URL OGP embeds by default."""
        from pathlib import Path

        from c_lord.claude.context_usage import ContextUsage
        from c_lord.cogs import _run_helper

        monkeypatch.delenv("CLORD_SHOW_URL_EMBEDS", raising=False)
        _run_helper._context_window_cache.clear()

        monkeypatch.setattr(
            _run_helper, "read_latest_usage", lambda _p: ContextUsage(input_tokens=60_000)
        )
        monkeypatch.setattr(_run_helper, "latest_session_jsonl", lambda _d: Path("/tmp/fake.jsonl"))

        cfg = self._config(probe_total=1_000_000)
        asyncio.run(_run_helper._post_context_usage(cfg, "sess-suppress"))

        cfg.thread.send.assert_awaited_once()
        assert cfg.thread.send.await_args.kwargs.get("suppress_embeds") is True

    def test_send_respects_show_url_embeds_optin(self, monkeypatch) -> None:
        """#455/#372: CLORD_SHOW_URL_EMBEDS=true keeps embeds enabled on send."""
        from pathlib import Path

        from c_lord.claude.context_usage import ContextUsage
        from c_lord.cogs import _run_helper

        monkeypatch.setenv("CLORD_SHOW_URL_EMBEDS", "true")
        _run_helper._context_window_cache.clear()

        monkeypatch.setattr(
            _run_helper, "read_latest_usage", lambda _p: ContextUsage(input_tokens=60_000)
        )
        monkeypatch.setattr(_run_helper, "latest_session_jsonl", lambda _d: Path("/tmp/fake.jsonl"))

        cfg = self._config(probe_total=1_000_000)
        asyncio.run(_run_helper._post_context_usage(cfg, "sess-optin"))

        cfg.thread.send.assert_awaited_once()
        assert cfg.thread.send.await_args.kwargs.get("suppress_embeds") is False

    def test_always_sends_new_message(self, monkeypatch) -> None:
        """#455: footer always sent as new message regardless of reply tracker state."""
        from pathlib import Path

        from c_lord.claude.context_usage import ContextUsage
        from c_lord.cogs import _run_helper
        from c_lord.skills import reply_tracker

        _run_helper._context_window_cache.clear()
        reply_tracker.reset_tracker()
        monkeypatch.setattr(
            _run_helper,
            "read_latest_usage",
            lambda _p: ContextUsage(input_tokens=60_000),
        )
        monkeypatch.setattr(_run_helper, "latest_session_jsonl", lambda _d: Path("/tmp/fake.jsonl"))

        cfg = self._config(probe_total=1_000_000)
        asyncio.run(_run_helper._post_context_usage(cfg, "sess-no-track"))
        cfg.thread.send.assert_awaited_once()

    def test_skips_when_no_usage_in_transcript(self, monkeypatch) -> None:
        rh = self._patch_usage(monkeypatch, used=None)
        cfg = self._config()
        asyncio.run(rh._post_context_usage(cfg, "sess-5"))
        cfg.thread.send.assert_not_called()


# <<REDACTED 174 lines>>
class TestNoSkillFallbackNotice:
    """#712: the skill-push path is gone, so the "Claude never called
    discord-reply" notice (#67) must never appear — under the JSONL mirror the
    tracker it consulted was always empty, so it would fire on every turn."""
# <<REDACTED 4 lines>>
        t.id = 67670001
        # <<REDACTED 5 lines>>
        return MagicMock()
# <<REDACTED 7 lines>>
    def _make_async_gen(self, events):
        # <<REDACTED 6 lines>>
    @pytest.mark.asyncio
    @pytest.mark.parametrize("legacy_env", [{}, {"CLORD_BRIDGE_MODE": "skill"}])
    async def test_no_fallback_notice_on_a_normal_turn(
        self,
        thread: MagicMock,
        runner: MagicMock,
        repo: MagicMock,
        monkeypatch: pytest.MonkeyPatch,
        legacy_env: dict[str, str],
    ) -> None:
        for key, value in legacy_env.items():
            monkeypatch.setenv(key, value)

        # <<REDACTED 5 lines>>
                session_id="sess-1",
            # <<REDACTED 4 lines>>
        await run_claude_in_thread(thread, runner, repo, "hello", None)

        for call in thread.send.call_args_list:
            for arg in call.args:
                if isinstance(arg, str):
                    assert "discord-reply" not in arg
            content = call.kwargs.get("content")
            if isinstance(content, str):
                assert "discord-reply" not in content


class TestRecoverMissedPaneAsk:
    """Issue #219: the run loop can finalize a turn just before an
    AskUserQuestion menu renders, leaving Claude blocked on a TUI menu that was
    never bridged to Discord buttons (the user sees no choices).  Before posting
    the #67 'no discord-reply' fallback, run_claude_with_config must re-check the
    pane and bridge an open menu instead of posting the misleading notice."""
# <<REDACTED 4 lines>>
        t.id = 219219219
        # <<REDACTED 9 lines>>
    def _make_async_gen(self, events):
        # <<REDACTED 6 lines>>
    @pytest.mark.asyncio
    async def test_open_menu_is_bridged_and_no_fallback(
        self, thread: MagicMock, repo: MagicMock
    ) -> None:
        from unittest.mock import patch

        from c_lord.claude.tmux_runner import TmuxClaudeRunner
        from c_lord.claude.types import AskOption, AskQuestion
        from c_lord.skills.reply_tracker import reset_tracker

        reset_tracker()

        runner = MagicMock(spec=TmuxClaudeRunner)
        runner.stopped = False  # a live (non-pre-empted) run still re-bridges post-turn (#315)
        runner.run = self._make_async_gen(
            [
                # <<REDACTED 1 lines>>
                StreamEvent(message_type=MessageType.RESULT, is_complete=True, session_id="sess-1"),
            ]
        )
        pending = AskQuestion(
            question="Which environment?",
            header="Deploy",
            options=[AskOption(label="Production", description="本番")],
        )
        runner.peek_pending_ask = AsyncMock(return_value=pending)

        with patch("c_lord.cogs._run_helper.bridge_pane_ask", new=AsyncMock()) as bridge:
            await run_claude_in_thread(thread, runner, repo, "hello", None)

        bridge.assert_awaited_once()
        # The misleading "no discord-reply" fallback must NOT be posted.
        for call in thread.send.call_args_list:
            for arg in call.args:
                if isinstance(arg, str):
                    assert "discord-reply" not in arg
            content = call.kwargs.get("content")
            if isinstance(content, str):
                assert "discord-reply" not in content

    @pytest.mark.asyncio
    async def test_open_menu_bridged_with_legacy_env_set(
        self, thread: MagicMock, repo: MagicMock, monkeypatch
    ) -> None:
        """#222: post-turn menu recovery must run even with a legacy env set.

        The menu is read from the pane, independent of delivery, so gating it
        behind the (now removed) skill-mode check left production never
        recovering a post-turn menu — the user saw no choices.
        """
        from unittest.mock import patch

        from c_lord.claude.tmux_runner import TmuxClaudeRunner
        from c_lord.claude.types import AskOption, AskQuestion
        from c_lord.skills.reply_tracker import reset_tracker

        reset_tracker()
        monkeypatch.setenv("CLORD_BRIDGE_MODE", "skill")

        runner = MagicMock(spec=TmuxClaudeRunner)
        runner.stopped = False  # a live (non-pre-empted) run still re-bridges post-turn (#315)
        runner.run = self._make_async_gen(
            [
                # <<REDACTED 1 lines>>
                StreamEvent(message_type=MessageType.RESULT, is_complete=True, session_id="sess-1"),
            ]
        )
        pending = AskQuestion(
            question="Which environment?",
            header="Deploy",
            options=[AskOption(label="Production", description="本番")],
        )
        runner.peek_pending_ask = AsyncMock(return_value=pending)

        with patch("c_lord.cogs._run_helper.bridge_pane_ask", new=AsyncMock()) as bridge:
            await run_claude_in_thread(thread, runner, repo, "hello", None)

        bridge.assert_awaited_once()

    @pytest.mark.asyncio
    async def test_open_menu_bridged_even_after_the_answer_landed(
        self, thread: MagicMock, repo: MagicMock
    ) -> None:
        """#222: a follow-up menu rendered AFTER the answer was delivered must
        still be recovered — delivery must not gate menu recovery."""
        from unittest.mock import patch

        from c_lord.claude.tmux_runner import TmuxClaudeRunner
        from c_lord.claude.types import AskOption, AskQuestion
        from c_lord.skills.reply_tracker import record_reply_message, reset_tracker

        reset_tracker()

        runner = MagicMock(spec=TmuxClaudeRunner)
        runner.stopped = False  # a live (non-pre-empted) run still re-bridges post-turn (#315)

        async def gen_with_reply(*args, **kwargs):
            yield StreamEvent(message_type=MessageType.SYSTEM, session_id="sess-1")
            record_reply_message(thread.id, MagicMock(spec=discord.Message))
            yield StreamEvent(
                message_type=MessageType.RESULT, is_complete=True, session_id="sess-1"
            )

        runner.run = gen_with_reply
        pending = AskQuestion(
            question="Which environment?",
            header="Deploy",
            options=[AskOption(label="Production", description="本番")],
        )
        runner.peek_pending_ask = AsyncMock(return_value=pending)

        with patch("c_lord.cogs._run_helper.bridge_pane_ask", new=AsyncMock()) as bridge:
            await run_claude_in_thread(thread, runner, repo, "hello", None)

        bridge.assert_awaited_once()

    @pytest.mark.asyncio
    async def test_stopped_run_does_not_rebridge_post_turn(
        self, thread: MagicMock, repo: MagicMock
    ) -> None:
        """A pre-empted (stopped) run must NOT re-bridge its leftover menu (#315).

        When a follow-up message interrupts a turn parked on a menu, the run is
        torn down (``runner.stopped`` is True).  Re-bridging here would re-park it
        on a fresh ``timeout=None`` await, so the new turn could never start — the
        exact failure observed on staging (the ⚡ fired but no new run began).
        """
        from unittest.mock import patch

        from c_lord.claude.tmux_runner import TmuxClaudeRunner
        from c_lord.claude.types import AskOption, AskQuestion
        from c_lord.skills.reply_tracker import reset_tracker

        reset_tracker()

        runner = MagicMock(spec=TmuxClaudeRunner)
        runner.stopped = True  # deliberately pre-empted by a follow-up message
        runner.run = self._make_async_gen(
            [
                # <<REDACTED 1 lines>>
                StreamEvent(message_type=MessageType.RESULT, is_complete=True, session_id="sess-1"),
            ]
        )
        runner.peek_pending_ask = AsyncMock(
            return_value=AskQuestion(
                question="Which environment?",
                header="Deploy",
                options=[AskOption(label="Production", description="本番")],
            )
        )

        with patch("c_lord.cogs._run_helper.bridge_pane_ask", new=AsyncMock()) as bridge:
            await run_claude_in_thread(thread, runner, repo, "hello", None)

        bridge.assert_not_awaited()
        runner.peek_pending_ask.assert_not_called()


# <<REDACTED 67 lines>>
    async def test_concurrency_notice_not_injected_into_prompt(
        # <<REDACTED 2 lines>>
        """When registry is provided, the session is registered but the concurrency
        notice is NOT injected into the CLI (tmux TUI mode doesn't support
        --append-system-prompt). The user prompt must be passed unchanged.
        # <<REDACTED 13 lines>>
        # Prompt is unchanged — no concurrency notice injected.
        # <<REDACTED 3 lines>>
        # clone() is NOT called — tmux TUI mode doesn't support --append-system-prompt.
        # <<REDACTED 55 lines>>
    async def test_other_sessions_registered_but_not_injected(
        # <<REDACTED 2 lines>>
        """When other sessions exist, the registry side effect still happens
        but the concurrency notice is NOT injected into the CLI.
        """
        # <<REDACTED 14 lines>>
        # User prompt is unchanged — no concurrency notice injected.
        # <<REDACTED 3 lines>>
        # clone() is NOT called — tmux TUI mode doesn't support --append-system-prompt.
        # <<REDACTED 2 lines>>
    @pytest.mark.asyncio
    async def test_lounge_repo_is_not_read_per_turn(
        # <<REDACTED 2 lines>>
        """#766: the lounge text went nowhere, so a turn no longer reads the lounge DB.

        ``lounge_repo`` is still accepted (backward compatible) but ignored.
        """
        lounge_repo = MagicMock()
        lounge_repo.get_recent = AsyncMock(return_value=[])
        # <<REDACTED 2 lines>>
        await run_claude_in_thread(
            thread, runner, repo, "fix the bug", None, lounge_repo=lounge_repo
        )

        lounge_repo.get_recent.assert_not_called()

    @pytest.mark.asyncio
    async def test_no_concurrency_notice_built_log(
        self,
        thread: MagicMock,
        runner: MagicMock,
        repo: MagicMock,
        caplog: pytest.LogCaptureFixture,
    ) -> None:
        """#766: the log must not say a notice was "built" — nothing is built any more."""
        # <<REDACTED 3 lines>>
        with caplog.at_level("DEBUG"):
            # <<REDACTED 2 lines>>
        assert "Concurrency notice built" not in caplog.text

    # <<REDACTED 22 lines>>


class TestFooterCliVersion:
    """#693: the footer's CLI version must track the CLI's own auto-updates."""

    @staticmethod
    def _config():
        cfg = MagicMock()
        cfg.thread.id = 1
        cfg.thread.send = AsyncMock()
        cfg.runner.working_dir = "/tmp/x"
        cfg.runner.model = "opus"
        cfg.runner.probe_context_window = AsyncMock(return_value=200_000)
        cfg.settings_repo = None
        return cfg

    @staticmethod
    def _patch(monkeypatch, usage):
        from pathlib import Path

        from c_lord.cogs import _run_helper

        _run_helper._context_window_cache.clear()
        monkeypatch.setattr(_run_helper, "read_latest_usage", lambda _p: usage)
        monkeypatch.setattr(_run_helper, "latest_session_jsonl", lambda _d: Path("/tmp/f.jsonl"))
        return _run_helper

    def test_footer_uses_the_version_of_this_turn(self, monkeypatch) -> None:
        from c_lord.claude.context_usage import ContextUsage

        rh = self._patch(monkeypatch, ContextUsage(input_tokens=20_000, cli_version="2.1.260"))
        cfg = self._config()
        asyncio.run(rh._post_context_usage(cfg, "s"))
        assert "CLI 2.1.260" in cfg.thread.send.await_args.args[0]

    def test_footer_follows_an_upgrade_without_a_bot_restart(self, monkeypatch) -> None:
        """The bug: the first turn's version was cached for the process's life."""
        from c_lord.claude.context_usage import ContextUsage

        from c_lord.cogs import _run_helper

        versions = iter(["2.1.258", "2.1.260"])
        self._patch(monkeypatch, None)
        monkeypatch.setattr(
            _run_helper,
            "read_latest_usage",
            lambda _p: ContextUsage(input_tokens=20_000, cli_version=next(versions)),
        )
        cfg = self._config()
        asyncio.run(_run_helper._post_context_usage(cfg, "s"))
        first = cfg.thread.send.await_args.args[0]
        asyncio.run(_run_helper._post_context_usage(cfg, "s"))
        second = cfg.thread.send.await_args.args[0]
        assert "CLI 2.1.258" in first
        assert "CLI 2.1.260" in second, "footer kept the version it saw first"

    def test_footer_omits_the_item_when_no_version_is_recorded(self, monkeypatch) -> None:
        """AC3: never print ``None`` or a stale value."""
        from c_lord.claude.context_usage import ContextUsage

        rh = self._patch(monkeypatch, ContextUsage(input_tokens=20_000, cli_version=None))
        cfg = self._config()
        asyncio.run(rh._post_context_usage(cfg, "s"))
        assert "CLI" not in cfg.thread.send.await_args.args[0]

    def test_footer_spawns_no_subprocess(self, monkeypatch) -> None:
        """AC4: the version must not cost a ``claude --version`` per turn."""
        from c_lord.claude.context_usage import ContextUsage

        rh = self._patch(monkeypatch, ContextUsage(input_tokens=20_000, cli_version="2.1.260"))

        async def _boom(*_a, **_kw):  # pragma: no cover - must never run
            raise AssertionError("the footer spawned a subprocess")

        monkeypatch.setattr(asyncio, "create_subprocess_exec", _boom)
        cfg = self._config()
        asyncio.run(rh._post_context_usage(cfg, "s"))
        assert "CLI 2.1.260" in cfg.thread.send.await_args.args[0]


class TestFooterClordVersion:
    """#722: the footer must also say which *c-lord* build ran this turn.

    Regression for 2026-09-10: an instance stuck on an 09-02 build told its
    user a feature "does not exist" that had shipped two days earlier. Nothing
    the user or the operator could see named the build.
    """

    @staticmethod
    def _config(settings_repo: object | None = None):
        cfg = MagicMock()
        cfg.thread.id = 1
        cfg.thread.send = AsyncMock()
        cfg.runner.working_dir = "/tmp/x"
        cfg.runner.model = "opus"
        cfg.runner.effort = None
        cfg.runner.probe_context_window = AsyncMock(return_value=200_000)
        cfg.settings_repo = settings_repo
        return cfg

    @staticmethod
    def _patch(monkeypatch, version: str = "v1.4.183-bd80c47e-20260908"):
        from pathlib import Path

        from c_lord.claude.context_usage import ContextUsage
        from c_lord.cogs import _run_helper
        from c_lord.version import runtime_version

        _run_helper._context_window_cache.clear()
        monkeypatch.setattr(
            _run_helper,
            "read_latest_usage",
            lambda _p: ContextUsage(input_tokens=20_000, cli_version="2.1.263"),
        )
        monkeypatch.setattr(_run_helper, "latest_session_jsonl", lambda _d: Path("/tmp/f.jsonl"))
        monkeypatch.setattr("c_lord.version.resolve_version", lambda: version)
        runtime_version.cache_clear()
        return _run_helper

    def test_footer_names_the_running_build(self, monkeypatch) -> None:
        from c_lord.version import runtime_version

        rh = self._patch(monkeypatch)
        cfg = self._config()
        try:
            asyncio.run(rh._post_context_usage(cfg, "s"))
        finally:
            runtime_version.cache_clear()
        line = cfg.thread.send.await_args.args[0]
        assert "c-lord v1.4.183-bd80c47e-20260908" in line
        assert "CLI 2.1.263" in line

    def test_footer_omits_the_item_when_the_build_is_unknown(self, monkeypatch) -> None:
        """知らないことは黙る — never print ``c-lord unknown``."""
        from c_lord.version import runtime_version

        rh = self._patch(monkeypatch, version="unknown")
        cfg = self._config()
        try:
            asyncio.run(rh._post_context_usage(cfg, "s"))
        finally:
            runtime_version.cache_clear()
        assert "c-lord" not in cfg.thread.send.await_args.args[0]

    def test_item_can_be_switched_off(self, monkeypatch) -> None:
        """Like every other footer item: ``context_footer.clord_version=0``."""
        from c_lord.version import runtime_version

        rh = self._patch(monkeypatch)
        settings = MagicMock()

        async def get(key: str, default: str = "1") -> str:
            return "0" if key == "context_footer.clord_version" else default

        settings.get = get
        cfg = self._config(settings_repo=settings)
        try:
            asyncio.run(rh._post_context_usage(cfg, "s"))
        finally:
            runtime_version.cache_clear()
        line = cfg.thread.send.await_args.args[0]
        assert "c-lord" not in line
        assert "CLI 2.1.263" in line, "switching off one item must not drop the others"

    def test_footer_spawns_no_subprocess_per_turn(self, monkeypatch) -> None:
        """The build is pinned at boot — no ``git describe`` per turn."""
        import subprocess

        from c_lord.version import runtime_version

        rh = self._patch(monkeypatch)
        cfg = self._config()
        try:
            asyncio.run(rh._post_context_usage(cfg, "s"))

            def _boom(*_a, **_kw):  # pragma: no cover - must never run
                raise AssertionError("the footer shelled out for the version")

            monkeypatch.setattr(subprocess, "run", _boom)
            asyncio.run(rh._post_context_usage(cfg, "s"))
        finally:
            runtime_version.cache_clear()
        assert "c-lord v1.4.183-bd80c47e-20260908" in cfg.thread.send.await_args.args[0]

    @staticmethod
    def _footer_for(monkeypatch, days_old: int) -> str:
        from datetime import date, timedelta

        from c_lord.version import runtime_version

        built = (date.today() - timedelta(days=days_old)).strftime("%Y%m%d")
        rh = TestFooterClordVersion._patch(monkeypatch, version=f"v1.4.183-bd80c47e-{built}")
        cfg = TestFooterClordVersion._config()
        try:
            asyncio.run(rh._post_context_usage(cfg, "s"))
        finally:
            runtime_version.cache_clear()
        return cfg.thread.send.await_args.args[0]

    def test_stale_build_shows_its_age(self, monkeypatch) -> None:
        """#756 AC2: 7+ days old → ``c-lord v… (Nd)`` in the footer."""
        line = self._footer_for(monkeypatch, 10)
        assert "-bd80c47e-" in line
        assert line.split("c-lord ", 1)[1].split(" · ")[0].endswith(" (10d)"), line

    def test_fresh_build_shows_no_age(self, monkeypatch) -> None:
        """#756 AC2: under 7 days the item is unchanged."""
        line = self._footer_for(monkeypatch, 6)
        assert "d)" not in line.split("c-lord ", 1)[1].split(" · ")[0], line

    def test_age_is_measured_per_turn_not_at_boot(self, monkeypatch) -> None:
        """#756: the process can run for days — the age is today's, not boot's.

        Production ran from 09-15 without a restart; a build that was fresh at
        boot must start showing its age once it crosses the line.
        """
        from datetime import date, timedelta

        from c_lord.version import runtime_version

        built = date(2026, 9, 15)
        rh = self._patch(monkeypatch, version="v1.4.197-b3f06814-20260915")
        cfg = self._config()

        class _Day(date):
            now = built + timedelta(days=1)

            @classmethod
            def today(cls):  # type: ignore[override]
                return cls.now

        monkeypatch.setattr("c_lord.version.date", _Day)
        try:
            asyncio.run(rh._post_context_usage(cfg, "s"))
            first = cfg.thread.send.await_args.args[0]
            _Day.now = built + timedelta(days=8)
            asyncio.run(rh._post_context_usage(cfg, "s"))
            later = cfg.thread.send.await_args.args[0]
        finally:
            runtime_version.cache_clear()
        assert "(1d)" not in first and "d)" not in first.split("c-lord ", 1)[1].split(" · ")[0]
        assert "c-lord v1.4.197-b3f06814-20260915 (8d)" in later
