"""#671: a bot restart must not turn every open question menu into a dead button.

Production, 2026-09-01. The menu watchdog bridged a question to Discord at
16:48:53; the bot was redeployed at 17:01:22; at 17:29 the user pressed a button
on that menu and got Discord's red **"アプリケーションは時間内に応答しませんでした"**.
The bot log carried **not one line** about the press — nothing had a handler for
that ``custom_id`` any more, so nobody answered Discord's 3-second ACK. The user
gave up and sent an ordinary message, which the runner then had to Esc the menu
away to deliver (``17:29:52 Open menu detected before delivering new input``) —
so the question was not just unanswerable, it was destroyed.

The restart-recovery machinery already existed and was wired into ``on_ready``
(``pending_asks`` + ``_restore_pending_ask_views``). It had simply never run:
**all four routes that put a menu on screen go through ``bridge_pane_ask``, and
none of them recorded a row.** The only writer, ``collect_ask_answers``, needs
``StreamEvent.ask_questions``, which nothing in the tree ever sets. Measured on
production at ``e7015d6``: ``select count(*) from pending_asks`` = 0, and
``Restoring N pending AskUserQuestion view(s)`` appears 0 times across all 169
bot logs ever written.

These tests pin the three halves of the fix:

* **①** every bridged menu is recorded, and startup re-arms a live handler for it
  — so the click is ACKed and logged instead of timing out in silence;
* **②** the re-armed button **delivers**: the TUI menu is still open in the pane
  with Claude blocked on it, so the answer is typed straight into it — but only
  after the pane is re-read and confirmed to still be showing *that* question;
* **③** a menu the pane no longer shows is retired at startup rather than left
  looking clickable.

AC4 is pinned negatively throughout: recovery must **never post a menu**. Posting
one is what #633 fixed (188 re-bridges, the same ❓ six times over three days),
and the production bot restarts up to six times a day.
"""

from __future__ import annotations

import asyncio
import json
import logging
from unittest.mock import AsyncMock, MagicMock

import pytest

from c_lord.claude.types import FREE_TEXT_NOTES, AskOption, AskQuestion
from c_lord.database.ask_repo import PendingAskRecord
from c_lord.discord_ui import ask_handler
from c_lord.discord_ui.ask_bus import ask_bus
from c_lord.discord_ui.ask_handler import bridge_pane_ask
from c_lord.discord_ui.ask_view import AskView

THREAD_ID = 671_000_001
MESSAGE_ID = 671_999_001


def _question() -> AskQuestion:
    return AskQuestion(
        question="どちらの方針で進めますか?",
        header="W144 方針",
        options=[
            AskOption("確定ボタンを残す", "選ぶ→確定"),
            AskOption("即送信にする", "選んだ瞬間に送る"),
            AskOption("いまは決めない", ""),
        ],
    )


def _other_question() -> AskQuestion:
    """A *different* question — what the pane shows after the user moved on."""
    return AskQuestion(
        question="このPRをマージしますか?",
        header="マージ",
        options=[AskOption("する", ""), AskOption("しない", "")],
    )


def _record(question: AskQuestion, *, thread_id: int = THREAD_ID) -> PendingAskRecord:
    from c_lord.claude.types import ask_question_to_dict

    return PendingAskRecord(
        thread_id=thread_id,
        session_id="",
        questions_json=json.dumps([ask_question_to_dict(question)]),
        question_idx=0,
        created_at="2026-09-01 16:48:53",
        message_id=MESSAGE_ID,
    )


def _thread(thread_id: int = THREAD_ID) -> tuple[MagicMock, MagicMock]:
    thread = MagicMock()
    thread.id = thread_id
    msg = MagicMock()
    msg.id = MESSAGE_ID
    msg.edit = AsyncMock()
    thread.send = AsyncMock(return_value=msg)
    return thread, msg


