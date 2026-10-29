"""Behaviour of ClaudeChatCog paths the rest of the suite does not reach.

Each test pins what a user (or the log reader) observes on a branch that is
mostly a failure path: what is posted to the thread, which ephemeral answer a
command gives, which tmux / runner calls are (not) made. The Issue number in
each docstring is the one the code under test cites.

Nothing here touches a real tmux server — every manager is a mock.
"""

from __future__ import annotations

import asyncio
import logging
import subprocess
from datetime import datetime, timedelta, timezone
from pathlib import Path
from types import SimpleNamespace
from unittest.mock import AsyncMock, MagicMock, patch

import discord
import pytest

from c_lord.cogs.channel_repo import ChannelRepoCog
from c_lord.cogs.claude_chat import ClaudeChatCog
from c_lord.gateway_backfill import Outage, PreviousOutage, Startup
from c_lord.host_restart_notice import TurnEnding
from c_lord.session_reattach import Plan, Recovery, auto_reattach_notice, reattach_notice
from tests.test_claude_chat import _make_channel_cog_mock, _make_cog

MOD = "c_lord.cogs.claude_chat"
UTC = timezone.utc  # noqa: UP017 — datetime.UTC is 3.11+, we support 3.10


def _thread(thread_id: int = 4242, parent_id: int | None = 999) -> MagicMock:
    thread = MagicMock(spec=discord.Thread)
    thread.id = thread_id
    thread.parent_id = parent_id
    thread.name = "W1 │ topic"
    thread.send = AsyncMock(return_value=MagicMock())
    return thread


def _sent_texts(thread: MagicMock) -> list[str]:
    out: list[str] = []
    for call in thread.send.call_args_list:
        text = call.args[0] if call.args else call.kwargs.get("content")
        out.append(text or "")
    return out


def _record(**kw: object) -> MagicMock:
    base = {
        "session_id": None,
        "closed_at": None,
        "slept_at": None,
        "working_dir": None,
        "topic": None,
        "auto_topic_locked": 0,
        "state": "alive",
        "issue_ref": None,
        "origin_issue_ref": None,
        "tmux_window_id": None,
    }
    base.update(kw)
    return MagicMock(**base)


# ── module helpers ────────────────────────────────────────────────────────


class TestModuleHelpers:
    def test_span_text_shows_hours_only_when_there_are_some(self) -> None:
        from c_lord.cogs.claude_chat import _span_text

        assert _span_text(timedelta(hours=6, minutes=35, seconds=45)) == "6h35m45s"
        assert _span_text(timedelta(minutes=3, seconds=7)) == "3m07s"
        # A negative span (clock skew) never renders as a negative duration.
        assert _span_text(timedelta(seconds=-5)) == "0m00s"

    def test_missed_entry_carries_author_text_and_attachment_urls(self) -> None:
        """#745: an earlier missed message goes into the merged prompt by name and URL."""
        from c_lord.cogs.claude_chat import _missed_entry

        att = SimpleNamespace(filename="a.png", url="https://cdn/a.png")
        msg = SimpleNamespace(
            created_at=datetime(2026, 10, 1, tzinfo=UTC),
            author=SimpleNamespace(display_name=None, name="yousan"),
            content=None,
            attachments=[att],
        )
        entry = _missed_entry(msg)  # type: ignore[arg-type]
        assert entry.author == "yousan"  # falls back to .name when no display name
        assert entry.text == ""
        assert entry.attachments == ("a.png <https://cdn/a.png>",)

    def test_author_name_falls_back_to_question_mark(self) -> None:
        from c_lord.cogs.claude_chat import _author_name

        msg = SimpleNamespace(author=SimpleNamespace(display_name="", name=""))
        assert _author_name(msg) == "?"  # type: ignore[arg-type]


# ── resolvers ─────────────────────────────────────────────────────────────


class TestResolvers:
    async def test_no_channel_id_resolves_no_session_dir_manager(self) -> None:
        cog = _make_cog(channel_cog=_make_channel_cog_mock(session_dir_manager=MagicMock()))
        assert await cog._resolve_session_dir_manager(None) is None
        cog.bot.get_cog.assert_not_called()

    async def test_divergent_session_name_needs_both_ids(self) -> None:
        """#618: a thread-like object without a parent never asks the channel cog."""
        channel_cog = _make_channel_cog_mock()
        channel_cog.divergent_session_name = AsyncMock(return_value="other")
        cog = _make_cog(channel_cog=channel_cog)
        assert await cog._divergent_session_name(SimpleNamespace(id=1, parent_id=None)) is None
        channel_cog.divergent_session_name.assert_not_awaited()

    async def test_divergent_session_name_comes_from_the_channel_cog(self) -> None:
        channel_cog = _make_channel_cog_mock()
        channel_cog.divergent_session_name = AsyncMock(return_value="c-lord")
        cog = _make_cog(channel_cog=channel_cog)
        got = await cog._divergent_session_name(SimpleNamespace(id=5, parent_id=9))
        assert got == "c-lord"
        channel_cog.divergent_session_name.assert_awaited_once_with(9, 5)

    async def test_divergent_session_name_failure_is_decoration_not_an_error(self) -> None:
        """#618: the label is decoration — a lookup error means "no label"."""
        channel_cog = _make_channel_cog_mock()
        channel_cog.divergent_session_name = AsyncMock(side_effect=RuntimeError("db"))
        cog = _make_cog(channel_cog=channel_cog)
        assert await cog._divergent_session_name(SimpleNamespace(id=5, parent_id=9)) is None

    async def test_bind_thread_repo_without_channel_cog_returns_none(self) -> None:
        """#514: no ChannelRepoCog → no thread binding (the caller falls back)."""
        cog = _make_cog()  # get_cog returns None
        assert await cog._bind_thread_repo(1, 2, "https://github.com/o/r") is None


# ── naming ────────────────────────────────────────────────────────────────


