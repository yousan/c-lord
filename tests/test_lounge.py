"""The AI Lounge — one-line notes Claude sessions leave for each other.

Three faces of it are checked here:

- LoungeRepository: what is stored, what is cut, what is thrown away
- /api/lounge: reading and posting over REST, and the copy sent to Discord
- run_claude_in_thread no longer reads the lounge per turn (#766)
"""

from __future__ import annotations

import logging
import re
from collections.abc import AsyncIterator
from pathlib import Path
from typing import Any
from unittest.mock import AsyncMock, MagicMock

import aiosqlite
import discord
import pytest
from aiohttp.test_utils import TestClient, TestServer

from c_lord.database.lounge_repo import LoungeMessage, LoungeRepository
from c_lord.database.models import init_db
from c_lord.database.notification_repo import NotificationRepository
from c_lord.ext.api_server import ApiServer

LOCAL_TIMESTAMP = re.compile(r"\d{4}-\d{2}-\d{2} \d{2}:\d{2}:\d{2}")
COORDINATION_ENV = "COORDINATION_CHANNEL_ID"
LOUNGE_CHANNEL = 777000777


@pytest.fixture
async def db_path(tmp_path: Path) -> str:
    path = str(tmp_path / "sessions.db")
    await init_db(path)
    return path


@pytest.fixture
def lounge(db_path: str) -> LoungeRepository:
    return LoungeRepository(db_path)


# ---------------------------------------------------------------------------
# LoungeRepository
# ---------------------------------------------------------------------------


class TestLoungeRepository:
    async def test_a_posted_note_is_returned_as_stored_and_can_be_read(
        self, lounge: LoungeRepository
    ) -> None:
        note = await lounge.post(message="starting the migration", label="w3")

        assert isinstance(note, LoungeMessage)
        assert note.id > 0
        assert (note.label, note.message) == ("w3", "starting the migration")
        assert LOCAL_TIMESTAMP.fullmatch(note.posted_at)
        assert await lounge.get_recent() == [note]

    async def test_an_empty_lounge_reads_as_nothing(self, lounge: LoungeRepository) -> None:
        assert await lounge.get_recent() == []
        assert await lounge.count() == 0

    async def test_notes_are_read_oldest_first(self, lounge: LoungeRepository) -> None:
        for text in ("one", "two", "three"):
            await lounge.post(message=text)

        assert [n.message for n in await lounge.get_recent()] == ["one", "two", "three"]

    async def test_a_limit_keeps_the_newest_notes_still_oldest_first(
        self, lounge: LoungeRepository
    ) -> None:
        for number in range(1, 6):
            await lounge.post(message=f"note {number}")

        recent = await lounge.get_recent(limit=2)

        assert [n.message for n in recent] == ["note 4", "note 5"]

    async def test_ten_notes_are_read_unless_asked_otherwise(
        self, lounge: LoungeRepository
    ) -> None:
        for number in range(1, 13):
            await lounge.post(message=f"note {number}")

        recent = await lounge.get_recent()

        assert [n.message for n in recent] == [f"note {n}" for n in range(3, 13)]

    async def test_count_is_the_number_of_notes_stored(self, lounge: LoungeRepository) -> None:
        for text in ("a", "b", "c"):
            await lounge.post(message=text)

        assert await lounge.count() == 3

    async def test_an_over_long_label_is_cut_to_fifty_characters(
        self, lounge: LoungeRepository
    ) -> None:
        label = "".join(chr(ord("a") + i % 26) for i in range(80))

        note = await lounge.post(message="hello", label=label)

        assert note.label == label[:50]
        assert (await lounge.get_recent())[0].label == label[:50]

    async def test_an_over_long_message_is_cut_to_a_thousand_characters(
        self, lounge: LoungeRepository
    ) -> None:
        """Cut, not refused: a session must not lose its turn over a long note."""
        message = "".join(str(i % 10) for i in range(1500))

        note = await lounge.post(message=message)

        assert note.message == message[:1000]
        assert (await lounge.get_recent())[0].message == message[:1000]

    async def test_input_at_the_limits_is_kept_whole(self, lounge: LoungeRepository) -> None:
        note = await lounge.post(message="m" * 1000, label="l" * 50)

        assert (len(note.message), len(note.label)) == (1000, 50)

    async def test_a_note_without_a_label_is_signed_ai(self, lounge: LoungeRepository) -> None:
        note = await lounge.post(message="who said this?")

        assert note.label == "AI"

    @pytest.mark.parametrize("no_label", [None, ""])
    async def test_a_falsy_label_is_signed_ai(
        self, lounge: LoungeRepository, no_label: Any
    ) -> None:
        note = await lounge.post(message="anonymous", label=no_label)

        assert note.label == "AI"

    @pytest.mark.parametrize("no_message", [None, ""])
    async def test_a_falsy_message_is_stored_as_an_empty_note(
        self, lounge: LoungeRepository, no_message: Any
    ) -> None:
        """Refusing an empty message is the API's job; here it is just stored."""
        note = await lounge.post(message=no_message, label="quiet")

        assert note.message == ""
        assert await lounge.count() == 1

    async def test_only_the_newest_notes_are_kept(self, lounge: LoungeRepository) -> None:
        from c_lord.database.lounge_repo import _MAX_STORED_MESSAGES

        # Fill the board directly — going through post() 200 times proves nothing more.
        async with aiosqlite.connect(lounge._db_path) as db:
            await db.executemany(
                "INSERT INTO lounge_messages (label, message) VALUES ('old', ?)",
                [(f"old {n}",) for n in range(_MAX_STORED_MESSAGES)],
            )
            await db.commit()
        oldest_before = (await lounge.get_recent(limit=_MAX_STORED_MESSAGES + 10))[0]

        newest = [await lounge.post(message=f"new {n}", label="new") for n in range(3)]

        kept = await lounge.get_recent(limit=_MAX_STORED_MESSAGES + 10)
        assert await lounge.count() == _MAX_STORED_MESSAGES
        assert len(kept) == _MAX_STORED_MESSAGES
        assert kept[-3:] == newest
        # The three that made room were the three oldest.
        assert kept[0].id == oldest_before.id + 3

    async def test_nothing_is_thrown_away_below_the_limit(self, lounge: LoungeRepository) -> None:
        for text in ("a", "b"):
            await lounge.post(message=text)

        assert await lounge.count() == 2

    async def test_a_post_is_logged_with_its_label(
        self, lounge: LoungeRepository, caplog: pytest.LogCaptureFixture
    ) -> None:
        with caplog.at_level(logging.INFO, logger="c_lord.database.lounge_repo"):
            note = await lounge.post(message="hi", label="w7")

        assert f"Lounge message posted by 'w7' (id={note.id})" in caplog.text


