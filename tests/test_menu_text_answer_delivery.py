"""文章で答えた回答が「送りました」と言われたまま消える経路を塞ぐ (#804).

2026-09-24、`W1 │ ゲーム管理` で yousan が開いている質問に文章で答えたところ、
Discord には ``✏️ この文章を、開いていた質問への回答として送りました`` が出たのに、
その2秒後に ``⚠️ 選んだ回答を Claude に届けられませんでした`` が続き、ペインには
``User declined to answer questions`` が記録されてターンが終わった。

3つの欠陥が重なっている:

* ① ``ask_bus.post_answer`` は**プロセス内キューに積むだけ**なのに、その直後に
  「送りました」と断言していた（#651 はボタン経路で同じ問題を直したが、文章経路が
  残っていた）。
* ② tmux ウィンドウが無いまま送ろうとしていた。``wake_workspace()`` (#642) が
  すでにあるのにこの経路からは呼ばれない。
* ③ 届かなかった結果が Claude 側に「利用者が拒否した」として渡り、ターンが終わる。

ここでの固定点は「**利用者の文章は必ずどこかに届く**」こと。メニューに届かなかった
なら、新しい指示として届く（``_maybe_answer_open_menu`` が False を返し、呼び出し元
が通常の指示として走らせる）。
"""

from __future__ import annotations

import asyncio
from typing import Any
from unittest.mock import AsyncMock, MagicMock

import pytest

from c_lord.claude.types import AskOption, AskQuestion
from c_lord.cogs.claude_chat import ClaudeChatCog
from c_lord.discord_ui import ask_handler
from c_lord.discord_ui.ask_bus import (
    DELIVERY_DELIVERED,
    DELIVERY_NOT_ANSWERED,
    DELIVERY_UNDELIVERED,
    AskAnswerBus,
    ask_bus,
)

# 「届いた」と断言している文面。これが出たのに届いていない、が #804。
CLAIMED_DELIVERED = "送りました"


def _thread(thread_id: int) -> Any:
    """A stand-in for the Discord thread, typed loosely for the cog's signature."""
    return _Thread(thread_id)


class _Thread:
    """A Discord thread that remembers everything c-lord showed the user.

    ``sent`` is what was posted, in order; ``final`` is what each of those
    messages says *now* (edits applied). #804 is about the two disagreeing, so
    the assertions need both.
    """

    def __init__(self, thread_id: int) -> None:
        self.id = thread_id
        self.parent_id = 804_9999
        self.sent: list[str] = []
        self.final: list[str] = []

    async def send(self, content=None, **_kwargs):  # noqa: ANN001, ANN201
        idx = len(self.final)
        self.sent.append(content or "")
        self.final.append(content or "")
        thread = self

        class _Msg:
            id = 900_000 + idx

            async def edit(self, content=None, **_kw):  # noqa: ANN001, ANN202
                thread.final[idx] = content or ""
                return self

        return _Msg()

    def says(self, needle: str) -> bool:
        """True when the thread, as it reads now, contains *needle*."""
        return any(needle in text for text in self.final)


def _cog(*, window: str | None = "w1", wake: bool = True) -> Any:
    """A cog wired with just enough to run ``_maybe_answer_open_menu``.

    *window* is what ``TmuxSessionManager.window_name`` reports — ``None`` is the
    #804 condition (the pane the menu lives in is gone).
    """
    cog = ClaudeChatCog.__new__(ClaudeChatCog)
    cog.bot = MagicMock()
    cog._authorizer = None
    cog.wake_workspace = AsyncMock(return_value=wake)
    tmux = MagicMock()
    tmux.window_name = MagicMock(return_value=window)
    cog._resolve_tmux_manager = AsyncMock(return_value=tmux)
    return cog


def _message(thread: Any, content: str) -> Any:
    message = MagicMock()
    message.content = content
    message.attachments = []
    message.channel = thread
    return message


async def _fake_bridge(thread_id: int, queue: asyncio.Queue, verdict: str) -> list[str]:
    """Stand in for the bridge: take the answer, report what became of it."""
    answers = await queue.get()
    await asyncio.sleep(0)
    ask_bus.note_delivery(thread_id, verdict)
    return answers


