"""REST API の制御面 (``c_lord.ext.api_server``) を HTTP 越しに確かめる。

どのテストも ``ApiServer.app`` を aiohttp のテストサーバに載せ、実際にリクエストを送る。
通知の保存先は本物の SQLite、Discord だけをモックにする。
"""

from __future__ import annotations

import asyncio
from collections.abc import AsyncIterator
from contextlib import asynccontextmanager
from datetime import datetime
from pathlib import Path
from unittest.mock import AsyncMock, MagicMock

import discord
import pytest
from aiohttp.test_utils import TestClient, TestServer

from c_lord.database.notification_repo import NotificationRepository
from c_lord.ext.api_server import ApiServer

DEFAULT_CHANNEL = 12345
JSON_BODY = {"Content-Type": "application/json"}


@asynccontextmanager
async def _serving(api: ApiServer) -> AsyncIterator[TestClient]:
    """``api`` を listen させたテストクライアント。ブロックを抜けると閉じる。"""
    client = TestClient(TestServer(api.app))
    await client.start_server()
    try:
        yield client
    finally:
        await client.close()


@pytest.fixture(autouse=True)
def _lounge_channel_not_inherited(monkeypatch: pytest.MonkeyPatch) -> None:
    """``COORDINATION_CHANNEL_ID`` が環境に残っていても lounge 転送先にならないようにする。"""
    monkeypatch.delenv("COORDINATION_CHANNEL_ID", raising=False)


@pytest.fixture
async def repo(tmp_path: Path) -> NotificationRepository:
    """本物の通知リポジトリ（テストごとに空の DB）。"""
    notifications = NotificationRepository(str(tmp_path / "notifications.db"))
    await notifications.init_db()
    return notifications


@pytest.fixture
def channel() -> MagicMock:
    """bot のキャッシュに居るテキストチャンネル。"""
    ch = MagicMock(spec=discord.TextChannel)
    ch.id = DEFAULT_CHANNEL
    ch.send = AsyncMock()
    return ch


@pytest.fixture
def bot(channel: MagicMock) -> MagicMock:
    """どの id を引いても ``channel`` を返す bot。cog は 1 つも載っていない。"""
    b = MagicMock()
    b.get_channel.return_value = channel
    b.fetch_channel = AsyncMock(return_value=channel)
    b.cogs = {}
    return b


@pytest.fixture
async def client(repo: NotificationRepository, bot: MagicMock) -> AsyncIterator[TestClient]:
    """secret 無し・既定チャンネルありの API。"""
    async with _serving(ApiServer(repo=repo, bot=bot, default_channel_id=DEFAULT_CHANNEL)) as c:
        yield c


@pytest.fixture
async def auth_client(repo: NotificationRepository, bot: MagicMock) -> AsyncIterator[TestClient]:
    """``api_secret`` を設定した API。"""
    api = ApiServer(
        repo=repo, bot=bot, default_channel_id=DEFAULT_CHANNEL, api_secret="correct-horse"
    )
    async with _serving(api) as c:
        yield c


def _uncached(bot: MagicMock, *, fetched: object = None, error: Exception | None = None) -> None:
    """キャッシュに無いチャンネルにする。fetch は ``fetched`` を返すか ``error`` を上げる。"""
    bot.get_channel.return_value = None
    bot.fetch_channel = AsyncMock(return_value=fetched, side_effect=error)


async def _post_broken_json(client: TestClient, path: str):
    """JSON として読めない本文を ``path`` に POST する。"""
    return await client.post(path, data=b"{not json", headers=JSON_BODY)


class TestHealth:
    """GET /api/health — 生きていることだけを答える。"""

    async def test_health_answers_ok_with_a_local_timestamp(self, client: TestClient) -> None:
        resp = await client.get("/api/health")

        assert resp.status == 200
        body = await resp.json()
        assert body["status"] == "ok"
        stamp = datetime.fromisoformat(body["timestamp"])
        assert stamp.tzinfo is None, "サーバのローカル時刻（タイムゾーン無し）で返す"
        assert abs((datetime.now() - stamp).total_seconds()) < 60

    async def test_health_also_answers_head(self, client: TestClient) -> None:
        """GET のルートは HEAD にも応える（監視が HEAD で叩く）。"""
        resp = await client.head("/api/health")

        assert resp.status == 200

    async def test_unknown_path_is_404(self, client: TestClient) -> None:
        resp = await client.get("/api/no-such-endpoint")

        assert resp.status == 404


class TestNotify:
    """POST /api/notify — embed を 1 通すぐ送る。"""

    async def test_message_is_sent_as_an_embed_to_the_default_channel(
        self, client: TestClient, bot: MagicMock, channel: MagicMock
    ) -> None:
        resp = await client.post("/api/notify", json={"message": "デプロイ完了"})

        assert resp.status == 200
        assert await resp.json() == {"status": "sent"}
        bot.get_channel.assert_called_with(DEFAULT_CHANNEL)
        channel.send.assert_awaited_once()
        assert channel.send.call_args.args == (), "本文 (content) は付けない"
        embed = channel.send.call_args.kwargs["embed"]
        assert embed.description == "デプロイ完了"
        assert embed.title == "Notification"
        assert embed.colour.value == 0x00BFFF

    async def test_title_and_color_are_used_when_given(
        self, client: TestClient, channel: MagicMock
    ) -> None:
        resp = await client.post(
            "/api/notify", json={"message": "m", "title": "CI", "color": 0xFF0000}
        )

        assert resp.status == 200
        embed = channel.send.call_args.kwargs["embed"]
        assert (embed.title, embed.colour.value) == ("CI", 0xFF0000)

    async def test_color_zero_falls_back_to_the_default_color(
        self, client: TestClient, channel: MagicMock
    ) -> None:
        """notify では ``color: 0`` は「指定なし」と同じ扱い（schedule の保存値とは違う）。"""
        await client.post("/api/notify", json={"message": "m", "color": 0})

        assert channel.send.call_args.kwargs["embed"].colour.value == 0x00BFFF

    async def test_channel_id_in_the_body_wins_over_the_default(
        self, client: TestClient, bot: MagicMock
    ) -> None:
        resp = await client.post("/api/notify", json={"message": "m", "channel_id": 777})

        assert resp.status == 200
        bot.get_channel.assert_called_with(777)

    async def test_channel_missing_from_the_cache_is_fetched(
        self, client: TestClient, bot: MagicMock
    ) -> None:
        fetched = MagicMock()
        fetched.send = AsyncMock()
        _uncached(bot, fetched=fetched)

        resp = await client.post("/api/notify", json={"message": "m"})

        assert resp.status == 200
        bot.fetch_channel.assert_awaited_once_with(DEFAULT_CHANNEL)
        fetched.send.assert_awaited_once()

    async def test_fetch_failure_is_500_with_the_reason(
        self, client: TestClient, bot: MagicMock
    ) -> None:
        _uncached(bot, error=RuntimeError("Unknown Channel"))

        resp = await client.post("/api/notify", json={"message": "m"})

        assert resp.status == 500
        assert await resp.json() == {"error": "Unknown Channel"}

    async def test_channel_that_cannot_be_sent_to_is_400(
        self, client: TestClient, bot: MagicMock
    ) -> None:
        bot.get_channel.return_value = MagicMock(spec=["id"])

        resp = await client.post("/api/notify", json={"message": "m"})

        assert resp.status == 400
        assert await resp.json() == {"error": "Channel is not messageable"}

    @pytest.mark.parametrize("body", [{}, {"message": ""}, {"message": None}, {"title": "t"}])
    async def test_message_is_required(
        self, client: TestClient, channel: MagicMock, body: dict
    ) -> None:
        resp = await client.post("/api/notify", json=body)

        assert resp.status == 400
        assert await resp.json() == {"error": "message is required"}
        channel.send.assert_not_called()

    async def test_broken_json_is_400(self, client: TestClient, channel: MagicMock) -> None:
        resp = await _post_broken_json(client, "/api/notify")

        assert resp.status == 400
        assert await resp.json() == {"error": "Invalid JSON"}
        channel.send.assert_not_called()

    async def test_json_that_is_not_an_object_is_400(self, client: TestClient) -> None:
        resp = await client.post("/api/notify", json=["message"])

        assert resp.status == 400
        assert await resp.json() == {"error": "Body must be a JSON object"}

    async def test_no_channel_anywhere_is_400(
        self, repo: NotificationRepository, bot: MagicMock, channel: MagicMock
    ) -> None:
        """本文にも既定にもチャンネルが無ければ、どこにも送らない。"""
        async with _serving(ApiServer(repo=repo, bot=bot)) as c:
            resp = await c.post("/api/notify", json={"message": "m"})

            assert resp.status == 400
            assert await resp.json() == {"error": "No channel specified"}
        channel.send.assert_not_called()