def _open_pane_runner(question: AskQuestion | None) -> MagicMock:
    """A runner whose pane keeps showing *question* (or nothing when None)."""
    runner = MagicMock()
    runner.peek_pending_ask = AsyncMock(return_value=question)
    runner.peek_menu_state = AsyncMock(return_value=(question, True))
    runner.answer_menu = AsyncMock(return_value=True)
    runner.answer_menu_multi = AsyncMock(return_value=True)
    runner.answer_menu_text = AsyncMock(return_value=True)
    runner.cancel_menu = AsyncMock(return_value=True)
    return runner


def _repo() -> MagicMock:
    repo = MagicMock()
    repo.save = AsyncMock()
    repo.delete = AsyncMock()
    repo.list_all = AsyncMock(return_value=[])
    return repo


# ── ① the menu has to be written down before it can be restored ──────────────


class TestEveryBridgedMenuIsRecorded:
    """The bug in one assertion: the production path never saved anything."""

    @pytest.mark.asyncio
    async def test_pane_bridged_menu_is_recorded_so_a_restart_can_find_it(
        self, monkeypatch
    ) -> None:
        monkeypatch.setattr(ask_handler, "_PANE_RESOLVE_POLL", 0.01)
        thread, msg = _thread()
        repo = _repo()
        question = _question()

        task = asyncio.create_task(
            bridge_pane_ask(thread, question, _open_pane_runner(question), ask_repo=repo)
        )
        await asyncio.sleep(0.1)  # let it post the menu, then stop waiting for a click
        task.cancel()
        with pytest.raises(asyncio.CancelledError):
            await task
        ask_bus.unregister(thread.id)

        repo.save.assert_awaited(), (
            "bridge_pane_ask posted a menu without recording it — this is #671: "
            "the restart-recovery table stayed empty in production forever"
        )
        kwargs = repo.save.await_args.kwargs
        assert kwargs["thread_id"] == THREAD_ID
        assert kwargs["message_id"] == MESSAGE_ID, (
            "the message id is what lets startup retire a menu the pane has closed"
        )
        saved = kwargs["questions"][0]
        assert [o["label"] for o in saved["options"]] == [o.label for o in question.options], (
            "the option ORDER is how an answer is typed (Down × index) — it must survive"
        )

    @pytest.mark.asyncio
    async def test_the_record_carries_what_answering_needs(self) -> None:
        """``allow_other``/``free_text_mode`` decide which keystrokes deliver text.

        Dropping them would restore a menu that types the answer into the wrong
        affordance — the #650 failure, where every key landed and Claude still
        recorded "(No answer provided)".
        """
        from c_lord.claude.types import ask_question_from_dict, ask_question_to_dict

        original = AskQuestion(
            question="どうする?",
            header="plan",
            options=[AskOption("A", "one"), AskOption("B", "two")],
            multi_select=True,
            allow_other=False,
            free_text_mode=FREE_TEXT_NOTES,
        )
        restored = ask_question_from_dict(ask_question_to_dict(original))

        assert restored.allow_other is False
        assert restored.free_text_mode == FREE_TEXT_NOTES
        assert restored.multi_select is True
        assert [o.label for o in restored.options] == ["A", "B"]