class TestNamingGaps:
    async def test_auto_topic_failure_falls_back_to_the_heuristic(self) -> None:
        """#705: with CLORD_AUTO_TOPIC on, an LLM failure still yields a topic.

        No row yet → the topic is stashed (source ``heuristic``) and the rename
        still goes out. A window lookup that answers None leaves the W number off.
        """
        cog = _make_cog()
        cog._auto_topic = True
        cog._thread_lamp = False
        thread = _thread(77)
        thread.name = "untitled"
        thread.edit = AsyncMock()
        tmux = MagicMock()
        tmux.get_window_info = MagicMock(return_value=None)
        with patch("c_lord.topic.generate_topic", AsyncMock(side_effect=RuntimeError("llm down"))):
            await cog._apply_thread_naming(
                thread=thread,
                tmux_manager=tmux,
                first_message="ログインのバグを直して",
                working_dir=None,
            )
        topic, source = cog._pending_topic[77]
        assert source == "heuristic"
        assert topic
        assert 77 not in cog._pending_tmux_window_id
        new_name = thread.edit.await_args.kwargs["name"]
        assert topic in new_name
        assert not new_name.startswith("W")

    async def test_on_session_saved_is_a_noop_without_a_stash(self) -> None:
        """#856: nothing stashed → not even a DB read."""
        cog = _make_cog()
        await cog.on_session_saved(1)
        cog.repo.get.assert_not_awaited()

    async def test_on_session_saved_keeps_the_stash_when_the_row_is_missing(self) -> None:
        cog = _make_cog()
        cog._pending_topic[1] = ("t", "thread_name")
        cog.repo.get = AsyncMock(return_value=None)
        await cog.on_session_saved(1)
        assert cog._pending_topic[1] == ("t", "thread_name")

    async def test_on_session_saved_never_raises(self, caplog: pytest.LogCaptureFixture) -> None:
        cog = _make_cog()
        cog._pending_topic[1] = ("t", "thread_name")
        cog.repo.get = AsyncMock(side_effect=RuntimeError("db locked"))
        with caplog.at_level(logging.WARNING):
            await cog.on_session_saved(1)
        assert "could not persist the pending thread naming" in caplog.text


class TestGitCurrentBranch:
    def test_reads_the_branch_of_a_real_checkout(self, tmp_path: Path) -> None:
        subprocess.run(
            ["git", "init", "-q", "-b", "feature/12-x", str(tmp_path)],
            check=True,
            capture_output=True,
        )
        assert ClaudeChatCog._git_current_branch(str(tmp_path)) == "feature/12-x"

    def test_git_missing_is_none_not_an_exception(self) -> None:
        with patch("subprocess.run", side_effect=FileNotFoundError("git")):
            assert ClaudeChatCog._git_current_branch("/nowhere") is None


# ── on_message early exits ────────────────────────────────────────────────


def _human_msg(channel: object) -> MagicMock:
    msg = MagicMock(spec=discord.Message)
    msg.webhook_id = None
    msg.channel = channel
    msg.created_at = datetime.now(UTC)
    msg.author = MagicMock(bot=False, id=5)
    msg.id = 123
    return msg


class TestOnMessageEarlyExits:
    async def test_message_in_the_home_channel_starts_nothing(self) -> None:
        """Channel messages never create sessions (slash / API only)."""
        cog = _make_cog()
        channel = MagicMock(spec=discord.TextChannel)
        channel.id = cog.bot.channel_id
        cog._is_runnable_request = AsyncMock(return_value=True)
        await cog.on_message(_human_msg(channel))
        cog._is_runnable_request.assert_not_awaited()
        cog.repo.get.assert_not_awaited()

    async def test_runnable_message_outside_a_thread_is_ignored(self) -> None:
        cog = _make_cog()
        channel = MagicMock(spec=discord.TextChannel)
        channel.id = 31337
        cog._is_runnable_request = AsyncMock(return_value=True)
        cog._handle_thread_reply = AsyncMock()
        await cog.on_message(_human_msg(channel))
        cog.repo.get.assert_not_awaited()
        cog._handle_thread_reply.assert_not_awaited()


# ── #862 helpers ──────────────────────────────────────────────────────────


class TestClaimsAndFreshStart:
    def test_a_cog_whose_claims_message_raises_does_not_claim(self) -> None:
        """#862: a broken consumer cog must not swallow every message."""
        cog = _make_cog()
        msg = MagicMock()

        class _NoClaims:
            claims_message = None  # not callable → skipped

        class _Broken:
            def claims_message(self, _m: object) -> bool:
                raise RuntimeError("boom")

        cog.bot.cogs = {"self": cog, "a": _NoClaims(), "b": _Broken()}
        assert cog._claimed_by_another_cog(msg) is False

        class _Claims:
            def claims_message(self, _m: object) -> bool:
                return True

        cog.bot.cogs["c"] = _Claims()
        assert cog._claimed_by_another_cog(msg) is True

    async def test_binding_lookup_failure_means_no_fresh_start(self) -> None:
        cog = _make_cog()
        cog._resolve_session_dir_manager = AsyncMock(side_effect=RuntimeError("db"))
        assert await cog._can_start_fresh(_thread(), 999) is False

    async def test_close_leftover_window_without_tmux_kills_nothing(self) -> None:
        cog = _make_cog()
        cog._resolve_tmux_manager = AsyncMock(return_value=None)
        await cog._close_leftover_window(_thread())  # must not raise

    async def test_close_leftover_window_swallows_tmux_errors(
        self, caplog: pytest.LogCaptureFixture
    ) -> None:
        """#862: worst case is the pre-#862 risk, never a dropped message."""
        cog = _make_cog()
        tmux = MagicMock()
        tmux.kill_session = MagicMock(side_effect=RuntimeError("no server"))
        cog._resolve_tmux_manager = AsyncMock(return_value=tmux)
        with caplog.at_level(logging.WARNING):
            await cog._close_leftover_window(_thread(8))
        tmux.kill_session.assert_called_once_with(8)
        assert "could not close the leftover window" in caplog.text


class TestAutoReattach:
    async def test_checkout_vanishing_mid_reattach_hands_back_none(self) -> None:
        """#700/#538 AC8: plan said WORKDIR, the act found nothing → caller's notice.

        The one-line notice went out first, but the resume-announced mark is
        withdrawn so the next resume notice is not silenced.
        """
        cog = _make_cog()
        cog._resolve_session_dir_manager = AsyncMock(return_value=SimpleNamespace(base_dir="/b"))
        cog._reattach_thread = AsyncMock(return_value=Plan(Recovery.NONE))
        thread = _thread(5)
        plan = Plan(Recovery.WORKDIR, working_dir="/b/5")
        with patch(f"{MOD}.plan_recovery", return_value=plan):
            got = await cog._auto_reattach(thread, 999)
        assert got is None
        assert 5 not in cog._resume_announced
        assert _sent_texts(thread) == [auto_reattach_notice(plan)]

    async def test_origin_check_failure_errs_toward_speaking(self) -> None:
        """#556: a resolver hiccup must not swallow a message silently."""
        cog = _make_cog()
        cog._resolve_session_dir_manager = AsyncMock(side_effect=RuntimeError("db"))
        assert await cog._was_ever_our_thread(_thread(), 999) is True

    async def test_collect_thread_history_skips_empty_messages(self) -> None:
        cog = _make_cog()
        thread = _thread()

        def _m(text: str | None, name: str, stamp: datetime | None) -> SimpleNamespace:
            return SimpleNamespace(
                content=text, author=SimpleNamespace(display_name=name), created_at=stamp
            )

        msgs = [
            _m("  hello  ", "alice", datetime(2026, 10, 1, 9, 5)),
            _m("", "bot", None),  # embed-only card
            _m("bye", "bob", None),
        ]

        async def _history(**_kw: object):  # noqa: ANN202
            for m in msgs:
                yield m

        thread.history = _history
        out = await cog._collect_thread_history(thread)
        assert out == [("alice", "2026-10-01 09:05", "hello"), ("bob", "", "bye")]


