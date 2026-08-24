"""Long input must never be swallowed by tmux's IPC size cap (#527).

tmux hands a command to its server over an imsg whose payload is capped at
``MAX_IMSGSIZE`` (16384 bytes).  A single ``send-keys -l`` carrying a whole
prompt therefore fails outright with ``command too long`` once the prompt
passes ~16KB.

What that looked like in production (2026-08-22, thread W126): a 19,852-byte
``.md`` attachment was inlined into the prompt, ``send_input`` failed, and the
user got ``❌ Failed to send input to Claude in tmux`` — the message never
reached Claude at all (no ``user`` row in the session transcript).

So every path that types a variable-length payload into the pane must split it
into pieces small enough for tmux, on **character** boundaries so a multi-byte
character is never cut in half.
"""

from __future__ import annotations

import shutil
import subprocess
import uuid
from unittest.mock import MagicMock, patch

import pytest

from c_lord.tmux import _SEND_KEYS_CHUNK_BYTES, TmuxSessionManager, _chunk_for_send_keys

# Comfortably past tmux's ~16KB ceiling, and mixed-width so a naive byte split
# would corrupt it.
_LONG_TEXT = "\n".join(f"## 見出し {i} — section {i} " + "あいうえお abcde" * 8 for i in range(300))


def _mgr() -> TmuxSessionManager:
    mgr = TmuxSessionManager(mapping_path="")
    mgr._available = True
    mgr.session_name = "t"
    mgr._find_window_for_thread = lambda _tid: "w1"  # type: ignore[method-assign]
    return mgr


def _literal_payloads(calls: list[list[str]]) -> list[str]:
    """The text carried by every ``send-keys -l`` call, in order."""
    return [c[-1] for c in calls if "send-keys" in c and "-l" in c]


def _run_recorder(calls: list[list[str]], pane: str = ""):
    def fake_run(args):
        calls.append(list(args))
        if "capture-pane" in args:
            return MagicMock(returncode=0, stdout=pane)
        return MagicMock(returncode=0, stdout="")

    return fake_run


# ── the chunker itself ──────────────────────────────────────────────


def test_chunker_respects_byte_limit() -> None:
    for chunk in _chunk_for_send_keys(_LONG_TEXT):
        assert len(chunk.encode("utf-8")) <= _SEND_KEYS_CHUNK_BYTES


def test_chunker_is_lossless() -> None:
    assert "".join(_chunk_for_send_keys(_LONG_TEXT)) == _LONG_TEXT


def test_chunker_never_splits_a_multibyte_character() -> None:
    """Each chunk must be valid UTF-8 on its own — tmux gets bytes, not str."""
    dense = "あ" * 20000  # 60KB, every character 3 bytes
    chunks = _chunk_for_send_keys(dense)
    assert "".join(chunks) == dense
    for chunk in chunks:
        raw = chunk.encode("utf-8")
        assert len(raw) <= _SEND_KEYS_CHUNK_BYTES
        assert raw.decode("utf-8") == chunk


def test_chunker_leaves_short_text_in_one_piece() -> None:
    assert _chunk_for_send_keys("hello") == ["hello"]
    assert _chunk_for_send_keys("") == [""]


# ── the three send paths ────────────────────────────────────────────


def test_send_input_splits_long_prompt() -> None:
    calls: list[list[str]] = []
    with patch("c_lord.tmux._run", side_effect=_run_recorder(calls)):
        assert _mgr().send_input(12345, _LONG_TEXT) is True

    payloads = _literal_payloads(calls)
    assert len(payloads) > 1, "a 30KB prompt must not go out as one send-keys (#527)"
    for p in payloads:
        assert len(p.encode("utf-8")) <= _SEND_KEYS_CHUNK_BYTES
    # Lossless: the concatenation is the prompt (plus the jsonl ZWSP marker).
    assert "".join(payloads).endswith(_LONG_TEXT)
    # Still submitted exactly once, after the text.
    assert [c for c in calls if "send-keys" in c][-1][-1] == "Enter"


