"""Tests for c_lord.transcript.repeat_fold — the same line, over and over (#747).

On 2026-09-15 one thread received **616** copies of three short lines in 16
minutes (``待機します。`` / ``待機中です。`` / ``待機中。``) and the twelve
messages worth reading were buried under them.  The fold turns that into a
handful of messages plus one that counts.
"""

from __future__ import annotations

import logging

import pytest

from c_lord.transcript.repeat_fold import REPEAT_FOLD_AFTER, RepeatFold

THREAD = 1549210701263016008


class _Clock:
    def __init__(self) -> None:
        self.t = 1000.0

    def __call__(self) -> float:
        return self.t

    def advance(self, seconds: float) -> None:
        self.t += seconds


class _Discord:
    """Records what the fold posts and edits, like a thread would show it."""

    def __init__(self) -> None:
        self.posts: list[str] = []
        self.edits: list[tuple[object, str]] = []

    async def post(self, text: str) -> object:
        self.posts.append(text)
        return f"m{len(self.posts)}"

    async def edit(self, handle: object, text: str) -> None:
        self.edits.append((handle, text))

    def shown(self, handle: str) -> str:
        """What the message *handle* reads now."""
        text = self.posts[int(handle[1:]) - 1]
        for h, t in self.edits:
            if h == handle:
                text = t
        return text


def _fold(discord: _Discord, clock: _Clock | None = None) -> RepeatFold:
    return RepeatFold(
        thread_id=THREAD,
        post=discord.post,
        edit=discord.edit,
        clock=clock or _Clock(),
    )


async def _offer_all(fold: RepeatFold, lines: list[str], clock: _Clock | None = None) -> list[bool]:
    folded = []
    for line in lines:
        folded.append(await fold.offer(line))
        if clock is not None:
            clock.advance(1.4)  # the median gap measured in the #747 thread
    return folded


async def test_sixty_identical_lines_become_a_few_messages_and_one_counter() -> None:
    """AC1: past N copies, no new message — one message counts the rest."""
    discord = _Discord()
    clock = _Clock()
    fold = _fold(discord, clock)

    folded = await _offer_all(fold, ["待機中。"] * 60, clock)
    await fold.close()

    passed = folded.count(False)
    assert passed == REPEAT_FOLD_AFTER, folded
    assert folded[:REPEAT_FOLD_AFTER] == [False] * REPEAT_FOLD_AFTER
    assert all(folded[REPEAT_FOLD_AFTER:])
    # Only one new message for all the folded copies.
    assert len(discord.posts) == 1, discord.posts
    assert passed + len(discord.posts) <= 10
    # And it ends up saying how many it stands for.
    final = discord.shown("m1")
    assert "57" in final, final
    assert "待機中。" in final, final


def test_the_threshold_is_a_single_named_constant() -> None:
    """AC1: N is decided once, in one place."""
    assert isinstance(REPEAT_FOLD_AFTER, int)
    assert 1 <= REPEAT_FOLD_AFTER <= 5


async def test_the_real_three_line_loop_is_folded_too() -> None:
    """The measured loop never repeated a line back to back.

    #742's thread alternated three lines, so "the same as the previous line"
    would have folded **none** of its 616 copies.  Opening of that loop, verbatim
    from its transcript (2026-09-15T00:51Z).
    """
    lines = [
        "待機します。",
        "Monitor に載せたので、RED ラウンドの完了通知を待ちます。",
        "待機中です。",
        "待機します。通知が来るまで新しい操作はしません。",
        "待機中。",
    ] + ["待機します。", "待機中です。", "待機します。", "待機中。"] * 150
    discord = _Discord()
    fold = _fold(discord)

    folded = await _offer_all(fold, lines)

    passed = folded.count(False)
    assert passed <= 10, f"{passed} lines reached the thread on their own"
    assert len(discord.posts) == 1, discord.posts


async def test_a_new_line_ends_the_fold_and_is_posted_normally() -> None:
    """AC2: the fold must not swallow what comes after the repetition."""
    discord = _Discord()
    fold = _fold(discord)
    await _offer_all(fold, ["待機中。"] * 20)

    assert await fold.offer("RED を再現しました。次は GREEN です。") is False


async def test_going_back_to_the_loop_after_a_real_line_starts_a_new_counter() -> None:
    """The #742 thread had real posts *inside* the loop; each keeps its place.

    Once a turn has been seen looping, returning to it does not spend another N
    messages — but it does get a fresh counter below the real post, so the real
    post is not sandwiched out of order.
    """
    discord = _Discord()
    fold = _fold(discord)
    await _offer_all(fold, ["待機中。"] * 10)
    assert await fold.offer("セットアップ成立（A=最初のターン）") is False

    assert await fold.offer("待機中。") is True
    assert len(discord.posts) == 2, discord.posts