# ── /clord ────────────────────────────────────────────────────────────────


class TestClordImplGaps:
    async def _call(self, cog: ClaudeChatCog, channel: object, **kw: object) -> AsyncMock:
        respond = AsyncMock()
        await cog._clord_impl(
            channel=channel,
            channel_id_fallback=kw.pop("fallback", None),
            user=MagicMock(id=7),
            prompt="do it",
            respond=respond,
            ack=AsyncMock(),
            **kw,  # type: ignore[arg-type]
        )
        return respond

    async def test_unbound_thread_slash_gets_the_binding_hint(self) -> None:
        cog = _make_cog()
        respond = await self._call(cog, _thread(10, parent_id=555))
        text = respond.await_args.args[0]
        assert "/clord-thread-init" in text
        assert respond.await_args.kwargs["ephemeral"] is True

    async def test_unbound_thread_text_command_in_foreign_channel_is_silent(self) -> None:
        """#522: a bystander bot does not answer for another instance's channel."""
        cog = _make_cog()
        msg = _human_msg(_thread(10, parent_id=555))
        respond = await self._call(cog, _thread(10, parent_id=555), message=msg)
        respond.assert_not_awaited()

    async def test_non_text_channel_cannot_spawn(self) -> None:
        cog = _make_cog()
        cog._resolve_session_dir_manager = AsyncMock(return_value=MagicMock())
        cog._resolve_tmux_manager = AsyncMock(return_value=MagicMock())
        cog.spawn_session = AsyncMock()
        respond = await self._call(cog, MagicMock(spec=discord.ForumChannel), fallback=1)
        assert respond.await_args.args[0] == "This command must be used in a text channel."
        cog.spawn_session.assert_not_awaited()

    async def test_seed_send_forbidden_answers_with_permission_help(self) -> None:
        """#75: the interaction is answered instead of hanging on "thinking"."""
        from c_lord.cogs.claude_chat import _SEND_PERMISSION_HELP

        cog = _make_cog()
        cog._resolve_session_dir_manager = AsyncMock(return_value=MagicMock())
        cog._resolve_tmux_manager = AsyncMock(return_value=MagicMock())
        cog.spawn_session = AsyncMock(
            side_effect=discord.Forbidden(MagicMock(status=403), "Missing Access")
        )
        channel = MagicMock(spec=discord.TextChannel)
        channel.id = 1
        respond = await self._call(cog, channel)
        respond.assert_awaited_once_with(_SEND_PERMISSION_HELP, ephemeral=True)


class TestRepoAutocomplete:
    def _cog(self, default: str | None, known: list[str]) -> tuple[ClaudeChatCog, MagicMock]:
        channel_cog = MagicMock(spec=ChannelRepoCog)
        channel_cog.channel_repo = AsyncMock(return_value=default)
        channel_cog.known_repos = AsyncMock(return_value=known)
        return _make_cog(channel_cog=channel_cog), channel_cog

    @staticmethod
    def _interaction(parent_id: int | None = 50, channel_id: int = 60) -> MagicMock:
        interaction = MagicMock()
        interaction.channel = SimpleNamespace(parent_id=parent_id)
        interaction.channel_id = channel_id
        return interaction

    async def test_no_channel_cog_offers_nothing(self) -> None:
        cog = _make_cog()
        assert await cog._repo_autocomplete(self._interaction(), "") == []

    async def test_default_first_then_other_known_repos(self) -> None:
        """#514: the default is named as such, and not repeated below it."""
        a, b, c = (f"https://github.com/o/{n}" for n in ("a", "b", "c"))
        cog, channel_cog = self._cog(default=a, known=[a, b, c])
        choices = await cog._repo_autocomplete(self._interaction(parent_id=50), "")
        channel_cog.channel_repo.assert_awaited_once_with(50)
        assert [ch.value for ch in choices] == [a, b, c]
        assert choices[0].name.startswith("未指定ならこれ")
        assert choices[1].name == b

    async def test_filter_is_case_insensitive_and_applies_to_the_default_too(self) -> None:
        a, b = "https://github.com/o/Alpha", "https://github.com/o/beta"
        cog, _ = self._cog(default=a, known=[a, b])
        choices = await cog._repo_autocomplete(self._interaction(), "BET")
        assert [ch.value for ch in choices] == [b]

    async def test_outside_a_thread_the_channel_itself_is_asked(self) -> None:
        cog, channel_cog = self._cog(default=None, known=[])
        await cog._repo_autocomplete(self._interaction(parent_id=None, channel_id=60), "")
        channel_cog.channel_repo.assert_awaited_once_with(60)

    async def test_capped_at_discords_25_choices(self) -> None:
        known = [f"https://github.com/o/r{i}" for i in range(40)]
        cog, _ = self._cog(default=None, known=known)
        choices = await cog._repo_autocomplete(self._interaction(), "")
        assert len(choices) == 25

    async def test_lookup_failures_degrade_to_fewer_choices(self) -> None:
        cog, channel_cog = self._cog(default=None, known=[])
        channel_cog.channel_repo = AsyncMock(side_effect=RuntimeError("db"))
        channel_cog.known_repos = AsyncMock(side_effect=RuntimeError("db"))
        assert await cog._repo_autocomplete(self._interaction(), "") == []

    @pytest.mark.xfail(
        strict=True,
        reason="#1116: repo URLs over 100 chars are offered with a truncated value — picking "
        "one sends a different (non-existent) URL as repo:",
    )
    async def test_a_long_repo_url_is_never_offered_truncated(self) -> None:
        long_repo = "https://github.com/some-organisation/" + "x" * 90
        assert len(long_repo) > 100
        cog, _ = self._cog(default=None, known=[long_repo])
        choices = await cog._repo_autocomplete(self._interaction(), "")
        assert all(ch.value == long_repo for ch in choices)


# ── /clord-reattach ───────────────────────────────────────────────────────