def test_send_input_short_prompt_stays_one_call() -> None:
    """No churn for ordinary messages — one literal send, one Enter."""
    calls: list[list[str]] = []
    with patch("c_lord.tmux._run", side_effect=_run_recorder(calls)):
        _mgr().send_input(12345, "普通の返信")
    assert len(_literal_payloads(calls)) == 1


def test_send_input_reports_failure_of_any_chunk() -> None:
    calls: list[list[str]] = []

    def fake_run(args):
        calls.append(list(args))
        if "capture-pane" in args:
            return MagicMock(returncode=0, stdout="")
        # Fail the third literal chunk.
        if "-l" in args and len(_literal_payloads(calls)) == 3:
            return MagicMock(returncode=1, stdout="", stderr="command too long")
        return MagicMock(returncode=0, stdout="")

    with patch("c_lord.tmux._run", side_effect=fake_run):
        assert _mgr().send_input(12345, _LONG_TEXT) is False
    # A failed send must not be "submitted" — no Enter after a broken payload.
    assert not any(c[-1] == "Enter" for c in calls if "send-keys" in c)
    # …and the half-typed text must be wiped, or it would prepend itself to the
    # user's *next* message.
    assert any(c[-1] == "C-u" for c in calls if "send-keys" in c)


def test_send_literal_splits_long_text() -> None:
    calls: list[list[str]] = []
    with patch("c_lord.tmux._run", side_effect=_run_recorder(calls)):
        assert _mgr().send_literal(12345, _LONG_TEXT) is True

    payloads = _literal_payloads(calls)
    assert len(payloads) > 1
    assert "".join(payloads) == _LONG_TEXT
    # send_literal never submits.
    assert not any(c[-1] == "Enter" for c in calls if "send-keys" in c)


def test_start_claude_splits_long_command() -> None:
    calls: list[list[str]] = []
    with patch("c_lord.tmux._run", side_effect=_run_recorder(calls)):
        assert _mgr().start_claude(12345, _LONG_TEXT, "sonnet") is True

    payloads = _literal_payloads(calls)
    assert len(payloads) > 1, "the cold-start command line must be chunked too (#527)"
    for p in payloads:
        assert len(p.encode("utf-8")) <= _SEND_KEYS_CHUNK_BYTES
    cmd = "".join(payloads)
    assert cmd.startswith("unalias claude")
    assert _LONG_TEXT in cmd
    assert [c for c in calls if "send-keys" in c][-1][-1] == "Enter"


# ── the real thing: prove the cap exists and that chunking clears it ──


@pytest.mark.skipif(shutil.which("tmux") is None, reason="tmux not installed")
def test_real_tmux_rejects_a_single_oversized_send_but_accepts_chunks() -> None:
    """RED/GREEN against a live tmux server, not a mock.

    The first half is the bug: one ``send-keys -l`` of the same payload that
    broke W126 is refused by tmux.  The second half is the fix.
    """
    session = f"clord-chunk-test-{uuid.uuid4().hex[:8]}"
    subprocess.run(
        ["tmux", "new-session", "-d", "-s", session, "-x", "200", "-y", "50"], check=True
    )
    try:
        payload = "あ" * 7000  # 21,000 bytes — just past what W126 hit
        one_shot = subprocess.run(
            ["tmux", "send-keys", "-l", "-t", session, payload],
            capture_output=True,
            text=True,
        )
        assert one_shot.returncode != 0, "expected tmux to refuse an oversized send-keys"
        assert "too long" in one_shot.stderr

        for chunk in _chunk_for_send_keys(payload):
            r = subprocess.run(
                ["tmux", "send-keys", "-l", "-t", session, chunk],
                capture_output=True,
                text=True,
            )
            assert r.returncode == 0, r.stderr
    finally:
        subprocess.run(["tmux", "kill-session", "-t", session], capture_output=True)
