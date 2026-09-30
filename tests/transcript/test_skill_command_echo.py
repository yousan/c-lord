"""``/skill`` must not come back as a 👤 ``<command-message>…`` line (#834).

c-lord types ``/<name> <args>`` into the pane and records exactly that in the
#808 ``pane_echo`` registry. Claude Code does not write it back as typed: a
skill invocation is stored as

    <command-message>name</command-message>
    <command-name>/name</command-name>
    <command-args>args</command-args>

so the exact-match echo test never matched, and every ``/skill`` run posted the
user's request back to the thread as raw tags. The formatter now reads the
record back into the ``/name args`` the user typed, and the mirror's existing
echo test does the rest — c-lord's own command is dropped, one typed in the pane
by a person is still posted.
"""

from __future__ import annotations

from unittest.mock import MagicMock, patch

import pytest

from c_lord.transcript.formatter import render_event
from c_lord.transcript.pane_echo import pane_echo

from .test_echo_without_zwsp import TID, _mgr, _mirror, _send_input, _user


@pytest.fixture(autouse=True)
def _clean_registry():
    pane_echo.clear()
    yield
    pane_echo.clear()


def _skill_record(name: str, args: str) -> str:
    # Verbatim shape of a real 2.1.278 transcript (staging-2, 2026-09-29).
    return (
        f"<command-message>{name}</command-message>\n"
        f"<command-name>/{name}</command-name>\n"
        f"<command-args>{args}</command-args>"
    )


def test_skill_record_renders_as_the_command_the_user_typed() -> None:
    r = render_event(_user(_skill_record("copywriting", "何も書かず ok とだけ返して")))
    assert r is not None
    assert r.kind == "user_input"
    assert r.body == "/copywriting 何も書かず ok とだけ返して"


def test_skill_record_without_args() -> None:
    r = render_event(_user(_skill_record("copywriting", "")))
    assert r is not None
    assert r.body == "/copywriting"


def test_builtin_command_record_is_unchanged() -> None:
    """``<command-name>``-first records (/compact, /clear …) keep their #628 rendering."""
    r = render_event(
        _user(
            "<command-name>/compact</command-name>\n"
            "<command-message>compact</command-message>\n"
            "<command-args></command-args>"
        )
    )
    assert r is not None
    assert r.kind == "tool_use"


def test_human_mentioning_the_tag_is_plain_input() -> None:
    r = render_event(_user("<command-message> ってタグの話をしたい"))
    assert r is not None
    assert r.kind == "user_input"
    assert r.body == "<command-message> ってタグの話をしたい"


async def test_skill_via_send_input_is_not_echoed(tmp_path) -> None:
    """AC1 (in-thread ``/skill`` → ``send_input``)."""
    assert _send_input("/copywriting 何も書かず ok とだけ返して") is True
    posted = await _mirror(
        tmp_path, [_user(_skill_record("copywriting", "何も書かず ok とだけ返して"))]
    )
    assert not [p for p in posted if "copywriting" in p], posted


async def test_skill_via_start_claude_is_not_echoed(tmp_path) -> None:
    """AC1 (new-thread ``/skill`` → ``start_claude(as_command=True)``)."""
    with patch("c_lord.tmux._run", return_value=MagicMock(returncode=0, stdout="")):
        assert (
            _mgr().start_claude(TID, "/copywriting ok とだけ返して", "sonnet", as_command=True)
            is True
        )
    posted = await _mirror(tmp_path, [_user(_skill_record("copywriting", "ok とだけ返して"))])
    assert not [p for p in posted if "copywriting" in p], posted


async def test_skill_typed_in_the_pane_is_still_mirrored(tmp_path) -> None:
    """AC2: a command a person typed in the pane (no c-lord record) is posted."""
    assert _send_input("Discord からの発言") is True
    posted = await _mirror(tmp_path, [_user(_skill_record("copywriting", "ペインで直接"))])
    assert any("👤" in p and "/copywriting ペインで直接" in p for p in posted), posted
    assert not [p for p in posted if "<command-" in p], posted