class TestClordReattachSlash:
    @staticmethod
    def _interaction(channel: object) -> MagicMock:
        interaction = MagicMock()
        interaction.channel = channel
        interaction.user = MagicMock(id=7)
        interaction.response.send_message = AsyncMock()
        interaction.response.defer = AsyncMock()
        interaction.followup.send = AsyncMock()
        return interaction

    async def test_outside_a_thread(self) -> None:
        cog = _make_cog()
        inter = self._interaction(MagicMock(spec=discord.TextChannel))
        await cog.clord_reattach.callback(cog, inter)
        inter.response.send_message.assert_awaited_once_with(
            "このコマンドはスレッド内でのみ使えます。", ephemeral=True
        )

    async def test_unauthorized_user(self) -> None:
        cog = _make_cog()
        cog._is_allowed = MagicMock(return_value=False)
        inter = self._interaction(_thread())
        await cog.clord_reattach.callback(cog, inter)
        inter.response.send_message.assert_awaited_once_with(
            "You are not authorized to use this command.", ephemeral=True
        )

    async def test_another_bots_thread_gets_the_owner_notice(self) -> None:
        """#811: the foreign-owner notice, and nothing reattached."""
        cog = _make_cog()
        cog._reattach_thread = AsyncMock()
        inter = self._interaction(_thread())
        with patch(f"{MOD}.foreign_owner_notice_for", AsyncMock(return_value="<@1> の担当です")):
            await cog.clord_reattach.callback(cog, inter)
        inter.response.send_message.assert_awaited_once_with("<@1> の担当です", ephemeral=True)
        cog._reattach_thread.assert_not_awaited()

    async def test_a_thread_with_its_record_is_left_alone(self) -> None:
        """#538: nothing lost → say so, change nothing."""
        cog = _make_cog()
        cog.repo.get = AsyncMock(return_value=_record())
        cog._reattach_thread = AsyncMock()
        inter = self._interaction(_thread())
        with patch(f"{MOD}.foreign_owner_notice_for", AsyncMock(return_value=None)):
            await cog.clord_reattach.callback(cog, inter)
        text = inter.response.send_message.await_args.args[0]
        assert "失われていません" in text
        cog._reattach_thread.assert_not_awaited()

    async def test_a_lost_record_is_reattached_and_reported(self) -> None:
        """#538 AC6: defer, reattach, then post what was recovered."""
        cog = _make_cog()
        plan = Plan(Recovery.FULL, working_dir="/w", session_id="abc")
        cog._reattach_thread = AsyncMock(return_value=plan)
        thread = _thread(91)
        inter = self._interaction(thread)
        with patch(f"{MOD}.foreign_owner_notice_for", AsyncMock(return_value=None)):
            await cog.clord_reattach.callback(cog, inter)
        inter.response.defer.assert_awaited_once()
        cog._reattach_thread.assert_awaited_once_with(thread)
        inter.followup.send.assert_awaited_once_with(reattach_notice(plan))


# ── stop / clear ──────────────────────────────────────────────────────────


class TestWakeupGuard:
    async def test_guard_stays_only_while_no_new_runner_took_over(self) -> None:
        """#878: the guard stops suppressing once the user's next turn registered."""
        cog = _make_cog()
        captured: dict[str, object] = {}

        async def _fake_suppress(thread_id, *, tmux, still_ours):  # noqa: ANN001, ANN202
            captured["still_ours"] = still_ours

        stopped = object()
        with patch(f"{MOD}.suppress_wakeups", _fake_suppress):
            cog._start_wakeup_guard(3, MagicMock(), stopped)
            await asyncio.gather(*cog._stop_guard_tasks)
        still_ours = captured["still_ours"]
        assert callable(still_ours)
        assert still_ours() is True  # nothing registered
        cog._active_runners[3] = stopped  # type: ignore[assignment]
        assert still_ours() is True  # still the stopped one
        cog._active_runners[3] = MagicMock()  # the next turn's runner
        assert still_ours() is False
        assert not cog._stop_guard_tasks


class TestClearGaps:
    async def test_no_tmux_for_the_thread(self) -> None:
        cog = _make_cog()
        cog.repo.get = AsyncMock(return_value=_record())
        respond = AsyncMock()
        with patch(f"{MOD}.foreign_owner_notice_for", AsyncMock(return_value=None)):
            await cog._clear_impl(_thread(), respond, user=MagicMock())
        respond.assert_awaited_once_with("tmux is not configured for this thread.", ephemeral=True)

    async def test_failed_keystrokes_are_reported_and_no_enter_is_sent(self) -> None:
        tmux = MagicMock()
        tmux.is_claude_running = MagicMock(return_value=True)
        tmux.send_literal = MagicMock(return_value=False)
        tmux.project_dir_for = MagicMock(return_value=None)
        cog = _make_cog(channel_cog=_make_channel_cog_mock(tmux_manager=tmux))
        cog.repo.get = AsyncMock(return_value=_record())
        respond = AsyncMock()
        with patch(f"{MOD}.foreign_owner_notice_for", AsyncMock(return_value=None)):
            await cog._clear_impl(_thread(12), respond, user=MagicMock())
        respond.assert_awaited_once_with("Failed to send /clear to the session.", ephemeral=True)
        tmux.send_keys.assert_not_called()

    async def test_adopt_polls_until_the_new_transcript_appears(self, tmp_path: Path) -> None:
        """#803: the claim follows the transcript /clear started."""
        cog = _make_cog()
        results = iter([None, "new-uuid"])
        with (
            patch(f"{MOD}.adopt_cleared_session", side_effect=lambda *_a: next(results)),
            patch(f"{MOD}._SLASH_POLL_INTERVAL", 0),
        ):
            got = await cog._adopt_cleared_session(1, tmp_path, set())
        assert got == "new-uuid"

    async def test_adopt_gives_up_after_the_deadline(
        self, tmp_path: Path, caplog: pytest.LogCaptureFixture
    ) -> None:
        cog = _make_cog()
        with (
            patch(f"{MOD}.adopt_cleared_session", return_value=None) as adopt,
            patch(f"{MOD}._CLEAR_ADOPT_TIMEOUT", 0),
            caplog.at_level(logging.ERROR),
        ):
            got = await cog._adopt_cleared_session(1, tmp_path, {"old"})
        assert got is None
        adopt.assert_called_once_with(tmp_path, {"old"})
        assert "no new transcript appeared" in caplog.text


# ── spawn / shutdown / on_ready ───────────────────────────────────────────


