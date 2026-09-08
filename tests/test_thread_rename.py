"""スレッド名の自動要約を既定オフにし、`/thread-rename` で明示的に呼ぶ — Issue #705.

**Why**: サイドバーは利用者が自分のスレッドを見分けるための場所なので、そこが
意図と無関係に書き換わると探していたスレッドが見つからなくなる。#414 で会話途中の
付け替え (``maybe_retitle``) は既定オフになったが、**初回の LLM 要約**は既定オンの
ままだった。#705 でそれも既定オフにし、要約は利用者が明示的に呼んだときだけ走る。

ここで固定する挙動:

* 既定 (env 無設定) では ``topic.generate_topic`` (LLM) が **呼ばれない**。
  トピックはスレッドに既に付いている名前から取る — つまり利用者が付けた名前は残る
* ``CLORD_AUTO_TOPIC=1`` で旧挙動 (初回 LLM 要約) に戻せる
* ``/thread-rename`` / ``!thread-rename`` が明示的に要約し直す。モデルは **sonnet**
* 要約後も ``W<番号> │`` / ``<session>:W<番号> │`` / ``#<番号>`` / ``[停止]`` /
  ``→#<番号>`` の装飾は保たれる (#618, #593, #512)
* ``auto_topic_locked`` (#95) は自動経路を止めるだけ — コマンドは効く
"""

from __future__ import annotations

import asyncio
from unittest.mock import AsyncMock, MagicMock, patch

import discord
import pytest

from c_lord import thread_rename, topic
from c_lord.cogs.session_manage import SessionManageCog
from c_lord.thread_name import replace_topic_in_name, topic_auto_enabled


# ── AC1: 既定オフのスイッチそのもの ────────────────────────────────────────────
class TestTopicAutoEnabled:
    def test_off_by_default(self, monkeypatch) -> None:
        monkeypatch.delenv("CLORD_AUTO_TOPIC", raising=False)
        assert topic_auto_enabled() is False

    @pytest.mark.parametrize("value", ["1", "true", "yes", "on", "ON"])
    def test_env_opts_in(self, monkeypatch, value: str) -> None:
        monkeypatch.setenv("CLORD_AUTO_TOPIC", value)
        assert topic_auto_enabled() is True

    @pytest.mark.parametrize("value", ["0", "false", "no", "off", ""])
    def test_env_off_values(self, monkeypatch, value: str) -> None:
        monkeypatch.setenv("CLORD_AUTO_TOPIC", value)
        assert topic_auto_enabled() is False

    def test_explicit_argument_wins(self, monkeypatch) -> None:
        monkeypatch.setenv("CLORD_AUTO_TOPIC", "1")
        assert topic_auto_enabled(False) is False
        monkeypatch.delenv("CLORD_AUTO_TOPIC", raising=False)
        assert topic_auto_enabled(True) is True