class TestStartupReArmsTheButtons:
    """AC1: after a restart the click must reach code, not time out."""

    @pytest.mark.asyncio
    async def test_recovery_registers_a_live_handler_for_each_open_menu(self) -> None:
        from c_lord.ask_menu_recovery import recover_ask_menus

        repo = _repo()
        repo.list_all = AsyncMock(return_value=[_record(_question())])
        thread, _msg = _thread()
        bot = MagicMock()
        bot.add_view = MagicMock()
        bot.get_channel = MagicMock(return_value=thread)

        recovered = await recover_ask_menus(
            bot, repo, runner_factory=_never_a_runner_factory(_question())
        )

        assert recovered == 1
        assert bot.add_view.called, (
            "without add_view the restored menu's custom_id has no handler and "
            "Discord shows the red 3-second ACK timeout (#671)"
        )

    @pytest.mark.asyncio
    async def test_recovery_says_so_in_the_log(self, caplog) -> None:
        """AC3: the press must stop being invisible — starting with the re-arm."""
        from c_lord.ask_menu_recovery import recover_ask_menus

        repo = _repo()
        repo.list_all = AsyncMock(return_value=[_record(_question())])
        bot = MagicMock()
        bot.get_channel = MagicMock(return_value=_thread()[0])

        with caplog.at_level(logging.INFO):
            await recover_ask_menus(bot, repo, runner_factory=_never_a_runner_factory(_question()))

        assert any("671" in r.message or "re-armed" in r.message for r in caplog.records), (
            "a silent recovery is indistinguishable from no recovery — #585"
        )

    @pytest.mark.asyncio
    async def test_recovery_never_posts_a_menu(self) -> None:
        """AC4: #633 must not come back through the recovery door.

        A second copy of an open menu is exactly what #633 removed. Recovery
        re-arms the message that is **already on screen**; it must never send.
        """
        from c_lord.ask_menu_recovery import recover_ask_menus

        repo = _repo()
        repo.list_all = AsyncMock(return_value=[_record(_question())])
        thread, _msg = _thread()
        bot = MagicMock()
        bot.get_channel = MagicMock(return_value=thread)

        await recover_ask_menus(bot, repo, runner_factory=_never_a_runner_factory(_question()))

        thread.send.assert_not_called(), (
            "recovery posted a new menu — that is #633 (188 re-bridges, the same "
            "question six times over three days) reintroduced"
        )


# ── ② the re-armed button has to actually deliver ────────────────────────────


def _never_a_runner_factory(pane_question: AskQuestion | None):
    """A factory handing out a runner whose pane shows *pane_question*."""
    runner = _open_pane_runner(pane_question)

    async def factory(thread_id: int):
        return runner

    factory.runner = runner  # type: ignore[attr-defined]
    return factory


