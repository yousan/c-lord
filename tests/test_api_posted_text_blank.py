"""#1288: text the REST API posts to Discord was accepted when it was invisible only.

#1258 refused task prompts / names and ``/api/spawn`` prompts made only of
zero-width spaces, direction marks, the Hangul filler and the like. The routes that
*post* text to Discord still looked at ``.strip()`` only: ``POST /api/lounge`` and
``POST /api/threads/{id}/messages`` answered 201 to a body of one ``U+200B`` and put
a message with nothing visible in it into the channel. The same hole was in
``/api/notify`` and ``/api/schedule`` (the embed's message), ``/api/reply``
(content) and ``/api/prompt-choice`` (question and each choice's label). They all
answer 400 now, with the #1258 test (``_is_blank``), and post nothing; text with
anything visible in it is still accepted.
"""

from __future__ import annotations

from typing import Any

import pytest

from .test_api_text_fields_coerce_1208 import Env
from .test_api_text_fields_coerce_1208 import env as env2  # noqa: F401

INVISIBLE = ["​", "‎", "ㅤ"]


@pytest.mark.parametrize("value", INVISIBLE, ids=repr)
async def test_lounge_refuses_invisible_only(env2: Env, value: str) -> None:  # noqa: F811
    r = await env2.c.post("/api/lounge", json={"message": value})
    assert r.status == 400, r.status
    assert await env2.lounge.get_recent() == []


@pytest.mark.parametrize("value", INVISIBLE, ids=repr)
async def test_thread_message_refuses_invisible_only(env2: Env, value: str) -> None:  # noqa: F811
    r = await env2.c.post("/api/threads/5/messages", json={"content": value})
    assert r.status == 400, (r.status, env2.thread.send.await_args_list)
    env2.thread.send.assert_not_awaited()


def _bodies(value: str) -> list[tuple[str, dict[str, Any]]]:
    """The other routes that post caller text to Discord, with *value* in it."""
    return [
        ("/api/notify", {"message": value}),
        ("/api/schedule", {"message": value, "scheduled_at": "2099-01-01T00:00:00"}),
        ("/api/reply", {"thread_id": 5, "content": value}),
        ("/api/prompt-choice", {"thread_id": 5, "question": value, "choices": [{"label": "a"}]}),
        ("/api/prompt-choice", {"thread_id": 5, "question": "q?", "choices": [{"label": value}]}),
    ]


_ROUTE_IDS = ["notify", "schedule", "reply", "prompt-question", "prompt-label"]


@pytest.mark.parametrize("index", range(len(_ROUTE_IDS)), ids=_ROUTE_IDS)
@pytest.mark.parametrize("value", INVISIBLE, ids=repr)
async def test_other_posting_routes_refuse_invisible_only(
    env2: Env,  # noqa: F811
    value: str,
    index: int,
) -> None:
    path, body = _bodies(value)[index]
    r = await env2.c.post(path, json=body)
    assert r.status == 400, (path, r.status)
    env2.thread.send.assert_not_awaited()


@pytest.mark.parametrize("value", ["​hello", "\U0001f468‍\U0001f469", "❤️"])
async def test_visible_text_is_still_posted(env2: Env, value: str) -> None:  # noqa: F811
    r = await env2.c.post("/api/lounge", json={"message": value})
    assert r.status == 201, r.status
    r = await env2.c.post("/api/threads/5/messages", json={"content": value})
    assert r.status != 400, r.status
    for path, body in _bodies(value):
        r = await env2.c.post(path, json=body)
        assert r.status != 400, (path, r.status, await r.text())
