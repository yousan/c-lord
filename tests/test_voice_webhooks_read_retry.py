"""Review3 #1072 (from #1050 round 2): a failed read must not be recorded as read."""

from __future__ import annotations

import os
from pathlib import Path

import pytest

from c_lord import voice_webhooks as v


@pytest.mark.skipif(os.geteuid() == 0, reason="root ignores chmod")
def test_transient_read_error_is_retried_when_file_unchanged(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    p = tmp_path / "voice-webhooks.json"
    monkeypatch.setattr(v, "registry_path", lambda: p)
    v._reset_for_tests()
    p.write_text("[777]")
    p.chmod(0)  # one failed read (EACCES)
    try:
        assert not v.is_voice_webhook(777)
    finally:
        p.chmod(0o600)  # readable again; chmod changes neither mtime nor size
    assert v.is_voice_webhook(777)  # another instance's voice must be recognised
    v._reset_for_tests()