# ── AC1: 命名パスが既定で LLM を呼ばない ───────────────────────────────────────
class TestNamingPassDoesNotSummarizeByDefault:
    """``_apply_thread_naming`` の初回命名が、既定では LLM を呼ばないこと。"""

    def _make(self, *, thread_name: str, locked: int = 0):
        from tests.test_claude_chat import _make_cog

        cog = _make_cog()
        record = MagicMock()
        record.topic = None
        record.auto_topic_locked = locked
        record.state = "running"
        record.tmux_window_id = "@1"
        record.issue_ref = None
        record.origin_issue_ref = None
        cog.repo.get = AsyncMock(return_value=record)
        cog.repo.set_topic = AsyncMock()
        cog.repo.set_tmux_window_id = AsyncMock()
        cog.repo.set_issue_ref = AsyncMock()

        thread = MagicMock(spec=discord.Thread)
        thread.id = 55555
        thread.name = thread_name
        thread.edit = AsyncMock()

        tmux = MagicMock()
        tmux.get_window_info = MagicMock(return_value=("@1", 1))
        return cog, thread, tmux

    @pytest.mark.asyncio
    async def test_default_keeps_the_users_own_name(self, monkeypatch) -> None:
        """既定: LLM 要約は呼ばれず、いまの名前がそのままトピックになる。"""
        monkeypatch.delenv("CLORD_AUTO_TOPIC", raising=False)
        from c_lord.cogs import claude_chat as cc

        cog, thread, tmux = self._make(thread_name="設計相談")
        with patch.object(
            cc.topic_module, "generate_topic", new=AsyncMock(return_value=("LLM要約", "llm"))
        ) as generate:
            await cog._apply_thread_naming(
                thread=thread, tmux_manager=tmux, first_message="スレッド名の件を相談したいです"
            )

        generate.assert_not_awaited()
        cog.repo.set_topic.assert_awaited_once()
        assert cog.repo.set_topic.await_args.args[1] == "設計相談"
        name = thread.edit.await_args.kwargs["name"]
        assert "設計相談" in name
        assert "LLM要約" not in name

    @pytest.mark.asyncio
    async def test_opt_in_restores_the_llm_summary(self, monkeypatch) -> None:
        """``CLORD_AUTO_TOPIC=1`` で旧挙動 (初回 LLM 要約) に戻る。"""
        monkeypatch.setenv("CLORD_AUTO_TOPIC", "1")
        from c_lord.cogs import claude_chat as cc

        cog, thread, tmux = self._make(thread_name="設計相談")
        with patch.object(
            cc.topic_module, "generate_topic", new=AsyncMock(return_value=("LLM要約", "llm"))
        ) as generate:
            await cog._apply_thread_naming(
                thread=thread, tmux_manager=tmux, first_message="スレッド名の件を相談したいです"
            )

        generate.assert_awaited_once()
        assert cog.repo.set_topic.await_args.args[1] == "LLM要約"

    @pytest.mark.asyncio
    async def test_locked_thread_is_untouched(self, monkeypatch) -> None:
        """AC4: 手で付けた名前 (locked) は既定経路で書き換えない。"""
        monkeypatch.delenv("CLORD_AUTO_TOPIC", raising=False)
        cog, thread, tmux = self._make(thread_name="手で付けた名前", locked=1)
        await cog._apply_thread_naming(
            thread=thread, tmux_manager=tmux, first_message="なにか長めの指示メッセージ"
        )
        cog.repo.set_topic.assert_not_awaited()


class TestInitialTopic:
    """LLM を使わない初期トピックの決め方。"""

    def test_prefers_the_existing_thread_name(self) -> None:
        assert topic.initial_topic("W3 │ #404 認証リファクタ", "本文") == "認証リファクタ"

    def test_falls_back_to_the_first_message(self) -> None:
        assert topic.initial_topic("", "スレッド名の要約を止めたい") == "スレッド名の要約を止めたい"

    def test_never_empty(self) -> None:
        assert topic.initial_topic("", "") == "新しいスレッド"

    def test_caps_at_twenty_chars(self) -> None:
        assert len(topic.initial_topic("あ" * 80, "")) == 20


# ── AC5: 装飾を壊さずにトピックだけ差し替える ─────────────────────────────────
class TestReplaceTopicInName:
    @pytest.mark.parametrize(
        ("name", "expected"),
        [
            ("認証リファクタ", "新トピック"),
            ("W3 │ 認証リファクタ", "W3 │ 新トピック"),
            ("W3 │ #404 認証リファクタ", "W3 │ #404 新トピック"),
            ("🟢 W3 │ #404 認証リファクタ", "🟢 W3 │ #404 新トピック"),
            ("qiita-article:W1 │ #598 記事執筆", "qiita-article:W1 │ #598 新トピック"),
            ("W131 │ #540 メモリ →#588", "W131 │ #540 新トピック →#588"),
            ("[停止] #404 認証リファクタ", "[停止] #404 新トピック"),
            ("[終了] #404 認証リファクタ", "[終了] #404 新トピック"),
        ],
    )
    def test_decorations_survive(self, name: str, expected: str) -> None:
        assert replace_topic_in_name(name, "新トピック") == expected

    def test_respects_the_length_cap(self) -> None:
        from c_lord.thread_name import MAX_NAME_LEN

        out = replace_topic_in_name("W3 │ #404 みじかい", "あ" * 60)
        assert len(out) <= MAX_NAME_LEN
        assert out.startswith("W3 │ #404 ")

    def test_empty_topic_leaves_the_name_alone(self) -> None:
        assert replace_topic_in_name("W3 │ #404 認証", "  ") == "W3 │ #404 認証"