class TestSchedule:
    """POST /api/schedule — 予約通知を DB に 1 件入れる。Discord には触らない。"""

    async def test_notification_is_stored_and_its_id_returned(
        self, client: TestClient, repo: NotificationRepository, channel: MagicMock
    ) -> None:
        resp = await client.post(
            "/api/schedule",
            json={"message": "朝会", "scheduled_at": "2030-01-02T09:30:00", "title": "予定"},
        )

        assert resp.status == 200
        body = await resp.json()
        assert body["status"] == "scheduled"
        (row,) = await repo.get_pending()
        assert row["id"] == body["id"]
        assert (row["message"], row["title"]) == ("朝会", "予定")
        assert row["scheduled_at"] == "2030-01-02T09:30:00"
        assert row["source"] == "api"
        channel.send.assert_not_called()

    async def test_defaults_are_no_title_default_color_and_no_channel(
        self, client: TestClient, repo: NotificationRepository
    ) -> None:
        """``channel_id`` を省いても既定チャンネルは保存しない（送るときに決まる）。"""
        await client.post("/api/schedule", json={"message": "m", "scheduled_at": "2030-01-02"})

        (row,) = await repo.get_pending()
        assert row["title"] is None
        assert row["color"] == 0x00BFFF
        assert row["channel_id"] is None

    async def test_channel_and_color_from_the_body_are_stored(
        self, client: TestClient, repo: NotificationRepository
    ) -> None:
        await client.post(
            "/api/schedule",
            json={"message": "m", "scheduled_at": "2030-01-02", "channel_id": 42, "color": 0},
        )

        (row,) = await repo.get_pending()
        assert row["channel_id"] == 42
        assert row["color"] == 0, "0（黒）は既定色に置き換えない"

    @pytest.mark.parametrize(
        ("given", "stored"),
        [
            ("2030-01-02T09:30:15.987654", "2030-01-02T09:30:15"),
            ("2030-01-02T09:30:15+09:00", "2030-01-02T09:30:15"),
            ("2030-01-02 09:30", "2030-01-02T09:30:00"),
        ],
        ids=["fraction-dropped", "offset-dropped-not-converted", "space-separator"],
    )
    async def test_time_is_stored_to_the_second_as_written(
        self, client: TestClient, repo: NotificationRepository, given: str, stored: str
    ) -> None:
        resp = await client.post("/api/schedule", json={"message": "m", "scheduled_at": given})

        assert resp.status == 200
        (row,) = await repo.get_pending()
        assert row["scheduled_at"] == stored

    async def test_message_is_required(
        self, client: TestClient, repo: NotificationRepository
    ) -> None:
        resp = await client.post("/api/schedule", json={"scheduled_at": "2030-01-02T09:30:00"})

        assert resp.status == 400
        assert await resp.json() == {"error": "message is required"}
        assert await repo.get_pending() == []

    @pytest.mark.parametrize("body", [{"message": "m"}, {"message": "m", "scheduled_at": ""}])
    async def test_scheduled_at_is_required(
        self, client: TestClient, repo: NotificationRepository, body: dict
    ) -> None:
        resp = await client.post("/api/schedule", json=body)

        assert resp.status == 400
        assert await resp.json() == {"error": "scheduled_at is required"}
        assert await repo.get_pending() == []

    @pytest.mark.parametrize("when", ["tomorrow", "2030-13-45", 1893456000])
    async def test_scheduled_at_must_be_iso_8601(
        self, client: TestClient, repo: NotificationRepository, when: object
    ) -> None:
        resp = await client.post("/api/schedule", json={"message": "m", "scheduled_at": when})

        assert resp.status == 400
        assert await resp.json() == {"error": "scheduled_at must be ISO 8601 format"}
        assert await repo.get_pending() == []

    async def test_bad_color_is_reported_before_an_unparseable_time(
        self, client: TestClient
    ) -> None:
        """検証の順序: 時刻の型 → channel/color → 時刻の解釈。"""
        resp = await client.post(
            "/api/schedule", json={"message": "m", "scheduled_at": "tomorrow", "color": -1}
        )

        assert resp.status == 400
        assert "color" in (await resp.json())["error"]

    async def test_broken_json_is_400(self, client: TestClient) -> None:
        resp = await _post_broken_json(client, "/api/schedule")

        assert resp.status == 400
        assert await resp.json() == {"error": "Invalid JSON"}


class TestListScheduled:
    """GET /api/scheduled — まだ送っていない予約の一覧。"""

    async def test_nothing_scheduled_is_an_empty_list(self, client: TestClient) -> None:
        resp = await client.get("/api/scheduled")

        assert resp.status == 200
        assert await resp.json() == {"notifications": []}

    async def test_scheduled_notifications_are_listed(self, client: TestClient) -> None:
        for message, when in (("later", "2031-01-01T00:00:00"), ("sooner", "2030-01-01T00:00:00")):
            await client.post("/api/schedule", json={"message": message, "scheduled_at": when})

        resp = await client.get("/api/scheduled")

        listed = (await resp.json())["notifications"]
        assert [n["message"] for n in listed] == ["sooner", "later"]
        assert all(n["status"] == "pending" for n in listed)