# ---------------------------------------------------------------------------
# /api/lounge
# ---------------------------------------------------------------------------


def _bot_with_channel(channel: Any) -> MagicMock:
    bot = MagicMock()
    bot.cogs = {}
    bot.get_channel.return_value = channel
    bot.fetch_channel = AsyncMock(return_value=channel)
    return bot


def _text_channel() -> MagicMock:
    channel = MagicMock()
    channel.send = AsyncMock()
    return channel


async def _serve(tmp_path: Path, **api_kwargs: Any) -> TestClient:
    notifications = NotificationRepository(str(tmp_path / "notifications.db"))
    await notifications.init_db()
    api_kwargs.setdefault("bot", _bot_with_channel(_text_channel()))
    api = ApiServer(repo=notifications, default_channel_id=1, port=0, **api_kwargs)
    client = TestClient(TestServer(api.app))
    await client.start_server()
    return client


@pytest.fixture
async def api(
    tmp_path: Path, lounge: LoungeRepository, monkeypatch: pytest.MonkeyPatch
) -> AsyncIterator[TestClient]:
    """The API with a real lounge and no Discord channel to copy notes to."""
    monkeypatch.delenv(COORDINATION_ENV, raising=False)
    client = await _serve(tmp_path, lounge_repo=lounge)
    yield client
    await client.close()


