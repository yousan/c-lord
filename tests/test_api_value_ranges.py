"""REST API: integers SQLite cannot hold, bodies that are not UTF-8, and the
schedule / notify values nobody checked (#1081 #1082 #1083).

- an integer of 2**63 or more passed ``int()`` and failed in SQLite with
  ``OverflowError`` — a 500, and on PATCH after ``enabled`` was already saved;
- a body that is not UTF-8 raised ``UnicodeDecodeError``, which is a
  ``ValueError`` but not a ``JSONDecodeError`` — a 500 instead of "Invalid JSON";
- ``/api/schedule`` saved any ``channel_id`` (``Infinity``, ``"1"``, 1.5) with a
  200, ``scheduled_at`` that is not a string and an oversized ``color`` were
  500s, and ``/api/notify``'s ``color`` reached ``discord.Embed`` unchecked.
"""

from __future__ import annotations

import json
from pathlib import Path
from unittest.mock import AsyncMock, MagicMock

import pytest
from aiohttp.test_utils import TestClient, TestServer

from c_lord.database.models import init_db
from c_lord.database.notification_repo import NotificationRepository
from c_lord.database.resume_repo import PendingResumeRepository
from c_lord.database.task_repo import TaskRepository
from c_lord.ext.api_server import ApiServer

H = {"Content-Type": "application/json"}
BIG = 10**30
TASK = {"name": "t", "prompt": "p", "interval_seconds": 60, "channel_id": 1}
BAD = ["Infinity", "NaN", str(2**63), '"1"', "1.5", "true"]


@pytest.fixture
async def env(tmp_path: Path):  # type: ignore[no-untyped-def]
    repo = NotificationRepository(str(tmp_path / "n.db"))
    await repo.init_db()
    tasks = TaskRepository(str(tmp_path / "t.db"))
    await tasks.init_db()
    await init_db(str(tmp_path / "r.db"))
    resume = PendingResumeRepository(str(tmp_path / "r.db"))
    bot = MagicMock()
    bot.get_channel.return_value.send = AsyncMock()
    bot.cogs = {}
    api = ApiServer(
        repo=repo,
        bot=bot,
        default_channel_id=1,
        task_repo=tasks,
        lounge_repo=MagicMock(),
        resume_repo=resume,
        port=0,
    )
    client = TestClient(TestServer(api.app))
    await client.start_server()
    yield client, repo, tasks, bot
    await client.close()


def _with(base: dict, field: str, raw: str) -> str:
    return json.dumps({**base, field: "__X__"}).replace('"__X__"', raw)


# -- #1081: integers SQLite cannot hold ---------------------------------------


@pytest.mark.parametrize(
    "kw",
    [
        {"interval_seconds": BIG},
        {"channel_id": BIG},
        {"interval_seconds": 2**63},
        {"channel_id": 2**63},
        {"channel_id": -(2**63) - 1},
    ],
    ids=["interval-1e30", "channel-1e30", "interval-2^63", "channel-2^63", "channel-neg"],
)
async def test_create_out_of_sqlite_range_is_400(env, kw: dict) -> None:  # type: ignore[no-untyped-def]
    client, _, tasks, _ = env
    resp = await client.post("/api/tasks", data=json.dumps({**TASK, **kw}), headers=H)
    assert resp.status == 400, (resp.status, await resp.text())
    assert await tasks.get_all() == []


@pytest.mark.parametrize("value", [BIG, 2**63])
async def test_patch_out_of_range_changes_nothing(env, value: int) -> None:  # type: ignore[no-untyped-def]
    """AC2: a 400 means the task is unchanged — ``enabled`` is not saved first."""
    client, _, tasks, _ = env
    tid = (await (await client.post("/api/tasks", json=TASK)).json())["id"]
    raw = '{"enabled": false, "interval_seconds": %d}' % value
    resp = await client.patch(f"/api/tasks/{tid}", data=raw, headers=H)
    assert resp.status == 400, (resp.status, await resp.text())
    task = await tasks.get(tid)
    assert task is not None and task["enabled"] and task["interval_seconds"] == 60