class TestCancelScheduled:
    """DELETE /api/scheduled/{id} — まだ送っていない予約を取り消す。"""

    async def _schedule(self, client: TestClient) -> int:
        resp = await client.post(
            "/api/schedule", json={"message": "m", "scheduled_at": "2030-01-02T09:30:00"}
        )
        return (await resp.json())["id"]

    async def test_pending_notification_is_cancelled(self, client: TestClient) -> None:
        notification_id = await self._schedule(client)

        resp = await client.delete(f"/api/scheduled/{notification_id}")

        assert resp.status == 200
        assert await resp.json() == {"status": "cancelled"}
        assert (await (await client.get("/api/scheduled")).json()) == {"notifications": []}

    async def test_unknown_id_is_404(self, client: TestClient) -> None:
        resp = await client.delete("/api/scheduled/424242")

        assert resp.status == 404
        assert await resp.json() == {"error": "Not found or already processed"}

    async def test_cancelling_twice_is_404_the_second_time(self, client: TestClient) -> None:
        notification_id = await self._schedule(client)
        await client.delete(f"/api/scheduled/{notification_id}")

        resp = await client.delete(f"/api/scheduled/{notification_id}")

        assert resp.status == 404

    async def test_id_that_is_not_a_number_is_400(self, client: TestClient) -> None:
        resp = await client.delete("/api/scheduled/first")

        assert resp.status == 400
        assert await resp.json() == {"error": "Invalid ID"}


class TestAuthentication:
    """Bearer auth. The *other* way in — being the bot's own Unix user — and
    the refusal of everyone else live in ``tests/test_api_peer_auth.py`` (#457).
    """

    async def test_health_is_served_to_the_bots_own_user(self, auth_client: TestClient) -> None:
        """Health used to bypass auth for *everyone*; now it is UID-gated (#457)."""
        resp = await auth_client.get("/api/health")

        assert resp.status == 200

    async def test_own_user_needs_no_auth_header(self, auth_client: TestClient) -> None:
        """A caller running as the bot's user can already read the secret.

        Before #457 this answered 401. Requiring the header from our own user
        bought nothing (the secret is in ``.env``, readable by that same user)
        while breaking the control plane, since ``CLORD_API_SECRET`` is stripped
        from the tmux environment by #353 — a Claude session has no way to send
        it. A foreign UID with no header gets 403, which is the case that
        matters and is pinned in ``test_api_peer_auth.py``.
        """
        resp = await auth_client.get("/api/scheduled")

        assert resp.status == 200

    async def test_wrong_bearer_token_is_401_even_from_the_bots_own_user(
        self, auth_client: TestClient
    ) -> None:
        """トークンを出したのに違っていたら、同じ UID でも通さない。"""
        resp = await auth_client.get("/api/health", headers={"Authorization": "Bearer wrong"})

        assert resp.status == 401
        assert await resp.json() == {"error": "Invalid token"}

    async def test_correct_bearer_token_is_served(self, auth_client: TestClient) -> None:
        resp = await auth_client.get(
            "/api/scheduled", headers={"Authorization": "Bearer correct-horse"}
        )

        assert resp.status == 200
        assert await resp.json() == {"notifications": []}

    async def test_token_must_match_in_full(self, auth_client: TestClient) -> None:
        """前方一致・末尾の空白つきは別のトークン。"""
        for token in ("correct", "correct-horse ", "correct-horse-battery"):
            resp = await auth_client.get(
                "/api/health", headers={"Authorization": f"Bearer {token}"}
            )
            assert resp.status == 401, token

    async def test_another_auth_scheme_is_judged_by_uid_not_as_a_bad_token(
        self, auth_client: TestClient
    ) -> None:
        """``Bearer`` 以外のヘッダはトークンとして扱わず、接続元の UID で決める。"""
        resp = await auth_client.get("/api/health", headers={"Authorization": "Basic abc"})

        assert resp.status == 200

    async def test_bearer_header_without_a_configured_secret_is_ignored(
        self, client: TestClient
    ) -> None:
        resp = await client.get("/api/health", headers={"Authorization": "Bearer anything"})

        assert resp.status == 200

    async def test_refused_request_never_reaches_the_handler(
        self, auth_client: TestClient, channel: MagicMock
    ) -> None:
        resp = await auth_client.post(
            "/api/notify", json={"message": "m"}, headers={"Authorization": "Bearer wrong"}
        )

        assert resp.status == 401
        channel.send.assert_not_called()


class TestSpawn:
    """POST /api/spawn — 新しいスレッドを作って Claude を起動する。"""

    @pytest.fixture
    def spawned_thread(self) -> MagicMock:
        thread = MagicMock(spec=discord.Thread)
        thread.id = 987654321098765432
        thread.name = "調査スレッド"
        return thread

    @pytest.fixture
    def chat_cog(self, bot: MagicMock, spawned_thread: MagicMock) -> MagicMock:
        """bot に載っている ClaudeChatCog。"""
        cog = MagicMock()
        cog.spawn_session = AsyncMock(return_value=spawned_thread)
        bot.cogs = {"ClaudeChatCog": cog}
        return cog

    async def test_spawn_answers_201_with_the_new_thread(
        self, client: TestClient, chat_cog: MagicMock
    ) -> None:
        resp = await client.post("/api/spawn", json={"prompt": "ログを調べて"})

        assert resp.status == 201
        assert await resp.json() == {
            "status": "spawned",
            "thread_id": "987654321098765432",  # 文字列: JS の数値精度を超える
            "thread_name": "調査スレッド",
        }

    async def test_channel_and_trimmed_prompt_are_handed_to_the_cog(
        self, client: TestClient, chat_cog: MagicMock, channel: MagicMock
    ) -> None:
        await client.post("/api/spawn", json={"prompt": "  ログを調べて\n"})

        chat_cog.spawn_session.assert_awaited_once()
        assert chat_cog.spawn_session.call_args.args == (channel, "ログを調べて")

    async def test_thread_name_is_passed_by_keyword(
        self, client: TestClient, chat_cog: MagicMock
    ) -> None:
        await client.post("/api/spawn", json={"prompt": "p", "thread_name": "夜間バッチ"})

        assert chat_cog.spawn_session.call_args.kwargs == {"thread_name": "夜間バッチ"}

    @pytest.mark.parametrize("extra", [{}, {"thread_name": None}, {"thread_name": ""}])
    async def test_thread_name_is_none_when_not_given(
        self, client: TestClient, chat_cog: MagicMock, extra: dict
    ) -> None:
        await client.post("/api/spawn", json={"prompt": "p", **extra})

        assert chat_cog.spawn_session.call_args.kwargs == {"thread_name": None}

    @pytest.mark.parametrize("body", [{}, {"prompt": None}, {"prompt": ""}, {"prompt": " \n\t "}])
    async def test_prompt_is_required(
        self, client: TestClient, chat_cog: MagicMock, body: dict
    ) -> None:
        resp = await client.post("/api/spawn", json=body)

        assert resp.status == 400
        assert await resp.json() == {"error": "prompt is required"}
        chat_cog.spawn_session.assert_not_called()

    async def test_without_the_chat_cog_it_is_503_before_any_discord_call(
        self, client: TestClient, bot: MagicMock
    ) -> None:
        resp = await client.post("/api/spawn", json={"prompt": "p"})

        assert resp.status == 503
        assert await resp.json() == {"error": "ClaudeChatCog is not loaded"}
        bot.get_channel.assert_not_called()

    async def test_no_channel_anywhere_is_400(
        self, repo: NotificationRepository, bot: MagicMock, chat_cog: MagicMock
    ) -> None:
        async with _serving(ApiServer(repo=repo, bot=bot)) as c:
            resp = await c.post("/api/spawn", json={"prompt": "p"})

            assert resp.status == 400
            assert await resp.json() == {"error": "No channel specified"}
        chat_cog.spawn_session.assert_not_called()

    async def test_broken_json_is_400(self, client: TestClient, chat_cog: MagicMock) -> None:
        resp = await _post_broken_json(client, "/api/spawn")

        assert resp.status == 400
        assert await resp.json() == {"error": "Invalid JSON"}
        chat_cog.spawn_session.assert_not_called()

    @pytest.mark.parametrize("channel_id", [555, "555"], ids=["number", "digit-string"])
    async def test_channel_id_in_the_body_is_the_channel_looked_up(
        self, client: TestClient, bot: MagicMock, chat_cog: MagicMock, channel_id: object
    ) -> None:
        resp = await client.post("/api/spawn", json={"prompt": "p", "channel_id": channel_id})

        assert resp.status == 201
        bot.get_channel.assert_called_once_with(555)

    async def test_channel_id_that_is_not_a_number_is_400(
        self, client: TestClient, chat_cog: MagicMock
    ) -> None:
        resp = await client.post("/api/spawn", json={"prompt": "p", "channel_id": "general"})

        assert resp.status == 400
        assert await resp.json() == {"error": "channel_id must be an integer"}

    async def test_channel_that_cannot_hold_threads_is_400(
        self, client: TestClient, bot: MagicMock, chat_cog: MagicMock
    ) -> None:
        """スレッド自身や VC を渡されても、そこにスレッドは作れない。"""
        bot.get_channel.return_value = MagicMock(spec=discord.Thread)

        resp = await client.post("/api/spawn", json={"prompt": "p"})

        assert resp.status == 400
        assert await resp.json() == {
            "error": "Channel must be a text channel that supports threads"
        }
        chat_cog.spawn_session.assert_not_called()

    async def test_uncached_channel_is_fetched(
        self, client: TestClient, bot: MagicMock, chat_cog: MagicMock
    ) -> None:
        fetched = MagicMock(spec=discord.TextChannel)
        _uncached(bot, fetched=fetched)

        resp = await client.post("/api/spawn", json={"prompt": "p"})

        assert resp.status == 201
        assert chat_cog.spawn_session.call_args.args[0] is fetched

    async def test_fetch_failure_is_500_with_the_reason(
        self, client: TestClient, bot: MagicMock, chat_cog: MagicMock
    ) -> None:
        _uncached(bot, error=RuntimeError("Missing Access"))

        resp = await client.post("/api/spawn", json={"prompt": "p"})

        assert resp.status == 500
        assert await resp.json() == {"error": "Missing Access"}

    async def test_failure_inside_spawn_session_is_500_with_the_reason(
        self, client: TestClient, chat_cog: MagicMock
    ) -> None:
        chat_cog.spawn_session.side_effect = RuntimeError("tmux is not running")

        resp = await client.post("/api/spawn", json={"prompt": "p"})

        assert resp.status == 500
        assert await resp.json() == {"error": "tmux is not running"}


