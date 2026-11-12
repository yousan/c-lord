"""REST API: a text field sent as something other than a string (#1183).

#1037 / #1081〜#1083 made a value of the wrong type a ``400`` naming the field, but
the text fields were left behind: ``/api/reply`` ``content`` of ``1``,
``/api/lounge`` ``message`` of ``null``, ``/api/tasks`` ``working_dir`` of a list…
reached ``.strip()`` or SQLite and came back as a **500** with no reason. The caller
(a script, CI, another Claude) could not tell what it did wrong.
"""

from __future__ import annotations

from collections.abc import AsyncIterator
from pathlib import Path
from unittest.mock import AsyncMock, MagicMock

import discord
import pytest
from aiohttp.test_utils import TestClient, TestServer

from c_lord.database.lounge_repo import LoungeRepository
from c_lord.database.models import init_db
from c_lord.database.notification_repo import NotificationRepository
from c_lord.database.resume_repo import PendingResumeRepository
from c_lord.database.task_repo import TaskRepository
from c_lord.ext.api_server import ApiServer
# <<REDACTED 3 lines>>
async def env(tmp_path: Path) -> AsyncIterator[tuple[TestClient, TaskRepository]]:
    repo = NotificationRepository(str(tmp_path / "n.db"))
    await repo.init_db()
    tr = TaskRepository(str(tmp_path / "t.db"))
    await tr.init_db()
    await init_db(str(tmp_path / "r.db"))
    bot = MagicMock()
    ch = MagicMock(spec=discord.TextChannel)
    ch.send = AsyncMock()
    bot.get_channel.return_value = ch
    cog = MagicMock()
    cog.spawn_session = AsyncMock()
    bot.cogs = {"ClaudeChatCog": cog}
    api = ApiServer(
        repo=repo,
        bot=bot,
        default_channel_id=1,
        task_repo=tr,
        lounge_repo=LoungeRepository(str(tmp_path / "r.db")),
        resume_repo=PendingResumeRepository(str(tmp_path / "r.db")),
        port=0,
    )
    c = TestClient(TestServer(api.app))
    await c.start_server()
    yield c, tr
    await c.close()


TASK = {"name": "n", "prompt": "p", "interval_seconds": 60, "channel_id": 1}


@pytest.mark.parametrize(
    "path,body,field",
    [
        ("/api/reply", {"thread_id": 5, "content": 1}, "content"),
        (
            "/api/prompt-choice",
            {"thread_id": 5, "question": True, "choices": [{"label": "a"}]},
            "question",
        ),
        ("/api/spawn", {"prompt": 1.5}, "prompt"),
        ("/api/spawn", {"prompt": "p", "thread_name": 5}, "thread_name"),  # review
        ("/api/threads/5/messages", {"content": {"a": 1}}, "content"),
        ("/api/lounge", {"message": None}, "message"),
        ("/api/lounge", {"message": 1}, "message"),
        ("/api/mark-resume", {"thread_id": 5, "session_id": {"a": 1}}, "session_id"),
        ("/api/mark-resume", {"thread_id": 5, "resume_prompt": 2**63}, "resume_prompt"),
        ("/api/tasks", {**TASK, "working_dir": ["x"]}, "working_dir"),
        ("/api/tasks", {**TASK, "working_dir": 2**63}, "working_dir"),
    ],
)
async def test_text_field_of_wrong_type_is_not_500(
    env: tuple[TestClient, TaskRepository], path: str, body: dict, field: str
) -> None:
    c, _ = env
    r = await c.post(path, json=body)
    assert r.status != 500, (path, body, r.status)
    assert r.status == 400, (path, body, r.status)
    assert field in (await r.json())["error"]


async def test_patch_task_working_dir_of_wrong_type_is_400(
    env: tuple[TestClient, TaskRepository],
) -> None:
    c, tr = env
    tid = (await (await c.post("/api/tasks", json=TASK)).json())["id"]
    r = await c.patch(f"/api/tasks/{tid}", json={"working_dir": ["x"]})
    assert r.status == 400
    task = await tr.get(tid)
    assert task is not None and task["working_dir"] is None


async def test_patch_task_working_dir_null_is_not_given(
    env: tuple[TestClient, TaskRepository],
) -> None:
    """Review: ``null`` reads as "not given" everywhere (it was saved as "None")."""
    c, tr = env
    tid = (await (await c.post("/api/tasks", json={**TASK, "working_dir": "/w"})).json())["id"]
    r = await c.patch(f"/api/tasks/{tid}", json={"working_dir": None, "prompt": "q"})
    assert r.status == 200, await r.text()
    task = await tr.get(tid)
    assert task is not None and task["working_dir"] == "/w"
