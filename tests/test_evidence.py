"""Tests for the auto-evidence orchestrator (#243).

On Claude turn completion c-lord renders the current thread to a PNG, saves it
under ``<working_dir>/.evidence/`` for PR evidence, and (by default) posts it
back to the thread. All behavior is env-gated and debounced. Rendering itself
is monkeypatched here — the pixel output is covered by test_conversation_renderer.
"""

from __future__ import annotations

from unittest.mock import AsyncMock, MagicMock

import pytest

from c_lord.discord_ui import evidence
from c_lord.discord_ui.conversation_renderer import ConvMessage


@pytest.fixture(autouse=True)
def _reset_debounce():
    evidence._last_capture.clear()
    yield
    evidence._last_capture.clear()


def _thread() -> MagicMock:
    t = MagicMock()
    t.id = 999
    t.name = "dev-thread"
    t.send = AsyncMock()

    async def _hist(*a, **k):
        for m in ():
            yield m

    t.history = MagicMock(return_value=_hist())
    return t


class TestEnvGates:
    def test_enabled_by_default(self, monkeypatch) -> None:
        monkeypatch.delenv("CLORD_AUTO_SCREENSHOT", raising=False)
        assert evidence.auto_screenshot_enabled() is True

    def test_disabled_when_zero(self, monkeypatch) -> None:
        monkeypatch.setenv("CLORD_AUTO_SCREENSHOT", "0")
        assert evidence.auto_screenshot_enabled() is False

    def test_engine_default_is_pillow(self, monkeypatch) -> None:
        monkeypatch.delenv("CLORD_AUTO_SCREENSHOT_ENGINE", raising=False)
        assert evidence._engine() == "pillow"


class TestDebounce:
    def test_first_call_captures(self) -> None:
        assert evidence.should_capture(1, now=100.0) is True

    def test_rapid_second_call_skipped(self) -> None:
        assert evidence.should_capture(1, now=100.0) is True
        assert evidence.should_capture(1, now=101.0) is False

    def test_after_interval_captures_again(self) -> None:
        assert evidence.should_capture(1, now=100.0) is True
        assert evidence.should_capture(1, now=100.0 + evidence._DEFAULT_DEBOUNCE_S + 1) is True

    def test_different_threads_independent(self) -> None:
        assert evidence.should_capture(1, now=100.0) is True
        assert evidence.should_capture(2, now=100.0) is True

    def test_debounce_map_is_bounded(self) -> None:
        # A bot that touches many threads must not grow _last_capture forever.
        for tid in range(evidence._MAX_TRACKED + 50):
            evidence.should_capture(tid, now=float(tid))
        assert len(evidence._last_capture) <= evidence._MAX_TRACKED
        # The most recent thread is retained.
        assert (evidence._MAX_TRACKED + 49) in evidence._last_capture


class TestCaptureEvidence:
    async def test_renders_saves_and_posts(self, monkeypatch, tmp_path) -> None:
        monkeypatch.delenv("CLORD_AUTO_SCREENSHOT_POST", raising=False)
        monkeypatch.setattr(
            evidence, "fetch_conversation", AsyncMock(return_value=[ConvMessage(author="a")])
        )
        monkeypatch.setattr(
            evidence, "render_conversation", lambda msgs, **kw: b"\x89PNG\r\n\x1a\nX"
        )

        thread = _thread()
        saved = await evidence.capture_evidence(thread, str(tmp_path))

        assert saved is not None
        assert saved.endswith(".png")
        from pathlib import Path

        assert Path(saved).read_bytes().startswith(b"\x89PNG")
        thread.send.assert_awaited_once()
        assert thread.send.await_args.kwargs.get("file") is not None

    async def test_post_disabled_saves_but_does_not_send(self, monkeypatch, tmp_path) -> None:
        monkeypatch.setenv("CLORD_AUTO_SCREENSHOT_POST", "0")
        monkeypatch.setattr(
            evidence, "fetch_conversation", AsyncMock(return_value=[ConvMessage(author="a")])
        )
        monkeypatch.setattr(
            evidence, "render_conversation", lambda msgs, **kw: b"\x89PNG\r\n\x1a\nX"
        )

        thread = _thread()
        saved = await evidence.capture_evidence(thread, str(tmp_path))

        assert saved is not None
        thread.send.assert_not_awaited()

    async def test_renderer_unavailable_returns_none(self, monkeypatch, tmp_path) -> None:
        monkeypatch.setattr(
            evidence, "fetch_conversation", AsyncMock(return_value=[ConvMessage(author="a")])
        )
        monkeypatch.setattr(evidence, "render_conversation", lambda msgs, **kw: None)

        thread = _thread()
        saved = await evidence.capture_evidence(thread, str(tmp_path))
        assert saved is None
        thread.send.assert_not_awaited()

    async def test_empty_thread_skips(self, monkeypatch, tmp_path) -> None:
        monkeypatch.setattr(evidence, "fetch_conversation", AsyncMock(return_value=[]))
        render_spy = MagicMock(return_value=b"x")
        monkeypatch.setattr(evidence, "render_conversation", render_spy)

        thread = _thread()
        saved = await evidence.capture_evidence(thread, str(tmp_path))
        assert saved is None
        render_spy.assert_not_called()

    async def test_no_working_dir_still_posts(self, monkeypatch) -> None:
        monkeypatch.delenv("CLORD_AUTO_SCREENSHOT_POST", raising=False)
        monkeypatch.setattr(
            evidence, "fetch_conversation", AsyncMock(return_value=[ConvMessage(author="a")])
        )
        monkeypatch.setattr(
            evidence, "render_conversation", lambda msgs, **kw: b"\x89PNG\r\n\x1a\nX"
        )

        thread = _thread()
        saved = await evidence.capture_evidence(thread, None)
        assert saved is None  # nothing saved without a working_dir
        thread.send.assert_awaited_once()  # but still posted

    async def test_render_exception_is_swallowed(self, monkeypatch, tmp_path) -> None:
        monkeypatch.setattr(
            evidence, "fetch_conversation", AsyncMock(return_value=[ConvMessage(author="a")])
        )

        def _boom(msgs, **kw):
            raise RuntimeError("render blew up")

        monkeypatch.setattr(evidence, "render_conversation", _boom)
        thread = _thread()
        # Must never propagate — a screenshot failure can't break the Claude run.
        assert await evidence.capture_evidence(thread, str(tmp_path)) is None


class TestGitExclude:
    def test_adds_evidence_to_git_exclude(self, tmp_path) -> None:

        info = tmp_path / ".git" / "info"
        info.mkdir(parents=True)
        evidence._save(b"\x89PNG\r\n\x1a\nX", str(tmp_path))
        exclude = (info / "exclude").read_text()
        assert ".evidence/" in exclude

    def test_does_not_duplicate_exclude_entry(self, tmp_path) -> None:
        info = tmp_path / ".git" / "info"
        info.mkdir(parents=True)
        (info / "exclude").write_text(".evidence/\n")
        evidence._save(b"\x89PNG\r\n\x1a\nX", str(tmp_path))
        assert (info / "exclude").read_text().count(".evidence/") == 1

    def test_non_git_dir_does_not_crash(self, tmp_path) -> None:
        # No .git — saving must still succeed without raising.
        saved = evidence._save(b"\x89PNG\r\n\x1a\nX", str(tmp_path))
        assert saved is not None
