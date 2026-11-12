"""定期タスクの REST API (``/api/tasks``) — 登録・一覧・削除・変更。

タスクを実行するのは SchedulerCog で、API は ``scheduled_tasks`` の行を出し入れするだけ。
だからここでは本物の ``TaskRepository``（テストごとの SQLite）を使い、HTTP の応答と
「DB に何が残ったか」の両方を見る。
"""

from __future__ import annotations

import time
from collections.abc import AsyncIterator
from pathlib import Path
from typing import Any
from unittest.mock import MagicMock

import pytest
from aiohttp.test_utils import TestClient, TestServer

from c_lord.database.notification_repo import NotificationRepository
from c_lord.database.task_repo import TaskRepository
from c_lord.ext.api_server import ApiServer

NO_SUCH_TASK = 424242


@pytest.fixture(autouse=True)
def _lounge_channel_not_inherited(monkeypatch: pytest.MonkeyPatch) -> None:
    """``COORDINATION_CHANNEL_ID`` が環境に残っていても API の構成に入り込まないようにする。"""
    monkeypatch.delenv("COORDINATION_CHANNEL_ID", raising=False)


@pytest.fixture
async def notifications(tmp_path: Path) -> NotificationRepository:
    repo = NotificationRepository(str(tmp_path / "notifications.db"))
    await repo.init_db()
    return repo


@pytest.fixture
async def tasks(tmp_path: Path) -> TaskRepository:
    """本物のタスクリポジトリ（テストごとに空）。"""
    repo = TaskRepository(str(tmp_path / "tasks.db"))
    await repo.init_db()
    return repo


async def _serve(api: ApiServer) -> TestClient:
    client = TestClient(TestServer(api.app))
    await client.start_server()
    return client


@pytest.fixture
async def client(
    notifications: NotificationRepository, tasks: TaskRepository
) -> AsyncIterator[TestClient]:
    """タスクの保存先を持つ API。"""
    c = await _serve(ApiServer(repo=notifications, bot=MagicMock(), task_repo=tasks))
    yield c
    await c.close()


@pytest.fixture
async def client_without_tasks(
    notifications: NotificationRepository,
) -> AsyncIterator[TestClient]:
    """SchedulerCog が組まれていない bot の API（``task_repo`` 無し）。"""
    c = await _serve(ApiServer(repo=notifications, bot=MagicMock()))
    yield c
    await c.close()


def _task(**overrides: Any) -> dict[str, Any]:
    """登録に足りる本文。``overrides`` で差し替え、``None`` を渡した欄は取り除く。"""
    body: dict[str, Any] = {
        "name": "nightly-report",
        "prompt": "昨日の PR をまとめて",
        "interval_seconds": 3600,
        "channel_id": 9001,
    }
    body.update(overrides)
    return {key: value for key, value in body.items() if value is not None}


async def _register(client: TestClient, **overrides: Any) -> int:
    """タスクを 1 件登録して id を返す。"""
    resp = await client.post("/api/tasks", json=_task(**overrides))
    assert resp.status == 201, await resp.text()
    return (await resp.json())["id"]