class TestMarkResume:
    """POST /api/mark-resume — 再起動後にこのスレッドを再開させる印を付ける。"""

    THREAD = 111222333444

    @pytest.fixture
    async def resume_repo(self, tmp_path: Path):
        from c_lord.database.models import init_db as _init
        from c_lord.database.resume_repo import PendingResumeRepository

        db = str(tmp_path / "resume.db")
        await _init(db)
        return PendingResumeRepository(db)

    @pytest.fixture
    def api(self, repo: NotificationRepository, bot: MagicMock, resume_repo) -> ApiServer:
        return ApiServer(repo=repo, bot=bot, resume_repo=resume_repo)

    @pytest.fixture
    async def resume_client(self, api: ApiServer) -> AsyncIterator[TestClient]:
        async with _serving(api) as c:
            yield c

    @staticmethod
    def _session_store(*, session_id: str | None = None, error: Exception | None = None):
        """``thread → session`` の記録。``session_id`` があればそのレコードを返す。"""
        record = None if session_id is None else MagicMock(session_id=session_id)
        store = MagicMock()
        store.get = AsyncMock(return_value=record, side_effect=error)
        return store

    async def test_thread_is_marked_with_the_default_reason(
        self, resume_client: TestClient, resume_repo
    ) -> None:
        resp = await resume_client.post("/api/mark-resume", json={"thread_id": self.THREAD})

        assert resp.status == 201
        (pending,) = await resume_repo.get_pending()
        assert await resp.json() == {"status": "marked", "id": pending.id}
        assert pending.thread_id == self.THREAD
        assert pending.reason == "self_restart"
        assert pending.session_id is None
        assert pending.resume_prompt is None

    async def test_every_given_field_is_stored(
        self, resume_client: TestClient, resume_repo
    ) -> None:
        resp = await resume_client.post(
            "/api/mark-resume",
            json={
                "thread_id": str(self.THREAD),  # Discord の id は文字列でも来る
                "session_id": "0f3c-ab12",
                "reason": "deploy",
                "resume_prompt": "続きから",
            },
        )

        assert resp.status == 201
        (pending,) = await resume_repo.get_pending()
        assert pending.thread_id == self.THREAD
        assert (pending.session_id, pending.reason, pending.resume_prompt) == (
            "0f3c-ab12",
            "deploy",
            "続きから",
        )

    async def test_empty_optional_fields_count_as_not_given(
        self, resume_client: TestClient, resume_repo
    ) -> None:
        await resume_client.post(
            "/api/mark-resume",
            json={"thread_id": self.THREAD, "session_id": "", "reason": "", "resume_prompt": ""},
        )

        (pending,) = await resume_repo.get_pending()
        assert (pending.session_id, pending.reason, pending.resume_prompt) == (
            None,
            "self_restart",
            None,
        )

    @pytest.mark.parametrize("body", [{}, {"thread_id": None}, {"thread_id": 0}, {"reason": "x"}])
    async def test_thread_id_is_required(
        self, resume_client: TestClient, resume_repo, body: dict
    ) -> None:
        resp = await resume_client.post("/api/mark-resume", json=body)

        assert resp.status == 400
        assert await resp.json() == {"error": "thread_id is required"}
        assert await resume_repo.get_pending() == []

    async def test_thread_id_that_is_not_a_number_is_400(
        self, resume_client: TestClient, resume_repo
    ) -> None:
        resp = await resume_client.post("/api/mark-resume", json={"thread_id": "my-thread"})

        assert resp.status == 400
        assert await resp.json() == {"error": "thread_id must be an integer"}
        assert await resume_repo.get_pending() == []

    async def test_without_a_resume_store_it_is_503(self, client: TestClient) -> None:
        """``client`` の API には resume_repo が無い。本文より先にそれを答える。"""
        resp = await _post_broken_json(client, "/api/mark-resume")

        assert resp.status == 503
        assert await resp.json() == {
            "error": "PendingResumeRepository not configured (resume_repo is None)"
        }

    async def test_broken_json_is_400(self, resume_client: TestClient) -> None:
        resp = await _post_broken_json(resume_client, "/api/mark-resume")

        assert resp.status == 400
        assert await resp.json() == {"error": "Invalid JSON"}

    async def test_session_id_is_taken_from_the_threads_record_when_omitted(
        self, api: ApiServer, resume_client: TestClient, resume_repo
    ) -> None:
        api.session_repo = self._session_store(session_id="recorded-session")

        resp = await resume_client.post("/api/mark-resume", json={"thread_id": self.THREAD})

        assert resp.status == 201
        api.session_repo.get.assert_awaited_once_with(self.THREAD)
        (pending,) = await resume_repo.get_pending()
        assert pending.session_id == "recorded-session"

    async def test_session_id_in_the_body_is_not_replaced_by_the_record(
        self, api: ApiServer, resume_client: TestClient, resume_repo
    ) -> None:
        api.session_repo = self._session_store(session_id="recorded-session")

        await resume_client.post(
            "/api/mark-resume", json={"thread_id": self.THREAD, "session_id": "from-body"}
        )

        (pending,) = await resume_repo.get_pending()
        assert pending.session_id == "from-body"
        api.session_repo.get.assert_not_called()

    @pytest.mark.parametrize(
        "store_kwargs",
        [{}, {"error": RuntimeError("db is locked")}],
        ids=["no-record", "lookup-fails"],
    )
    async def test_thread_is_still_marked_when_no_session_id_can_be_found(
        self, api: ApiServer, resume_client: TestClient, resume_repo, store_kwargs: dict
    ) -> None:
        """記録の引き当ては best effort。失敗しても印は付く。"""
        api.session_repo = self._session_store(**store_kwargs)

        resp = await resume_client.post("/api/mark-resume", json={"thread_id": self.THREAD})

        assert resp.status == 201
        (pending,) = await resume_repo.get_pending()
        assert pending.session_id is None

    async def test_marking_again_replaces_the_earlier_mark(
        self, resume_client: TestClient, resume_repo
    ) -> None:
        for reason in ("first", "second"):
            await resume_client.post(
                "/api/mark-resume", json={"thread_id": self.THREAD, "reason": reason}
            )

        (pending,) = await resume_repo.get_pending()
        assert pending.reason == "second"


