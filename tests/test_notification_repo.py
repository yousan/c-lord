"""``NotificationRepository`` — notifications queued for later delivery.

A notification waits as ``pending`` and leaves the queue exactly once: sent,
failed or cancelled. ``scheduled_at`` is whatever text the caller gave and is
compared as text, boundary included.
"""

from __future__ import annotations

import logging
import re
from pathlib import Path
from typing import Any

import aiosqlite
import pytest

from c_lord.database.notification_repo import NotificationRepository

LOCAL_TIMESTAMP = re.compile(r"\d{4}-\d{2}-\d{2} \d{2}:\d{2}:\d{2}")


@pytest.fixture
async def queue(tmp_path: Path) -> NotificationRepository:
    repository = NotificationRepository(str(tmp_path / "notifications.db"))
    await repository.init_db()
    return repository


async def _row(queue: NotificationRepository, notification_id: int) -> dict[str, Any]:
    """A notification in whatever state it is in (``get_pending`` only shows pending)."""
    async with aiosqlite.connect(queue.db_path) as db:
        db.row_factory = aiosqlite.Row
        cursor = await db.execute(
            "SELECT * FROM scheduled_notifications WHERE id = ?", (notification_id,)
        )
        row = await cursor.fetchone()
    assert row is not None
    return dict(row)


class TestInitDb:
    async def test_can_be_run_again_without_losing_the_queue(
        self, queue: NotificationRepository
    ) -> None:
        await queue.create("still here", "2030-01-01T00:00:00")

        await queue.init_db()

        assert len(await queue.get_pending()) == 1

    async def test_says_where_the_database_is(
        self, tmp_path: Path, caplog: pytest.LogCaptureFixture
    ) -> None:
        path = str(tmp_path / "fresh.db")

        with caplog.at_level(logging.INFO, logger="c_lord.database.notification_repo"):
            await NotificationRepository(path).init_db()

        assert f"Notification DB initialized at {path}" in caplog.text


class TestCreate:
    async def test_returns_a_new_id_each_time(self, queue: NotificationRepository) -> None:
        first = await queue.create("one", "2030-01-01T09:00:00")
        second = await queue.create("two", "2030-01-01T09:00:00")

        assert isinstance(first, int) and first > 0
        assert second > first

    async def test_message_and_time_alone_get_the_documented_defaults(
        self, queue: NotificationRepository
    ) -> None:
        notification_id = await queue.create("ping", "2030-01-01T09:00:00")

        row = await _row(queue, notification_id)

        assert row["message"] == "ping"
        assert row["scheduled_at"] == "2030-01-01T09:00:00"
        assert row["title"] is None
        assert row["color"] == 0x00BFFF
        assert row["source"] == "api"
        assert row["channel_id"] is None
        assert row["status"] == "pending"
        assert row["sent_at"] is None
        assert row["error_message"] is None
        assert LOCAL_TIMESTAMP.fullmatch(row["created_at"])

    async def test_every_optional_field_is_stored_as_given(
        self, queue: NotificationRepository
    ) -> None:
        notification_id = await queue.create(
            "deploy finished",
            "2030-06-01T12:30:00",
            title="Deploy",
            color=0xFF0000,
            source="ci",
            channel_id=987654321,
        )

        (pending,) = await queue.get_pending()

        assert pending["id"] == notification_id
        assert pending["message"] == "deploy finished"
        assert pending["title"] == "Deploy"
        assert pending["color"] == 0xFF0000
        assert pending["source"] == "ci"
        assert pending["channel_id"] == 987654321

    async def test_scheduled_at_is_kept_verbatim(self, queue: NotificationRepository) -> None:
        """No validation or reformatting here — that is the API's business."""
        notification_id = await queue.create("odd", "  whenever, really ")

        assert (await _row(queue, notification_id))["scheduled_at"] == "  whenever, really "

    async def test_logs_what_was_queued(
        self, queue: NotificationRepository, caplog: pytest.LogCaptureFixture
    ) -> None:
        with caplog.at_level(logging.INFO, logger="c_lord.database.notification_repo"):
            notification_id = await queue.create("m", "2030-01-01T00:00:00")

        assert (
            f"Notification scheduled: id={notification_id}, at=2030-01-01T00:00:00" in caplog.text
        )


