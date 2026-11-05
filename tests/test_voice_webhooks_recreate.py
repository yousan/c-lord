"""#1222: a deleted voice-webhooks.json that cannot be recreated must be logged.

#1204 recreates ``~/.c-lord/voice-webhooks.json`` when it has been removed. But
every attempt from ``is_voice_webhook`` was made with ``quiet=True``, so when the
directory was not writable the failure was never logged — not once — while our
own voice webhook id stayed off disk (a restart, or another instance, would then
take our sub-agents' voices as orders, design decision 12). The first failure is
now a WARNING; the retries after it stay quiet, like the other write paths.
"""

from __future__ import annotations

import json
import logging
from pathlib import Path

import pytest

from c_lord import voice_webhooks as v


@pytest.fixture
def registry(tmp_path: Path, monkeypatch: pytest.MonkeyPatch):  # type: ignore[no-untyped-def]
    d = tmp_path / "home"
    d.mkdir()
    p = d / "voice-webhooks.json"
    monkeypatch.setattr(v, "registry_path", lambda: p)
    v._reset_for_tests()
    yield p
    d.chmod(0o755)
    v._reset_for_tests()


def _could_not_write(caplog: pytest.LogCaptureFixture) -> int:
    return sum("could not write" in r.getMessage() for r in caplog.records)


def test_failed_recreation_is_logged_at_least_once(
    registry: Path, caplog: pytest.LogCaptureFixture
) -> None:
    """The file is removed and its directory is not writable."""
    v.register(111)
    assert 111 in json.loads(registry.read_text())
    registry.unlink()
    registry.parent.chmod(0o555)  # lock file exists and stays writable; tmp cannot be made
    with caplog.at_level(logging.WARNING, logger="c_lord.voice_webhooks"):
        assert v.is_voice_webhook(111)  # our own voice — recreation attempted, fails
        v._next_retry = 0.0  # 60 s later
        v.is_voice_webhook(999)  # an unknown webhook
        v._next_retry = 0.0
        v.is_voice_webhook(111)
    assert not registry.exists()
    assert v._unwritten  # the module itself knows ours is not on disk
    assert _could_not_write(caplog) >= 1  # failed on main: 0


def test_failed_recreation_is_logged_only_once(
    registry: Path, caplog: pytest.LogCaptureFixture
) -> None:
    """One WARNING for the first failure, then quiet — not one per 60 s window."""
    v.register(111)
    registry.unlink()
    registry.parent.chmod(0o555)
    with caplog.at_level(logging.WARNING, logger="c_lord.voice_webhooks"):
        for _ in range(5):
            v._next_retry = 0.0
            v.is_voice_webhook(111)
            v._next_retry = 0.0
            v.is_voice_webhook(999)
    assert _could_not_write(caplog) == 1


def test_failed_recreation_is_not_a_loop(registry: Path, monkeypatch) -> None:  # noqa: ANN001
    """No endless rewrite: one attempt per 60 s window however many posts come."""
    v.register(111)
    registry.unlink()
    registry.parent.chmod(0o555)
    calls = []
    real = v._write_merged
    monkeypatch.setattr(v, "_write_merged", lambda p, **k: calls.append(1) or real(p, **k))
    v._next_retry = 0.0
    for _ in range(500):
        v.is_voice_webhook(111)
        v.is_voice_webhook(999)
    assert len(calls) == 1


def test_recreation_keeps_other_instances_ids_we_had_read(registry: Path) -> None:
    """Recreation writes everything this process knows, not only ours."""
    registry.write_text(json.dumps([222]))  # another instance's
    v.register(111)
    registry.unlink()
    v._next_retry = 0.0  # 60 s later
    v.is_voice_webhook(111)
    assert set(json.loads(registry.read_text())) == {111, 222}


def test_deleted_within_60s_of_a_write_is_not_recreated_before_a_restart(
    registry: Path,
) -> None:
    """Window (by design, 60 s throttle): removed right after register; our own
    voices keep posting; the process restarts inside the window -> our id is gone."""
    v.register(111)
    registry.unlink()
    for _ in range(10):
        assert v.is_voice_webhook(111)  # within 60 s of the register
    assert not registry.exists()
    v._reset_for_tests()  # restart
    assert not v.is_voice_webhook(111)  # the catch-up would obey this voice
