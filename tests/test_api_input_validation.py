"""#1037: malformed input gets a 400 with the reason — not a fake 409, not a 500.

The REST API is the control plane Claude drives with ``curl`` (design decision 7),
and the caller acts on the answer it gets back:

- ``POST /api/tasks`` with ``"interval_seconds": "daily"`` answered **409 "Task name
  already exists"** — the ``int()`` ValueError was caught by the ``except
  Exception`` meant for the name's UNIQUE constraint, for a name that did not exist;
- ``PATCH /api/tasks/{id}`` with ``"interval_seconds": "hourly"`` was a **500**;
- a body that is valid JSON but not an object (``[1]``) was a **500** on every
  endpoint that reads one (``data.get`` on a list).
"""

from __future__ import annotations

import os
import tempfile
from unittest.mock import AsyncMock, MagicMock

import pytest
from aiohttp.test_utils import TestClient, TestServer

from c_lord.database.notification_repo import NotificationRepository
from c_lord.database.task_repo import TaskRepository
from c_lord.ext.api_server import ApiServer


@pytest.fixture
async def client():  # type: ignore[no-untyped-def]
    paths = []
    for _ in range(2):
        fd, path = tempfile.mkstemp(suffix=".db")
        os.close(fd)
        paths.append(path)
    repo = NotificationRepository(paths[0])
    await repo.init_db()
    task_repo = TaskRepository(paths[1])
    await task_repo.init_db()
    bot = MagicMock()
    bot.get_channel.return_value.send = AsyncMock()
    bot.cogs = {}
    # lounge / mark-resume answer 503 before reading the body when their repo is
    # missing; a stand-in lets the body check be what answers.
    api = ApiServer(
        repo=repo,
        bot=bot,
        default_channel_id=1,
        task_repo=task_repo,
        lounge_repo=MagicMock(),
        resume_repo=MagicMock(),
        port=0,
    )
    c = TestClient(TestServer(api.app))
    await c.start_server()
    yield c
    await c.close()
    for path in paths:
        os.unlink(path)


TASK = {"name": "daily-review", "prompt": "p", "interval_seconds": 60, "channel_id": 1}


async def test_non_integer_interval_on_create_is_400_not_a_name_clash(
    client: TestClient,
) -> None:
    resp = await client.post("/api/tasks", json={**TASK, "interval_seconds": "daily"})

    assert resp.status == 400
    assert "interval_seconds" in (await resp.json())["error"]


async def test_non_integer_channel_on_create_is_400(client: TestClient) -> None:
    resp = await client.post("/api/tasks", json={**TASK, "channel_id": "general"})

    assert resp.status == 400
    assert "channel_id" in (await resp.json())["error"]


async def test_a_real_name_clash_is_still_409(client: TestClient) -> None:
    assert (await client.post("/api/tasks", json=TASK)).status == 201

    resp = await client.post("/api/tasks", json=TASK)

    assert resp.status == 409
    assert (await resp.json()) == {"error": "Task name already exists"}


async def test_non_integer_interval_on_patch_is_400(client: TestClient) -> None:
    created = await client.post("/api/tasks", json=TASK)
    task_id = (await created.json())["id"]

    resp = await client.patch(f"/api/tasks/{task_id}", json={"interval_seconds": "hourly"})

    assert resp.status == 400
    assert "interval_seconds" in (await resp.json())["error"]


@pytest.mark.parametrize(
    ("method", "path"),
    [
        ("POST", "/api/notify"),
        ("POST", "/api/schedule"),
        ("POST", "/api/tasks"),
        ("PATCH", "/api/tasks/1"),
        ("POST", "/api/lounge"),
        ("POST", "/api/spawn"),
        ("POST", "/api/threads/1/messages"),
        ("POST", "/api/reply"),
        ("POST", "/api/prompt-choice"),
        ("POST", "/api/mark-resume"),
    ],
)
@pytest.mark.parametrize("body", [[1], "text", 3])
async def test_a_body_that_is_not_an_object_is_400(
    client: TestClient, method: str, path: str, body: object
) -> None:
    resp = await client.request(method, path, json=body)

    assert resp.status == 400
    assert (await resp.json()) == {"error": "Body must be a JSON object"}
