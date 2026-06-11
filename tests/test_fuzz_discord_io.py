"""Unit tests for the pure helpers in scripts.fuzz.discord_io (Issue #377).

The network methods are exercised on staging; here we lock down the message
filtering that decides "is this the seed?" / "is this the bot's answer?" —
the part most likely to silently mis-classify and corrupt the oracle's input.
"""

from __future__ import annotations

from scripts.fuzz.discord_io import _collect_answer, _find_seed, _reaction_names

BOT = "999"


def _msg(mid: str, *, author: str = BOT, content: str = "", webhook: bool = False, reactions=None):
    m: dict = {"id": mid, "author": {"id": author}, "content": content}
    if webhook:
        m["webhook_id"] = "wh1"
    if reactions:
        m["reactions"] = [{"emoji": {"name": n}} for n in reactions]
    return m


def test_reaction_names_extracts_emoji() -> None:
    m = _msg("1", reactions=["🟢", "❌"])
    assert _reaction_names(m) == {"🟢", "❌"}


def test_reaction_names_empty_when_none() -> None:
    assert _reaction_names(_msg("1")) == set()


def test_find_seed_matches_bot_message_with_prompt_content() -> None:
    msgs = [
        _msg("3", content="some answer"),
        _msg("2", content="hello world", webhook=False),  # the seed
        _msg("1", author="111", content="hello world"),  # a human, not the seed
    ]
    seed = _find_seed(msgs, "hello world", BOT)
    assert seed is not None and seed["id"] == "2"


def test_find_seed_none_when_absent() -> None:
    msgs = [_msg("3", content="unrelated")]
    assert _find_seed(msgs, "hello world", BOT) is None


def test_collect_answer_skips_seed_status_and_webhook() -> None:
    msgs = [
        _msg("5", content="final answer here"),  # the answer
        _msg("4", content="-# 💻 (cli) some status"),  # status line, skip
        _msg("3", content="", reactions=["🟢"]),  # tool embed (no content), skip
        _msg("2", content="the prompt", webhook=False),  # seed, excluded by id
        _msg("1", author="111", content="human msg"),  # not the bot, skip
    ]
    answer = _collect_answer(msgs, bot_id=BOT, exclude_ids={"2"}, scenario_text="the prompt")
    assert answer == "final answer here"


def test_collect_answer_concatenates_multiple_chunks_in_order() -> None:
    msgs = [
        _msg("7", content="part two"),
        _msg("6", content="part one"),
    ]
    answer = _collect_answer(msgs, bot_id=BOT, exclude_ids=set(), scenario_text="x")
    assert answer == "part one\npart two"


def test_collect_answer_none_when_only_seed_present() -> None:
    msgs = [_msg("2", content="the prompt")]
    answer = _collect_answer(msgs, bot_id=BOT, exclude_ids=set(), scenario_text="the prompt")
    assert answer is None