class TestLoungeApiEndpoints:
    async def test_reading_an_empty_lounge(self, api: TestClient) -> None:
        response = await api.get("/api/lounge")

        assert response.status == 200
        assert await response.json() == {"messages": []}

    async def test_a_posted_note_is_answered_with_what_was_stored(
        self, api: TestClient, lounge: LoungeRepository
    ) -> None:
        response = await api.post("/api/lounge", json={"message": "tests are green", "label": "w2"})

        body = await response.json()
        (stored,) = await lounge.get_recent()
        assert response.status == 201
        assert body == {
            "status": "posted",
            "id": stored.id,
            "label": "w2",
            "message": "tests are green",
            "posted_at": stored.posted_at,
        }

    async def test_a_posted_note_can_be_read_back(self, api: TestClient) -> None:
        posted = await (await api.post("/api/lounge", json={"message": "first"})).json()
        await api.post("/api/lounge", json={"message": "second", "label": "w9"})

        body = await (await api.get("/api/lounge")).json()

        assert [(m["label"], m["message"]) for m in body["messages"]] == [
            ("AI", "first"),
            ("w9", "second"),
        ]
        assert body["messages"][0] == {
            "id": posted["id"],
            "label": "AI",
            "message": "first",
            "posted_at": posted["posted_at"],
        }

    async def test_surrounding_whitespace_is_dropped_before_storing(
        self, api: TestClient, lounge: LoungeRepository
    ) -> None:
        await api.post("/api/lounge", json={"message": "  padded  ", "label": "  w4 \n"})

        (stored,) = await lounge.get_recent()
        assert (stored.label, stored.message) == ("w4", "padded")

    @pytest.mark.parametrize("label", [None, "", "   "])
    async def test_a_note_without_a_usable_label_is_signed_ai(
        self, api: TestClient, label: str | None
    ) -> None:
        response = await api.post("/api/lounge", json={"message": "hello", "label": label})

        assert (await response.json())["label"] == "AI"

    @pytest.mark.parametrize(
        "body",
        [{}, {"label": "w1"}, {"message": None}, {"message": ""}, {"message": " \n\t "}],
        ids=["empty", "label-only", "null", "empty-string", "whitespace"],
    )
    async def test_a_post_without_a_message_is_refused(
        self, api: TestClient, lounge: LoungeRepository, body: dict[str, Any]
    ) -> None:
        response = await api.post("/api/lounge", json=body)

        assert response.status == 400
        assert await response.json() == {"error": "message is required"}
        assert await lounge.count() == 0

    async def test_a_message_of_invisible_characters_is_refused(
        self, api: TestClient, lounge: LoungeRepository
    ) -> None:
        """#1288: zero-width characters survive ``strip()`` but show nothing."""
        response = await api.post("/api/lounge", json={"message": "\u200b\u200b"})

        assert response.status == 400
        assert await response.json() == {"error": "message is required"}
        assert await lounge.count() == 0

    @pytest.mark.parametrize("field", ["message", "label"])
    async def test_a_text_field_that_is_not_text_is_refused(
        self, api: TestClient, lounge: LoungeRepository, field: str
    ) -> None:
        body = {"message": "ok", "label": "ok", field: ["not", "text"]}

        response = await api.post("/api/lounge", json=body)

        assert response.status == 400
        assert await response.json() == {"error": f"{field} must be a string"}
        assert await lounge.count() == 0

    async def test_a_body_that_is_not_json_is_refused(
        self, api: TestClient, lounge: LoungeRepository
    ) -> None:
        response = await api.post(
            "/api/lounge", data="{not json", headers={"Content-Type": "application/json"}
        )

        assert response.status == 400
        assert await response.json() == {"error": "Invalid JSON"}
        assert await lounge.count() == 0

    async def test_a_json_body_that_is_not_an_object_is_refused(self, api: TestClient) -> None:
        response = await api.post("/api/lounge", json=["message", "hello"])

        assert response.status == 400
        assert await response.json() == {"error": "Body must be a JSON object"}

    async def test_limit_chooses_how_many_notes_are_read(
        self, api: TestClient, lounge: LoungeRepository
    ) -> None:
        for number in range(1, 6):
            await lounge.post(message=f"note {number}")

        body = await (await api.get("/api/lounge?limit=2")).json()

        assert [m["message"] for m in body["messages"]] == ["note 4", "note 5"]

    async def test_ten_notes_are_read_without_a_limit(
        self, api: TestClient, lounge: LoungeRepository
    ) -> None:
        for number in range(12):
            await lounge.post(message=f"note {number}")

        body = await (await api.get("/api/lounge")).json()

        assert len(body["messages"]) == 10

    async def test_a_limit_above_fifty_reads_fifty(
        self, api: TestClient, lounge: LoungeRepository
    ) -> None:
        async with aiosqlite.connect(lounge._db_path) as db:
            await db.executemany(
                "INSERT INTO lounge_messages (message) VALUES (?)",
                [(f"note {n}",) for n in range(60)],
            )
            await db.commit()

        response = await api.get("/api/lounge?limit=500")

        assert response.status == 200
        assert len((await response.json())["messages"]) == 50

    @pytest.mark.parametrize("limit", ["0", "-7"])
    async def test_a_limit_below_one_reads_one(
        self, api: TestClient, lounge: LoungeRepository, limit: str
    ) -> None:
        for text in ("older", "newest"):
            await lounge.post(message=text)

        response = await api.get(f"/api/lounge?limit={limit}")

        assert response.status == 200
        assert [m["message"] for m in (await response.json())["messages"]] == ["newest"]

    @pytest.mark.parametrize("limit", ["many", "2.5", ""])
    async def test_a_limit_that_is_not_a_whole_number_is_refused(
        self, api: TestClient, limit: str
    ) -> None:
        response = await api.get("/api/lounge", params={"limit": limit})

        assert response.status == 400
        assert await response.json() == {"error": "limit must be an integer"}