class TestLifecycle:
    """``start()`` / ``stop()`` — ソケットを開くのは start だけ。"""

    async def test_start_listens_and_records_the_port_actually_bound(
        self, repo: NotificationRepository, bot: MagicMock, monkeypatch: pytest.MonkeyPatch
    ) -> None:
        from c_lord import api_endpoint

        monkeypatch.delenv("CLORD_API_URL", raising=False)  # 運用者が書いた URL が勝つため
        api = ApiServer(repo=repo, bot=bot, port=0)
        await api.start()
        try:
            assert api.port != 0, "port=0 は OS が選んだ実際のポートに置き換わる"
            assert api_endpoint.current() is not None
            assert f":{api.port}" in api_endpoint.current()
        finally:
            await api.stop()
        assert api_endpoint.current() is None

    async def test_taken_port_raises_and_leaves_nothing_running(
        self, repo: NotificationRepository, bot: MagicMock
    ) -> None:
        first = ApiServer(repo=repo, bot=bot, port=0)
        await first.start()
        try:
            second = ApiServer(repo=repo, bot=bot, port=first.port)

            with pytest.raises(OSError):
                await second.start()

            assert second._runner is None
            await second.stop()  # 失敗した start の後でも安全
        finally:
            await first.stop()

    async def test_stop_before_start_is_harmless(
        self, repo: NotificationRepository, bot: MagicMock
    ) -> None:
        api = ApiServer(repo=repo, bot=bot)

        await api.stop()

        assert api._runner is None


class TestPostThreadMessage:
    """Tests for POST /api/threads/{id}/messages — post message to a Discord thread."""

    @pytest.fixture
    def thread_mock(self) -> MagicMock:
        import discord

        thread = MagicMock(spec=discord.Thread)
        thread.id = 111222333
        thread.send = AsyncMock()
        return thread

    @pytest.fixture
    def bot_with_thread(self, thread_mock: MagicMock) -> MagicMock:
        b = MagicMock()
        b.get_channel.return_value = thread_mock
        return b

    @pytest.fixture
    async def thread_client(
        self, repo: NotificationRepository, bot_with_thread: MagicMock
    ) -> TestClient:
        api = ApiServer(repo=repo, bot=bot_with_thread, default_channel_id=12345)
        async with _serving(api) as client:
            yield client

    async def test_post_message_returns_200(
        self, thread_client: TestClient, thread_mock: MagicMock
    ) -> None:
        resp = await thread_client.post(
            "/api/threads/111222333/messages",
            json={"content": "Hello from CLI"},
        )
        assert resp.status == 200
        assert await resp.json() == {"status": "sent"}
        thread_mock.send.assert_called_once()
        sent_text = thread_mock.send.call_args.args[0]
        assert "Hello from CLI" in sent_text

    @pytest.mark.asyncio
    async def test_post_message_with_source(
        self, thread_client: TestClient, thread_mock: MagicMock
    ) -> None:
        resp = await thread_client.post(
            "/api/threads/111222333/messages",
            json={"content": "Test input", "source": "tmux"},
        )
        assert resp.status == 200
        sent_text = thread_mock.send.call_args.args[0]
        assert "tmux" in sent_text.lower() or "Test input" in sent_text

    @pytest.mark.asyncio
    async def test_post_message_missing_content_returns_400(
        self, thread_client: TestClient
    ) -> None:
        resp = await thread_client.post("/api/threads/111222333/messages", json={})
        assert resp.status == 400
        data = await resp.json()
        assert "content" in data["error"]

    @pytest.mark.asyncio
    async def test_post_message_empty_content_returns_400(self, thread_client: TestClient) -> None:
        resp = await thread_client.post("/api/threads/111222333/messages", json={"content": "   "})
        assert resp.status == 400
        assert (await resp.json()) == {"error": "content is required"}

    async def test_post_message_invalid_thread_id_returns_400(
        self, thread_client: TestClient
    ) -> None:
        resp = await thread_client.post(
            "/api/threads/not-a-number/messages", json={"content": "Test"}
        )
        assert resp.status == 400
        assert (await resp.json()) == {"error": "Invalid thread_id"}

    async def test_post_message_thread_not_found(self, repo: NotificationRepository) -> None:
        bot = MagicMock()
        bot.get_channel.return_value = None
        bot.fetch_channel = AsyncMock(side_effect=Exception("Not found"))
        api = ApiServer(repo=repo, bot=bot, default_channel_id=12345)
        async with _serving(api) as client:
            resp = await client.post("/api/threads/999/messages", json={"content": "Test"})
            assert resp.status == 404
            assert (await resp.json()) == {"error": "Thread not found"}

    async def test_post_message_non_messageable_returns_400(
        self, repo: NotificationRepository
    ) -> None:
        bot = MagicMock()
        # Return a channel without send method
        channel = MagicMock(spec=[])
        bot.get_channel.return_value = channel
        api = ApiServer(repo=repo, bot=bot, default_channel_id=12345)
        async with _serving(api) as client:
            resp = await client.post("/api/threads/123/messages", json={"content": "Test"})
            assert resp.status == 400
            assert (await resp.json()) == {"error": "Channel is not messageable"}

    @pytest.mark.asyncio
    async def test_post_message_invalid_json_returns_400(self, thread_client: TestClient) -> None:
        resp = await thread_client.post(
            "/api/threads/111222333/messages",
            data=b"{not json",
            headers={"Content-Type": "application/json"},
        )
        assert resp.status == 400
        assert (await resp.json()) == {"error": "Invalid JSON"}