class TestSpawnUnboundRepo:
    async def test_repo_without_thread_bindings_still_starts(
        self, caplog: pytest.LogCaptureFixture
    ) -> None:
        """#514: no binding support → fall back to the channel binding, say so in the log."""
        cog = _make_cog()  # no ChannelRepoCog → _bind_thread_repo returns None
        thread = _thread(40)
        channel = MagicMock()
        channel.id = 4
        channel.create_thread = AsyncMock(return_value=thread)
        with (
            patch.object(cog, "_run_claude", new=AsyncMock()) as run,
            caplog.at_level(logging.WARNING),
        ):
            got = await cog.spawn_session(channel, "go", repo="https://github.com/o/r")
            await asyncio.sleep(0)
        assert got is thread
        run.assert_awaited_once()
        assert "thread bindings are not enabled" in caplog.text


class TestCogUnloadGaps:
    async def test_lamp_save_failure_still_records_the_restart_notice(self) -> None:
        """#870: failing to save the lamp message must not lose the restart mark."""
        bot = MagicMock()
        repo = MagicMock()
        repo.get = AsyncMock(return_value=_record(session_id="s1"))
        repo.update_trigger_message = AsyncMock(side_effect=RuntimeError("db"))
        resume_repo = MagicMock()
        resume_repo.mark = AsyncMock()
        cog = ClaudeChatCog(bot=bot, repo=repo, runner=MagicMock(), resume_repo=resume_repo)
        cog._active_runners[11] = MagicMock()
        cog._turn_triggers[11] = 555
        await cog.cog_unload()
        repo.update_trigger_message.assert_awaited_once_with(11, 555)
        resume_repo.mark.assert_awaited_once_with(
            11, session_id="s1", reason="bot_shutdown", resume_prompt=None
        )


def _pending(thread_id: int) -> MagicMock:
    return MagicMock(id=1, thread_id=thread_id, session_id=None, reason="bot_shutdown")


class TestOnReadyGaps:
    def _cog(self, bot: MagicMock) -> tuple[ClaudeChatCog, MagicMock]:
        resume_repo = MagicMock()
        resume_repo.delete = AsyncMock()
        cog = ClaudeChatCog(bot=bot, repo=MagicMock(), runner=MagicMock(), resume_repo=resume_repo)
        cog._on_gateway_back = MagicMock()  # type: ignore[method-assign]
        cog._run_startup_recovery = AsyncMock()  # type: ignore[method-assign]
        cog._recover_orphaned_lamp = AsyncMock()  # type: ignore[method-assign]
        return cog, resume_repo

    async def _ready(self, cog: ClaudeChatCog) -> None:
        with patch(f"{MOD}.resolve_fallback_owner_ids", AsyncMock()):
            await cog.on_ready()
        await asyncio.sleep(0)

    async def test_a_deleted_thread_is_skipped(self) -> None:
        bot = MagicMock()
        bot.get_channel.return_value = None
        bot.fetch_channel = AsyncMock(side_effect=discord.NotFound(MagicMock(status=404), "x"))
        cog, resume_repo = self._cog(bot)
        resume_repo.get_pending = AsyncMock(return_value=[_pending(70)])
        await self._ready(cog)
        resume_repo.delete.assert_awaited_once_with(1)
        cog._recover_orphaned_lamp.assert_not_awaited()

    async def test_a_thread_without_text_channel_parent_is_skipped(self) -> None:
        thread = _thread(71)
        thread.parent = MagicMock(spec=discord.ForumChannel)
        bot = MagicMock()
        bot.get_channel.return_value = thread
        cog, resume_repo = self._cog(bot)
        resume_repo.get_pending = AsyncMock(return_value=[_pending(71)])
        await self._ready(cog)
        thread.send.assert_not_awaited()
        cog._recover_orphaned_lamp.assert_not_awaited()

    async def test_notice_failure_still_recovers_the_lamp(
        self, caplog: pytest.LogCaptureFixture
    ) -> None:
        """#718: the lamp is taken back even when the notice could not be posted."""
        thread = _thread(72)
        thread.parent = MagicMock(spec=discord.TextChannel)
        thread.send = AsyncMock(side_effect=discord.HTTPException(MagicMock(status=500), "x"))
        bot = MagicMock()
        bot.get_channel.return_value = thread
        cog, resume_repo = self._cog(bot)
        resume_repo.get_pending = AsyncMock(return_value=[_pending(72)])
        with caplog.at_level(logging.ERROR):
            await self._ready(cog)
        assert "Failed to post restart notice in thread 72" in caplog.text
        cog._recover_orphaned_lamp.assert_awaited_once_with(thread)


class TestRecoverOrphanedLampGaps:
    async def test_missing_trigger_in_a_non_text_parent_skips_recovery(
        self, caplog: pytest.LogCaptureFixture
    ) -> None:
        """#718: the starter-message fallback only exists under a TextChannel."""
        cog = _make_cog()
        cog.repo.get = AsyncMock(return_value=_record(trigger_message_id=9, working_dir="/w"))
        thread = _thread(73)
        thread.fetch_message = AsyncMock(side_effect=discord.NotFound(MagicMock(status=404), "x"))
        thread.parent = MagicMock(spec=discord.ForumChannel)
        with (
            patch(f"{MOD}.adopt_orphaned_lamp", AsyncMock()) as adopt,
            caplog.at_level(logging.WARNING),
        ):
            await cog._recover_orphaned_lamp(thread)
        adopt.assert_not_awaited()
        assert 73 not in cog._adopted_lamps
        assert "lamp recovery skipped" in caplog.text


class TestCloseRecoveredTurnGaps:
    async def test_every_step_failing_still_summons_the_requester(self) -> None:
        """#907/#978: footer, ending and event are best-effort; the 🟡 still goes out.

        An unreadable ending is treated as a plain end (no api_error / limit).
        """
        cog = _make_cog()
        dashboard = MagicMock()
        cog._get_dashboard = MagicMock(return_value=dashboard)
        thread = _thread(80)
        message = MagicMock(content="hello\nworld", created_at=None, webhook_id=None)
        with (
            patch(f"{MOD}._post_context_usage", AsyncMock(side_effect=RuntimeError("x"))),
            patch(f"{MOD}.recovered_turn_ending", side_effect=RuntimeError("x")),
            patch("c_lord.turn_events.announce_recovered_turn", side_effect=RuntimeError("x")),
            patch(f"{MOD}._safe_set_state", AsyncMock()) as set_state,
        ):
            await cog._close_recovered_turn(
                thread, message, working_dir="/w", session_id="s", already_over=False
            )
        set_state.assert_awaited_once()
        args, kwargs = set_state.await_args
        assert args[1] == 80
        assert args[3] == "hello world"
        assert kwargs["api_error"] is None and kwargs["usage_limit"] is None
        assert kwargs["no_response"] is False

    async def test_no_dashboard_means_no_summons(self) -> None:
        cog = _make_cog()
        cog._get_dashboard = MagicMock(return_value=None)
        with (
            patch(f"{MOD}._post_context_usage", AsyncMock()),
            patch(f"{MOD}.recovered_turn_ending", return_value=TurnEnding()),
            patch("c_lord.turn_events.announce_recovered_turn"),
            patch(f"{MOD}._safe_set_state", AsyncMock()) as set_state,
        ):
            await cog._close_recovered_turn(
                _thread(81), MagicMock(), working_dir="/w", session_id=None, already_over=False
            )
        set_state.assert_not_awaited()


