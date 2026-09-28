"""#786: a menu left at ⏳ 確認中 by a restart is corrected by the next process.

#746 turned an answer that could not be confirmed within 12s into ⏳ 確認中,
with a watcher in the bot process that rewrites it ✅ / ⚠️ once Claude's
transcript records the result. A restart kills that watcher, and the menu said
確認中 forever — production restarts up to 6 times a day, and a multi-question
ask records its result only after its last answer (+53 min measured).

Nothing new is stored for this: the ⏳ menu message itself carries the question
and the answer, the thread's session row carries the workspace, and the
workspace's transcript carries the ask and its result. On startup the new
process finds its ⏳ menus in Discord and picks the watch up again.
"""

from __future__ import annotations

import asyncio
import datetime as dt
import json
from pathlib import Path
from unittest.mock import AsyncMock, MagicMock

import discord
import pytest

from c_lord.ask_confirm_recovery import resume_confirming_menus
from c_lord.database.repository import SessionRecord
from c_lord.discord_ui import ask_handler
from c_lord.discord_ui.embeds import (
    ask_answered_embed,
    ask_confirming_embed,
    parse_confirming_embed,
)
from c_lord.transcript.ask_result import find_ask_for_question
from c_lord.transcript.resolver import derive_project_dir

UTC = dt.timezone.utc  # noqa: UP017 — dt.UTC is 3.11+, we support 3.10
BOT_ID = 4242
WORKDIR = "/work/thread-501"
STARTED = dt.datetime(2026, 9, 28, 14, 30, tzinfo=UTC)
MENU_AT = dt.datetime(2026, 9, 28, 14, 10, tzinfo=UTC)

Q1 = "#686 罫線はどうしますか？"
Q2 = "#525 通知はどうしますか？"
ANSWERED = (
    f'Your questions have been answered: "{Q1}"="長すぎたら畳む", "{Q2}"="blocked に戻す". '
    "You can now continue with the user's answers in mind."
)
REJECTED = (
    "The user doesn't want to proceed with this tool use. The tool use was rejected.\n"
    "The user wants to clarify these questions.\n"
    f'- "{Q1}"\n  (No answer provided)'
)


def _use(tool_use_id: str, ts: str) -> dict:
    return {
        "timestamp": ts,
        "message": {
            "content": [
                {
                    "type": "tool_use",
                    "id": tool_use_id,
                    "name": "AskUserQuestion",
                    "input": {
                        "questions": [
                            {
                                "question": Q1,
                                "header": "罫線",
                                "options": [
                                    {"label": "長すぎたら畳む", "description": "a"},
                                    {"label": "そのまま", "description": "b"},
                                ],
                            },
                            {"question": Q2, "header": "通知", "options": []},
                        ]
                    },
                }
            ]
        },
    }


def _result(tool_use_id: str, ts: str, text: str) -> dict:
    return {
        "timestamp": ts,
        "message": {
            "content": [{"type": "tool_result", "tool_use_id": tool_use_id, "content": text}]
        },
    }


def _append(project_dir: Path, *events: dict) -> None:
    project_dir.mkdir(parents=True, exist_ok=True)
    with (project_dir / "s.jsonl").open("a", encoding="utf-8") as fh:
        for e in events:
            fh.write(json.dumps(e, ensure_ascii=False) + "\n")


# ── the ⏳ menu reads back ────────────────────────────────────────────────────


class TestParseConfirmingEmbed:
    def test_round_trips_question_header_and_answer(self) -> None:
        embed = ask_confirming_embed(Q1, "罫線", ["長すぎたら畳む", "そのまま"])
        parsed = parse_confirming_embed(embed)
        assert parsed is not None
        assert parsed.question == Q1
        assert parsed.header == "罫線"
        assert parsed.selected == ["長すぎたら畳む", "そのまま"]

    def test_other_menu_states_are_not_confirming(self) -> None:
        assert parse_confirming_embed(ask_answered_embed(Q1, "罫線", ["x"])) is None
        assert parse_confirming_embed(discord.Embed(title="⏳ 罫線", description="別物")) is None


# ── which ask in the transcript the menu belongs to ─────────────────────────


