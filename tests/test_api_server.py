# <<REDACTED 4 lines>>
import asyncio
# <<REDACTED 7 lines>>
from c_lord.database.notification_repo import NotificationRepository
from c_lord.ext.api_server import ApiServer
# <<REDACTED 184 lines>>
    """Bearer auth. The *other* way in — being the bot's own Unix user — and
    the refusal of everyone else live in ``tests/test_api_peer_auth.py`` (#457).
    """

    # <<REDACTED 1 lines>>
    async def test_health_is_served_to_the_bots_own_user(self, auth_client: TestClient) -> None:
        """Health used to bypass auth for *everyone*; now it is UID-gated (#457)."""
        # <<REDACTED 4 lines>>
    async def test_own_user_needs_no_auth_header(self, auth_client: TestClient) -> None:
        """A caller running as the bot's user can already read the secret.

        Before #457 this answered 401. Requiring the header from our own user
        bought nothing (the secret is in ``.env``, readable by that same user)
        while breaking the control plane, since ``CLORD_API_SECRET`` is stripped
        from the tmux environment by #353 — a Claude session has no way to send
        it. A foreign UID with no header gets 403, which is the case that
        matters and is pinned in ``test_api_peer_auth.py``.
        """
        # <<REDACTED 1 lines>>
        assert resp.status == 200
# <<REDACTED 161 lines>>
        from c_lord.database.models import init_db as _init
        from c_lord.database.resume_repo import PendingResumeRepository
# <<REDACTED 73 lines>>


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
        # <<REDACTED 3 lines>>
    async def thread_client(
        self, repo: NotificationRepository, bot_with_thread: MagicMock
    ) -> TestClient:
        api = ApiServer(repo=repo, bot=bot_with_thread, default_channel_id=12345)
        # <<REDACTED 7 lines>>
    async def test_post_message_returns_200(
        self, thread_client: TestClient, thread_mock: MagicMock
    ) -> None:
        resp = await thread_client.post(
            "/api/threads/111222333/messages",
            json={"content": "Hello from CLI"},
        )
        # <<REDACTED 3 lines>>
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
        # <<REDACTED 2 lines>>
        assert "content" in data["error"]

    @pytest.mark.asyncio
    async def test_post_message_empty_content_returns_400(self, thread_client: TestClient) -> None:
        resp = await thread_client.post("/api/threads/111222333/messages", json={"content": "   "})
        # <<REDACTED 3 lines>>
    async def test_post_message_invalid_thread_id_returns_400(
        self, thread_client: TestClient
    ) -> None:
        resp = await thread_client.post(
            "/api/threads/not-a-number/messages", json={"content": "Test"}
        )
        # <<REDACTED 3 lines>>
    async def test_post_message_thread_not_found(self, repo: NotificationRepository) -> None:
        bot = MagicMock()
        bot.get_channel.return_value = None
        bot.fetch_channel = AsyncMock(side_effect=Exception("Not found"))
        # <<REDACTED 5 lines>>
            resp = await client.post("/api/threads/999/messages", json={"content": "Test"})
            assert resp.status == 404
        # <<REDACTED 4 lines>>
    async def test_post_message_non_messageable_returns_400(
        self, repo: NotificationRepository
    ) -> None:
        bot = MagicMock()
        # Return a channel without send method
        channel = MagicMock(spec=[])
        bot.get_channel.return_value = channel
        api = ApiServer(repo=repo, bot=bot, default_channel_id=12345)
        # <<REDACTED 4 lines>>
            resp = await client.post("/api/threads/123/messages", json={"content": "Test"})
            # <<REDACTED 4 lines>>
    @pytest.mark.asyncio
    async def test_post_message_invalid_json_returns_400(self, thread_client: TestClient) -> None:
        resp = await thread_client.post(
            "/api/threads/111222333/messages",
            # <<REDACTED 4 lines>>


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
        # <<REDACTED 7 lines>>
    async def test_plain_content_posts_to_thread(
        self, reply_client: TestClient, thread_mock: MagicMock
    ) -> None:
        resp = await reply_client.post(
            "/api/reply",
            json={"thread_id": 555666777, "content": "final answer here"},
        )
        # <<REDACTED 3 lines>>
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
        # <<REDACTED 2 lines>>
        bot_with_thread: MagicMock,
        thread_mock: MagicMock,
    ) -> None:
        """#372: opt-in via show_url_embeds=True restores OGP previews —
        send() is called with suppress_embeds=False."""
        # <<REDACTED 2 lines>>
            bot=bot_with_thread,
            # <<REDACTED 1 lines>>
            show_url_embeds=True,
        # <<REDACTED 4 lines>>
        try:
            resp = await client.post(
                "/api/reply",
                json={"thread_id": 555666777, "content": "see https://example.com for details"},
            )
            assert resp.status == 200
            thread_mock.send.assert_called_once()
            assert thread_mock.send.call_args.kwargs.get("suppress_embeds") is False
        # <<REDACTED 3 lines>>
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
            # <<REDACTED 3 lines>>

    @pytest.mark.asyncio
    async def test_thread_not_found_returns_404(self, repo: NotificationRepository) -> None:
        b = MagicMock()
        b.get_channel.return_value = None
        b.fetch_channel = AsyncMock(side_effect=Exception("not found"))
        api = ApiServer(repo=repo, bot=b, default_channel_id=1)
        # <<REDACTED 4 lines>>
            resp = await client.post(
                "/api/reply",
                json={"thread_id": 999, "content": "x"},
            )
            assert resp.status == 404
        # <<REDACTED 4 lines>>
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
            # <<REDACTED 1 lines>>
            session_repo=session_repo,
        )
        # <<REDACTED 4 lines>>
            resp = await client.post(
                "/api/reply",
                json={"thread_id": 555666777, "content": "plain answer"},
            )
            assert resp.status == 200
            kwargs = thread_mock.send.call_args.kwargs
            assert "reference" in kwargs
        # <<REDACTED 3 lines>>

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
        # <<REDACTED 5 lines>>

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
        # <<REDACTED 2 lines>>
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
            # <<REDACTED 6 lines>>
    async def test_thread_not_found_returns_404(self, repo: NotificationRepository) -> None:
        b = MagicMock()
        b.get_channel.return_value = None
        b.fetch_channel = AsyncMock(side_effect=Exception("not found"))
        api = ApiServer(repo=repo, bot=b, default_channel_id=1)
        # <<REDACTED 4 lines>>
            resp = await client.post(
                "/api/prompt-choice",
                json={
                    "thread_id": 999,
                    "question": "q?",
                    "choices": [{"label": "1", "description": "x"}],
                },
            )
            assert resp.status == 404
        # <<REDACTED 4 lines>>
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
