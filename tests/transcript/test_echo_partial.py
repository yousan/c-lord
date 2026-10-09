"""Say so in the log when Claude received only part of what c-lord typed (#872).

In production a 1,660-character message reached the transcript as its last 342
characters, and nothing in the log said a message had been cut: the only trace
was the tail coming back as a 👤 line. When a ``user`` event is a strict part of
something c-lord typed into that thread, the mirror logs a WARNING with both
sizes, so ``grep '#872'`` finds every occurrence.
"""

from __future__ import annotations

import logging

import pytest

from c_lord.transcript.pane_echo import PaneEchoRegistry

from .test_echo_without_zwsp import _mirror, _send_input, _user

TID = 872

_HEAD = "【おぷーから: 試作 v4】前半の目的と制約です。" * 40
_SENT = _HEAD + "ここから後半。セーフエリアの枠を足す。" * 10
_TAIL = _SENT[-342:]


@pytest.fixture
def reg() -> PaneEchoRegistry:
    return PaneEchoRegistry()


def test_a_recorded_tail_is_reported_with_both_sizes(reg: PaneEchoRegistry) -> None:
    reg.register(TID, _SENT)
    sizes = (len("".join(_SENT.split())), len("".join(_TAIL.split())))
    assert reg.consume_partial(TID, _TAIL) == sizes


def test_the_whole_message_is_not_a_partial(reg: PaneEchoRegistry) -> None:
    reg.register(TID, _SENT)
    assert reg.consume_partial(TID, _SENT) is None


def test_unrelated_text_is_not_a_partial(reg: PaneEchoRegistry) -> None:
    reg.register(TID, _SENT)
    assert reg.consume_partial(TID, "ペインで人が打った別の文章です。十分に長い文。") is None


def test_a_few_characters_are_not_evidence(reg: PaneEchoRegistry) -> None:
    """ "ok" is inside many messages — too short to say anything was cut."""
    reg.register(TID, "この作業が終わったら ok とだけ返してください")
    assert reg.consume_partial(TID, "ok") is None


async def test_mirror_warns_when_only_the_tail_arrived(tmp_path, caplog) -> None:  # noqa: ANN001
    assert _send_input(_SENT) is True
    with caplog.at_level(logging.WARNING):
        await _mirror(tmp_path, [_user(_TAIL)])
    warnings = [r.getMessage() for r in caplog.records if r.levelno >= logging.WARNING]
    assert any("#872" in w for w in warnings), warnings