# ── AC3: コマンド経由の要約は sonnet ───────────────────────────────────────────
class TestCommandSummaryUsesSonnet:
    @pytest.mark.asyncio
    async def test_summarize_thread_passes_sonnet(self) -> None:
        seen: dict[str, str] = {}

        async def fake_call(prompt: str, *, model: str = "haiku") -> str:
            seen["model"] = model
            seen["prompt"] = prompt
            return "認証リファクタ"

        with patch.object(topic, "_call_claude_p", fake_call):
            result = await topic.summarize_thread("yousan: スレッド名の話をしています")

        assert result == "認証リファクタ"
        assert seen["model"] == "sonnet"
        assert "スレッド名の話" in seen["prompt"]

    @pytest.mark.asyncio
    async def test_cli_argv_says_model_sonnet(self) -> None:
        """実際に ``claude -p --model sonnet`` を起動すること。"""
        captured: list[str] = []

        async def fake_exec(*args, **kwargs):
            captured.extend(args)
            proc = MagicMock()
            proc.returncode = 0
            proc.communicate = AsyncMock(return_value=("認証リファクタ\n".encode(), b""))
            return proc

        with patch.object(asyncio, "create_subprocess_exec", fake_exec):
            result = await topic.summarize_thread("会話ログ")

        assert result == "認証リファクタ"
        assert captured[:4] == ["claude", "-p", "--model", "sonnet"]
        assert "--" in captured

    @pytest.mark.asyncio
    async def test_auto_path_still_uses_haiku(self) -> None:
        """既定経路 (オプトイン時) のモデルは haiku のまま — 回帰よけ。"""
        captured: list[str] = []

        async def fake_exec(*args, **kwargs):
            captured.extend(args)
            proc = MagicMock()
            proc.returncode = 0
            proc.communicate = AsyncMock(return_value=("要約\n".encode(), b""))
            return proc

        with patch.object(asyncio, "create_subprocess_exec", fake_exec):
            await topic._invoke_claude_haiku("初回メッセージ")

        assert captured[:4] == ["claude", "-p", "--model", "haiku"]


# ── AC2: /thread-rename と !thread-rename の双子 ───────────────────────────────
def _history(messages: list[MagicMock]):
    class _Aiter:
        def __aiter__(self):
            async def gen():
                for m in messages:
                    yield m

            return gen()

    return _Aiter()


def _message(content: str, author: str = "yousan") -> MagicMock:
    msg = MagicMock()
    msg.content = content
    msg.author = MagicMock()
    msg.author.display_name = author
    return msg


def _make_thread(name: str = "W3 │ #404 認証リファクタ", messages=None) -> MagicMock:
    thread = MagicMock(spec=discord.Thread)
    thread.id = 55555
    thread.name = name
    thread.edit = AsyncMock()
    thread.history = MagicMock(
        return_value=_history(messages if messages is not None else [_message("認可の設計の話")])
    )
    return thread


def _make_cog(record: MagicMock | None = None) -> SessionManageCog:
    bot = MagicMock()
    repo = MagicMock()
    repo.get = AsyncMock(return_value=record)
    repo.set_topic = AsyncMock()
    return SessionManageCog(bot=bot, repo=repo)


