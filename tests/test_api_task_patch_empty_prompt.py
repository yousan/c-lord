"""#1229: ``PATCH /api/tasks/{id}`` with ``"prompt": ""`` saved an empty instruction.

``POST /api/tasks`` refuses an empty prompt with a 400, but PATCH only checked the
type, so ``{"prompt": ""}`` came back 200 and every scheduled run after that woke
Claude with an empty instruction. Create and update now follow one rule (#1064):
an empty prompt is a 400 and the saved prompt is left alone. ``name`` cannot be
changed by PATCH at all, so ``{"name": ""}`` changes nothing either.
"""

from __future__ import annotations

from pathlib import Path
from unittest.mock import MagicMock

import pytest
from aiohttp.test_utils import TestClient, TestServer
# <<REDACTED 1 lines>>
from c_lord.database.notification_repo import NotificationRepository
from c_lord.database.task_repo import TaskRepository
from c_lord.ext.api_server import ApiServer


@pytest.fixture
async def env(tmp_path: Path):  # type: ignore[no-untyped-def]
    repo = NotificationRepository(str(tmp_path / "n.db"))
    await repo.init_db()
    tr = TaskRepository(str(tmp_path / "t.db"))
    await tr.init_db()
    tid = await tr.create(name="seed", prompt="original", interval_seconds=60, channel_id=1)
    api = ApiServer(repo=repo, bot=MagicMock(), default_channel_id=1, task_repo=tr, port=0)
    c = TestClient(TestServer(api.app))
    await c.start_server()
    yield c, tr, tid
    await c.close()


async def test_post_refuses_empty_prompt(env):  # type: ignore[no-untyped-def]
    c, _, _ = env
    r = await c.post(
        "/api/tasks", json={"name": "a", "prompt": "", "interval_seconds": 60, "channel_id": 1}
    )
    assert r.status == 400


async def test_patch_empty_prompt_is_refused_like_post(env):  # type: ignore[no-untyped-def]
    c, tr, tid = env
    r = await c.patch(f"/api/tasks/{tid}", json={"prompt": ""})
    stored = (await tr.get(tid))["prompt"]
    assert r.status == 400 and stored == "original", (r.status, repr(stored))


async def test_patch_empty_prompt_changes_nothing_else(env):  # type: ignore[no-untyped-def]
    """Checked before anything is written (#1064): ``enabled`` is not saved either."""
    c, tr, tid = env
    r = await c.patch(f"/api/tasks/{tid}", json={"prompt": "", "enabled": False})
    assert r.status == 400
    task = await tr.get(tid)
    assert task["prompt"] == "original" and task["enabled"]


async def test_patch_empty_name_changes_nothing(env):  # type: ignore[no-untyped-def]
    c, tr, tid = env
    r = await c.patch(f"/api/tasks/{tid}", json={"name": ""})
    assert r.status == 400
    assert (await tr.get(tid))["name"] == "seed"


async def test_patch_a_real_prompt_still_saves(env):  # type: ignore[no-untyped-def]
    c, tr, tid = env
    r = await c.patch(f"/api/tasks/{tid}", json={"prompt": "new"})
    assert r.status == 200
    assert (await tr.get(tid))["prompt"] == "new"