# ── #745 / #776 / #920 gateway pick-up ────────────────────────────────────


NOW = datetime(2026, 10, 30, 12, 0, tzinfo=UTC)


class TestGatewayGaps:
    async def test_keep_open_outage_survives_a_settings_failure(
        self, caplog: pytest.LogCaptureFixture
    ) -> None:
        cog = _make_cog()
        cog._settings_repo = MagicMock()
        cog._settings_repo.delete = AsyncMock(side_effect=RuntimeError("db"))
        with caplog.at_level(logging.WARNING):
            await cog._keep_open_outage()
        assert "could not store the open gateway outage" in caplog.text

    def test_a_reconnect_without_an_outage_reads_nothing_back(self) -> None:
        cog = _make_cog()
        cog._gateway = MagicMock()
        cog._gateway.first_connect.return_value = None
        cog._gateway.connected.return_value = None
        cog._start_backfill = MagicMock()  # type: ignore[method-assign]
        cog._on_gateway_back("resumed")
        cog._start_backfill.assert_not_called()

    async def test_startup_pick_up_failure_keeps_the_previous_outage(self) -> None:
        """#776/#920: a failed read-back must not clear the stored outage.

        Clearing it would make the next process read from an hour before *it*
        started and lose what the previous one was deaf for.
        """
        cog = _make_cog()
        prev = PreviousOutage(down_at=NOW - timedelta(minutes=30), last_alive_at=NOW)
        cog._previous_outage = AsyncMock(return_value=prev)  # type: ignore[method-assign]
        cog._keep_open_outage = AsyncMock()  # type: ignore[method-assign]
        cog._pick_up_missed = AsyncMock(side_effect=RuntimeError("discord"))  # type: ignore[method-assign]
        startup = Startup(started_at=NOW, up_at=NOW, since=NOW - timedelta(hours=1))
        await cog._backfill_startup(startup)
        cog._pick_up_missed.assert_awaited_once()
        assert cog._pick_up_missed.await_args.kwargs["cause"] == "gateway"
        cog._keep_open_outage.assert_not_awaited()

    async def test_outage_pick_up_failure_is_logged_not_raised(
        self, caplog: pytest.LogCaptureFixture
    ) -> None:
        cog = _make_cog()
        cog._keep_open_outage = AsyncMock()  # type: ignore[method-assign]
        cog._pick_up_missed = AsyncMock(side_effect=RuntimeError("discord"))  # type: ignore[method-assign]
        outage = Outage(down_at=NOW, last_alive_at=NOW, back_at=NOW, since=NOW)
        with caplog.at_level(logging.ERROR):
            await cog._backfill_outage(outage, "ready")
        cog._keep_open_outage.assert_awaited_once()
        assert "reading back missed messages failed" in caplog.text

    async def test_one_failing_thread_does_not_stop_the_others(self) -> None:
        """#745: the walk counts the thread as read back and moves on."""
        cog = _make_cog()
        t1, t2, t3 = _thread(1), _thread(2), _thread(3)
        cog._threads_active_since = AsyncMock(return_value=[t1, t2, t3])  # type: ignore[method-assign]
        records = {1: _record(), 2: _record(), 3: None}  # thread 3 is not ours
        cog.repo.get = AsyncMock(side_effect=lambda tid: records[tid])

        async def _pick(thread, *_a, **_k):  # noqa: ANN001, ANN202
            if thread.id == 1:
                raise RuntimeError("history 500")
            return 3

        cog._pick_up_thread = AsyncMock(side_effect=_pick)  # type: ignore[method-assign]
        got = await cog._pick_up_missed(since=NOW, down_at=NOW, back_at=NOW, cause="gateway")
        assert got == (3, 1, 2)
        assert cog._pick_up_thread.await_count == 2

    async def test_threads_active_since_skips_an_unreadable_guild(self) -> None:
        cog = _make_cog()
        bad = MagicMock(id=1)
        bad.active_threads = AsyncMock(side_effect=discord.HTTPException(MagicMock(), "x"))
        fresh = SimpleNamespace(
            last_message_id=discord.utils.time_snowflake(NOW + timedelta(minutes=1))
        )
        stale = SimpleNamespace(
            last_message_id=discord.utils.time_snowflake(NOW - timedelta(minutes=1))
        )
        quiet = SimpleNamespace(last_message_id=None)
        good = MagicMock(id=2)
        good.active_threads = AsyncMock(return_value=[fresh, stale, quiet])
        cog.bot.guilds = [bad, good]
        assert await cog._threads_active_since(NOW) == [fresh]

    async def test_a_full_history_page_is_flagged(self, caplog: pytest.LogCaptureFixture) -> None:
        """#745: hitting the history cap is said in the log — it may have missed more."""
        cog = _make_cog()
        thread = _thread(9)

        async def _history(**_kw: object):  # noqa: ANN202
            for i in range(2):
                yield MagicMock(id=i)

        thread.history = _history
        cog._is_runnable_request = AsyncMock(return_value=False)  # type: ignore[method-assign]
        with patch(f"{MOD}.HISTORY_LIMIT", 2), caplog.at_level(logging.WARNING):
            got = await cog._pick_up_thread(
                thread, _record(), since=NOW, down_at=NOW, back_at=NOW, cause="gateway"
            )
        assert got == 0
        assert "read back only the first 2 messages" in caplog.text
        thread.send.assert_not_awaited()


# ── thread reply path ─────────────────────────────────────────────────────


def _reply_msg(thread: MagicMock, msg_id: int, text: str, minute: int) -> MagicMock:
    msg = MagicMock(spec=discord.Message)
    msg.id = msg_id
    msg.channel = thread
    msg.content = text
    msg.attachments = []
    msg.created_at = datetime(2026, 10, 30, 3, minute, tzinfo=UTC)
    msg.author = MagicMock(display_name="yousan", bot=False, id=5)
    return msg