class TestTasksCreate:
    """POST /api/tasks"""

    async def test_task_is_stored_and_answered_with_201(
        self, client: TestClient, tasks: TaskRepository
    ) -> None:
        resp = await client.post("/api/tasks", json=_task())

        assert resp.status == 201
        body = await resp.json()
        assert body["status"] == "created"
        stored = await tasks.get(body["id"])
        assert stored is not None
        assert stored["name"] == "nightly-report"
        assert stored["prompt"] == "昨日の PR をまとめて"
        assert stored["interval_seconds"] == 3600
        assert stored["channel_id"] == 9001
        assert stored["working_dir"] is None
        assert stored["enabled"] is True

    @pytest.mark.parametrize("missing", ["name", "prompt", "interval_seconds", "channel_id"])
    async def test_each_required_field_is_named_when_missing(
        self, client: TestClient, tasks: TaskRepository, missing: str
    ) -> None:
        resp = await client.post("/api/tasks", json=_task(**{missing: None}))

        assert resp.status == 400
        assert await resp.json() == {"error": f"{missing} is required"}
        assert await tasks.get_all() == []

    async def test_first_missing_field_in_documented_order_is_reported(
        self, client: TestClient
    ) -> None:
        """何も無い本文なら、最初に見る ``name`` が返る。"""
        resp = await client.post("/api/tasks", json={})

        assert await resp.json() == {"error": "name is required"}

    @pytest.mark.parametrize("field", ["name", "prompt"])
    async def test_text_with_nothing_visible_counts_as_missing(
        self, client: TestClient, tasks: TaskRepository, field: str
    ) -> None:
        resp = await client.post("/api/tasks", json=_task(**{field: " \t\n"}))

        assert resp.status == 400
        assert await resp.json() == {"error": f"{field} is required"}
        assert await tasks.get_all() == []

    async def test_working_dir_is_stored_when_given(
        self, client: TestClient, tasks: TaskRepository
    ) -> None:
        task_id = await _register(client, working_dir="/srv/reports")

        stored = await tasks.get(task_id)
        assert stored is not None
        assert stored["working_dir"] == "/srv/reports"

    async def test_name_and_prompt_are_stored_untrimmed(
        self, client: TestClient, tasks: TaskRepository
    ) -> None:
        task_id = await _register(client, name=" spaced ", prompt="  手順:\n  1. 見る\n")

        stored = await tasks.get(task_id)
        assert stored is not None
        assert (stored["name"], stored["prompt"]) == (" spaced ", "  手順:\n  1. 見る\n")

    async def test_first_run_is_due_at_once_by_default(
        self, client: TestClient, tasks: TaskRepository
    ) -> None:
        task_id = await _register(client)

        assert [t["id"] for t in await tasks.get_due()] == [task_id]

    async def test_run_immediately_false_waits_one_interval(
        self, client: TestClient, tasks: TaskRepository
    ) -> None:
        before = time.time()
        task_id = await _register(client, run_immediately=False)

        assert await tasks.get_due() == []
        stored = await tasks.get(task_id)
        assert stored is not None
        assert stored["next_run_at"] >= before + 3600

    async def test_numbers_sent_as_digit_strings_are_accepted(
        self, client: TestClient, tasks: TaskRepository
    ) -> None:
        """Discord の id は文字列で届くことが多い。"""
        task_id = await _register(client, interval_seconds="120", channel_id="123456789012345678")

        stored = await tasks.get(task_id)
        assert stored is not None
        assert (stored["interval_seconds"], stored["channel_id"]) == (120, 123456789012345678)

    @pytest.mark.parametrize("field", ["interval_seconds", "channel_id"])
    async def test_number_fields_must_be_integers(
        self, client: TestClient, tasks: TaskRepository, field: str
    ) -> None:
        resp = await client.post("/api/tasks", json=_task(**{field: "soon"}))

        assert resp.status == 400
        assert await resp.json() == {"error": f"{field} must be an integer"}
        assert await tasks.get_all() == []

    async def test_interval_below_one_second_is_refused(
        self, client: TestClient, tasks: TaskRepository
    ) -> None:
        resp = await client.post("/api/tasks", json=_task(interval_seconds=-5))

        assert resp.status == 400
        assert await resp.json() == {"error": "interval_seconds must be an integer of 1 or more"}
        assert await tasks.get_all() == []

    async def test_name_already_taken_is_409_and_keeps_the_first_task(
        self, client: TestClient, tasks: TaskRepository
    ) -> None:
        first = await _register(client, prompt="最初の指示")

        resp = await client.post("/api/tasks", json=_task(prompt="後から来た指示"))

        assert resp.status == 409
        assert await resp.json() == {"error": "Task name already exists"}
        (only,) = await tasks.get_all()
        assert (only["id"], only["prompt"]) == (first, "最初の指示")

    async def test_broken_json_is_400(self, client: TestClient, tasks: TaskRepository) -> None:
        resp = await client.post(
            "/api/tasks", data=b"{not json", headers={"Content-Type": "application/json"}
        )

        assert resp.status == 400
        assert await resp.json() == {"error": "Invalid JSON"}
        assert await tasks.get_all() == []

    async def test_json_that_is_not_an_object_is_400(self, client: TestClient) -> None:
        resp = await client.post("/api/tasks", json=[_task()])

        assert resp.status == 400
        assert await resp.json() == {"error": "Body must be a JSON object"}