async def test_distinct_lines_are_never_folded() -> None:
    discord = _Discord()
    fold = _fold(discord)

    folded = await _offer_all(fold, [f"ステップ {i} を実行しました。" for i in range(60)])

    assert not any(folded)
    assert discord.posts == []


async def test_a_line_that_recurs_between_new_ones_is_not_a_loop() -> None:
    """Saying the same thing now and then, with real work between, is normal."""
    discord = _Discord()
    fold = _fold(discord)
    lines: list[str] = []
    for i in range(20):
        lines += ["CI を待ちます。", f"テスト {i} 件目が通りました。"]

    folded = await _offer_all(fold, lines)

    assert not any(folded)
    assert discord.posts == []


async def test_reset_forgets_the_previous_turn() -> None:
    """A new turn starts clean: its first copies are shown again."""
    discord = _Discord()
    fold = _fold(discord)
    await _offer_all(fold, ["待機中。"] * 10)
    await fold.reset()

    assert await _offer_all(fold, ["待機中。"] * REPEAT_FOLD_AFTER) == [False] * REPEAT_FOLD_AFTER


async def test_edits_are_throttled_but_the_last_count_always_lands() -> None:
    """One edit per copy at 5/s would trip Discord's edit limit; drop none at the end."""
    discord = _Discord()
    clock = _Clock()
    fold = _fold(discord, clock)
    for _ in range(40):
        await fold.offer("待機中。")
        clock.advance(0.2)  # the fastest gap measured

    assert len(discord.edits) < 10, len(discord.edits)
    await fold.close()
    assert "37" in discord.shown("m1"), discord.shown("m1")


async def test_flush_pushes_a_pending_count_without_new_lines() -> None:
    """The mirror's idle tick calls this so a stalled loop shows its real count."""
    discord = _Discord()
    clock = _Clock()
    fold = _fold(discord, clock)
    for _ in range(REPEAT_FOLD_AFTER + 3):
        await fold.offer("待機中。")  # no time passes → edits held back

    clock.advance(30)
    await fold.flush()
    assert "3" in discord.shown("m1"), discord.shown("m1")


async def test_the_counter_is_one_short_line() -> None:
    discord = _Discord()
    fold = _fold(discord)
    long_line = "とても長い待機メッセージです。" * 20
    await _offer_all(fold, [long_line] * 10)
    await fold.close()

    text = discord.shown("m1")
    assert "\n" not in text, text
    assert text.startswith("-# "), text
    assert len(text) < 200, text


async def test_folding_is_logged_at_info_with_the_thread(caplog: pytest.LogCaptureFixture) -> None:
    """AC3: what was folded, and how much, is in the normal log (#678)."""
    discord = _Discord()
    fold = _fold(discord)
    with caplog.at_level(logging.INFO, logger="c_lord.transcript.repeat_fold"):
        await _offer_all(fold, ["待機中。"] * 60)
        await fold.close()

    infos = [r.getMessage() for r in caplog.records if r.levelno == logging.INFO]
    assert any(f"thread={THREAD}" in m and "待機中。" in m for m in infos), infos
    closing = [m for m in infos if f"thread={THREAD}" in m and "57" in m]
    assert closing, infos


async def test_a_failed_post_still_keeps_the_flood_out() -> None:
    """Best effort, like the progress line: Discord failing is not a reason to flood."""

    async def post(text: str) -> object:
        raise RuntimeError("discord is down")

    async def edit(handle: object, text: str) -> None:  # pragma: no cover - never reached
        raise AssertionError

    fold = RepeatFold(thread_id=THREAD, post=post, edit=edit, clock=_Clock())
    folded = [await fold.offer("待機中。") for _ in range(10)]

    assert folded.count(False) == REPEAT_FOLD_AFTER


async def test_a_counter_that_cannot_be_edited_does_not_claim_a_number() -> None:
    """Without an editor the first text is all anyone sees — it must not go stale."""
    posts: list[str] = []

    async def post(text: str) -> None:
        posts.append(text)
        return None

    fold = RepeatFold(thread_id=THREAD, post=post, edit=None, clock=_Clock())
    await _offer_all(fold, ["待機中。"] * 30)
    await fold.close()

    assert len(posts) == 1
    assert not any(ch.isdigit() for ch in posts[0].replace("待機中。", "")), posts[0]