class TestHandleThreadReplyGaps:
    async def test_an_answer_to_an_open_menu_starts_no_turn(self) -> None:
        """#536 AC7: the sentence went to the menu — no pre-emption, no new turn."""
        cog = _make_cog()
        cog.repo.get = AsyncMock(return_value=_record())
        cog._maybe_answer_open_menu = AsyncMock(return_value=True)  # type: ignore[method-assign]
        cog._run_claude = AsyncMock()  # type: ignore[method-assign]
        thread = _thread(20)
        await cog._handle_thread_reply(_reply_msg(thread, 1, "y", 0))
        cog._run_claude.assert_not_awaited()
        assert 20 not in cog._active_tasks

    async def test_missed_messages_run_as_one_merged_turn(self) -> None:
        """#745: earlier missed messages ride in the same prompt, oldest first."""
        cog = _make_cog()
        cog.repo.get = AsyncMock(return_value=_record())
        cog._run_claude = AsyncMock()  # type: ignore[method-assign]
        thread = _thread(21)
        first = _reply_msg(thread, 1, "まずこれ", 0)
        last = _reply_msg(thread, 2, "次にこれ", 1)
        with patch(f"{MOD}.enrich_discord_references", AsyncMock(side_effect=lambda p, *_: p)):
            await cog._handle_thread_reply(last, earlier=[first])
            await cog._active_tasks[21]
        prompt = cog._run_claude.await_args.args[2]
        assert "2 件のメッセージ" in prompt
        assert prompt.index("まずこれ") < prompt.index("次にこれ")
        assert "1/2" in prompt and "2/2" in prompt
        # The menu is never consulted for a batch — several messages are not one answer.
        assert cog._run_claude.await_args.args[0] is last


# ── typed menu answers (#536 / #804) ──────────────────────────────────────


class TestMenuAnswerGaps:
    @staticmethod
    def _bus(**kw: object) -> MagicMock:
        bus = MagicMock()
        bus.accepts_free_text.return_value = True
        bus.watch_delivery.return_value = asyncio.Queue()
        for k, v in kw.items():
            setattr(bus, k, v)
        return bus

    async def test_dead_window_wake_failure_hands_the_text_to_the_instruction_path(
        self,
    ) -> None:
        """#804 ②: no pane, wake raised → still False, so the text runs as an order."""
        cog = _make_cog()
        cog._menu_window_is_gone = AsyncMock(return_value=True)  # type: ignore[method-assign]
        cog.wake_workspace = AsyncMock(side_effect=RuntimeError("tmux"))  # type: ignore[method-assign]
        thread = _thread(30)
        bus = self._bus()
        with patch("c_lord.discord_ui.ask_bus.ask_bus", bus):
            got = await cog._maybe_answer_open_menu(_reply_msg(thread, 1, "はい", 0), thread)
        assert got is False
        assert "停止していました" in _sent_texts(thread)[0]
        bus.post_answer.assert_not_called()

    async def test_answer_not_accepted_by_the_bus_falls_through(self) -> None:
        cog = _make_cog()
        cog._menu_window_is_gone = AsyncMock(return_value=False)  # type: ignore[method-assign]
        thread = _thread(31)
        bus = self._bus()
        bus.post_answer.return_value = False
        with patch("c_lord.discord_ui.ask_bus.ask_bus", bus):
            got = await cog._maybe_answer_open_menu(_reply_msg(thread, 1, "はい", 0), thread)
        assert got is False
        thread.send.assert_not_awaited()  # nothing claimed
        bus.unwatch_delivery.assert_called_once_with(31)

    async def test_the_rerun_button_sends_the_text_as_a_new_instruction(self) -> None:
        """#536 AC7: the one-button escape runs the same message on the reply path."""
        from c_lord.discord_ui.ask_bus import DELIVERY_DELIVERED

        cog = _make_cog()
        cog._menu_window_is_gone = AsyncMock(return_value=False)  # type: ignore[method-assign]
        cog._await_answer_verdict = AsyncMock(return_value=DELIVERY_DELIVERED)  # type: ignore[method-assign]
        cog._handle_thread_reply = AsyncMock()  # type: ignore[method-assign]
        thread = _thread(32)
        msg = _reply_msg(thread, 1, "はい", 0)
        captured: dict[str, object] = {}

        def _view(cb, **_kw):  # noqa: ANN001, ANN202
            captured["cb"] = cb
            return MagicMock()

        bus = self._bus()
        bus.post_answer.return_value = True
        with (
            patch("c_lord.discord_ui.ask_bus.ask_bus", bus),
            patch("c_lord.discord_ui.ask_menus.disable_stale_copies", AsyncMock()),
            patch(f"{MOD}.TextAnsweredMenuView", side_effect=_view),
        ):
            assert await cog._maybe_answer_open_menu(msg, thread) is True
        cb = captured["cb"]
        await cb(MagicMock())  # type: ignore[operator]
        cog._handle_thread_reply.assert_awaited_once_with(msg)

    async def test_window_check_error_counts_as_window_present(self) -> None:
        """#804: "cannot tell" is False — a live menu must not be taken for a ghost."""
        cog = _make_cog()
        tmux = MagicMock()
        tmux.window_name = MagicMock(side_effect=RuntimeError("tmux"))
        cog._resolve_tmux_manager = AsyncMock(return_value=tmux)  # type: ignore[method-assign]
        assert await cog._menu_window_is_gone(_thread()) is False

    async def test_replace_notice_without_a_message_posts_plain_text(self) -> None:
        cog = _make_cog()
        thread = _thread()
        await cog._replace_notice(thread, None, content="done")
        thread.send.assert_awaited_once_with(content="done")


# ── wake ──────────────────────────────────────────────────────────────────


class TestWakeGaps:
    async def test_naming_failure_does_not_fail_the_wake(
        self, caplog: pytest.LogCaptureFixture
    ) -> None:
        """#642/#95: the restore worked; a rename error is only logged."""
        cog = _make_cog()
        tmux = MagicMock()
        cog._resolve_tmux_manager = AsyncMock(return_value=tmux)  # type: ignore[method-assign]
        cog._resolve_session_dir_manager = AsyncMock(return_value=None)  # type: ignore[method-assign]
        cog._fix_turn_settings = AsyncMock(return_value=None)  # type: ignore[method-assign]
        cog._get_current_model = AsyncMock(return_value=None)  # type: ignore[method-assign]
        cog._apply_thread_naming = AsyncMock(side_effect=RuntimeError("rename"))  # type: ignore[method-assign]
        cog.repo.touch = AsyncMock()
        cog.repo.set_slept = AsyncMock()
        fake_runner = MagicMock()
        fake_runner.wake = AsyncMock(return_value=True)
        with (
            patch(f"{MOD}.TmuxClaudeRunner", return_value=fake_runner),
            caplog.at_level(logging.WARNING),
        ):
            assert await cog.wake_workspace(_thread(33)) is True
        cog.repo.set_slept.assert_awaited_once_with(33, False)
        assert "wake: thread naming failed" in caplog.text


