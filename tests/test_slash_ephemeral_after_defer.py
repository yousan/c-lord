"""A reply marked ``ephemeral=True`` must stay with its invoker, even after ``ack()`` (#748).

``/tmux-screenshot`` defers publicly (``ack()``) because capture + render can
outlast Discord's 3-second window, then answers errors with
``respond(..., ephemeral=True)``.  Those errors were landing in the thread for
everyone — a ``pip install c-lord[table]`` hint sat as the last message of an
unrelated user's work thread.

Discord's rule is the reason: the first followup sent while a deferred
"thinking…" placeholder is still pending does not create a message — it
*edits the placeholder*, and the placeholder keeps the visibility the defer
gave it.  The followup's own ``ephemeral`` flag is ignored (Discord API docs,
"Create Followup Message").  So a public defer made every later
"only for you" reply public, for every command built on the same plumbing.

These tests drive the real slash entry points against a fake that applies
that rule, and look at the thread the way the Issue did: which messages would
``GET /channels/<thread>/messages`` return?
"""

from __future__ import annotations

from typing import Any
from unittest.mock import AsyncMock, MagicMock

import discord
import pytest
from discord import app_commands

from c_lord.bot import ClaudeDiscordBot
from c_lord.cogs.claude_chat import ClaudeChatCog
from c_lord.cogs.session_manage import SessionManageCog
from c_lord.cogs.skill_command import SkillCommandCog
from c_lord.discord_ui.authorization import Authorizer

PILLOW_HINT = "⚠️ スクリーンショットのレンダリングに必要な依存が見つかりません。"


class FakeDiscord:
    """Discord's interaction-response rules, as far as *who can see what* goes."""

    def __init__(self) -> None:
        self.messages: list[dict[str, Any]] = []
        self._placeholder: dict[str, Any] | None = None
        self._original: dict[str, Any] | None = None
        self._done = False

    def interaction(self, channel: object = None) -> MagicMock:
        it = MagicMock(spec=discord.Interaction)
        it.extras = {}
        it.channel = channel
        it.channel_id = getattr(channel, "id", None)
        it.user = MagicMock(spec=discord.Member)
        it.user.id = 1
        it.response = MagicMock()
        it.response.defer = AsyncMock(side_effect=self._defer)
        it.response.send_message = AsyncMock(side_effect=self._send_message)
        it.response.is_done = MagicMock(side_effect=lambda: self._done)
        it.followup = MagicMock()
        it.followup.send = AsyncMock(side_effect=self._followup)
        it.delete_original_response = AsyncMock(side_effect=self._delete_original)
        return it

    def _post(self, content: object, file: object, ephemeral: bool) -> dict[str, Any]:
        msg = {"content": content, "file": file, "ephemeral": ephemeral, "deleted": False}
        self.messages.append(msg)
        return msg

    async def _defer(self, *, ephemeral: bool = False, thinking: bool = False) -> None:
        self._done = True
        self._original = self._placeholder = self._post("<thinking…>", None, ephemeral)

    async def _send_message(
        self, content: object = None, *, ephemeral: bool = False, file: object = None, **_: object
    ) -> None:
        self._done = True
        self._original = self._post(content, file, ephemeral)

    async def _followup(
        self, content: object = "", *, ephemeral: bool = False, file: object = None, **_: object
    ) -> None:
        if self._placeholder is not None:
            # The deferred placeholder is edited in place; its visibility wins.
            self._placeholder.update(content=content, file=file)
            self._placeholder = None
            return
        self._post(content, file, ephemeral)

    async def _delete_original(self) -> None:
        assert self._original is not None, "nothing to delete"
        self._original["deleted"] = True
        if self._placeholder is self._original:
            self._placeholder = None

    def thread(self) -> list[dict[str, Any]]:
        """What ``GET /channels/<thread>/messages`` returns: public, not deleted."""
        return [m for m in self.messages if not m["ephemeral"] and not m["deleted"]]

    def only_for_invoker(self) -> list[dict[str, Any]]:
        return [m for m in self.messages if m["ephemeral"] and not m["deleted"]]


def _text(messages: list[dict[str, Any]]) -> str:
    return "\n".join(str(m["content"]) for m in messages)