class TestGetPending:
    async def test_an_empty_queue_is_an_empty_list(self, queue: NotificationRepository) -> None:
        assert await queue.get_pending() == []

    async def test_lists_what_is_waiting_earliest_first(
        self, queue: NotificationRepository
    ) -> None:
        await queue.create("third", "2030-03-01T00:00:00")
        await queue.create("first", "2030-01-01T00:00:00")
        await queue.create("second", "2030-02-01T00:00:00")

        pending = await queue.get_pending()

        assert [n["message"] for n in pending] == ["first", "second", "third"]

    async def test_before_keeps_only_what_is_due_by_then(
        self, queue: NotificationRepository
    ) -> None:
        await queue.create("due", "2030-01-01T00:00:00")
        await queue.create("not yet", "2030-12-31T00:00:00")

        pending = await queue.get_pending(before="2030-06-01T00:00:00")

        assert [n["message"] for n in pending] == ["due"]

    async def test_before_includes_the_exact_moment(self, queue: NotificationRepository) -> None:
        await queue.create("on the dot", "2030-06-01T00:00:00")

        assert len(await queue.get_pending(before="2030-06-01T00:00:00")) == 1
        assert await queue.get_pending(before="2030-05-31T23:59:59") == []

    @pytest.mark.parametrize("no_filter", [None, ""])
    async def test_no_before_means_no_filter(
        self, queue: NotificationRepository, no_filter: str | None
    ) -> None:
        """An empty string is "no cut-off", not "due before the beginning of
        time" — it must not hide everything."""
        await queue.create("soon", "2030-01-01T00:00:00")
        await queue.create("far", "2999-01-01T00:00:00")

        assert len(await queue.get_pending(before=no_filter)) == 2

    async def test_rows_are_plain_dicts_with_every_column(
        self, queue: NotificationRepository
    ) -> None:
        await queue.create("m", "2030-01-01T00:00:00")

        (pending,) = await queue.get_pending()

        assert type(pending) is dict
        assert set(pending) == {
            "id",
            "message",
            "title",
            "color",
            "scheduled_at",
            "source",
            "channel_id",
            "status",
            "sent_at",
            "error_message",
            "created_at",
        }


class TestMarkSent:
    async def test_a_sent_notification_leaves_the_queue(
        self, queue: NotificationRepository
    ) -> None:
        sent = await queue.create("sent", "2030-01-01T00:00:00")
        waiting = await queue.create("waiting", "2030-01-01T00:00:00")

        await queue.mark_sent(sent)

        assert [n["id"] for n in await queue.get_pending()] == [waiting]

    async def test_records_the_status_and_when_it_went_out(
        self, queue: NotificationRepository
    ) -> None:
        notification_id = await queue.create("m", "2030-01-01T00:00:00")

        await queue.mark_sent(notification_id)

        row = await _row(queue, notification_id)
        assert row["status"] == "sent"
        assert LOCAL_TIMESTAMP.fullmatch(row["sent_at"])
        assert row["error_message"] is None


class TestMarkFailed:
    async def test_a_failed_notification_leaves_the_queue(
        self, queue: NotificationRepository
    ) -> None:
        failed = await queue.create("failed", "2030-01-01T00:00:00")
        waiting = await queue.create("waiting", "2030-01-01T00:00:00")

        await queue.mark_failed(failed, "channel is gone")

        assert [n["id"] for n in await queue.get_pending()] == [waiting]

    async def test_records_the_error_and_no_send_time(self, queue: NotificationRepository) -> None:
        notification_id = await queue.create("m", "2030-01-01T00:00:00")

        await queue.mark_failed(notification_id, "403 Forbidden")

        row = await _row(queue, notification_id)
        assert row["status"] == "failed"
        assert row["error_message"] == "403 Forbidden"
        assert row["sent_at"] is None


class TestCancel:
    async def test_a_pending_notification_is_cancelled(self, queue: NotificationRepository) -> None:
        notification_id = await queue.create("m", "2030-01-01T00:00:00")

        assert await queue.cancel(notification_id) is True

        assert await queue.get_pending() == []
        assert (await _row(queue, notification_id))["status"] == "cancelled"

    async def test_unknown_id_is_false(self, queue: NotificationRepository) -> None:
        assert await queue.cancel(424242) is False

    async def test_what_was_already_sent_cannot_be_cancelled(
        self, queue: NotificationRepository
    ) -> None:
        notification_id = await queue.create("m", "2030-01-01T00:00:00")
        await queue.mark_sent(notification_id)

        assert await queue.cancel(notification_id) is False

        assert (await _row(queue, notification_id))["status"] == "sent"

    async def test_cancelling_twice_only_counts_once(self, queue: NotificationRepository) -> None:
        notification_id = await queue.create("m", "2030-01-01T00:00:00")
        await queue.cancel(notification_id)

        assert await queue.cancel(notification_id) is False

    async def test_cancelling_one_leaves_the_others_waiting(
        self, queue: NotificationRepository
    ) -> None:
        gone = await queue.create("gone", "2030-01-01T00:00:00")
        kept = await queue.create("kept", "2030-01-01T00:00:00")

        await queue.cancel(gone)

        assert [n["id"] for n in await queue.get_pending()] == [kept]