class TestThreadRenameCommand:
    def test_both_twins_are_registered(self) -> None:
        slash = {c.name for c in SessionManageCog.__cog_app_commands__}
        text = {c.name for c in SessionManageCog.__cog_commands__}
        assert "thread-rename" in slash, "/thread-rename (slash) が無い"
        assert "thread-rename" in text, "!thread-rename (text twin) が無い"

    def test_twins_share_one_implementation(self) -> None:
        assert hasattr(SessionManageCog, "_thread_rename_impl")

    @pytest.mark.asyncio
    async def test_renames_and_persists(self) -> None:
        record = MagicMock()
        record.topic = "認証リファクタ"
        record.auto_topic_locked = 0
        cog = _make_cog(record)
        thread = _make_thread()
        sent: list[str] = []

        async def respond(content=None, **kwargs):
            sent.append(content or "")

        with patch.object(
            thread_rename.topic_module, "summarize_thread", new=AsyncMock(return_value="認可の設計")
        ):
            await cog._thread_rename_impl(channel=thread, respond=respond, ack=AsyncMock())

        thread.edit.assert_awaited_once()
        assert thread.edit.await_args.kwargs["name"] == "W3 │ #404 認可の設計"
        cog.repo.set_topic.assert_awaited_once()
        assert cog.repo.set_topic.await_args.args[1] == "認可の設計"
        assert any("認可の設計" in s for s in sent)

    @pytest.mark.asyncio
    async def test_works_on_a_locked_thread(self) -> None:
        """AC4: 明示的に打ったコマンドは、手動リネーム済み (locked) でも効く。"""
        record = MagicMock()
        record.topic = "手で付けた名前"
        record.auto_topic_locked = 1
        cog = _make_cog(record)
        thread = _make_thread(name="手で付けた名前")

        with patch.object(
            thread_rename.topic_module, "summarize_thread", new=AsyncMock(return_value="新しい要約")
        ):
            await cog._thread_rename_impl(channel=thread, respond=AsyncMock(), ack=AsyncMock())

        thread.edit.assert_awaited_once()
        assert thread.edit.await_args.kwargs["name"] == "新しい要約"

    @pytest.mark.asyncio
    async def test_reply_cannot_ping_the_channel(self) -> None:
        """要約は会話由来なので、返信に混ざった @everyone がそのまま鳴らないこと。"""
        cog = _make_cog()
        thread = _make_thread(name="W3 │ 認証")
        sent: list[str] = []

        async def respond(content=None, **kwargs):
            sent.append(content or "")

        with patch.object(
            thread_rename.topic_module, "summarize_thread", new=AsyncMock(return_value="@everyone")
        ):
            await cog._thread_rename_impl(channel=thread, respond=respond, ack=AsyncMock())

        assert sent and "@everyone" not in sent[0]

    @pytest.mark.asyncio
    async def test_outside_a_thread_is_refused(self) -> None:
        cog = _make_cog()
        channel = MagicMock(spec=discord.TextChannel)
        sent: list[str] = []

        async def respond(content=None, **kwargs):
            sent.append(content or "")

        await cog._thread_rename_impl(channel=channel, respond=respond, ack=AsyncMock())
        assert sent and "スレッド" in sent[0]

    @pytest.mark.asyncio
    async def test_llm_failure_keeps_the_name(self) -> None:
        cog = _make_cog()
        thread = _make_thread()
        sent: list[str] = []

        async def respond(content=None, **kwargs):
            sent.append(content or "")

        with patch.object(
            thread_rename.topic_module, "summarize_thread", new=AsyncMock(return_value=None)
        ):
            await cog._thread_rename_impl(channel=thread, respond=respond, ack=AsyncMock())

        thread.edit.assert_not_awaited()
        cog.repo.set_topic.assert_not_awaited()
        assert sent and "❌" in sent[0]

    @pytest.mark.asyncio
    async def test_rename_failure_is_reported(self) -> None:
        cog = _make_cog()
        thread = _make_thread()
        thread.edit = AsyncMock(side_effect=discord.Forbidden(MagicMock(status=403), "nope"))
        sent: list[str] = []

        async def respond(content=None, **kwargs):
            sent.append(content or "")

        with patch.object(
            thread_rename.topic_module, "summarize_thread", new=AsyncMock(return_value="認可の設計")
        ):
            await cog._thread_rename_impl(channel=thread, respond=respond, ack=AsyncMock())

        assert sent and "Manage Threads" in sent[0]


class TestConversationCollection:
    @pytest.mark.asyncio
    async def test_skips_empty_and_command_messages(self) -> None:
        thread = _make_thread(
            messages=[
                _message("!thread-rename"),
                _message(""),
                _message("認可の設計の話", author="yousan"),
            ]
        )
        text = await thread_rename.collect_recent_text(thread)
        assert "認可の設計の話" in text
        assert "thread-rename" not in text

    @pytest.mark.asyncio
    async def test_is_capped(self) -> None:
        msgs = [_message("あ" * 500) for _ in range(20)]
        text = await thread_rename.collect_recent_text(thread=_make_thread(messages=msgs))
        assert len(text) <= thread_rename.MAX_CONVERSATION_CHARS

    @pytest.mark.asyncio
    async def test_history_failure_is_not_fatal(self) -> None:
        thread = _make_thread()
        thread.history = MagicMock(side_effect=discord.HTTPException(MagicMock(status=500), "x"))
        assert await thread_rename.collect_recent_text(thread) == ""