@pytest.mark.parametrize(
    ("method", "path", "body"),
    [
        ("PATCH", f"/api/tasks/{2**63}", '{"enabled": true}'),
        ("DELETE", f"/api/tasks/{2**63}", None),
        ("DELETE", f"/api/scheduled/{2**63}", None),
        ("POST", f"/api/threads/{2**63}/messages", '{"content": "x"}'),
        ("POST", f"/api/threads/{2**63}/sleep", None),
    ],
)
async def test_path_id_of_2_63_is_not_500(env, method: str, path: str, body: str | None) -> None:  # type: ignore[no-untyped-def]
    client, *_ = env
    resp = await client.request(method, path, data=body, headers=H)
    assert resp.status in (400, 404), (resp.status, await resp.text())


async def test_mark_resume_big_thread_id_is_400(env) -> None:  # type: ignore[no-untyped-def]
    client, *_ = env
    resp = await client.post("/api/mark-resume", data='{"thread_id": %d}' % BIG, headers=H)
    assert resp.status == 400, (resp.status, await resp.text())


# -- #1082: a body that is not UTF-8 ------------------------------------------


@pytest.mark.parametrize(
    "path",
    ["/api/notify", "/api/schedule", "/api/tasks", "/api/reply", "/api/mark-resume", "/api/spawn"],
)
async def test_invalid_utf8_body_is_400(env, path: str) -> None:  # type: ignore[no-untyped-def]
    client, *_ = env
    resp = await client.post(path, data=b"\xff\xfe{", headers=H)
    assert resp.status == 400, (resp.status, await resp.text())
    assert (await resp.json()) == {"error": "Invalid JSON"}


# -- #1083: /api/schedule and /api/notify -------------------------------------

SCHEDULE = {"message": "x", "scheduled_at": "2030-01-01T00:00:00"}


@pytest.mark.parametrize("raw", BAD)
@pytest.mark.parametrize("field", ["channel_id", "color", "scheduled_at"])
async def test_schedule_bad_values_are_400_and_nothing_is_saved(env, field: str, raw: str) -> None:  # type: ignore[no-untyped-def]
    client, repo, *_ = env
    resp = await client.post("/api/schedule", data=_with(SCHEDULE, field, raw), headers=H)
    assert resp.status == 400, (field, raw, resp.status, await resp.text())
    assert field in (await resp.json())["error"]
    assert await repo.get_pending() == []


@pytest.mark.parametrize("raw", BAD)
@pytest.mark.parametrize("field", ["channel_id", "color"])
async def test_notify_bad_values_are_400(env, field: str, raw: str) -> None:  # type: ignore[no-untyped-def]
    client, _, _, bot = env
    resp = await client.post("/api/notify", data=_with({"message": "x"}, field, raw), headers=H)
    assert resp.status == 400, (field, raw, resp.status, await resp.text())
    assert field in (await resp.json())["error"]
    bot.get_channel.return_value.send.assert_not_awaited()


@pytest.mark.parametrize("raw", ["-1", str(0x1000000)])
async def test_color_outside_rgb_is_400(env, raw: str) -> None:  # type: ignore[no-untyped-def]
    client, *_ = env
    for path, base in (("/api/notify", {"message": "x"}), ("/api/schedule", SCHEDULE)):
        resp = await client.post(path, data=_with(base, "color", raw), headers=H)
        assert resp.status == 400, (path, raw, resp.status)


async def test_good_schedule_and_notify_still_work(env) -> None:  # type: ignore[no-untyped-def]
    client, repo, _, bot = env
    body = {**SCHEDULE, "channel_id": 123, "color": 0xFF0000}
    resp = await client.post("/api/schedule", json=body)
    assert resp.status == 200
    (row,) = await repo.get_pending()
    assert row["channel_id"] == 123 and row["color"] == 0xFF0000
    resp = await client.post("/api/notify", json={"message": "x", "channel_id": 1, "color": 0})
    assert resp.status == 200
    bot.get_channel.return_value.send.assert_awaited_once()
