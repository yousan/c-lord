"""#1258: a prompt / name made only of other invisible characters was still saved.

#1238 refused prompts of zero-width spaces and the BOM, but its list had five
characters. Direction marks (LRM / RLM, LRE…PDF, LRI…PDI), the soft hyphen,
U+2061, U+180E, U+034F, a lone variation selector, the Hangul filler and the
braille blank still came back 201 from POST and 200 from PATCH, and every
scheduled run then woke Claude with an instruction that said nothing. "Invisible"
is now decided by Unicode's categories (control / format / separator) and the
default-ignorable code points, not a hand list. Text with anything visible in
it — a ZWJ emoji sequence, a letter or emoji with VS16, a combining accent —
is still accepted on all four routes.
"""

from __future__ import annotations

import time
from pathlib import Path
from unittest.mock import MagicMock

import pytest
from aiohttp.test_utils import TestClient, TestServer

from c_lord.database.notification_repo import NotificationRepository
from c_lord.database.task_repo import TaskRepository
from c_lord.ext.api_server import ApiServer, _is_blank

# Ordinary prompts that contain invisible / combining / format characters.
LEGIT = {
    "ja_zwsp": "こんにちは​世界",
    "zwj_family": "\U0001f468‍\U0001f469‍\U0001f467",
    "zwj_rainbow_flag": "\U0001f3f3️‍\U0001f308",
    "vs16_heart": "❤️",
    "letter_vs16": "a️",
    "keycap_hash": "#️⃣",
    "ideograph_vs": "葛\U000e0100",
    "combining": "é",
    "lone_combining_accent": "́",
    "newlines_then_x": "\n\n\tx\n",
    "rtl_mark_hebrew": "‏שלום",
    "bom_hello": "﻿hello",
    "zwsp_run_x": "​" * 50 + "x",
    "nbsp_x": " x",
    "ideo_space_x": "　x",
    "soft_hyphen_word": "co­operate",
    "hangul": "한국어",
}

# Invisible-only strings that were not in #1238's list.
INVISIBLE_ONLY = {
    "lrm": "‎",
    "rlm": "‏",
    "soft_hyphen": "­",
    "mongolian_vs": "᠎",
    "func_apply": "⁡",
    "vs16_alone": "️",
    "vs_supplement_alone": "\U000e0100",
    "cgj": "͏",
    "lre_pdf": "‪‬",
    "lri_pdi": "⁦⁩",
    "hangul_filler": "ㅤ",
    "hangul_choseong_jungseong_fillers": "ᅟᅠ",
    "halfwidth_hangul_filler": "ﾠ",
    "braille_blank": "⠀",
    "line_para_sep": "  ",
    "tag_chars": "\U000e0001\U000e0041",
    "nul_bel": "\x00\x07",
    "mixed": " ‎️ㅤ⠀\n",
}


@pytest.fixture
async def env(tmp_path: Path):  # type: ignore[no-untyped-def]
    repo = NotificationRepository(str(tmp_path / "n.db"))
    await repo.init_db()
    tr = TaskRepository(str(tmp_path / "t.db"))
    await tr.init_db()
    tid = await tr.create(name="seed", prompt="original", interval_seconds=60, channel_id=1)
    bot = MagicMock()
    bot.cogs.get.return_value = None  # /api/spawn -> 503 once past the prompt check
    api = ApiServer(repo=repo, bot=bot, default_channel_id=1, task_repo=tr, port=0)
    c = TestClient(TestServer(api.app))
    await c.start_server()
    yield c, tr, tid
    await c.close()


async def _four(c, tid, value):  # type: ignore[no-untyped-def]
    """POST prompt, POST name, PATCH prompt, /api/spawn prompt."""
    p = await c.post(
        "/api/tasks", json={"name": "n", "prompt": value, "interval_seconds": 60, "channel_id": 1}
    )
    n = await c.post(
        "/api/tasks", json={"name": value, "prompt": "p", "interval_seconds": 60, "channel_id": 1}
    )
    pa = await c.patch(f"/api/tasks/{tid}", json={"prompt": value})
    s = await c.post("/api/spawn", json={"prompt": value})
    return p.status, n.status, pa.status, s.status


@pytest.mark.parametrize("value", list(LEGIT.values()), ids=list(LEGIT))
async def test_legit_input_not_refused(env, value):  # type: ignore[no-untyped-def]
    c, tr, tid = env
    st = await _four(c, tid, value)
    # POST prompt, POST name -> 201; PATCH -> 200; spawn -> 503 (no cog), never 400
    assert st == (201, 201, 200, 503), st
    assert (await tr.get(tid))["prompt"] == value


@pytest.mark.parametrize("value", list(INVISIBLE_ONLY.values()), ids=list(INVISIBLE_ONLY))
async def test_other_invisible_only_is_refused(env, value):  # type: ignore[no-untyped-def]
    c, tr, tid = env
    assert await _four(c, tid, value) == (400, 400, 400, 400)
    assert (await tr.get(tid))["prompt"] == "original"
    assert [t["name"] for t in await tr.get_all()] == ["seed"]


def test_is_blank_long_inputs_stay_linear() -> None:
    cases = {
        "blank_1M": (" ​" * 500_000, True),
        "blank_mixed_1M": ("‎️ㅤ⠀" * 250_000, True),
        "blank_ws_1M": (" " * 1_000_000, True),
        "x_at_end_1M": ("​" * 999_999 + "x", False),
        "x_first_1M": ("x" + "​" * 999_999, False),
    }
    for name, (s, want) in cases.items():
        t = time.perf_counter()
        assert _is_blank(s) is want, name
        dt = time.perf_counter() - t
        assert dt < 0.5, (name, dt)