class TestLoungeWithoutARepository:
    """An instance that never wired a lounge says so instead of failing."""

    @pytest.fixture
    async def bare_api(self, tmp_path: Path) -> AsyncIterator[TestClient]:
        client = await _serve(tmp_path, lounge_repo=None)
        yield client
        await client.close()

    async def test_reading_answers_503(self, bare_api: TestClient) -> None:
        response = await bare_api.get("/api/lounge")

        assert response.status == 503
        assert await response.json() == {"error": "AI Lounge not configured (lounge_repo is None)"}

    async def test_posting_answers_503(self, bare_api: TestClient) -> None:
        response = await bare_api.post("/api/lounge", json={"message": "anyone?"})

        assert response.status == 503
        assert await response.json() == {"error": "AI Lounge not configured (lounge_repo is None)"}


class TestLoungeCopyToDiscord:
    """A posted note is also shown in the lounge channel, when there is one."""

    async def _post(
        self, tmp_path: Path, body: dict[str, Any], **api_kwargs: Any
    ) -> tuple[int, dict[str, Any]]:
        """POST one note to an API built with *api_kwargs*: (status, JSON answer)."""
        client = await _serve(tmp_path, **api_kwargs)
        try:
            response = await client.post("/api/lounge", json=body)
            return response.status, await response.json()
        finally:
            await client.close()

    async def test_the_note_is_sent_to_the_lounge_channel(
        self, tmp_path: Path, lounge: LoungeRepository
    ) -> None:
        channel = _text_channel()
        bot = _bot_with_channel(channel)

        status, answer = await self._post(
            tmp_path,
            {"message": "deploy done", "label": "w5"},
            bot=bot,
            lounge_repo=lounge,
            lounge_channel_id=LOUNGE_CHANNEL,
        )

        (stored,) = await lounge.get_recent()
        hour_and_minute = stored.posted_at[11:16]
        assert status == 201
        bot.get_channel.assert_called_once_with(LOUNGE_CHANNEL)
        channel.send.assert_awaited_once_with(f"**[w5]** deploy done *({hour_and_minute})*")
        assert re.fullmatch(r"\d\d:\d\d", hour_and_minute)

    async def test_the_copy_carries_what_was_stored_not_what_was_sent(self, tmp_path: Path) -> None:
        """The repository may cut the text; Discord shows the stored note."""
        channel = _text_channel()
        stored = LoungeMessage(id=9, label="cut", message="short", posted_at="2031-02-03 04:05:06")
        repo = MagicMock()
        repo.post = AsyncMock(return_value=stored)

        status, answer = await self._post(
            tmp_path,
            {"message": "something much longer", "label": "original"},
            bot=_bot_with_channel(channel),
            lounge_repo=repo,
            lounge_channel_id=LOUNGE_CHANNEL,
        )

        repo.post.assert_awaited_once_with(message="something much longer", label="original")
        channel.send.assert_awaited_once_with("**[cut]** short *(04:05)*")
        assert (status, answer["message"]) == (201, "short")

    async def test_a_time_too_short_to_cut_is_shown_whole(self, tmp_path: Path) -> None:
        channel = _text_channel()
        repo = MagicMock()
        repo.post = AsyncMock(
            return_value=LoungeMessage(id=1, label="w1", message="hi", posted_at="just now")
        )

        await self._post(
            tmp_path,
            {"message": "hi"},
            bot=_bot_with_channel(channel),
            lounge_repo=repo,
            lounge_channel_id=LOUNGE_CHANNEL,
        )

        channel.send.assert_awaited_once_with("**[w1]** hi *(just now)*")

    async def test_a_channel_not_in_the_cache_is_fetched(
        self, tmp_path: Path, lounge: LoungeRepository
    ) -> None:
        channel = _text_channel()
        bot = _bot_with_channel(channel)
        bot.get_channel.return_value = None

        await self._post(
            tmp_path,
            {"message": "hello"},
            bot=bot,
            lounge_repo=lounge,
            lounge_channel_id=LOUNGE_CHANNEL,
        )

        bot.fetch_channel.assert_awaited_once_with(LOUNGE_CHANNEL)
        channel.send.assert_awaited_once()

    async def test_a_failing_discord_does_not_fail_the_post(
        self, tmp_path: Path, lounge: LoungeRepository, caplog: pytest.LogCaptureFixture
    ) -> None:
        channel = _text_channel()
        channel.send.side_effect = RuntimeError("discord is down")

        with caplog.at_level(logging.WARNING, logger="c_lord.ext.api_server"):
            status, answer = await self._post(
                tmp_path,
                {"message": "still stored"},
                bot=_bot_with_channel(channel),
                lounge_repo=lounge,
                lounge_channel_id=LOUNGE_CHANNEL,
            )

        assert status == 201
        assert [n.message for n in await lounge.get_recent()] == ["still stored"]
        assert "Failed to forward lounge message to Discord" in caplog.text

    async def test_something_that_cannot_be_sent_to_is_left_alone(
        self, tmp_path: Path, lounge: LoungeRepository, caplog: pytest.LogCaptureFixture
    ) -> None:
        """A category, say: the id resolves, but there is nowhere to write.

        Not a failure to report — the note is stored, nothing is attempted,
        and the log does not claim a forward went wrong.
        """
        not_a_text_channel = MagicMock(spec=["id", "name"])
        bot = _bot_with_channel(not_a_text_channel)

        with caplog.at_level(logging.DEBUG, logger="c_lord.ext.api_server"):
            status, answer = await self._post(
                tmp_path,
                {"message": "hello"},
                bot=bot,
                lounge_repo=lounge,
                lounge_channel_id=LOUNGE_CHANNEL,
            )

        assert status == 201
        assert [n.message for n in await lounge.get_recent()] == ["hello"]
        bot.get_channel.assert_called_once_with(LOUNGE_CHANNEL)
        assert not_a_text_channel.mock_calls == []
        assert [r.getMessage() for r in caplog.records if r.levelno >= logging.WARNING] == []

    async def test_without_a_lounge_channel_nothing_is_sent(
        self, tmp_path: Path, lounge: LoungeRepository, monkeypatch: pytest.MonkeyPatch
    ) -> None:
        monkeypatch.delenv(COORDINATION_ENV, raising=False)
        channel = _text_channel()
        bot = _bot_with_channel(channel)

        status, answer = await self._post(
            tmp_path, {"message": "kept to ourselves"}, bot=bot, lounge_repo=lounge
        )

        assert status == 201
        bot.get_channel.assert_not_called()
        bot.fetch_channel.assert_not_awaited()
        channel.send.assert_not_awaited()

    async def test_a_refused_post_is_not_sent_either(
        self, tmp_path: Path, lounge: LoungeRepository
    ) -> None:
        channel = _text_channel()

        status, answer = await self._post(
            tmp_path,
            {"message": "   "},
            bot=_bot_with_channel(channel),
            lounge_repo=lounge,
            lounge_channel_id=LOUNGE_CHANNEL,
        )

        assert status == 400
        channel.send.assert_not_awaited()


