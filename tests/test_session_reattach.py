"""Getting a thread back after its ``sessions`` row is gone — #538 AC6–AC8.

c-lord could tell you a thread was unrecoverable but had no way to recover one.
The row is the only link from a Discord thread to its Claude session, and #554
deletes it after 30 days; when that happened the only repair was editing the
SQLite file by hand. ``/resync`` reconnects the Discord mirror to a tmux pane
and ``/reopen-workspace`` clears a ``/close-workspace``, but neither writes the
row.

What makes this worth doing is that the row's deletion destroys almost nothing.
``W3 │ Qiita`` was measured: the git clone is there with the half-written article
and its images, the Discord thread still holds 206 messages, and the user's own
prompts are in ``~/.claude/history.jsonl``. Only Claude's memory — the transcript
— was gone. So recovery is graded by what survived, not offered as one button
that either works or lies:

* **FULL** — transcript on disk, so ``--continue`` (#270) resumes the actual
  conversation. Nothing is lost.
* **WORKDIR** — the checkout survived, the transcript did not. The work
  continues; the conversation is rebuilt from the Discord thread, which c-lord
  can read with its own token.
* **NONE** — nothing to reattach to (AC8): say so, and name the way forward.

AC7 constrains all of this: recovery **reattaches to what exists**. It never
clones, never creates a session dir, never adopts a thread that was not already
c-lord's — otherwise it is just #551's takeover under a friendlier name.
"""

from __future__ import annotations

from pathlib import Path

from c_lord.session_reattach import (
    HISTORY_FILENAME,
    Plan,
    Recovery,
    plan_recovery,
    reattach_notice,
)

THREAD_ID = 1508626302813601843  # W3 │ Qiita


def _seed(tmp_path: Path, *, checkout: bool, transcript: bool) -> Path:
    """Build the on-disk state for ``THREAD_ID`` and return the projects root."""
    base = tmp_path / "sessions" / "9999"
    projects = tmp_path / "projects"
    projects.mkdir(parents=True, exist_ok=True)
    work = base / str(THREAD_ID)
    if checkout:
        work.mkdir(parents=True, exist_ok=True)
        (work / "draft.md").write_text("# 書きかけの記事\n", encoding="utf-8")
    if transcript:
        slug = str(work).replace("/", "-").replace(".", "-")
        pdir = projects / slug
        pdir.mkdir(parents=True, exist_ok=True)
        (pdir / "1fcfb524-aaaa-bbbb-cccc-ddddeeeeffff.jsonl").write_text(
            '{"type":"system"}\n', encoding="utf-8"
        )
    return projects


class TestPlanRecovery:
    def test_transcript_and_checkout_is_a_full_recovery(self, tmp_path) -> None:
        projects = _seed(tmp_path, checkout=True, transcript=True)
        plan = plan_recovery(
            session_dir_base=str(tmp_path / "sessions" / "9999"),
            thread_id=THREAD_ID,
            projects_root=projects,
        )
        assert plan.kind is Recovery.FULL
        assert plan.working_dir is not None
        # The session id comes from the transcript filename — that is the handle
        # --resume/--continue needs, and it is the only place left holding it.
        assert plan.session_id == "1fcfb524-aaaa-bbbb-cccc-ddddeeeeffff"

    def test_checkout_without_transcript_is_a_workdir_recovery(self, tmp_path) -> None:
        """The measured Qiita case."""
        projects = _seed(tmp_path, checkout=True, transcript=False)
        plan = plan_recovery(
            session_dir_base=str(tmp_path / "sessions" / "9999"),
            thread_id=THREAD_ID,
            projects_root=projects,
        )
        assert plan.kind is Recovery.WORKDIR
        assert plan.working_dir is not None
        assert plan.session_id is None

    def test_nothing_on_disk_is_not_recoverable(self, tmp_path) -> None:
        projects = _seed(tmp_path, checkout=False, transcript=False)
        plan = plan_recovery(
            session_dir_base=str(tmp_path / "sessions" / "9999"),
            thread_id=THREAD_ID,
            projects_root=projects,
        )
        assert plan.kind is Recovery.NONE
        assert plan.working_dir is None

    def test_a_transcript_without_a_checkout_is_not_recoverable(self, tmp_path) -> None:
        """AC7: with no session dir there is nothing to reattach *to*. Writing a
        row here would point the thread at a directory that does not exist —
        which is creating a session, not recovering one."""
        projects = _seed(tmp_path, checkout=False, transcript=True)
        plan = plan_recovery(
            session_dir_base=str(tmp_path / "sessions" / "9999"),
            thread_id=THREAD_ID,
            projects_root=projects,
        )
        assert plan.kind is Recovery.NONE

    def test_no_base_dir_is_not_recoverable(self, tmp_path) -> None:
        """AC7 again: an unbound channel gives no base to look under, so there is
        no evidence — and no recovery. It must not fall back to inventing one."""
        plan = plan_recovery(session_dir_base=None, thread_id=THREAD_ID)
        assert plan.kind is Recovery.NONE

    def test_never_raises_on_a_bad_path(self) -> None:
        plan = plan_recovery(session_dir_base="\0bad", thread_id=THREAD_ID)
        assert plan.kind is Recovery.NONE


