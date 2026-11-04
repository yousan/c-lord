"""#1108 / #1109 — a voice-webhook registry that cannot be read (review 4 of #1091).

#1108: since #1075 a failed read is retried on every unknown webhook message —
right, but each retry logged a WARNING with a traceback, so a file that stays
broken (nothing rewrites it unless voices are in use) produced one per webhook
message, up to five per message. Now: retried every time, logged once per
version of the file.

#1109: ``register`` treated a failed read as an empty file and wrote back only
its own ids, erasing every other instance's (#1024's whole point).

#1176: the id #1109 kept in memory never reached the file once it could be read
again, so a restart or another instance obeyed this process's voices.
"""

from __future__ import annotations

import json
import logging
import os
import time
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


def _could_not_read(caplog: pytest.LogCaptureFixture) -> int:
    return sum("could not read" in r.getMessage() for r in caplog.records)


def test_corrupt_file_logs_once_per_change_not_per_message(
    registry: Path, caplog: pytest.LogCaptureFixture
) -> None:
    registry.write_text("[777, 8")  # half-written / garbage that stays
    with caplog.at_level(logging.WARNING, logger="c_lord.voice_webhooks"):
        for _ in range(100):  # 100 webhook messages, e.g. from a dispatch webhook
            v.is_voice_webhook(12345)
    assert _could_not_read(caplog) <= 1


def test_a_new_version_of_a_still_broken_file_is_logged_again(
    registry: Path, caplog: pytest.LogCaptureFixture
) -> None:
    """Once per *change*: a different broken file is news, so it is logged."""
    registry.write_text("[777, 8")
    with caplog.at_level(logging.WARNING, logger="c_lord.voice_webhooks"):
        v.is_voice_webhook(12345)
        registry.write_text("[777, 88")  # size changes -> a new stamp
        v.is_voice_webhook(12345)
    assert _could_not_read(caplog) == 2


def test_a_quiet_retry_still_reads_the_file_once_it_is_fixed(registry: Path) -> None:
    """#1075 is kept: not logging the retry must not mean not retrying it."""
    registry.write_text("[777, 8")
    for _ in range(3):
        assert not v.is_voice_webhook(777)
    registry.write_text("[777]")
    assert v.is_voice_webhook(777)


@pytest.mark.skipif(os.geteuid() == 0, reason="root ignores chmod")
def test_register_after_failed_read_keeps_other_instances_ids(registry: Path) -> None:
    registry.write_text("[777]")  # another instance's id
    registry.chmod(0)
    try:
        v.register(888)
    finally:
        if registry.exists():
            registry.chmod(0o600)
    on_disk = json.loads(registry.read_text())
    assert 777 in on_disk, on_disk


@pytest.mark.skipif(os.geteuid() == 0, reason="root ignores chmod")
def test_an_id_not_written_after_a_failed_read_is_written_by_the_next_register(
    registry: Path,
) -> None:
    """Kept in memory, then sent to the file the next time the file can be read."""
    registry.write_text("[777]")
    registry.chmod(0)
    try:
        v.register(888)
        assert v.is_voice_webhook(888)  # still ours in this process
    finally:
        registry.chmod(0o600)
    v.register(999)
    assert set(json.loads(registry.read_text())) == {777, 888, 999}


def test_deleted_file_then_recreated_is_read(registry: Path) -> None:
    registry.write_text("[777]")
    assert v.is_voice_webhook(777)
    registry.unlink()
    assert v.is_voice_webhook(777)  # remembered in memory
    time.sleep(0.05)
    registry.write_text("[999]")
    assert v.is_voice_webhook(999)


# --- #1176: the id kept in memory must actually reach the file ---------------
#
# #1161 stopped writing while the file was unreadable (#1109) and promised the
# id would go to the file "with the next register that can read it". But the
# voice webhook is cached per parent channel per process, so there is usually
# no next register; and a register of an id already on disk returned early.
# Until the id is on disk, a restart or another instance obeys this process's
# sub-agent voices as orders (decision 12).