class TestTasksList:
    """GET /api/tasks"""

    async def test_no_tasks_is_an_empty_list(self, client: TestClient) -> None:
        resp = await client.get("/api/tasks")

        assert resp.status == 200
        assert await resp.json() == {"tasks": []}

    async def test_registered_tasks_are_listed_with_their_fields(self, client: TestClient) -> None:
        first = await _register(client, name="alpha", prompt="A の仕事")
        second = await _register(client, name="beta", prompt="B の仕事", working_dir="/b")

        resp = await client.get("/api/tasks")

        assert resp.status == 200
        listed = {t["id"]: t for t in (await resp.json())["tasks"]}
        assert set(listed) == {first, second}
        assert listed[first]["name"] == "alpha"
        assert listed[first]["prompt"] == "A の仕事"
        assert listed[second]["working_dir"] == "/b"
        assert listed[second]["interval_seconds"] == 3600

    async def test_disabled_tasks_are_listed_too(self, client: TestClient) -> None:
        task_id = await _register(client)
        await client.patch(f"/api/tasks/{task_id}", json={"enabled": False})

        (listed,) = (await (await client.get("/api/tasks")).json())["tasks"]

        assert listed["id"] == task_id
        assert not listed["enabled"]


class TestTasksDelete:
    """DELETE /api/tasks/{id}"""

    async def test_task_is_removed(self, client: TestClient, tasks: TaskRepository) -> None:
        doomed = await _register(client, name="doomed")
        kept = await _register(client, name="kept")

        resp = await client.delete(f"/api/tasks/{doomed}")

        assert resp.status == 200
        assert await resp.json() == {"status": "deleted"}
        assert [t["id"] for t in await tasks.get_all()] == [kept]

    async def test_unknown_id_is_404(self, client: TestClient) -> None:
        resp = await client.delete(f"/api/tasks/{NO_SUCH_TASK}")

        assert resp.status == 404
        assert await resp.json() == {"error": "Task not found"}

    async def test_id_that_is_not_a_number_is_400(
        self, client: TestClient, tasks: TaskRepository
    ) -> None:
        await _register(client)

        resp = await client.delete("/api/tasks/nightly-report")

        assert resp.status == 400
        assert await resp.json() == {"error": "Invalid ID"}
        assert len(await tasks.get_all()) == 1