class TestTheClaimWaitsForTheAnswerToLand:
    """AC1 / AC4 — 「送りました」は届いてからでないと言わない."""

    @pytest.mark.asyncio
    async def test_no_success_message_when_the_answer_never_reached_the_tui(self) -> None:
        """再現手順 4〜5: 「送りました」と「届けられませんでした」が両方出ていた."""
        tid = 804_0001
        queue = ask_bus.register(tid, allow_free_text=True)
        assert queue is not None
        try:
            thread = _thread(tid)
            bridge = asyncio.create_task(_fake_bridge(tid, queue, DELIVERY_UNDELIVERED))
            handled = await _cog()._maybe_answer_open_menu(
                _message(thread, "ルータでポート開放で"), thread
            )
            assert await bridge == ["ルータでポート開放で"]

            assert not thread.says(CLAIMED_DELIVERED), (
                "届いていないのに「送りました」と言っている — これが #804 の中心"
            )
            assert handled is False, (
                "届かなかった文章は消費してはいけない — 新しい指示として走らせる (AC3)"
            )
        finally:
            ask_bus.unregister(tid)

    @pytest.mark.asyncio
    async def test_a_delivered_answer_is_still_reported_as_delivered(self) -> None:
        """GREEN 側: 本当に届いたときは今までどおり「送りました」と出る."""
        tid = 804_0002
        queue = ask_bus.register(tid, allow_free_text=True)
        assert queue is not None
        try:
            thread = _thread(tid)
            bridge = asyncio.create_task(_fake_bridge(tid, queue, DELIVERY_DELIVERED))
            handled = await _cog()._maybe_answer_open_menu(
                _message(thread, "おぷーのままで"), thread
            )
            await bridge

            assert handled is True
            assert thread.says(CLAIMED_DELIVERED)
            assert thread.says("おぷーのままで"), "何を回答として送ったのかが残らない"
        finally:
            ask_bus.unregister(tid)

    @pytest.mark.asyncio
    async def test_a_verdict_that_never_arrives_is_not_reported_as_delivered(
        self, monkeypatch
    ) -> None:
        """誰も結果を報告しないなら、断言せずに「確認できていない」と言う."""
        import c_lord.cogs.claude_chat as cc

        monkeypatch.setattr(cc, "_ANSWER_DELIVERY_WAIT", 0.05)
        tid = 804_0003
        queue = ask_bus.register(tid, allow_free_text=True)
        assert queue is not None
        try:
            thread = _thread(tid)
            handled = await _cog()._maybe_answer_open_menu(_message(thread, "それで"), thread)

            assert queue.get_nowait() == ["それで"]
            assert handled is True, "届いた可能性がある以上、二重送信はしない"
            assert thread.says("確認できて"), "確認できていないことを言っていない"
        finally:
            ask_bus.unregister(tid)

    @pytest.mark.asyncio
    async def test_claude_recording_no_answer_is_not_a_success_either(self) -> None:
        """キーは届いても Claude が「回答なし」と記録したなら、送れていない (#650)."""
        tid = 804_0004
        queue = ask_bus.register(tid, allow_free_text=True)
        assert queue is not None
        try:
            thread = _thread(tid)
            bridge = asyncio.create_task(_fake_bridge(tid, queue, DELIVERY_NOT_ANSWERED))
            handled = await _cog()._maybe_answer_open_menu(_message(thread, "Aで"), thread)
            await bridge

            assert not thread.says(CLAIMED_DELIVERED)
            assert handled is False
        finally:
            ask_bus.unregister(tid)