class TestLoungeChannelChoice:
    """Which channel the copies go to: the one given, else the coordination channel."""

    def _api(self, **kwargs: Any) -> ApiServer:
        return ApiServer(repo=MagicMock(), bot=MagicMock(), port=0, **kwargs)

    def test_the_coordination_channel_is_used_when_none_is_given(
        self, monkeypatch: pytest.MonkeyPatch
    ) -> None:
        monkeypatch.setenv(COORDINATION_ENV, " 123456789 ")

        assert self._api().lounge_channel_id == 123456789

    def test_a_channel_given_explicitly_wins(self, monkeypatch: pytest.MonkeyPatch) -> None:
        monkeypatch.setenv(COORDINATION_ENV, "123456789")

        assert self._api(lounge_channel_id=LOUNGE_CHANNEL).lounge_channel_id == LOUNGE_CHANNEL

    @pytest.mark.parametrize("value", [None, "", "not-a-number", "²"])
    def test_no_usable_coordination_channel_means_no_lounge_channel(
        self, monkeypatch: pytest.MonkeyPatch, value: str | None
    ) -> None:
        if value is None:
            monkeypatch.delenv(COORDINATION_ENV, raising=False)
        else:
            monkeypatch.setenv(COORDINATION_ENV, value)

        assert self._api().lounge_channel_id is None


