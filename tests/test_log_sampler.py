"""One log line per window per key, and a count of what it stood for — #678.

A drop that is *quiet by design* still has to be **findable**. #556 made c-lord
swallow webhook messages that land in a thread with no ``sessions`` row (an
alerting webhook is not waiting for an answer, so a reply/reaction/notice would
be noise — see :mod:`tests.test_untracked_notice_scope`), and logged that drop at
DEBUG so a chatty webhook could not flood the log. The cost showed up on
2026-09-02: a probe was sent into such a thread and **nothing came back and
nothing was logged**, so "bot down / webhook broken / thread out of scope" could
not be told apart without reading the source (#678).

This module is the middle ground the Issue picked (option **b**): INFO, but at
most once per window per key, with the occurrences it stood for counted onto the
next line. Chatty stays cheap; the one-off probe is always visible.
"""

from __future__ import annotations

from c_lord.log_sampler import LogSampler


class _Clock:
    """A hand-wound clock — the window must be testable without sleeping."""

    def __init__(self) -> None:
        self.now = 1000.0

    def __call__(self) -> float:
        return self.now

    def advance(self, seconds: float) -> None:
        self.now += seconds


class TestFirstOccurrence:
    def test_it_emits(self) -> None:
        """The one-off probe of #678 — the case that must never be invisible."""
        sampler = LogSampler(window=600.0, clock=_Clock())

        assert sampler.sample(42).emit is True

    def test_it_reports_nothing_suppressed(self) -> None:
        sampler = LogSampler(window=600.0, clock=_Clock())

        assert sampler.sample(42).suppressed == 0


class TestWithinTheWindow:
    def test_the_second_occurrence_does_not_emit(self) -> None:
        clock = _Clock()
        sampler = LogSampler(window=600.0, clock=clock)
        sampler.sample(42)

        clock.advance(1.0)

        assert sampler.sample(42).emit is False

    def test_a_chatty_webhook_emits_exactly_once(self) -> None:
        """AC2: the flood #556 worried about must not reach INFO."""
        clock = _Clock()
        sampler = LogSampler(window=600.0, clock=clock)

        emitted = 0
        for _ in range(200):
            clock.advance(0.5)  # 100s of traffic — inside one window
            if sampler.sample(42).emit:
                emitted += 1

        assert emitted == 1

    def test_the_edge_of_the_window_still_suppresses(self) -> None:
        clock = _Clock()
        sampler = LogSampler(window=600.0, clock=clock)
        sampler.sample(42)

        clock.advance(599.9)

        assert sampler.sample(42).emit is False


class TestAfterTheWindow:
    def test_it_emits_again(self) -> None:
        clock = _Clock()
        sampler = LogSampler(window=600.0, clock=clock)
        sampler.sample(42)

        clock.advance(600.0)

        assert sampler.sample(42).emit is True

    def test_it_carries_the_count_it_stood_for(self) -> None:
        """The #585 half: the line says how much was dropped, not just that some was."""
        clock = _Clock()
        sampler = LogSampler(window=600.0, clock=clock)
        sampler.sample(42)
        for _ in range(37):
            sampler.sample(42)

        clock.advance(600.0)

        assert sampler.sample(42).suppressed == 37

    def test_the_count_is_not_reported_twice(self) -> None:
        clock = _Clock()
        sampler = LogSampler(window=600.0, clock=clock)
        sampler.sample(42)
        sampler.sample(42)
        clock.advance(600.0)
        sampler.sample(42)

        clock.advance(600.0)

        assert sampler.sample(42).suppressed == 0


class TestKeysAreIndependent:
    def test_another_thread_is_not_silenced_by_the_chatty_one(self) -> None:
        """AC3's other half: per-thread, or one noisy thread hides every other."""
        clock = _Clock()
        sampler = LogSampler(window=600.0, clock=clock)
        for _ in range(50):
            sampler.sample(111)

        assert sampler.sample(222).emit is True

    def test_each_key_keeps_its_own_count(self) -> None:
        clock = _Clock()
        sampler = LogSampler(window=600.0, clock=clock)
        sampler.sample(111)
        sampler.sample(222)
        sampler.sample(111)
        sampler.sample(111)

        clock.advance(600.0)

        assert (sampler.sample(111).suppressed, sampler.sample(222).suppressed) == (2, 0)


class TestSuffix:
    def test_it_is_empty_when_nothing_was_suppressed(self) -> None:
        """A quiet thread's line must not carry a "(+0 …)" tail."""
        assert LogSampler(window=600.0, clock=_Clock()).sample(42).suffix == ""

    def test_it_names_the_count_and_the_window(self) -> None:
        clock = _Clock()
        sampler = LogSampler(window=600.0, clock=clock)
        sampler.sample(42)
        sampler.sample(42)
        sampler.sample(42)

        clock.advance(600.0)

        assert sampler.sample(42).suffix == " (+2 suppressed in the last 600s)"


class TestMemory:
    def test_it_does_not_grow_without_bound(self) -> None:
        """A guild where every alert opens its own thread must not leak keys."""
        clock = _Clock()
        sampler = LogSampler(window=600.0, clock=clock, max_keys=64)

        for key in range(5000):
            clock.advance(1.0)
            sampler.sample(key)

        assert sampler.tracked_keys <= 64