class TestAStoppedWorkspaceIsRestoredFirst:
    """AC2 — 窓が無ければ ``wake_workspace()`` で復元してから送る."""

    @pytest.mark.asyncio
    async def test_a_missing_window_restores_the_workspace(self) -> None:
        tid = 804_0011
        queue = ask_bus.register(tid, allow_free_text=True)
        assert queue is not None
        try:
            thread = _thread(tid)
            cog = _cog(window=None)
            handled = await cog._maybe_answer_open_menu(_message(thread, "1番で"), thread)

            cog.wake_workspace.assert_awaited_once_with(thread)
            assert handled is False, (
                "復元後のペインには元のメニューが無い — 文章は新しい指示として届ける"
            )
            assert queue.empty(), "届くはずのないメニューに回答を積んではいけない"
        finally:
            ask_bus.unregister(tid)

    @pytest.mark.asyncio
    async def test_the_restore_is_not_silent(self) -> None:
        """#642 と同じ作法: 数秒かかる復元を無言で待たせない."""
        tid = 804_0012
        ask_bus.register(tid, allow_free_text=True)
        try:
            thread = _thread(tid)
            await _cog(window=None)._maybe_answer_open_menu(_message(thread, "1番で"), thread)

            assert any("復元" in text for text in thread.sent), (
                f"復元中であることが1行も出ていない: {thread.sent}"
            )
        finally:
            ask_bus.unregister(tid)

    @pytest.mark.asyncio
    async def test_a_live_window_is_left_alone(self) -> None:
        """窓があるなら復元は要らない — 走っている Claude を触らない."""
        tid = 804_0013
        queue = ask_bus.register(tid, allow_free_text=True)
        assert queue is not None
        try:
            thread = _thread(tid)
            cog = _cog(window="w3")
            bridge = asyncio.create_task(_fake_bridge(tid, queue, DELIVERY_DELIVERED))
            await cog._maybe_answer_open_menu(_message(thread, "2番で"), thread)
            await bridge

            cog.wake_workspace.assert_not_awaited()
        finally:
            ask_bus.unregister(tid)


class TestTheContradictoryPairIsGone:
    """AC4 — 再現手順 4〜5 の2通が同時に出ないことを、実物の bridge で固定する."""

    @pytest.mark.asyncio
    async def test_no_window_produces_one_story_not_two(self, monkeypatch) -> None:
        monkeypatch.setattr(ask_handler, "_PANE_RESOLVE_POLL", 0.01)
        monkeypatch.setattr(ask_handler, "_ANSWER_CONFIRM_TIMEOUT", 0.05)
        monkeypatch.setattr(ask_handler, "_ANSWER_CONFIRM_POLL", 0.01)
        tid = 804_0021
        question = AskQuestion(
            question="どれで進めますか？",
            header="Factorio",
            options=[
                AskOption("ルータでポート開放", "推奨"),
                AskOption("VPS 経由でリレー", ""),
                AskOption("説明文に playit を載せる", ""),
                AskOption("まず確実に壊れてるか再検証", ""),
            ],
        )
        thread = _thread(tid)
        runner = MagicMock()
        runner.peek_pending_ask = AsyncMock(return_value=question)
        runner.peek_menu_state = AsyncMock(return_value=(question, True))
        # #600 の条件: 窓が無いので、どのキーストロークも届かない。
        runner.answer_menu = AsyncMock(return_value=False)
        runner.answer_menu_multi = AsyncMock(return_value=False)
        runner.answer_menu_text = AsyncMock(return_value=False)
        runner.cancel_menu = AsyncMock(return_value=False)
        runner.transcript_project_dir = AsyncMock(return_value=None)

        bridge = asyncio.create_task(ask_handler.bridge_pane_ask(thread, question, runner))
        for _ in range(200):  # wait for the bridge to claim the menu
            if ask_bus.accepts_free_text(tid):
                break
            await asyncio.sleep(0.01)
        assert ask_bus.accepts_free_text(tid)

        handled = await _cog()._maybe_answer_open_menu(
            _message(thread, "ルータでポート開放で進めて"), thread
        )
        await asyncio.wait_for(bridge, timeout=5.0)

        assert not thread.says(CLAIMED_DELIVERED), (
            f"「送りました」と「届きませんでした」が両方出ている: {thread.final}"
        )
        assert any("届け" in text for text in thread.final), "届かなかったことが出ていない"
        assert handled is False