# ---------------------------------------------------------------------------
# run_claude_in_thread lounge tests
# ---------------------------------------------------------------------------


def _turn_doubles() -> tuple[MagicMock, MagicMock, MagicMock]:
    """A thread, a runner and a session repository good for one scripted turn."""
    thread = MagicMock(spec=discord.Thread)
    thread.id = 424242
    sent = MagicMock(spec=discord.Message)
    sent.edit = AsyncMock()
    thread.send = AsyncMock(return_value=sent)
    runner = MagicMock()
    runner.interrupt = AsyncMock()
    sessions = MagicMock()
    sessions.save = AsyncMock()
    sessions.get = AsyncMock(return_value=None)
    return thread, runner, sessions


class TestRunHelperLoungeInjection:
    """The lounge is never injected (#758), and #766 stopped reading it per turn."""

    async def test_lounge_repo_ignored(self) -> None:
        """A lounge_repo is still accepted but not read: the context it fed was
        never delivered (tmux TUI has no --append-system-prompt, #758), so #766
        stopped building it. The user prompt must be passed unchanged.
        """
        from c_lord.cogs._run_helper import run_claude_in_thread

        thread, runner, sessions = _turn_doubles()
        lounge_repo_mock = MagicMock()
        lounge_repo_mock.get_recent = AsyncMock(
            return_value=[LoungeMessage(id=1, label="w1", message="hi", posted_at="2031-01-01")]
        )
        lounge_repo_mock.post = AsyncMock()
        prompts_seen: list[str] = []

        async def scripted_run(prompt: str, session_id: str | None = None):  # type: ignore[no-untyped-def]
            from c_lord.claude.types import MessageType, StreamEvent

            prompts_seen.append(prompt)
            yield StreamEvent(message_type=MessageType.SYSTEM, session_id="abcd-1234")
            yield StreamEvent(
                message_type=MessageType.RESULT,
                is_complete=True,
                text="done",
                session_id="abcd-1234",
            )

        runner.run = scripted_run

        await run_claude_in_thread(
            thread,
            runner,
            sessions,
            "what the user typed",
            None,
            lounge_repo=lounge_repo_mock,
        )

        assert prompts_seen == ["what the user typed"]

        # #766: the lounge is not read — nothing is built from it any more.
        lounge_repo_mock.get_recent.assert_not_called()

        # clone() is NOT called — tmux TUI mode doesn't support --append-system-prompt.
        runner.clone.assert_not_called()
        lounge_repo_mock.post.assert_not_called()

    async def test_no_lounge_context_when_repo_is_none(self) -> None:
        """Without a lounge the turn runs just the same: same runner, same prompt."""
        from c_lord.cogs._run_helper import run_claude_in_thread

        thread, runner, sessions = _turn_doubles()
        prompts_seen: list[str] = []

        async def scripted_run(prompt: str, session_id: str | None = None):  # type: ignore[no-untyped-def]
            from c_lord.claude.types import MessageType, StreamEvent

            prompts_seen.append(prompt)
            yield StreamEvent(message_type=MessageType.SYSTEM, session_id="abcd-1234")
            yield StreamEvent(
                message_type=MessageType.RESULT,
                is_complete=True,
                text="done",
                session_id="abcd-1234",
            )

        runner.run = scripted_run

        await run_claude_in_thread(
            thread, runner, sessions, "what the user typed", None, lounge_repo=None
        )

        assert prompts_seen == ["what the user typed"]
        runner.clone.assert_not_called()
