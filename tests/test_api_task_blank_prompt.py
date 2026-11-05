"""#1238: a scheduled task whose prompt is only whitespace was saved.

#1229 made ``PATCH`` refuse ``"prompt": ""``, but ``"   "`` or ``"\\n\\t"`` still came
back 201 from POST and 200 from PATCH, and every scheduled run then woke Claude with
an empty instruction — nothing reached Discord and the user could not tell why.
``/api/spawn`` already strips before refusing an empty prompt; tasks now do the same.
"""

from __future__ import annotations

import pytest

from tests.test_api_task_patch_empty_prompt import env  # noqa: F401


@pytest.mark.parametrize("value", ["   ", "\n\t"], ids=repr)
async def test_post_blank_prompt(env, value):  # type: ignore[no-untyped-def]  # noqa: F811
    c, tr, _ = env
    r = await c.post(
        "/api/tasks", json={"name": "b", "prompt": value, "interval_seconds": 60, "channel_id": 1}
    )
    assert r.status == 400, (r.status, [t["prompt"] for t in await tr.get_all()])


@pytest.mark.parametrize("value", ["   ", "\n\t"], ids=repr)
async def test_patch_blank_prompt(env, value):  # type: ignore[no-untyped-def]  # noqa: F811
    c, tr, tid = env
    r = await c.patch(f"/api/tasks/{tid}", json={"prompt": value})
    stored = (await tr.get(tid))["prompt"]
    assert r.status == 400 and stored == "original", (r.status, repr(stored))


async def test_patch_null_prompt_with_enabled(env):  # type: ignore[no-untyped-def]  # noqa: F811
    """null = not given (consistent with working_dir); enabled still applied."""
    c, tr, tid = env
    r = await c.patch(f"/api/tasks/{tid}", json={"prompt": None, "enabled": False})
    t = await tr.get(tid)
    assert r.status == 200 and t["prompt"] == "original" and not t["enabled"]


async def test_patch_empty_prompt_does_not_apply_enabled(env):  # type: ignore[no-untyped-def]  # noqa: F811
    c, tr, tid = env
    r = await c.patch(f"/api/tasks/{tid}", json={"prompt": "", "enabled": False})
    t = await tr.get(tid)
    assert r.status == 400 and t["enabled"]


async def test_patch_empty_prompt_on_missing_task(env):  # type: ignore[no-untyped-def]  # noqa: F811
    """Order: 400 (bad value) before 404 — consistent with interval_seconds."""
    c, _, _ = env
    r1 = await c.patch("/api/tasks/9999", json={"prompt": ""})
    r2 = await c.patch("/api/tasks/9999", json={"interval_seconds": 0})
    assert r1.status == r2.status
