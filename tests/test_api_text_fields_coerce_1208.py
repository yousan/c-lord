"""#1208 — a text field sent as a list / object / number / bool was saved through ``str()``.

The REST API answered 2xx and stored the Python repr: a task's ``prompt: ["x"]``
became the instruction ``['x']`` and ran on schedule from then on. A caller —
Claude's curl, an outside automation — that got the type wrong should hear a
400 naming the field, as #1183 does for the fields that used to 500. Covers
``POST /api/tasks`` name / prompt, ``PATCH /api/tasks/{id}`` prompt,
``/api/lounge`` label, ``/api/threads/{id}/messages`` source and
``/api/mark-resume`` reason. Nothing is saved or posted on a 400.
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

WRONG = [["x"], {"a": 1}, 1, True]


class Env:
    def __init__(
        self,
        client: TestClient,
        tasks: TaskRepository,
        lounge: LoungeRepository,
        resume: PendingResumeRepository,
        thread: MagicMock,
    ) -> None:
        self.c = client
        self.tasks = tasks
        self.lounge = lounge
        self.resume = resume
        self.thread = thread


@pytest.fixture
async def env(tmp_path: Path) -> AsyncIterator[Env]:
    repo = NotificationRepository(str(tmp_path / "n.db"))
    await repo.init_db()
    tr = TaskRepository(str(tmp_path / "t.db"))
    await tr.init_db()
    await tr.create(name="seed", prompt="p", interval_seconds=60, channel_id=1)
    await init_db(str(tmp_path / "r.db"))
    bot = MagicMock()
    ch = MagicMock(spec=discord.Thread)
    ch.send = AsyncMock()
    ch.id = 5
    bot.get_channel.return_value = ch
    lounge = LoungeRepository(str(tmp_path / "r.db"))
    resume = PendingResumeRepository(str(tmp_path / "r.db"))
    api = ApiServer(
        repo=repo,
        bot=bot,
        default_channel_id=1,
        task_repo=tr,
        lounge_repo=lounge,
        resume_repo=resume,
        port=0,
    )
    c = TestClient(TestServer(api.app))
    await c.start_server()
    yield Env(c, tr, lounge, resume, ch)
    await c.close()


@pytest.mark.parametrize("value", WRONG)
async def test_task_prompt_must_be_a_string(env: Env, value: object) -> None:
    r = await env.c.post(
        "/api/tasks",
        json={"name": f"t{value!r}", "prompt": value, "interval_seconds": 60, "channel_id": 1},
    )
    stored = [t["prompt"] for t in await env.tasks.get_all() if t["name"] == f"t{value!r}"]
    assert r.status == 400, f"accepted {r.status}; stored prompt={stored!r}"
    assert (await r.json())["error"] == "prompt must be a string"


@pytest.mark.parametrize("value", WRONG)
async def test_task_name_must_be_a_string(env: Env, value: object) -> None:
    r = await env.c.post(
        "/api/tasks",
        json={"name": value, "prompt": "p", "interval_seconds": 60, "channel_id": 1},
    )
    names = [t["name"] for t in await env.tasks.get_all()]
    assert r.status == 400, f"accepted {r.status}; stored names={names!r}"
    assert (await r.json())["error"] == "name must be a string"
    assert names == ["seed"]


@pytest.mark.parametrize("value", WRONG)
async def test_patch_task_prompt_must_be_a_string(env: Env, value: object) -> None:
    r = await env.c.patch("/api/tasks/1", json={"prompt": value})
    task = await env.tasks.get(1)
    assert r.status == 400, f"accepted {r.status}; stored prompt={task and task['prompt']!r}"
    assert (await r.json())["error"] == "prompt must be a string"
    assert task is not None and task["prompt"] == "p"


async def test_patch_task_prompt_string_still_saved(env: Env) -> None:
    r = await env.c.patch("/api/tasks/1", json={"prompt": "new"})
    assert r.status == 200
    task = await env.tasks.get(1)
    assert task is not None and task["prompt"] == "new"


@pytest.mark.parametrize("value", WRONG)
async def test_lounge_label_must_be_a_string(env: Env, value: object) -> None:
    r = await env.c.post("/api/lounge", json={"message": "m", "label": value})
    labels = [m.label for m in await env.lounge.get_recent()]
    assert r.status == 400, f"accepted {r.status}; stored labels={labels!r}"
    assert (await r.json())["error"] == "label must be a string"
    assert labels == []


@pytest.mark.parametrize("value", WRONG)
async def test_thread_message_source_must_be_a_string(env: Env, value: object) -> None:
    r = await env.c.post("/api/threads/5/messages", json={"content": "c", "source": value})
    assert r.status == 400, f"accepted {r.status}; posted {env.thread.send.await_args_list!r}"
    assert (await r.json())["error"] == "source must be a string"
    env.thread.send.assert_not_awaited()


@pytest.mark.parametrize("value", WRONG)
async def test_mark_resume_reason_must_be_a_string(env: Env, value: object) -> None:
    r = await env.c.post(
        "/api/mark-resume", json={"thread_id": 5, "session_id": "abc", "reason": value}
    )
    pending = await env.resume.get_pending()
    assert r.status == 400, f"accepted {r.status}; stored {[p.reason for p in pending]!r}"
    assert (await r.json())["error"] == "reason must be a string"
    assert pending == []


async def test_strings_and_absent_fields_are_still_accepted(env: Env) -> None:
    r = await env.c.post("/api/lounge", json={"message": "m"})
    assert r.status == 201 and (await r.json())["label"] == "AI"
    r = await env.c.post("/api/threads/5/messages", json={"content": "c", "source": "tmux"})
    assert r.status == 200
    env.thread.send.assert_awaited_once_with("-# \U0001f4bb (tmux) c")
    r = await env.c.post(
        "/api/mark-resume", json={"thread_id": 5, "session_id": "abc", "reason": "deploy"}
    )
    assert r.status == 201