# ── attachments / workspace failure ───────────────────────────────────────


class TestAttachmentGaps:
    async def test_a_file_that_cannot_be_saved_is_named_with_the_reason(
        self, tmp_path: Path
    ) -> None:
        """#528: the user is told which file never reached Claude, and why."""
        cog = _make_cog()
        thread = _thread()
        att = MagicMock(filename="big.log", size=10)
        att.read = AsyncMock(return_value=b"0123456789")
        msg = MagicMock(id=1, attachments=[att])
        with patch(f"{MOD}.save_attachment", side_effect=OSError(28, "No space left on device")):
            sections = await cog._stage_attachments(msg, str(tmp_path), thread)
        assert sections == ""
        (text,) = _sent_texts(thread)
        assert "`big.log` — 保存に失敗しました (No space left on device)" in text


class TestWorkspaceFailureGaps:
    async def test_event_outlet_failure_still_resets_the_dashboard(self) -> None:
        """#917/#477: every step is guarded; the thread still goes back to waiting."""
        cog = _make_cog()
        thread = _thread(34)
        status = MagicMock()
        status.set_error = AsyncMock()
        dashboard = MagicMock()
        task = MagicMock()
        cog._active_tasks[34] = task
        with (
            patch("c_lord.turn_events.begin_turn", side_effect=RuntimeError("outlet")),
            patch(f"{MOD}._safe_set_state", AsyncMock()) as set_state,
        ):
            await cog._abort_turn_on_workspace_failure(
                thread,
                RuntimeError("git clone failed"),
                status=status,
                failure_notify_id=None,
                dashboard=dashboard,
                description="desc",
                current_task=task,
            )
        status.set_error.assert_awaited_once()
        assert 34 not in cog._active_tasks
        set_state.assert_awaited_once()
        assert set_state.await_args.args[2].name == "WAITING_INPUT"


# ── _run_claude ───────────────────────────────────────────────────────────


def _turn_cog() -> ClaudeChatCog:
    from tests.test_preempted_turn_ping import _make_cog as make

    cog = make()
    sdm = MagicMock()
    sdm.create_session_dir = MagicMock(return_value="/tmp/work")
    tmux = MagicMock()
    tmux.create_session = MagicMock(return_value="w1")
    cog._resolve_session_dir_manager = AsyncMock(return_value=sdm)  # type: ignore[method-assign]
    cog._resolve_tmux_manager = AsyncMock(return_value=tmux)  # type: ignore[method-assign]
    cog._get_dashboard = MagicMock(return_value=None)  # type: ignore[method-assign]
    cog._get_coordination = MagicMock(return_value=None)  # type: ignore[method-assign]
    cog._get_current_model = AsyncMock(return_value=None)  # type: ignore[method-assign]
    return cog


class TestRunClaudeGaps:
    async def test_naming_failures_never_stop_the_turn(
        self, caplog: pytest.LogCaptureFixture
    ) -> None:
        """#423/#910: both naming passes may fail; the turn still runs."""
        from tests.test_preempted_turn_ping import _message

        cog = _turn_cog()
        cog._apply_thread_naming = AsyncMock(side_effect=RuntimeError("rename"))  # type: ignore[method-assign]
        thread = _thread(501, parent_id=500)
        run = AsyncMock()
        with (
            patch(f"{MOD}.run_claude_with_config", run),
            caplog.at_level(logging.WARNING),
        ):
            await cog._run_claude(_message(), thread, "やって", None)
        run.assert_awaited_once()
        assert cog._apply_thread_naming.await_count == 2
        assert "thread naming failed" in caplog.text
        assert "turn-end thread naming failed" in caplog.text

    async def test_a_mirror_that_fails_to_start_still_gets_the_trigger(
        self, caplog: pytest.LogCaptureFixture
    ) -> None:
        """#71/#115: start_for failing is logged; the reply target is still recorded."""
        from tests.test_preempted_turn_ping import _message

        cog = _turn_cog()
        cog._apply_thread_naming = AsyncMock()  # type: ignore[method-assign]
        mirror = MagicMock()
        mirror.start_for = MagicMock(side_effect=RuntimeError("inotify"))
        cog.bot.transcript_mirror_cog = mirror
        thread = _thread(502, parent_id=500)
        msg = _message()
        with (
            patch(f"{MOD}.run_claude_with_config", AsyncMock()) as run,
            caplog.at_level(logging.WARNING),
        ):
            await cog._run_claude(msg, thread, "やって", None)
        mirror.start_for.assert_called_once_with(502, "/tmp/work")
        mirror.set_trigger_message.assert_called_once_with(502, msg.id)
        run.assert_awaited_once()
        assert "Failed to start TranscriptMirror for thread=502" in caplog.text

    async def test_a_full_house_says_it_is_waiting_for_a_slot(self) -> None:
        """#632: the courtesy notice goes out, then the turn runs once a slot frees."""
        from tests.test_preempted_turn_ping import _message

        cog = _turn_cog()
        cog._apply_thread_naming = AsyncMock()  # type: ignore[method-assign]
        cog._semaphore = asyncio.Semaphore(1)
        await cog._semaphore.acquire()
        thread = _thread(503, parent_id=500)
        run = AsyncMock()
        with patch(f"{MOD}.run_claude_with_config", run):
            task = asyncio.create_task(cog._run_claude(_message(), thread, "やって", None))
            for _ in range(20):
                await asyncio.sleep(0)
                if thread.send.await_count:
                    break
            assert "Waiting for a free session slot" in _sent_texts(thread)[0]
            run.assert_not_awaited()
            cog._semaphore.release()
            await task
        run.assert_awaited_once()


class TestClordReattachText:
    @staticmethod
    def _ctx(channel: object) -> MagicMock:
        ctx = MagicMock()
        ctx.channel = channel
        ctx.send = AsyncMock()
        return ctx

    async def test_outside_a_thread(self) -> None:
        cog = _make_cog()
        ctx = self._ctx(MagicMock(spec=discord.TextChannel))
        await cog.clord_reattach_text.callback(cog, ctx)
        ctx.send.assert_awaited_once_with("このコマンドはスレッド内でのみ使えます。")

    async def test_unauthorized_message(self) -> None:
        cog = _make_cog()
        cog._is_message_authorized = MagicMock(return_value=False)  # type: ignore[method-assign]
        cog._reattach_thread = AsyncMock()  # type: ignore[method-assign]
        ctx = self._ctx(_thread())
        await cog.clord_reattach_text.callback(cog, ctx)
        ctx.send.assert_awaited_once_with("You are not authorized to use this command.")
        cog._reattach_thread.assert_not_awaited()