class TestReattachNotice:
    def test_full_says_the_conversation_comes_back(self) -> None:
        text = reattach_notice(Plan(Recovery.FULL, "/tmp/x", "sess"))
        assert "会話" in text

    def test_workdir_is_honest_that_the_conversation_is_gone(
        self,
    ) -> None:
        """Overpromising here is the #538 failure in a new costume."""
        text = reattach_notice(Plan(Recovery.WORKDIR, "/tmp/x", None))
        assert "作業" in text
        assert "会話" in text

    def test_none_names_the_way_forward(self) -> None:
        """AC8: 'cannot recover' with no next step is where the DB editing began."""
        text = reattach_notice(Plan(Recovery.NONE, None, None))
        assert "/clord" in text

    def test_the_three_tiers_read_differently(self) -> None:
        texts = {
            reattach_notice(Plan(Recovery.FULL, "/tmp/x", "s")),
            reattach_notice(Plan(Recovery.WORKDIR, "/tmp/x", None)),
            reattach_notice(Plan(Recovery.NONE, None, None)),
        }
        assert len(texts) == 3


# ── the Discord thread as a stand-in for the lost transcript ─────────────────


class TestRenderHistory:
    def test_reads_oldest_first(self) -> None:
        from c_lord.session_reattach import render_history

        text = render_history(
            [
                ("yousan", "2026-06-18 10:00", "記事を書いて"),
                ("C-lord", "2026-06-18 10:01", "書きました"),
            ]
        )
        assert text.index("記事を書いて") < text.index("書きました")

    def test_names_who_said_what(self) -> None:
        from c_lord.session_reattach import render_history

        text = render_history([("yousan", "2026-06-18 10:00", "記事を書いて")])
        assert "yousan" in text
        assert "記事を書いて" in text

    def test_says_what_the_file_is_for(self) -> None:
        """Claude opens this cold; without a header it is an unexplained log."""
        from c_lord.session_reattach import render_history

        text = render_history([("yousan", "t", "x")])
        assert "経緯" in text or "過去ログ" in text

    def test_empty_history_still_produces_a_readable_file(self) -> None:
        from c_lord.session_reattach import render_history

        assert render_history([]).strip()


# ── AC6: reachable from Discord, and it writes the row back ──────────────────

import logging  # noqa: E402
from unittest.mock import AsyncMock, MagicMock  # noqa: E402

import discord  # noqa: E402
import pytest  # noqa: E402


def _cog(tmp_path):
    from c_lord.cogs.claude_chat import ClaudeChatCog

    bot = MagicMock()
    bot.channel_id = 999
    bot.settings_repo = None
    bot.user = MagicMock()
    bot.user.id = 777
    bot.get_cog = MagicMock(return_value=None)
    repo = MagicMock()
    repo.get = AsyncMock(return_value=None)
    repo.save = AsyncMock()
    runner = MagicMock()
    runner.clone = MagicMock(return_value=MagicMock())
    cog = ClaudeChatCog(bot=bot, repo=repo, runner=runner)
    sdm = MagicMock()
    sdm.base_dir = str(tmp_path / "sessions" / "9999")
    cog._resolve_session_dir_manager = AsyncMock(return_value=sdm)  # type: ignore[method-assign]
    cog._thread_binding_exists = AsyncMock(return_value=False)  # type: ignore[method-assign]
    cog._projects_root = tmp_path / "projects"
    return cog


def _thread(thread_id: int = THREAD_ID):
    t = MagicMock(spec=discord.Thread)
    t.id = thread_id
    t.parent_id = 999
    t.owner_id = 777
    t.send = AsyncMock()
    t.history = MagicMock()
    return t