class TestReplyEndpoint:
    """Tests for POST /api/reply — Skill-based final reply (#52 Phase 1)."""

    @pytest.fixture
    def thread_mock(self) -> MagicMock:
        import discord

        thread = MagicMock(spec=discord.Thread)
        thread.id = 555666777
        thread.send = AsyncMock()
        return thread

    @pytest.fixture
    def bot_with_thread(self, thread_mock: MagicMock) -> MagicMock:
        b = MagicMock()
        b.get_channel.return_value = thread_mock
        return b

    @pytest.fixture
    async def reply_client(
        self, repo: NotificationRepository, bot_with_thread: MagicMock, tmp_path
    ) -> TestClient:
        api = ApiServer(repo=repo, bot=bot_with_thread, default_channel_id=12345)
        # progress_file is attached only from under the session dirs (#1039);
        # the tests below write theirs into tmp_path.
        api.session_dir_base = str(tmp_path)
        async with _serving(api) as client:
            yield client

    async def test_plain_content_posts_to_thread(
        self, reply_client: TestClient, thread_mock: MagicMock
    ) -> None:
        resp = await reply_client.post(
            "/api/reply",
            json={"thread_id": 555666777, "content": "final answer here"},
        )
        assert resp.status == 200
        assert await resp.json() == {"status": "sent"}
        thread_mock.send.assert_called_once()
        kwargs = thread_mock.send.call_args.kwargs
        args = thread_mock.send.call_args.args
        sent = kwargs.get("content") if "content" in kwargs else (args[0] if args else "")
        assert "final answer here" in sent
        # Unlike post_thread_message, /api/reply MUST NOT add a "-# 💻 (cli)" prefix.
        assert "💻" not in sent
        assert not sent.startswith("-# ")

    @pytest.mark.asyncio
    async def test_suppresses_url_embeds_by_default(
        self, reply_client: TestClient, thread_mock: MagicMock
    ) -> None:
        """#372: by default (CLORD_SHOW_URL_EMBEDS unset) Discord OGP/link
        previews are suppressed — send() is called with suppress_embeds=True."""
        resp = await reply_client.post(
            "/api/reply",
            json={"thread_id": 555666777, "content": "see https://example.com for details"},
        )
        assert resp.status == 200
        thread_mock.send.assert_called_once()
        assert thread_mock.send.call_args.kwargs.get("suppress_embeds") is True

    @pytest.mark.asyncio
    async def test_shows_url_embeds_when_enabled(
        self,
        repo: NotificationRepository,
        bot_with_thread: MagicMock,
        thread_mock: MagicMock,
    ) -> None:
        """#372: opt-in via show_url_embeds=True restores OGP previews —
        send() is called with suppress_embeds=False."""
        api = ApiServer(
            repo=repo,
            bot=bot_with_thread,
            default_channel_id=12345,
            show_url_embeds=True,
        )
        client = TestClient(TestServer(api.app))
        await client.start_server()
        try:
            resp = await client.post(
                "/api/reply",
                json={"thread_id": 555666777, "content": "see https://example.com for details"},
            )
            assert resp.status == 200
            thread_mock.send.assert_called_once()
            assert thread_mock.send.call_args.kwargs.get("suppress_embeds") is False
        finally:
            await client.close()

    @pytest.mark.asyncio
    async def test_missing_content_returns_400(self, reply_client: TestClient) -> None:
        resp = await reply_client.post("/api/reply", json={"thread_id": 555666777})
        assert resp.status == 400
        assert "content" in (await resp.json())["error"]

    @pytest.mark.asyncio
    async def test_missing_thread_id_returns_400(self, reply_client: TestClient) -> None:
        resp = await reply_client.post("/api/reply", json={"content": "hi"})
        assert resp.status == 400
        assert "thread_id" in (await resp.json())["error"]

    @pytest.mark.asyncio
    async def test_invalid_json_returns_400(self, reply_client: TestClient) -> None:
        resp = await reply_client.post(
            "/api/reply",
            data=b"not json",
            headers={"Content-Type": "application/json"},
        )
        assert resp.status == 400
        assert (await resp.json()) == {"error": "Invalid JSON"}

    @pytest.mark.asyncio
    async def test_thread_not_found_returns_404(self, repo: NotificationRepository) -> None:
        b = MagicMock()
        b.get_channel.return_value = None
        b.fetch_channel = AsyncMock(side_effect=Exception("not found"))
        api = ApiServer(repo=repo, bot=b, default_channel_id=1)
        async with _serving(api) as client:
            resp = await client.post(
                "/api/reply",
                json={"thread_id": 999, "content": "x"},
            )
            assert resp.status == 404
            assert (await resp.json()) == {"error": "Thread not found"}

    async def test_attachment_via_progress_file(
        self,
        reply_client: TestClient,
        thread_mock: MagicMock,
        tmp_path,
    ) -> None:
        """When `progress_file` is given, it is attached to the Discord message."""
        progress = tmp_path / "progress.txt"
        progress.write_text("step1\nstep2\n")

        resp = await reply_client.post(
            "/api/reply",
            json={
                "thread_id": 555666777,
                "content": "done",
                "progress_file": str(progress),
            },
        )
        assert resp.status == 200
        thread_mock.send.assert_called_once()
        kwargs = thread_mock.send.call_args.kwargs
        # Discord.py's send(file=...) — verify a file was passed
        assert "file" in kwargs or "files" in kwargs

    @pytest.mark.asyncio
    async def test_missing_progress_file_returns_400(
        self, reply_client: TestClient, tmp_path
    ) -> None:
        # Inside the session dirs (#1039): a path outside is ignored before it is
        # looked at, so it cannot tell whether a file exists there.
        resp = await reply_client.post(
            "/api/reply",
            json={
                "thread_id": 555666777,
                "content": "x",
                "progress_file": str(tmp_path / "no-such.txt"),
            },
        )
        assert resp.status == 400

    @pytest.mark.asyncio
    async def test_long_content_is_chunked_across_multiple_messages(
        self, reply_client: TestClient, thread_mock: MagicMock
    ) -> None:
        """Regression: replies longer than Discord's 2000-char limit were cut
        off. The endpoint must split them into multiple sends, each <= 2000,
        with no content lost."""
        long_content = "\n".join(f"sentence number {i} here." for i in range(400))
        assert len(long_content) > 2000

        resp = await reply_client.post(
            "/api/reply",
            json={"thread_id": 555666777, "content": long_content},
        )
        assert resp.status == 200
        assert thread_mock.send.call_count >= 2

        sent_bodies: list[str] = []
        for call in thread_mock.send.call_args_list:
            body = call.kwargs.get("content")
            if body is None and call.args:
                body = call.args[0]
            assert body is not None
            assert len(body) <= 2000
            sent_bodies.append(body)

        # Every original sentence survives across the chunks, in order.
        joined = "\n".join(sent_bodies)
        for i in range(400):
            assert f"sentence number {i} here." in joined

    @pytest.mark.asyncio
    async def test_long_content_attaches_files_to_last_chunk_only(
        self, reply_client: TestClient, thread_mock: MagicMock, tmp_path
    ) -> None:
        """When content is chunked, the optional attachment must ride on the
        final message (not be dropped or duplicated across chunks)."""
        progress = tmp_path / "progress.txt"
        progress.write_text("detail\n")
        long_content = "\n".join(f"row {i}" for i in range(700))
        assert len(long_content) > 2000

        resp = await reply_client.post(
            "/api/reply",
            json={
                "thread_id": 555666777,
                "content": long_content,
                "progress_file": str(progress),
            },
        )
        assert resp.status == 200
        calls = thread_mock.send.call_args_list
        assert len(calls) >= 2
        # Only the final send carries the file.
        for call in calls[:-1]:
            assert "file" not in call.kwargs and "files" not in call.kwargs
        last = calls[-1].kwargs
        assert "file" in last or "files" in last

    @pytest.mark.asyncio
    async def test_successful_reply_records_the_message(self, reply_client: TestClient) -> None:
        """The posted message is remembered so post-turn helpers (e.g. the
        context-usage line) can append to that bubble."""
        from c_lord.skills.reply_tracker import get_last_reply_message, reset_tracker

        reset_tracker()
        resp = await reply_client.post(
            "/api/reply",
            json={"thread_id": 555666777, "content": "answered"},
        )
        assert resp.status == 200
        assert get_last_reply_message(555666777) is not None

    @pytest.mark.asyncio
    async def test_failed_reply_does_not_record(self, reply_client: TestClient) -> None:
        """A 400 (missing content) MUST NOT be recorded as a successful reply."""
        from c_lord.skills.reply_tracker import get_last_reply_message, reset_tracker

        reset_tracker()
        resp = await reply_client.post(
            "/api/reply",
            json={"thread_id": 555666777},  # missing content
        )
        assert resp.status == 400
        assert get_last_reply_message(555666777) is None

    @pytest.mark.asyncio
    async def test_reply_to_trigger_without_progress_file(
        self, repo: NotificationRepository, bot_with_thread: MagicMock, thread_mock: MagicMock
    ) -> None:
        """Issue #201: a plain reply (no progress_file) on a thread that has a
        trigger_message_id must not raise UnboundLocalError on ``discord``.

        The ``discord`` module used to be imported only inside the
        ``if progress_file:`` block, so the later ``discord.MessageReference``
        reference raised ``UnboundLocalError`` -> 500 when progress_file was absent.
        """
        session_repo = MagicMock()
        record = MagicMock()
        record.trigger_message_id = 111222333
        session_repo.get = AsyncMock(return_value=record)

        api = ApiServer(
            repo=repo,
            bot=bot_with_thread,
            default_channel_id=12345,
            session_repo=session_repo,
        )
        async with _serving(api) as client:
            resp = await client.post(
                "/api/reply",
                json={"thread_id": 555666777, "content": "plain answer"},
            )
            assert resp.status == 200
            kwargs = thread_mock.send.call_args.kwargs
            assert "reference" in kwargs
            assert kwargs["reference"].message_id == 111222333