def _thread() -> MagicMock:
    thread = MagicMock(spec=discord.Thread)
    thread.id = 123
    thread.parent_id = 456
    return thread


def _session_cog() -> SessionManageCog:
    bot = MagicMock()
    bot.channel_id = 999
    repo = MagicMock()
    repo.get = AsyncMock(return_value=None)
    return SessionManageCog(bot=bot, repo=repo)


def _tmux_mgr() -> MagicMock:
    tmux_mgr = MagicMock()
    tmux_mgr.session_name = "c-lord"
    tmux_mgr.window_name = MagicMock(return_value="w1")
    tmux_mgr.capture_screen = MagicMock(return_value="\x1b[31mhi\x1b[0m")
    tmux_mgr.list_window_tabs = MagicMock(return_value=[(1, "w1", True)])
    return tmux_mgr


# ── /tmux-screenshot — the case the Issue was filed from ─────────────────────


class TestTmuxScreenshot:
    @pytest.mark.asyncio
    async def test_missing_pillow_hint_is_not_left_in_the_thread(self, monkeypatch) -> None:
        """#748 AC1 — the hint hirobel-san found in their thread."""
        import c_lord.cogs.session_manage as sm

        fake = FakeDiscord()
        cog = _session_cog()
        cog._resolve_tmux_manager = AsyncMock(return_value=_tmux_mgr())
        monkeypatch.setattr(sm, "render_pane_png", lambda *_a, **_k: None)

        await cog.tmux_screenshot.callback(cog, fake.interaction(_thread()))

        assert fake.thread() == [], _text(fake.thread())
        assert PILLOW_HINT in _text(fake.only_for_invoker())

    @pytest.mark.asyncio
    async def test_unbound_channel_notice_is_not_left_in_the_thread(self) -> None:
        """#748 AC1 — the other post-ack ephemeral reply (Issue's reproduction step 2)."""
        fake = FakeDiscord()
        cog = _session_cog()
        cog._resolve_tmux_manager = AsyncMock(return_value=None)

        await cog.tmux_screenshot.callback(cog, fake.interaction(_thread()))

        assert fake.thread() == [], _text(fake.thread())
        assert "紐づけられていません" in _text(fake.only_for_invoker())

    @pytest.mark.asyncio
    async def test_screenshot_itself_is_still_public(self, monkeypatch) -> None:
        """#748 AC2 — the PNG is what the command is for; it stays in the thread."""
        import c_lord.cogs.session_manage as sm

        fake = FakeDiscord()
        cog = _session_cog()
        cog._resolve_tmux_manager = AsyncMock(return_value=_tmux_mgr())
        monkeypatch.setattr(sm, "render_pane_png", lambda *_a, **_k: b"\x89PNG\r\n\x1a\nDATA")
        interaction = fake.interaction(_thread())

        await cog.tmux_screenshot.callback(cog, interaction)

        public = fake.thread()
        assert len(public) == 1
        assert isinstance(public[0]["file"], discord.File)
        # Nothing was torn down on the way: the PNG replaces "thinking…" as before.
        interaction.delete_original_response.assert_not_called()


# ── the shared plumbing (#748 AC3 / AC4) ──────────────────────────────────────


class TestSessionManageSlashIO:
    """``SessionManageCog._slash_io`` serves every command in the Issue's table."""

    @pytest.mark.asyncio
    async def test_ephemeral_reply_after_public_ack_is_ephemeral(self) -> None:
        """#748 AC4 — before the fix this reply became the public placeholder."""
        fake = FakeDiscord()
        respond, ack = _session_cog()._slash_io(fake.interaction(_thread()))

        await ack()
        await respond("only for you", ephemeral=True)

        assert fake.thread() == []
        assert _text(fake.only_for_invoker()) == "only for you"

    @pytest.mark.asyncio
    async def test_public_reply_after_public_ack_is_public(self) -> None:
        fake = FakeDiscord()
        interaction = fake.interaction(_thread())
        respond, ack = _session_cog()._slash_io(interaction)

        await ack()
        await respond("for everyone")

        assert _text(fake.thread()) == "for everyone"
        interaction.delete_original_response.assert_not_called()

    @pytest.mark.asyncio
    async def test_ephemeral_ack_keeps_every_reply_private(self) -> None:
        """``/tmux-list`` acks ephemerally and replies without a flag — keep that private."""
        fake = FakeDiscord()
        respond, ack = _session_cog()._slash_io(fake.interaction(_thread()))

        await ack(ephemeral=True)
        await respond("windows")

        assert fake.thread() == []

    @pytest.mark.asyncio
    async def test_public_then_ephemeral(self) -> None:
        fake = FakeDiscord()
        respond, ack = _session_cog()._slash_io(fake.interaction(_thread()))

        await ack()
        await respond("for everyone")
        await respond("only for you", ephemeral=True)

        assert _text(fake.thread()) == "for everyone"
        assert _text(fake.only_for_invoker()) == "only for you"


