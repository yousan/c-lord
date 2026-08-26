"""send_input must not assume Claude Code runs in vim mode (#544).

c-lord used to read a bare ``⏵⏵ bypass permissions on …`` status bar as "vim is
in NORMAL mode" and press ``i`` before typing.  But ``⏵⏵`` is the *permission
mode* indicator: it is there whether or not the editor is in vim mode.  Verified
on Claude Code v2.1.246:

    vim on,  INSERT  ->  ``-- INSERT -- ⏵⏵ bypass permissions on …``
    vim on,  NORMAL  ->  ``⏵⏵ bypass permissions on …``
    vim off (default)->  ``⏵⏵ bypass permissions on …``   <- identical

So for a consumer who does not use vim mode, every single message sent from
Discord got a literal ``i`` typed in front of it (``iこんにちは`` reached Claude).
yousan's own machine has ``editorMode: "vim"``, which is why this never showed
up here — it is a bug that only fires in *other people's* installs, exactly the
class of failure the Zero-Config Principle exists to prevent.

Since the two ambiguous frames are byte-identical, the status bar alone cannot
decide.  send_input therefore *probes*: it presses ``i`` and looks at what
happened.  If ``-- INSERT`` appears, it really was vim NORMAL and the keypress
did its job.  If nothing changes, vim is off and the ``i`` was a literal
character — which is erased with a BSpace before the message is typed.
"""

from __future__ import annotations

from pathlib import Path
from unittest.mock import MagicMock, patch

from c_lord.tmux import TmuxSessionManager

_FIX = Path(__file__).parent / "fixtures" / "panes"

VIM_OFF = (_FIX / "vim_disabled_input_idle.txt").read_text()
VIM_ON_INSERT = (_FIX / "vim_enabled_insert.txt").read_text()
VIM_ON_NORMAL = (_FIX / "vim_enabled_normal.txt").read_text()
# A build that renders the explicit marker: unambiguous vim-but-not-INSERT.
VIM_ON_NORMAL_MARKED = VIM_ON_NORMAL.replace(
    "  ⏵⏵ bypass permissions", "  -- NORMAL -- ⏵⏵ bypass permissions"
)

MESSAGE = "こんにちは、テストです"


def _mgr() -> TmuxSessionManager:
    mgr = TmuxSessionManager(mapping_path="")
    mgr._available = True
    mgr.session_name = "t"
    mgr._find_window_for_thread = lambda _tid: "w1"  # type: ignore[method-assign]
    return mgr


class _Pane:
    """Records send-keys traffic and replays a scripted series of captures."""

    def __init__(self, *captures: str) -> None:
        self._captures = list(captures)
        self.calls: list[list[str]] = []

    def __call__(self, args: list[str]) -> MagicMock:
        self.calls.append(list(args))
        if "capture-pane" in args:
            # Last scripted frame repeats once the script runs out.
            frame = self._captures[0] if len(self._captures) == 1 else self._captures.pop(0)
            return MagicMock(returncode=0, stdout=frame)
        return MagicMock(returncode=0, stdout="")

    @property
    def keys(self) -> list[list[str]]:
        return [c for c in self.calls if "send-keys" in c]

    def _key_indices(self, key: str) -> list[int]:
        """Positions of a bare key press (``send-keys -t tgt KEY``) in key order."""
        return [i for i, c in enumerate(self.keys) if "-l" not in c and c[-1] == key]

    @property
    def i_presses(self) -> list[int]:
        return self._key_indices("i")

    @property
    def backspaces(self) -> list[int]:
        return self._key_indices("BSpace")

    @property
    def text_index(self) -> int | None:
        for i, c in enumerate(self.keys):
            if "-l" in c and any(MESSAGE in a for a in c):
                return i
        return None


def _send(pane: _Pane, mgr: TmuxSessionManager | None = None, text: str = MESSAGE) -> None:
    with patch("c_lord.tmux._run", side_effect=pane), patch("c_lord.tmux.time.sleep"):
        (mgr or _mgr()).send_input(12345, text)


