"""REST API: values in range, Infinity, and "nothing to update" (#1064 #1065 #1066).

- ``interval_seconds`` of 0 / negative / ``false`` was saved with a 200 on PATCH
  (and a negative one on create) — the type check of #1037 does not look at the range;
- ``"thread_id": Infinity`` (Python's ``json`` accepts it) reached ``int()``, whose
  ``OverflowError`` nobody caught: a 500 with no reason;
- a PATCH with no field it can update answered **404 Task not found** for a task that
  exists — a caller that typed ``enable`` for ``enabled`` went to re-create it.
"""

from __future__ import annotations

import pytest
from aiohttp.test_utils import TestClient

from tests.test_api_input_validation import client  # noqa: F401  (fixture)

TASK = {"name": "t", "prompt": "p", "interval_seconds": 60, "channel_id": 1}


async def _create(client: TestClient) -> int:  # noqa: F811
    r = await client.post("/api/tasks", json=TASK)
    assert r.status == 201
    return (await r.json())["id"]


async def _only_task(client: TestClient) -> dict:  # noqa: F811
    (t,) = (await (await client.get("/api/tasks")).json())["tasks"]
    return t


@pytest.mark.parametrize("v", [0, -5, False])
async def test_patch_zero_negative_interval_is_400(client: TestClient, v: object) -> None:  # noqa: F811
    tid = await _create(client)
    resp = await client.patch(f"/api/tasks/{tid}", json={"interval_seconds": v})
    assert resp.status == 400
    assert "interval_seconds" in (await resp.json())["error"]
    assert (await _only_task(client))["interval_seconds"] == 60


@pytest.mark.parametrize("v", [0, -5])
async def test_create_zero_negative_interval_is_400(client: TestClient, v: int) -> None:  # noqa: F811
    resp = await client.post("/api/tasks", json={**TASK, "interval_seconds": v})
    assert resp.status == 400
    assert "interval_seconds" in (await resp.json())["error"]
    assert (await (await client.get("/api/tasks")).json())["tasks"] == []


async def test_a_bad_interval_does_not_half_apply_the_patch(client: TestClient) -> None:  # noqa: F811
    """A 400 means nothing changed — ``enabled`` used to be written before the check."""
    tid = await _create(client)
    resp = await client.patch(f"/api/tasks/{tid}", json={"enabled": False, "interval_seconds": 0})
    assert resp.status == 400
    assert (await _only_task(client))["enabled"]


@pytest.mark.parametrize(
    "path", ["/api/reply", "/api/prompt-choice", "/api/mark-resume", "/api/spawn"]
)
async def test_thread_id_infinity_is_400(client: TestClient, path: str) -> None:  # noqa: F811
    raw = (
        '{"thread_id":Infinity,"channel_id":Infinity,"content":"x","prompt":"x",'
        '"question":"q","choices":[{"label":"a"}]}'
    )
    resp = await client.post(path, data=raw, headers={"Content-Type": "application/json"})
    assert resp.status == 400, await resp.text()


@pytest.mark.parametrize("field", ["interval_seconds", "channel_id"])
async def test_task_numbers_infinity_is_400(client: TestClient, field: str) -> None:  # noqa: F811
    raw = '{"name":"t","prompt":"p","interval_seconds":60,"channel_id":1}'.replace(
        f'"{field}":{TASK[field]}', f'"{field}":Infinity'
    )
    resp = await client.post("/api/tasks", data=raw, headers={"Content-Type": "application/json"})
    assert resp.status == 400, await resp.text()


async def test_patch_interval_infinity_is_400(client: TestClient) -> None:  # noqa: F811
    tid = await _create(client)
    resp = await client.patch(
        f"/api/tasks/{tid}",
        data='{"interval_seconds":Infinity}',
        headers={"Content-Type": "application/json"},
    )
    assert resp.status == 400, await resp.text()


async def test_patch_with_no_known_field_on_existing_task_is_400(client: TestClient) -> None:  # noqa: F811
    tid = await _create(client)
    resp = await client.patch(f"/api/tasks/{tid}", json={"enable": False})  # typo of "enabled"
    assert resp.status == 400
    body = await resp.json()
    assert "enabled" in body["error"] and "Task not found" not in body["error"]
    assert (await _only_task(client))["enabled"]


async def test_patch_on_missing_task_is_still_404(client: TestClient) -> None:  # noqa: F811
    resp = await client.patch("/api/tasks/999", json={"enabled": False})
    assert resp.status == 404
    resp = await client.patch("/api/tasks/999", json={"enable": False})
    assert resp.status == 404