def _other_process_sees(wid: int) -> bool:
    """A fresh process (or another instance) reading the file."""
    saved = (v._ids, v._seen_stamp, v._failed_stamp)
    v._ids, v._seen_stamp, v._failed_stamp = None, None, None
    try:
        return v.is_voice_webhook(wid)
    finally:
        v._ids, v._seen_stamp, v._failed_stamp = saved


@pytest.mark.skipif(os.geteuid() == 0, reason="root ignores chmod")
def test_permission_restored_our_id_reaches_file_without_another_register(
    registry: Path,
) -> None:
    registry.write_text("[777]")
    registry.chmod(0)
    try:
        v.register(888)  # our voice webhook (subagent_poster._acquire, once)
    finally:
        registry.chmod(0o644)  # permission restored
    for _ in range(5):  # our process keeps working: webhook messages arrive
        v.is_voice_webhook(888)
        v.is_voice_webhook(4242)
    assert 888 in json.loads(registry.read_text())
    assert _other_process_sees(888)


def test_deleted_then_recreated_by_other_instance_our_id_reaches_file(registry: Path) -> None:
    registry.write_text("[777")  # broken
    v.register(888)  # not written (#1109)
    registry.unlink()
    registry.write_text("[777]")  # another instance recreates it
    for _ in range(5):
        v.is_voice_webhook(4242)
    assert 888 in json.loads(registry.read_text())


def test_register_of_an_id_already_on_disk_flushes_pending_ids(registry: Path) -> None:
    """888 registered while unreadable; another instance later wrote 999 (a shared
    channel webhook); this one registers 999 too -> 888 must not stay off disk."""
    registry.write_text("[777")
    v.register(888)
    registry.write_text("[777, 999]")
    v.register(999)
    assert 888 in json.loads(registry.read_text())


# --- #1176, independent review: the stamp a failure leaves behind ---------------


def _fresh(tmp_path, monkeypatch):  # noqa: ANN001, ANN202
    from c_lord import voice_webhooks as v

    monkeypatch.setattr(v, "registry_path", lambda: tmp_path / "voice-webhooks.json")
    monkeypatch.setattr(v, "_RETRY_SECONDS", 0.0)
    v._reset_for_tests()
    return v, tmp_path / "voice-webhooks.json"


@pytest.mark.skipif(os.geteuid() == 0, reason="root ignores file permissions")
def test_id_reaches_the_file_after_a_permission_flip_on_a_file_read_before(
    tmp_path, monkeypatch
) -> None:  # noqa: ANN001
    """chmod 0 → register → chmod 644 changes neither mtime nor size."""
    v, registry = _fresh(tmp_path, monkeypatch)
    registry.write_text("[777]")
    assert v.is_voice_webhook(777)
    registry.chmod(0)
    v.register(888)
    registry.chmod(0o644)
    for _ in range(3):
        v.is_voice_webhook(4242)
    assert 888 in json.loads(registry.read_text())


@pytest.mark.skipif(os.geteuid() == 0, reason="root ignores file permissions")
def test_a_failed_write_is_tried_again(tmp_path, monkeypatch) -> None:  # noqa: ANN001
    """The directory was read-only during register; it is writable again later."""
    v, registry = _fresh(tmp_path, monkeypatch)
    registry.write_text("[777]")
    assert v.is_voice_webhook(777)
    tmp_path.chmod(0o555)
    try:
        v.register(888)
    finally:
        tmp_path.chmod(0o755)
    v.is_voice_webhook(4242)
    assert 888 in json.loads(registry.read_text())


@pytest.mark.skipif(os.geteuid() == 0, reason="root ignores file permissions")
def test_our_own_voice_is_enough_to_write_it(tmp_path, monkeypatch) -> None:  # noqa: ANN001
    """A quiet instance sees no unknown webhook; its own voices still get it written."""
    v, registry = _fresh(tmp_path, monkeypatch)
    registry.write_text("[777]")
    assert v.is_voice_webhook(777)
    registry.chmod(0)
    v.register(888)
    registry.chmod(0o644)
    assert v.is_voice_webhook(888)  # our own voice, known in memory
    assert 888 in json.loads(registry.read_text())
