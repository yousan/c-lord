"""A long Discord message must not come back as 👤 when the CLI wraps it (#808).

PR #816 recognised c-lord's own input by the copy ``pane_echo`` keeps of what
was typed. Claude Code (2.1.283, measured on an isolated tmux server) folds any
input of about 800 characters or more into ``[Pasted text #N]`` and writes it to
the transcript wrapped::

    \\n\\n<pasted_content id="666f">\\n<the text>\\n</pasted_content id="666f">\\n

The copy no longer matched, so the message came back as a 👤 line — 12 of the
13 echoes seen in production on 9/30–10/2, the shortest a 1-word "おねがい！"
carrying a quoted Discord message (988 characters in all). Part of the text can
also land *after* the closing tag when the paste is split (seen in production:
``…と\\n</pasted_content id="a5cd">\\n\\nどめる。…``), and an attachment note
can follow it.

The wrapper is the CLI's, not the user's, so it is ignored when comparing. The
match stays exact otherwise: a person's own paste in the pane still shows.
"""

from __future__ import annotations

import pytest

from c_lord.transcript.pane_echo import PaneEchoRegistry

from .test_echo_without_zwsp import _mirror, _send_input, _user

TID = 808

_LONG = "おねがい！\n\n--- Referenced Discord message (#W5, C-lord) ---\n" + "長い引用。" * 200


def _wrapped(text: str, pid: str = "666f") -> str:
    return f'\n\n<pasted_content id="{pid}">\n{text}\n</pasted_content id="{pid}">\n'


@pytest.fixture
def reg() -> PaneEchoRegistry:
    return PaneEchoRegistry()


def test_a_wrapped_echo_matches_what_was_typed(reg: PaneEchoRegistry) -> None:
    reg.register(TID, _LONG)
    assert reg.consume_match(TID, _wrapped(_LONG)) is True


def test_text_split_across_the_closing_tag_still_matches(reg: PaneEchoRegistry) -> None:
    text = "前半の文章です。" * 120 + "パスは例の形にとどめる。後半です。"
    head, tail = text.split("にと", 1)
    recorded = f'\n\n<pasted_content id="a5cd">\n{head}にと\n</pasted_content id="a5cd">\n\n{tail}'
    reg.register(TID, text)
    assert reg.consume_match(TID, recorded) is True


def test_wrapping_does_not_loosen_the_exact_match(reg: PaneEchoRegistry) -> None:
    reg.register(TID, _LONG)
    assert reg.consume_match(TID, _wrapped(_LONG + "（人が書き足した）")) is False
    assert reg.consume_match(TID, _wrapped(_LONG[:-10])) is False


def test_a_message_that_quotes_the_tag_still_matches_itself(reg: PaneEchoRegistry) -> None:
    """A Discord message *about* the wrapper (like this Issue) is still its own echo."""
    text = 'CLI が `<pasted_content id="834d">` で包むと一致しない、という話'
    reg.register(TID, text)
    assert reg.consume_match(TID, text) is True


async def test_long_discord_message_is_not_echoed_when_the_cli_wraps_it(tmp_path) -> None:
    """AC: the production shape — no 👤 re-post."""
    assert _send_input(_LONG) is True
    posted = await _mirror(tmp_path, [_user(_wrapped(_LONG))])
    assert not [p for p in posted if "おねがい" in p], posted


async def test_a_paste_typed_by_a_person_in_the_pane_is_still_mirrored(tmp_path) -> None:
    assert _send_input("Discord からの別の発言") is True
    posted = await _mirror(tmp_path, [_user(_wrapped("ターミナルに人が貼った長文" * 80))])
    assert any("👤" in p for p in posted), posted