class TestFindAskForQuestion:
    def test_finds_the_ask_carrying_the_question(self, tmp_path: Path) -> None:
        _append(tmp_path, _use("toolu_a", "2026-09-28T14:09:58.000Z"))
        found = find_ask_for_question(tmp_path, Q2, "2026-09-28T14:10:00.000Z")
        assert found is not None
        assert found.tool_use_id == "toolu_a"
        assert found.question["question"] == Q2

    def test_prefers_the_newest_ask_before_the_menu(self, tmp_path: Path) -> None:
        _append(
            tmp_path,
            _use("toolu_old", "2026-09-28T13:00:00.000Z"),
            _use("toolu_now", "2026-09-28T14:09:58.000Z"),
            _use("toolu_later", "2026-09-28T14:20:00.000Z"),
        )
        found = find_ask_for_question(tmp_path, Q1, "2026-09-28T14:10:00.000Z")
        assert found is not None and found.tool_use_id == "toolu_now"

    def test_an_ask_written_only_with_its_result_is_found_after_the_menu(
        self, tmp_path: Path
    ) -> None:
        """The CLI sometimes writes the tool_use together with its result (#746)."""
        _append(tmp_path, _use("toolu_late", "2026-09-28T14:40:00.000Z"))
        found = find_ask_for_question(tmp_path, Q1, "2026-09-28T14:10:00.000Z")
        assert found is not None and found.tool_use_id == "toolu_late"

    def test_an_ask_resolved_before_the_menu_was_drawn_is_not_its_ask(
        self, tmp_path: Path
    ) -> None:
        """Found on staging: the same question asked again, its ask not written
        yet (#746's lazy write). The previous ask — answered minutes before this
        menu existed — must not be taken for it, or the menu turns ✅ with the
        previous answer's verdict before this one was even given."""
        _append(
            tmp_path,
            _use("toolu_prev", "2026-09-28T14:00:00.000Z"),
            _result("toolu_prev", "2026-09-28T14:01:00.000Z", ANSWERED),
        )
        assert find_ask_for_question(tmp_path, Q1, "2026-09-28T14:10:00.000Z") is None

        _append(
            tmp_path,
            _use("toolu_now", "2026-09-28T14:40:00.000Z"),
            _result("toolu_now", "2026-09-28T14:40:00.000Z", ANSWERED),
        )
        found = find_ask_for_question(tmp_path, Q1, "2026-09-28T14:10:00.000Z")
        assert found is not None and found.tool_use_id == "toolu_now"

    def test_an_ask_resolved_after_the_menu_was_drawn_is_its_ask(self, tmp_path: Path) -> None:
        _append(
            tmp_path,
            _use("toolu_now", "2026-09-28T14:09:58.000Z"),
            _result("toolu_now", "2026-09-28T14:35:00.000Z", ANSWERED),
        )
        found = find_ask_for_question(tmp_path, Q1, "2026-09-28T14:10:00.000Z")
        assert found is not None and found.tool_use_id == "toolu_now"

    def test_none_when_no_ask_carries_it(self, tmp_path: Path) -> None:
        _append(tmp_path, _use("toolu_a", "2026-09-28T14:09:58.000Z"))
        assert find_ask_for_question(tmp_path, "別の質問", "2026-09-28T14:10:00.000Z") is None


# ── startup ──────────────────────────────────────────────────────────────────


def _menu_message(thread: MagicMock, *, author_id: int = BOT_ID) -> MagicMock:
    msg = MagicMock(spec=discord.Message)
    msg.id = discord.utils.time_snowflake(MENU_AT)
    msg.created_at = MENU_AT
    msg.channel = thread
    msg.author = MagicMock()
    msg.author.id = author_id
    msg.embeds = [ask_confirming_embed(Q1, "罫線", ["長すぎたら畳む"])]
    msg.edit = AsyncMock()
    return msg


