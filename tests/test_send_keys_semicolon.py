"""A request that ends in ``;`` must reach Claude with its ``;`` (#1284).

tmux reads an argv word that ends in ``;`` as a command separator, even after
``--`` and ``-l``. ``_type_literal`` ran ``send-keys -l -t <target> -- <chunk>``,
so a message ending in ``;`` (``x = 1;``, an SQL statement, a shell line)
reached Claude without it, one ending in ``\\;`` lost the backslash instead, and
when a long message was cut into 3,000-byte pieces at a ``;``, that ``;`` vanished
from the middle of the request. Claude then worked on something the user did not
write, and nobody was told.

The real-tmux tests run on an isolated server (``tmux -L``, #701) with ``cat``
in the pane writing exactly what arrived to a file.
"""

from __future__ import annotations

import os
import shutil
import subprocess
import time
import uuid
from pathlib import Path
from unittest.mock import MagicMock, patch

import pytest

from c_lord import tmux as tmux_mod
from c_lord.tmux import _SEND_KEYS_CHUNK_BYTES

_CASES = {
    "foo;": "foo;",
    "x = 1;": "x = 1;",
    "backslash-semicolon": "a\\;",
    "lone": ";",
    "double": ";;",
    "inside": "a;b",
    "escaped-inside-and-end": "a \\; b;",
    "lone-backslash": "\\",
    "piece-ends-in-semicolon": ("a" * (_SEND_KEYS_CHUNK_BYTES - 1)) + ";" + "b" * 10,
    "piece-ends-in-backslash-semicolon": ("a" * (_SEND_KEYS_CHUNK_BYTES - 2)) + "\\;" + "b" * 10,
}
_TEXTS = pytest.mark.parametrize("text", list(_CASES.values()), ids=list(_CASES))


def _typed(text: str) -> str:
    """What tmux's argv parser makes of each ``send-keys -l`` word we pass.

    Mirrors ``cmd_parse_from_arguments`` (tmux 3.4): a trailing ``;`` ends the
    command and is dropped, unless it follows a ``\\``, in which case the pair
    becomes one literal ``;``.
    """
    out: list[str] = []
    sent: list[list[str]] = []

    def run(args: list[str]) -> MagicMock:
        sent.append(args)
        return MagicMock(returncode=0, stdout="", stderr="")

    with patch("c_lord.tmux._run", side_effect=run):
        mgr = tmux_mod.TmuxSessionManager(mapping_path="")
        assert mgr._type_literal("s:0", text, what="t") is True
    for args in sent:
        assert args[:2] == ["tmux", "send-keys"], args
        word = args[-1]
        if word.endswith(";"):
            word = word[:-1]
            if word.endswith("\\"):
                word = word[:-1] + ";"
        out.append(word)
    return "".join(out)


@_TEXTS
def test_every_word_handed_to_tmux_types_the_text_back(text: str) -> None:
    assert _typed(text) == text


@_TEXTS
def test_no_piece_grows_past_the_send_keys_cap(text: str) -> None:
    sent: list[list[str]] = []

    def run(args: list[str]) -> MagicMock:
        sent.append(args)
        return MagicMock(returncode=0, stdout="", stderr="")

    with patch("c_lord.tmux._run", side_effect=run):
        tmux_mod.TmuxSessionManager(mapping_path="")._type_literal("s:0", text, what="t")
    assert all(len(a[-1].encode()) <= _SEND_KEYS_CHUNK_BYTES + 1 for a in sent)


@pytest.mark.skipif(shutil.which("tmux") is None, reason="needs tmux")
@_TEXTS
def test_type_literal_keeps_a_trailing_semicolon(tmp_path: Path, text: str) -> None:
    sock = f"clord-1284-{uuid.uuid4().hex[:8]}"
    out = tmp_path / "out.txt"
    base = ["tmux", "-L", sock, "-f", "/dev/null"]
    env = {k: v for k, v in os.environ.items() if k not in ("TMUX", "TMUX_PANE")}
    subprocess.run(
        [
            *base,
            "new-session",
            "-d",
            "-s",
            "s",
            "-x",
            "160",
            "-y",
            "40",
            f"stty raw -echo; cat > {out}",
        ],
        check=True,
        env=env,
    )
    real_run = tmux_mod._run
    try:
        time.sleep(0.3)
        with patch("c_lord.tmux._run", side_effect=lambda a: real_run([a[0], "-L", sock, *a[1:]])):
            mgr = tmux_mod.TmuxSessionManager(mapping_path="")
            assert mgr._type_literal("s:0", text, what="t") is True
        time.sleep(0.5)
    finally:
        subprocess.run([*base, "kill-server"], env=env, check=False, capture_output=True)
    assert out.read_text() == text