class TestOtherSlashCommands:
    """The two cogs in the table that carried their own copy of the plumbing."""

    @pytest.mark.asyncio
    async def test_clord(self) -> None:
        fake = FakeDiscord()
        cog = ClaudeChatCog(
            bot=MagicMock(),
            repo=MagicMock(),
            runner=MagicMock(),
            authorizer=Authorizer(allow_anyone=True),
        )

        async def impl(**kwargs: Any) -> None:
            await kwargs["ack"]()
            await kwargs["respond"]("⚠️ only for you", ephemeral=True)

        cog._clord_impl = impl  # type: ignore[method-assign]
        await cog.start_session.callback(cog, fake.interaction(_thread()), prompt="hi")

        assert fake.thread() == [], _text(fake.thread())

    @pytest.mark.asyncio
    async def test_skill(self) -> None:
        fake = FakeDiscord()
        cog = SkillCommandCog(
            bot=MagicMock(),
            repo=MagicMock(),
            runner=MagicMock(),
            claude_channel_id=999,
            authorizer=Authorizer(allow_anyone=True),
        )

        async def impl(**kwargs: Any) -> None:
            await kwargs["ack"]()
            await kwargs["respond"]("⚠️ only for you", ephemeral=True)

        cog._run_skill_impl = impl  # type: ignore[method-assign]
        await cog.run_skill.callback(cog, fake.interaction(_thread()), name="x")

        assert fake.thread() == [], _text(fake.thread())


class TestNoPrivateCopyOfThePlumbing:
    """#748 AC3, statically: an ephemeral followup goes through ``slash_io``.

    Three cogs each carried their own ``respond``/``ack`` closures, and all three
    had the bug. A fourth copy would have it too, so a followup that asks for
    ``ephemeral`` anywhere else is refused here.
    """

    # Component (button) callbacks defer without a "thinking…" placeholder
    # (DEFERRED_UPDATE_MESSAGE), so their followups are new messages already.
    ALLOWED = {"c_lord/discord_ui/slash_io.py", "c_lord/discord_ui/error_reporting.py"}

    def test_ephemeral_followups_only_in_slash_io(self) -> None:
        import ast
        from pathlib import Path

        offenders = []
        for path in sorted(Path("c_lord").rglob("*.py")):
            if path.as_posix() in self.ALLOWED:
                continue
            for node in ast.walk(ast.parse(path.read_text(encoding="utf-8"))):
                if (
                    isinstance(node, ast.Call)
                    and isinstance(node.func, ast.Attribute)
                    and node.func.attr == "send"
                    and isinstance(node.func.value, ast.Attribute)
                    and node.func.value.attr == "followup"
                    and any(k.arg == "ephemeral" for k in node.keywords)
                ):
                    offenders.append(f"{path}:{node.lineno}")
        assert offenders == [], (
            "ephemeral followup outside c_lord/discord_ui/slash_io.py — after a public "
            f"defer it would be posted to everyone (#748): {offenders}"
        )


class TestErrorAfterAck:
    """A command that raises after ``ack()`` is answered by ``on_app_command_error``."""

    @pytest.mark.asyncio
    async def test_error_notice_is_not_left_in_the_thread(self) -> None:
        fake = FakeDiscord()
        interaction = fake.interaction(_thread())
        _respond, ack = _session_cog()._slash_io(interaction)
        await ack()

        bot = ClaudeDiscordBot(channel_id=123)
        await bot.on_app_command_error(
            interaction, app_commands.CommandInvokeError(MagicMock(), RuntimeError("boom"))
        )

        assert fake.thread() == [], _text(fake.thread())
        assert fake.only_for_invoker()