class TestTheRestoredButtonDelivers:
    """The pane menu is still open and Claude is still blocked on it."""

    @pytest.mark.asyncio
    async def test_click_types_the_answer_into_the_still_open_pane(self) -> None:
        from c_lord.ask_menu_recovery import PaneMenuAnswerer

        question = _question()
        factory = _never_a_runner_factory(question)
        answerer = PaneMenuAnswerer(
            thread_id=THREAD_ID, question=question, runner_factory=factory
        )

        delivered, _reason = await answerer(["即送信にする"])

        assert delivered is True
        factory.runner.answer_menu.assert_awaited_with(1), (
            "the answer must be typed at the option's INDEX — the pane menu is "
            "navigated with Down × index, not by label"
        )

    @pytest.mark.asyncio
    async def test_it_refuses_when_the_pane_moved_on_to_another_question(self) -> None:
        """Typing into whatever menu happens to be open would answer the wrong one."""
        from c_lord.ask_menu_recovery import PaneMenuAnswerer

        factory = _never_a_runner_factory(_other_question())
        answerer = PaneMenuAnswerer(
            thread_id=THREAD_ID, question=_question(), runner_factory=factory
        )

        delivered, reason = await answerer(["即送信にする"])

        assert delivered is False
        assert reason, "a refusal has to explain itself to the person who clicked"
        factory.runner.answer_menu.assert_not_called()

    @pytest.mark.asyncio
    async def test_it_refuses_when_the_pane_shows_no_menu_at_all(self) -> None:
        from c_lord.ask_menu_recovery import PaneMenuAnswerer

        factory = _never_a_runner_factory(None)
        answerer = PaneMenuAnswerer(
            thread_id=THREAD_ID, question=_question(), runner_factory=factory
        )

        delivered, _reason = await answerer(["即送信にする"])

        assert delivered is False
        factory.runner.answer_menu.assert_not_called()

    @pytest.mark.asyncio
    async def test_view_falls_back_to_the_pane_when_no_session_is_waiting(self) -> None:
        """The end-to-end shape of #671: press → bus is empty → pane gets the keys.

        After a restart there is no ``ask_bus`` waiter (the coroutine that would
        have received the answer died with the old process), which is precisely
        when the old code gave up and said "session lost".
        """
        question = _question()
        recovery = AsyncMock(return_value=(True, ""))
        view = AskView(question, thread_id=THREAD_ID, q_idx=0, recovery=recovery)
        ask_bus.unregister(THREAD_ID)  # nobody is waiting — as after a restart

        interaction = MagicMock()
        interaction.message = MagicMock(id=MESSAGE_ID)
        interaction.response.edit_message = AsyncMock()

        button = next(c for c in view.children if getattr(c, "label", "") == "即送信にする")
        await button.callback(interaction)

        recovery.assert_awaited(), (
            "the bus had no waiter and the view gave up — that is the restart "
            "behaviour #671 is about; the still-open pane was never tried"
        )
        assert recovery.await_args.args[0] == ["即送信にする"]

    @pytest.mark.asyncio
    async def test_the_press_is_logged_even_when_it_cannot_be_delivered(self, caplog) -> None:
        """AC3: 'nothing in the log' is the half of #671 that hid it for a month."""
        recovery = AsyncMock(return_value=(False, "この質問はもう開いていません"))
        view = AskView(_question(), thread_id=THREAD_ID, q_idx=0, recovery=recovery)
        ask_bus.unregister(THREAD_ID)

        interaction = MagicMock()
        interaction.message = MagicMock(id=MESSAGE_ID)
        interaction.response.edit_message = AsyncMock()

        with caplog.at_level(logging.INFO):
            button = next(c for c in view.children if getattr(c, "label", "") == "即送信にする")
            await button.callback(interaction)

        assert any("671" in r.message or "recovery" in r.message for r in caplog.records)


# ── ③ a menu the pane has closed must stop looking clickable ─────────────────


class TestClosedMenusAreRetired:
    @pytest.mark.asyncio
    async def test_a_menu_the_pane_no_longer_shows_is_disabled_and_forgotten(self) -> None:
        from c_lord.ask_menu_recovery import recover_ask_menus

        repo = _repo()
        repo.list_all = AsyncMock(return_value=[_record(_question())])
        thread, msg = _thread()
        thread.fetch_message = AsyncMock(return_value=msg)
        bot = MagicMock()
        bot.get_channel = MagicMock(return_value=thread)

        await recover_ask_menus(bot, repo, runner_factory=_never_a_runner_factory(None))

        msg.edit.assert_awaited(), (
            "a menu whose pane is menu-free is dead — leaving it clickable is the "
            "'looks live and is not' state #634 removed for ⏹ Stop"
        )
        repo.delete.assert_awaited_with(THREAD_ID)
        thread.send.assert_not_called()  # AC4 again: retire, never re-post


# ── AC5: one startup entry point, not two ────────────────────────────────────


class TestOneStartupEntryPoint:
    @pytest.mark.asyncio
    async def test_startup_recovery_covers_stop_buttons_and_ask_menus(
        self, monkeypatch
    ) -> None:
        """#634's sweep and #671's re-arm are the same job: retire the previous
        process's dead UI. Splitting them across two ``on_ready`` handlers is how
        one of them silently never ran for a month."""
        import c_lord.startup_recovery as sr

        swept = AsyncMock(return_value=0)
        recovered = AsyncMock(return_value=0)
        monkeypatch.setattr(sr, "sweep_dead_stop_buttons", swept)
        monkeypatch.setattr(sr, "recover_ask_menus", recovered)

        await sr.run_startup_recovery(MagicMock(), MagicMock(), MagicMock())

        swept.assert_awaited()
        recovered.assert_awaited()