class _World:
    def __init__(self, tmp_path: Path, *, author_id: int = BOT_ID) -> None:
        self.projects_root = tmp_path
        self.project_dir = derive_project_dir(WORKDIR, projects_root=tmp_path)
        thread = MagicMock(spec=discord.Thread)
        thread.id = 501
        self.menu = _menu_message(thread, author_id=author_id)
        thread.last_message_id = self.menu.id

        def _history(**_kwargs: object) -> object:
            async def _gen():  # type: ignore[no-untyped-def]
                yield self.menu

            return _gen()

        thread.history = MagicMock(side_effect=_history)
        self.thread = thread
        guild = MagicMock()
        guild.active_threads = AsyncMock(return_value=[thread])
        self.bot = MagicMock()
        self.bot.user = MagicMock()
        self.bot.user.id = BOT_ID
        self.bot.guilds = [guild]
        record = SessionRecord(
            thread_id=501,
            session_id="s",
            working_dir=WORKDIR,
            model=None,
            origin="discord",
            summary=None,
            created_at="2026-09-28 10:00:00",
            last_used_at="2026-09-28 10:00:00",
        )
        self.repo = MagicMock()
        self.repo.get = AsyncMock(side_effect=lambda tid: record if tid == 501 else None)

    async def start(self) -> int:
        return await resume_confirming_menus(
            self.bot, self.repo, projects_root=self.projects_root, started_at=STARTED
        )

    def final_title(self) -> str:
        self.menu.edit.assert_awaited()
        return self.menu.edit.await_args.kwargs["embed"].title


@pytest.fixture(autouse=True)
def _fast_watch(monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.setattr(ask_handler, "_LATE_CONFIRM_POLL_MIN", 0.01)
    monkeypatch.setattr(ask_handler, "_LATE_CONFIRM_POLL_MAX", 0.01)


async def _watchers_done() -> None:
    pending = list(ask_handler._late_confirmations)
    if pending:
        await asyncio.wait_for(asyncio.gather(*pending), timeout=5)


class TestStartupResumesTheWatch:
    @pytest.mark.asyncio
    async def test_result_written_after_the_restart_turns_the_menu_green(
        self, tmp_path: Path
    ) -> None:
        """AC1 / AC3: the reproduction — answer Q1, restart, answer Q2."""
        w = _World(tmp_path)
        _append(w.project_dir, _use("toolu_1", "2026-09-28T14:09:58.000Z"))

        assert await w.start() == 1
        w.menu.edit.assert_not_awaited()  # nothing recorded yet — still ⏳

        _append(w.project_dir, _result("toolu_1", "2026-09-28T14:35:00.000Z", ANSWERED))
        await _watchers_done()

        assert w.final_title().startswith("✅")

    @pytest.mark.asyncio
    async def test_result_written_during_the_restart_turns_it_green_on_startup(
        self, tmp_path: Path
    ) -> None:
        """AC2: the result landed while no process was watching."""
        w = _World(tmp_path)
        _append(
            w.project_dir,
            _use("toolu_1", "2026-09-28T14:09:58.000Z"),
            _result("toolu_1", "2026-09-28T14:29:00.000Z", ANSWERED),
        )

        await w.start()
        await _watchers_done()

        assert w.final_title().startswith("✅")

    @pytest.mark.asyncio
    async def test_no_answer_result_turns_it_to_the_warning(self, tmp_path: Path) -> None:
        """AC2: ⚠️, with the options the menu offered (#804) read from the ask."""
        w = _World(tmp_path)
        _append(
            w.project_dir,
            _use("toolu_1", "2026-09-28T14:09:58.000Z"),
            _result("toolu_1", "2026-09-28T14:29:00.000Z", REJECTED),
        )

        await w.start()
        await _watchers_done()

        embed = w.menu.edit.await_args.kwargs["embed"]
        assert not embed.title.startswith(("✅", "⏳"))
        assert "そのまま" in embed.description

    @pytest.mark.asyncio
    async def test_an_ask_written_only_with_its_result_is_waited_for(
        self, tmp_path: Path
    ) -> None:
        w = _World(tmp_path)
        tmp_path.mkdir(exist_ok=True)

        await w.start()
        _append(
            w.project_dir,
            _use("toolu_1", "2026-09-28T14:35:00.000Z"),
            _result("toolu_1", "2026-09-28T14:35:00.000Z", ANSWERED),
        )
        await _watchers_done()

        assert w.final_title().startswith("✅")

    @pytest.mark.asyncio
    async def test_other_bots_menus_are_left_alone(self, tmp_path: Path) -> None:
        w = _World(tmp_path, author_id=999)
        _append(
            w.project_dir,
            _use("toolu_1", "2026-09-28T14:09:58.000Z"),
            _result("toolu_1", "2026-09-28T14:29:00.000Z", ANSWERED),
        )

        assert await w.start() == 0
        await _watchers_done()
        w.menu.edit.assert_not_awaited()