class TestReattachAction:
    async def test_full_recovery_writes_the_row_with_the_transcript_session_id(
        self, tmp_path
    ) -> None:
        """The id lives only in the transcript filename once the row is gone."""
        _seed(tmp_path, checkout=True, transcript=True)
        cog = _cog(tmp_path)
        thread = _thread()

        plan = await cog._reattach_thread(thread)

        assert plan.kind is Recovery.FULL
        cog.repo.save.assert_awaited_once()
        kwargs = cog.repo.save.await_args.kwargs
        assert kwargs["session_id"] == "1fcfb524-aaaa-bbbb-cccc-ddddeeeeffff"
        assert kwargs["working_dir"] == plan.working_dir

    async def test_workdir_recovery_writes_the_thread_history_into_the_checkout(
        self, tmp_path
    ) -> None:
        """The Qiita case: Claude reads the Discord thread instead of remembering."""
        _seed(tmp_path, checkout=True, transcript=False)
        cog = _cog(tmp_path)
        thread = _thread()

        async def fake_history(_thread, limit=None):
            return [("yousan", "2026-06-18 10:00", "Qiita の記事を書いて")]

        cog._collect_thread_history = fake_history  # type: ignore[method-assign]

        plan = await cog._reattach_thread(thread)

        assert plan.kind is Recovery.WORKDIR
        written = Path(plan.working_dir) / HISTORY_FILENAME
        assert written.is_file()
        assert "Qiita の記事を書いて" in written.read_text(encoding="utf-8")
        cog.repo.save.assert_awaited_once()

    async def test_nothing_to_recover_writes_no_row(self, tmp_path) -> None:
        """AC7: with nothing on disk, recovery must not invent a session —
        that is exactly the takeover #551 closes."""
        _seed(tmp_path, checkout=False, transcript=False)
        cog = _cog(tmp_path)

        plan = await cog._reattach_thread(_thread())

        assert plan.kind is Recovery.NONE
        cog.repo.save.assert_not_awaited()

    async def test_it_never_clones_or_creates_the_checkout(self, tmp_path) -> None:
        """AC7 stated as a property: recovery only ever reads the disk."""
        _seed(tmp_path, checkout=False, transcript=False)
        cog = _cog(tmp_path)

        await cog._reattach_thread(_thread())

        cog.runner.clone.assert_not_called()
        assert not (tmp_path / "sessions" / "9999" / str(THREAD_ID)).exists()

    async def test_a_history_write_failure_does_not_lose_the_recovery(
        self, tmp_path, caplog: pytest.LogCaptureFixture
    ) -> None:
        """The row is the recovery; the hand-off file is a bonus. Losing the
        bonus must not cost the thing that actually reconnects the thread."""
        _seed(tmp_path, checkout=True, transcript=False)
        cog = _cog(tmp_path)
        cog._collect_thread_history = AsyncMock(side_effect=RuntimeError("discord down"))

        with caplog.at_level(logging.WARNING):
            plan = await cog._reattach_thread(_thread())

        assert plan.kind is Recovery.WORKDIR
        cog.repo.save.assert_awaited_once()


# ── AC6/AC8: reachable from Discord without knowing a command exists ─────────


class TestUntrackedNoticeOffersRecovery:
    async def test_a_recoverable_thread_is_offered_the_button(self, tmp_path) -> None:
        """The person who hits this is mid-confusion in the thread — the way out
        belongs on the notice they are already reading, not in a command they
        would have to know about."""
        _seed(tmp_path, checkout=True, transcript=False)
        cog = _cog(tmp_path)
        message = MagicMock(spec=discord.Message)
        message.webhook_id = None
        message.add_reaction = AsyncMock()
        thread = _thread()

        await cog._handle_untracked_thread(message, thread)

        thread.send.assert_awaited_once()
        assert thread.send.await_args.kwargs.get("view") is not None
        assert "再接続" in str(thread.send.await_args.args[0])

    async def test_an_unrecoverable_thread_gets_the_plain_notice(self, tmp_path) -> None:
        """AC8: nothing to reattach to — say so and name the way forward."""
        _seed(tmp_path, checkout=False, transcript=False)
        cog = _cog(tmp_path)
        cog._thread_binding_exists = AsyncMock(return_value=True)  # still ours
        message = MagicMock(spec=discord.Message)
        message.webhook_id = None
        message.add_reaction = AsyncMock()
        thread = _thread()

        await cog._handle_untracked_thread(message, thread)

        thread.send.assert_awaited_once()
        said = str(thread.send.await_args.args[0])
        assert thread.send.await_args.kwargs.get("view") is None
        assert "/clord" in said

    async def test_the_button_reattaches_and_reports_what_it_got_back(self, tmp_path) -> None:
        from c_lord.discord_ui.views import ReattachSessionView

        _seed(tmp_path, checkout=True, transcript=True)
        cog = _cog(tmp_path)
        thread = _thread()
        interaction = MagicMock(spec=discord.Interaction)
        interaction.response = MagicMock()
        interaction.response.defer = AsyncMock()
        interaction.followup = MagicMock()
        interaction.followup.send = AsyncMock()
        interaction.message = MagicMock()
        interaction.message.edit = AsyncMock()

        view = ReattachSessionView(lambda i: cog._reattach_thread(thread))
        await view.reattach_button.callback(interaction)

        cog.repo.save.assert_awaited_once()
        said = " ".join(str(c.args[0]) for c in interaction.followup.send.await_args_list if c.args)
        assert "再接続" in said

    async def test_a_second_click_does_not_reattach_twice(self, tmp_path) -> None:
        from c_lord.discord_ui.views import ReattachSessionView

        _seed(tmp_path, checkout=True, transcript=True)
        cog = _cog(tmp_path)
        thread = _thread()

        def _interaction():
            i = MagicMock(spec=discord.Interaction)
            i.response = MagicMock()
            i.response.defer = AsyncMock()
            i.response.send_message = AsyncMock()
            i.followup = MagicMock()
            i.followup.send = AsyncMock()
            i.message = MagicMock()
            i.message.edit = AsyncMock()
            return i

        view = ReattachSessionView(lambda i: cog._reattach_thread(thread))
        await view.reattach_button.callback(_interaction())
        await view.reattach_button.callback(_interaction())

        assert cog.repo.save.await_count == 1
