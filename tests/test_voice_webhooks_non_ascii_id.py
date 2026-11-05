"""Review of #1218: a ``²`` in voice-webhooks.json made every webhook message raise.

The registry reader kept an entry when ``str(x).isdigit()`` and then called
``int(x)`` outside any ``try``. ``"²"`` is ``isdigit()`` but not ``int()``-able, so
one such entry in ``~/.c-lord/voice-webhooks.json`` raised ``ValueError`` on every
webhook message (the voice check runs first in ``on_message``) — dispatch from
other Claudes stopped too. Like the .env IDs (#1084), only ASCII digits are an
id; anything else is skipped, with one WARNING per version of the file.
"""

from __future__ import annotations

import json
import logging
from pathlib import Path

import pytest

from c_lord import voice_webhooks as v


@pytest.fixture
def registry(tmp_path: Path, monkeypatch: pytest.MonkeyPatch):  # type: ignore[no-untyped-def]
    p = tmp_path / "voice-webhooks.json"
    monkeypatch.setattr(v, "registry_path", lambda: p)
    v._reset_for_tests()
    yield p
    v._reset_for_tests()


def test_a_superscript_entry_does_not_raise(registry: Path) -> None:
    registry.write_text(json.dumps([111, "²", "１２３"]))
    assert v.is_voice_webhook(111)
    assert not v.is_voice_webhook(2)
    assert not v.is_voice_webhook(123)


def test_the_skipped_entries_are_logged_once_per_file_version(
    registry: Path, caplog: pytest.LogCaptureFixture
) -> None:
    registry.write_text(json.dumps([111, "²"]))
    with caplog.at_level(logging.WARNING, logger="c_lord.voice_webhooks"):
        for _ in range(5):
            v.is_voice_webhook(999)
    skipped = [r for r in caplog.records if "'²'" in r.getMessage()]
    assert len(skipped) == 1


def test_register_still_works_with_such_a_file(registry: Path) -> None:
    registry.write_text(json.dumps([222, "²"]))
    v.register(111)
    assert set(json.loads(registry.read_text())) == {111, 222}