class TestPromptChoiceEndpoint:
    """Issue #63: POST /api/prompt-choice — Skill posts a choice prompt."""

    @pytest.fixture
    def thread_mock(self) -> MagicMock:
        import discord

        thread = MagicMock(spec=discord.Thread)
        thread.id = 777888999
        thread.send = AsyncMock()
        return thread

    @pytest.fixture
    def bot_with_thread(self, thread_mock: MagicMock) -> MagicMock:
        b = MagicMock()
        b.get_channel.return_value = thread_mock
        return b

    @pytest.fixture
    async def choice_client(
        self, repo: NotificationRepository, bot_with_thread: MagicMock
    ) -> TestClient:
        api = ApiServer(repo=repo, bot=bot_with_thread, default_channel_id=12345)
        async with _serving(api) as client:
            yield client

    @pytest.mark.asyncio
    async def test_posts_formatted_choice_message(
        self, choice_client: TestClient, thread_mock: MagicMock
    ) -> None:
        resp = await choice_client.post(
            "/api/prompt-choice",
            json={
                "thread_id": 777888999,
                "question": "Which approach do you prefer?",
                "choices": [
                    {"label": "1", "description": "Refactor in place"},
                    {"label": "2", "description": "Rewrite from scratch"},
                    {"label": "3", "description": "Leave as-is"},
                ],
            },
        )
        assert resp.status == 200, await resp.text()
        assert await resp.json() == {"status": "sent"}
        thread_mock.send.assert_called_once()
        kwargs = thread_mock.send.call_args.kwargs
        args = thread_mock.send.call_args.args
        sent = kwargs.get("content") if "content" in kwargs else (args[0] if args else "")
        assert "Which approach" in sent
        assert "Refactor in place" in sent
        assert "Rewrite from scratch" in sent
        assert "Leave as-is" in sent
        # Each label is shown so the user can reply with it.
        assert "1" in sent and "2" in sent and "3" in sent

    @pytest.mark.asyncio
    async def test_missing_question_returns_400(self, choice_client: TestClient) -> None:
        resp = await choice_client.post(
            "/api/prompt-choice",
            json={"thread_id": 777888999, "choices": [{"label": "a", "description": "x"}]},
        )
        assert resp.status == 400
        assert "question" in (await resp.json())["error"]

    @pytest.mark.asyncio
    async def test_missing_choices_returns_400(self, choice_client: TestClient) -> None:
        resp = await choice_client.post(
            "/api/prompt-choice",
            json={"thread_id": 777888999, "question": "q?"},
        )
        assert resp.status == 400
        assert "choices" in (await resp.json())["error"]

    @pytest.mark.asyncio
    async def test_empty_choices_returns_400(self, choice_client: TestClient) -> None:
        resp = await choice_client.post(
            "/api/prompt-choice",
            json={"thread_id": 777888999, "question": "q?", "choices": []},
        )
        assert resp.status == 400
        assert "choices" in (await resp.json())["error"]

    @pytest.mark.asyncio
    async def test_missing_thread_id_returns_400(self, choice_client: TestClient) -> None:
        resp = await choice_client.post(
            "/api/prompt-choice",
            json={
                "question": "q?",
                "choices": [{"label": "1", "description": "x"}],
            },
        )
        assert resp.status == 400
        assert "thread_id" in (await resp.json())["error"]

    @pytest.mark.asyncio
    async def test_invalid_json_returns_400(self, choice_client: TestClient) -> None:
        resp = await choice_client.post(
            "/api/prompt-choice",
            data=b"{not json",
            headers={"Content-Type": "application/json"},
        )
        assert resp.status == 400
        assert (await resp.json()) == {"error": "Invalid JSON"}

    async def test_thread_not_found_returns_404(self, repo: NotificationRepository) -> None:
        b = MagicMock()
        b.get_channel.return_value = None
        b.fetch_channel = AsyncMock(side_effect=Exception("not found"))
        api = ApiServer(repo=repo, bot=b, default_channel_id=1)
        async with _serving(api) as client:
            resp = await client.post(
                "/api/prompt-choice",
                json={
                    "thread_id": 999,
                    "question": "q?",
                    "choices": [{"label": "1", "description": "x"}],
                },
            )
            assert resp.status == 404
            assert (await resp.json()) == {"error": "Thread not found"}

    async def test_label_only_choice_accepted(
        self, choice_client: TestClient, thread_mock: MagicMock
    ) -> None:
        """A choice without description still posts the label."""
        resp = await choice_client.post(
            "/api/prompt-choice",
            json={
                "thread_id": 777888999,
                "question": "Yes or no?",
                "choices": [{"label": "yes"}, {"label": "no"}],
            },
        )
        assert resp.status == 200
        sent = thread_mock.send.call_args.kwargs.get("content", "")
        assert "yes" in sent and "no" in sent