# --------------------------------------------------------------------------
# AC1 — vim disabled: no stray ``i`` may reach Claude
# --------------------------------------------------------------------------


def test_vim_disabled_pane_delivers_message_unprefixed() -> None:
    """The #544 bug: a vim-less pane must not end up with ``i`` before the text.

    Probing is allowed, but anything typed to probe has to be erased before the
    message itself is typed.
    """
    pane = _Pane(VIM_OFF)  # every capture shows the vim-less status bar
    _send(pane)

    assert pane.text_index is not None, "the message must still be sent"
    # Whatever probing happened must net out to zero characters in the box.
    assert len(pane.i_presses) == len(pane.backspaces), (
        f"every probe 'i' must be erased: i={pane.i_presses} bspace={pane.backspaces}"
    )
    for idx in pane.i_presses + pane.backspaces:
        assert idx < pane.text_index, "probing must finish before the message is typed"


def test_vim_disabled_probe_is_not_repeated_for_the_same_window() -> None:
    """Once a window is known to be vim-less, later sends skip the probe."""
    mgr = _mgr()
    first = _Pane(VIM_OFF)
    _send(first, mgr)
    assert first.i_presses, "the first send has to probe to find out"

    second = _Pane(VIM_OFF)
    _send(second, mgr)
    assert not second.i_presses, "a known vim-less window must never be sent 'i' again"
    assert second.text_index is not None


# --------------------------------------------------------------------------
# AC2 — vim enabled: the #147 NORMAL-mode correction must not regress
# --------------------------------------------------------------------------


def test_vim_normal_pane_is_corrected_into_insert() -> None:
    """#147 must survive: a vim pane in NORMAL still gets its ``i``.

    v2.1.246 renders no marker in NORMAL, so the first frame is ambiguous. The
    probe presses ``i``; the pane answers with ``-- INSERT``, proving vim — so
    the keypress is kept (no BSpace) and the message lands in the input box.
    """
    pane = _Pane(VIM_ON_NORMAL, VIM_ON_INSERT)
    _send(pane)

    assert len(pane.i_presses) == 1, "vim NORMAL must still be corrected with 'i' (#147)"
    assert not pane.backspaces, "the 'i' entered INSERT mode — erasing it would break #147"
    assert pane.text_index is not None and pane.text_index > pane.i_presses[0]


def test_explicit_normal_marker_needs_no_probe() -> None:
    """``-- NORMAL`` is positive proof — press ``i`` and type, no probing."""
    pane = _Pane(VIM_ON_NORMAL_MARKED, VIM_ON_INSERT)
    _send(pane)

    assert len(pane.i_presses) == 1
    assert not pane.backspaces


def test_insert_pane_is_left_alone() -> None:
    """An already-INSERT pane must not get a double-``i`` (#147 AC2)."""
    pane = _Pane(VIM_ON_INSERT)
    _send(pane)

    assert not pane.i_presses, "must not press 'i' when already in INSERT"
    assert pane.text_index is not None


def test_observing_insert_marks_the_window_as_vim() -> None:
    """Seeing ``-- INSERT`` once is enough to trust later ambiguous frames.

    After a send on an INSERT pane, a subsequent NORMAL drop (which renders no
    marker on v2.1.246) is corrected straight away rather than re-probed.
    """
    mgr = _mgr()
    _send(_Pane(VIM_ON_INSERT), mgr)

    later = _Pane(VIM_ON_NORMAL)  # ambiguous frame, but vim is already known
    _send(later, mgr)
    assert len(later.i_presses) == 1, "a known-vim window must be corrected without probing"
    assert not later.backspaces, "no probe ran, so nothing to erase"


# --------------------------------------------------------------------------
# Panes with no input prompt at all must stay untouched
# --------------------------------------------------------------------------


def test_pane_without_status_bar_is_untouched() -> None:
    """An indeterminate frame (mid-redraw, no status bar) gets no keypresses."""
    pane = _Pane("just some\nresponse text\n")
    _send(pane)

    assert not pane.i_presses and not pane.backspaces
    assert pane.text_index is not None