class TestTheQuestionSurvivesTheFailure:
    """AC5 — 失敗時に embed を上書きしても、何を聞かれたかが残る."""

    def test_the_undelivered_embed_still_lists_the_options(self) -> None:
        from c_lord.discord_ui.embeds import ask_undelivered_embed

        options = [
            AskOption("ルータでポート開放", "推奨"),
            AskOption("VPS 経由でリレー", ""),
            AskOption("説明文に playit を載せる", ""),
            AskOption("まず確実に壊れてるか再検証", ""),
        ]
        embed = ask_undelivered_embed(
            "どれで進めますか？", "Factorio", ["ルータでポート開放"], "窓がない", options=options
        )
        body = embed.description or ""
        for option in options:
            assert option.label in body, f"選択肢 {option.label!r} が履歴から消えている"

    def test_the_unconfirmed_embed_still_lists_the_options(self) -> None:
        from c_lord.discord_ui.embeds import ask_unconfirmed_embed

        options = [AskOption("はい", ""), AskOption("いいえ", "")]
        embed = ask_unconfirmed_embed("進めますか？", "確認", ["はい"], options=options)
        body = embed.description or ""
        assert "いいえ" in body


class TestTheButtonPathIsUnchanged:
    """AC6 — #651 で直したボタン経路の挙動は変えない."""

    def test_reporting_a_verdict_nobody_waits_for_is_a_no_op(self) -> None:
        bus = AskAnswerBus()
        bus.note_delivery(12345, DELIVERY_DELIVERED)  # must not raise

    @pytest.mark.asyncio
    async def test_a_button_click_still_answers_the_menu(self, monkeypatch) -> None:
        """クリック → キーストローク → ✅、という #651 の流れがそのまま残る."""
        monkeypatch.setattr(ask_handler, "_PANE_RESOLVE_POLL", 0.01)
        monkeypatch.setattr(ask_handler, "_ANSWER_CONFIRM_TIMEOUT", 0.05)
        monkeypatch.setattr(ask_handler, "_ANSWER_CONFIRM_POLL", 0.01)
        tid = 804_0031
        question = AskQuestion(
            question="repro?",
            header="テスト",
            options=[AskOption("A1", "one"), AskOption("A2", "two")],
        )
        thread = _thread(tid)
        runner = MagicMock()
        runner.peek_pending_ask = AsyncMock(return_value=question)
        runner.peek_menu_state = AsyncMock(return_value=(question, True))
        runner.answer_menu = AsyncMock(return_value=True)
        runner.answer_menu_text = AsyncMock(return_value=True)
        runner.cancel_menu = AsyncMock(return_value=True)
        runner.transcript_project_dir = AsyncMock(return_value=None)

        async def _click_soon() -> None:
            for _ in range(200):
                if ask_bus.is_active(tid):
                    break
                await asyncio.sleep(0.01)
            ask_bus.post_answer(tid, ["A2"])

        await asyncio.gather(
            asyncio.wait_for(ask_handler.bridge_pane_ask(thread, question, runner), timeout=5.0),
            _click_soon(),
        )

        runner.answer_menu.assert_awaited_once_with(1)
        # ボタン経路は誰も delivery verdict を待たないので、余計な投稿は増えない。
        assert not any("確認できて" in text for text in thread.sent)


class TestTheDeliveryChannel:
    """バス側の最小単位: 誰が待っていて、誰に報告するか."""

    @pytest.mark.asyncio
    async def test_a_watcher_receives_the_verdict(self) -> None:
        bus = AskAnswerBus()
        queue = bus.watch_delivery(7)
        bus.note_delivery(7, DELIVERY_UNDELIVERED)
        assert await asyncio.wait_for(queue.get(), timeout=1.0) == DELIVERY_UNDELIVERED

    @pytest.mark.asyncio
    async def test_unwatching_stops_the_reports(self) -> None:
        bus = AskAnswerBus()
        queue = bus.watch_delivery(7)
        bus.unwatch_delivery(7)
        bus.note_delivery(7, DELIVERY_DELIVERED)
        assert queue.empty()

    def test_verdicts_are_distinct(self) -> None:
        assert len({DELIVERY_DELIVERED, DELIVERY_UNDELIVERED, DELIVERY_NOT_ANSWERED}) == 3
