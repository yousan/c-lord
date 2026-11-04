"""REST API: ``interval_seconds`` / ``channel_id`` of a task, against the docs (#1184).

``docs/COMMANDS.md`` said "must be integers — anything else is a 400", but
``POST /api/tasks`` took ``true`` with a 201 and saved a task that runs **every
second**. ``true`` is now a ``400`` on create and on ``PATCH`` — the mirror of #1038
(a boolean field does not take ``0`` / ``1``, so an integer field does not take
``true`` / ``false``). A decimal string (``"60"``) and a JSON number with a fraction
(truncated toward zero) stay accepted, as #1126 decided — Discord ids often travel as
strings — and the docs now say so.
"""

from __future__ import annotations

import pytest
from aiohttp.test_utils import TestClient

from c_lord.database.task_repo import TaskRepository
from tests.test_api_text_fields import TASK, env  # noqa: F401  (fixture)


@pytest.mark.parametrize("field", ["interval_seconds", "channel_id"])
async def test_true_is_not_an_integer(
    env: tuple[TestClient, TaskRepository],  # noqa: F811
    field: str,
) -> None:
    c, tr = env
    r = await c.post("/api/tasks", json={**TASK, field: True})
    assert r.status == 400, (field, r.status, await tr.get_all())
    assert field in (await r.json())["error"]
    assert await tr.get_all() == []


async def test_patch_interval_true_is_400(env: tuple[TestClient, TaskRepository]) -> None:  # noqa: F811
    c, tr = env
    tid = (await (await c.post("/api/tasks", json=TASK)).json())["id"]
    r = await c.patch(f"/api/tasks/{tid}", json={"interval_seconds": True})
    assert r.status == 400
    task = await tr.get(tid)
    assert task is not None and task["interval_seconds"] == 60


@pytest.mark.parametrize("value,saved", [("90", 90), (90.7, 90)])
async def test_string_and_fraction_are_accepted_as_documented(
    env: tuple[TestClient, TaskRepository],  # noqa: F811
    value: object,
    saved: int,
) -> None:
    c, tr = env
    r = await c.post("/api/tasks", json={**TASK, "interval_seconds": value})
    assert r.status == 201
    (task,) = await tr.get_all()
    assert task["interval_seconds"] == saved