@pytest.mark.asyncio
async def test_stop_twice_concurrently_does_not_raise(
    repo: NotificationRepository, bot: MagicMock
) -> None:
    """#877: systemd's SIGTERM reaches python twice (once from systemd, once
    forwarded by ``uv``), so two shutdowns call ``stop()`` together.  aiohttp's
    ``AppRunner.cleanup()`` yields between checking ``_server`` and using it, so
    the second call found ``None`` and logged ``'NoneType' object has no
    attribute 'pre_shutdown'`` on every restart."""
    api = ApiServer(repo=repo, bot=bot, default_channel_id=1, host="127.0.0.1", port=0)
    await api.start()

    results = await asyncio.gather(api.stop(), api.stop(), return_exceptions=True)

    assert results == [None, None]


class TestLounge:
    """/api/lounge — セッション同士の伝言板。Discord への転送が失敗しても保存は残る。"""

    LOUNGE_CHANNEL = 5550001

    @pytest.fixture
    async def lounge(self, tmp_path: Path):
        from c_lord.database.lounge_repo import LoungeRepository
        from c_lord.database.models import init_db

        db = str(tmp_path / "lounge.db")
        await init_db(db)
        return LoungeRepository(db)

    @pytest.fixture
    def api(self, repo: NotificationRepository, bot: MagicMock, lounge) -> ApiServer:
        return ApiServer(repo=repo, bot=bot, lounge_repo=lounge)

    @pytest.fixture
    async def lounge_client(self, api: ApiServer) -> AsyncIterator[TestClient]:
        async with _serving(api) as c:
            yield c

    @pytest.mark.parametrize("method", ["GET", "POST"])
    async def test_without_a_lounge_store_it_is_503(self, client: TestClient, method: str) -> None:
        resp = await client.request(method, "/api/lounge", json={"message": "hi"})

        assert resp.status == 503
        assert await resp.json() == {"error": "AI Lounge not configured (lounge_repo is None)"}

    async def test_posted_message_is_saved_and_echoed_with_201(
        self, lounge_client: TestClient, lounge
    ) -> None:
        resp = await lounge_client.post(
            "/api/lounge", json={"message": "  ビルド中です ", "label": " 担当A "}
        )

        assert resp.status == 201
        (saved,) = await lounge.get_recent()
        assert (saved.label, saved.message) == ("担当A", "ビルド中です"), "前後の空白は落とす"
        assert await resp.json() == {
            "status": "posted",
            "id": saved.id,
            "label": "担当A",
            "message": "ビルド中です",
            "posted_at": saved.posted_at,
        }

    @pytest.mark.parametrize("extra", [{}, {"label": None}, {"label": "   "}])
    async def test_label_defaults_to_ai(self, lounge_client: TestClient, extra: dict) -> None:
        resp = await lounge_client.post("/api/lounge", json={"message": "hi", **extra})

        assert (await resp.json())["label"] == "AI"

    @pytest.mark.parametrize("body", [{}, {"message": ""}, {"message": " \n "}])
    async def test_message_is_required(self, lounge_client: TestClient, lounge, body: dict) -> None:
        resp = await lounge_client.post("/api/lounge", json=body)

        assert resp.status == 400
        assert await resp.json() == {"error": "message is required"}
        assert await lounge.get_recent() == []

    async def test_nothing_is_forwarded_without_a_lounge_channel(
        self, lounge_client: TestClient, bot: MagicMock, channel: MagicMock
    ) -> None:
        resp = await lounge_client.post("/api/lounge", json={"message": "hi"})

        assert resp.status == 201
        bot.get_channel.assert_not_called()
        channel.send.assert_not_called()

    async def test_saved_message_is_forwarded_to_the_lounge_channel_as_one_line(
        self, api: ApiServer, lounge_client: TestClient, bot: MagicMock, channel: MagicMock, lounge
    ) -> None:
        api.lounge_channel_id = self.LOUNGE_CHANNEL

        await lounge_client.post("/api/lounge", json={"message": "終わりました", "label": "B"})

        (saved,) = await lounge.get_recent()
        bot.get_channel.assert_called_once_with(self.LOUNGE_CHANNEL)
        channel.send.assert_awaited_once_with(f"**[B]** 終わりました *({saved.posted_at[11:16]})*")

    async def test_discord_failure_while_forwarding_does_not_fail_the_post(
        self, api: ApiServer, lounge_client: TestClient, channel: MagicMock, lounge
    ) -> None:
        api.lounge_channel_id = self.LOUNGE_CHANNEL
        channel.send.side_effect = RuntimeError("Discord is down")

        resp = await lounge_client.post("/api/lounge", json={"message": "hi"})

        assert resp.status == 201
        assert len(await lounge.get_recent()) == 1

    async def test_short_timestamp_is_forwarded_as_it_is(
        self, api: ApiServer, channel: MagicMock
    ) -> None:
        api.lounge_channel_id = self.LOUNGE_CHANNEL

        await api._send_lounge_to_discord("B", "m", "12:34")

        channel.send.assert_awaited_once_with("**[B]** m *(12:34)*")

    async def test_recent_messages_are_listed_with_their_fields(
        self, lounge_client: TestClient, lounge
    ) -> None:
        for text in ("one", "two"):
            await lounge_client.post("/api/lounge", json={"message": text, "label": "L"})

        resp = await lounge_client.get("/api/lounge")

        assert resp.status == 200
        listed = (await resp.json())["messages"]
        expected = await lounge.get_recent()
        assert [m["message"] for m in listed] == [m.message for m in expected]
        assert sorted(m["message"] for m in listed) == ["one", "two"]
        assert all(set(m) == {"id", "label", "message", "posted_at"} for m in listed)

    @pytest.mark.parametrize(
        ("asked", "used"), [(None, 10), ("3", 3), ("0", 1), ("-7", 1), ("51", 50), ("999", 50)]
    )
    async def test_limit_is_clamped_to_1_through_50(
        self, api: ApiServer, lounge_client: TestClient, asked: str | None, used: int
    ) -> None:
        api.lounge_repo = MagicMock()
        api.lounge_repo.get_recent = AsyncMock(return_value=[])

        resp = await lounge_client.get(
            "/api/lounge", params={} if asked is None else {"limit": asked}
        )

        assert resp.status == 200
        api.lounge_repo.get_recent.assert_awaited_once_with(limit=used)

    async def test_limit_that_is_not_a_number_is_400(self, lounge_client: TestClient) -> None:
        resp = await lounge_client.get("/api/lounge", params={"limit": "many"})

        assert resp.status == 400
        assert await resp.json() == {"error": "limit must be an integer"}