class TestTasksPatch:
    """PATCH /api/tasks/{id}"""

    async def test_task_can_be_disabled_and_enabled_again(
        self, client: TestClient, tasks: TaskRepository
    ) -> None:
        task_id = await _register(client)

        resp = await client.patch(f"/api/tasks/{task_id}", json={"enabled": False})

        assert resp.status == 200
        assert await resp.json() == {"status": "updated"}
        stored = await tasks.get(task_id)
        assert stored is not None and stored["enabled"] is False
        assert await tasks.get_due() == [], "止めたタスクは実行対象にならない"

        await client.patch(f"/api/tasks/{task_id}", json={"enabled": True})

        stored = await tasks.get(task_id)
        assert stored is not None and stored["enabled"] is True

    async def test_prompt_is_replaced_and_the_rest_left_alone(
        self, client: TestClient, tasks: TaskRepository
    ) -> None:
        task_id = await _register(client, working_dir="/srv")

        resp = await client.patch(f"/api/tasks/{task_id}", json={"prompt": "  新しい指示 "})

        assert resp.status == 200
        stored = await tasks.get(task_id)
        assert stored is not None
        assert stored["prompt"] == "  新しい指示 ", "trim しない"
        assert (stored["interval_seconds"], stored["working_dir"]) == (3600, "/srv")
        assert stored["enabled"] is True

    async def test_interval_and_working_dir_are_replaced(
        self, client: TestClient, tasks: TaskRepository
    ) -> None:
        task_id = await _register(client)

        resp = await client.patch(
            f"/api/tasks/{task_id}", json={"interval_seconds": 90, "working_dir": "/elsewhere"}
        )

        assert resp.status == 200
        stored = await tasks.get(task_id)
        assert stored is not None
        assert (stored["interval_seconds"], stored["working_dir"]) == (90, "/elsewhere")

    async def test_enabled_and_other_fields_are_applied_together(
        self, client: TestClient, tasks: TaskRepository
    ) -> None:
        task_id = await _register(client)

        await client.patch(f"/api/tasks/{task_id}", json={"enabled": False, "prompt": "次の指示"})

        stored = await tasks.get(task_id)
        assert stored is not None
        assert (stored["enabled"], stored["prompt"]) == (False, "次の指示")

    @pytest.mark.parametrize(
        "body", [{"enabled": False}, {"prompt": "x"}, {}], ids=["enabled", "prompt", "empty"]
    )
    async def test_unknown_id_is_404_whatever_the_body(
        self, client: TestClient, body: dict
    ) -> None:
        """空の本文でも「変える欄が無い」より「そのタスクは無い」が先。"""
        resp = await client.patch(f"/api/tasks/{NO_SUCH_TASK}", json=body)

        assert resp.status == 404
        assert await resp.json() == {"error": "Task not found"}

    @pytest.mark.parametrize("body", [{}, {"enable": False}, {"prompt": None}])
    async def test_body_with_nothing_to_change_names_the_accepted_fields(
        self, client: TestClient, body: dict
    ) -> None:
        task_id = await _register(client)

        resp = await client.patch(f"/api/tasks/{task_id}", json=body)

        assert resp.status == 400
        assert await resp.json() == {
            "error": "No field to update. Accepted fields: "
            "enabled, prompt, interval_seconds, working_dir"
        }

    @pytest.mark.parametrize("interval", [0, -1, "often", None])
    async def test_bad_interval_is_refused_and_nothing_is_written(
        self, client: TestClient, tasks: TaskRepository, interval: object
    ) -> None:
        task_id = await _register(client)

        resp = await client.patch(
            f"/api/tasks/{task_id}", json={"prompt": "書かれないはず", "interval_seconds": interval}
        )

        assert resp.status == 400
        assert await resp.json() == {"error": "interval_seconds must be an integer of 1 or more"}
        stored = await tasks.get(task_id)
        assert stored is not None
        assert (stored["prompt"], stored["interval_seconds"]) == ("昨日の PR をまとめて", 3600)

    async def test_blank_prompt_is_refused(self, client: TestClient, tasks: TaskRepository) -> None:
        task_id = await _register(client)

        resp = await client.patch(f"/api/tasks/{task_id}", json={"prompt": "   "})

        assert resp.status == 400
        assert await resp.json() == {"error": "prompt is required"}
        stored = await tasks.get(task_id)
        assert stored is not None and stored["prompt"] == "昨日の PR をまとめて"

    async def test_id_that_is_not_a_number_is_400(self, client: TestClient) -> None:
        resp = await client.patch("/api/tasks/nightly-report", json={"enabled": False})

        assert resp.status == 400
        assert await resp.json() == {"error": "Invalid ID"}

    async def test_broken_json_is_400(self, client: TestClient) -> None:
        task_id = await _register(client)

        resp = await client.patch(
            f"/api/tasks/{task_id}",
            data=b"{not json",
            headers={"Content-Type": "application/json"},
        )

        assert resp.status == 400
        assert await resp.json() == {"error": "Invalid JSON"}


class TestWithoutATaskStore:
    """``task_repo`` が無い bot では、タスク系はどれも 503 — 本文や id を見るより先に。"""

    @pytest.mark.parametrize(
        ("method", "path"),
        [
            ("POST", "/api/tasks"),
            ("GET", "/api/tasks"),
            ("DELETE", "/api/tasks/not-a-number"),
            ("PATCH", "/api/tasks/not-a-number"),
        ],
    )
    async def test_every_task_endpoint_answers_503(
        self, client_without_tasks: TestClient, method: str, path: str
    ) -> None:
        resp = await client_without_tasks.request(
            method, path, data=b"{not json", headers={"Content-Type": "application/json"}
        )

        assert resp.status == 503
        assert await resp.json() == {"error": "SchedulerCog not configured (task_repo is None)"}

    async def test_task_store_assigned_after_construction_is_used(
        self,
        notifications: NotificationRepository,
        tasks: TaskRepository,
    ) -> None:
        """``setup`` は API を作った後で ``task_repo`` を代入する。"""
        api = ApiServer(repo=notifications, bot=MagicMock())
        c = await _serve(api)
        try:
            assert (await c.get("/api/tasks")).status == 503

            api.task_repo = tasks

            assert (await c.get("/api/tasks")).status == 200
        finally:
            await c.close()
