"""#258: two c-lord instances on one host both get a REST API, zero-config.

Since #712 every bot binds the REST API, and since #715 a second bot that finds
8080 taken no longer crashes — it runs with no API at all, and says so with a
WARNING.  That is still a second instance with no ``/api/spawn``.  Unless the
operator pins ``CLORD_API_PORT``, the API now walks to the next free port.

The other half: Claude in a session has to reach *this* bot's API.  The URL it
used to read from a baked-in SKILL.md went away with that skill (#712), so it
now travels as ``CLORD_API_URL`` on the ``claude`` command line — and it names
the port that was actually bound, not the one that was asked for.
"""

from __future__ import annotations

import logging
import socket
from collections.abc import Iterator
from pathlib import Path
from unittest.mock import MagicMock, patch

import pytest

from c_lord import api_endpoint
from c_lord.main import build_api_server, start_api_server
from c_lord.tmux import TmuxSessionManager


def _free_port() -> int:
    with socket.socket() as probe:
        probe.bind(("127.0.0.1", 0))
        return probe.getsockname()[1]


@pytest.fixture
def bot() -> MagicMock:
    return MagicMock()


@pytest.fixture(autouse=True)
def _clean(monkeypatch: pytest.MonkeyPatch) -> Iterator[None]:
    monkeypatch.delenv("CLORD_API_PORT", raising=False)
    monkeypatch.delenv("CLORD_API_URL", raising=False)
    api_endpoint.clear()
    yield
    api_endpoint.clear()


class TestTwoInstancesZeroConfig:
    @pytest.mark.asyncio
    async def test_second_instance_gets_its_own_port(self, bot: MagicMock, tmp_path: Path) -> None:
        """AC1 — RED before: the second bind failed and that bot ran with no API."""
        base = _free_port()
        (tmp_path / "a").mkdir()
        (tmp_path / "b").mkdir()
        with patch("c_lord.main.DEFAULT_API_PORT", base):
            first = await build_api_server(bot, default_channel_id=1, data_dir=tmp_path / "a")
            second = await build_api_server(bot, default_channel_id=1, data_dir=tmp_path / "b")
        assert first is not None and second is not None
        try:
            assert await start_api_server(first) is True
            assert await start_api_server(second) is True
            assert first.port == base
            assert second.port != base
        finally:
            await first.stop()
            await second.stop()

    @pytest.mark.asyncio
    async def test_falls_back_to_an_os_port_when_the_range_is_full(
        self, bot: MagicMock, tmp_path: Path
    ) -> None:
        with socket.socket() as taken:
            taken.bind(("127.0.0.1", 0))
            taken.listen(1)
            port = taken.getsockname()[1]
            with (
                patch("c_lord.main.DEFAULT_API_PORT", port),
                patch("c_lord.ext.api_server.AUTO_PORT_TRIES", 1),
            ):
                api = await build_api_server(bot, default_channel_id=1, data_dir=tmp_path)
                assert api is not None
                try:
                    assert await start_api_server(api) is True
                    assert api.port not in (0, port)
                finally:
                    await api.stop()


class TestExplicitPortIsHonoured:
    @pytest.mark.asyncio
    async def test_explicit_port_in_use_is_not_walked_past(
        self,
        bot: MagicMock,
        tmp_path: Path,
        monkeypatch: pytest.MonkeyPatch,
        caplog: pytest.LogCaptureFixture,
    ) -> None:
        """AC3 — an operator who pinned a port gets that port or a clear WARNING."""
        with socket.socket() as taken:
            taken.bind(("127.0.0.1", 0))
            taken.listen(1)
            port = taken.getsockname()[1]
            monkeypatch.setenv("CLORD_API_PORT", str(port))
            api = await build_api_server(bot, default_channel_id=1, data_dir=tmp_path)
            assert api is not None
            with caplog.at_level(logging.WARNING, logger="c_lord.main"):
                assert await start_api_server(api) is False
        assert api.port == port
        assert api_endpoint.current() is None
        assert any("CLORD_API_PORT" in r.getMessage() for r in caplog.records)

    @pytest.mark.asyncio
    async def test_explicit_free_port_is_used(
        self, bot: MagicMock, tmp_path: Path, monkeypatch: pytest.MonkeyPatch
    ) -> None:
        port = _free_port()
        monkeypatch.setenv("CLORD_API_PORT", str(port))
        api = await build_api_server(bot, default_channel_id=1, data_dir=tmp_path)
        assert api is not None
        try:
            assert await start_api_server(api) is True
            assert api.port == port
        finally:
            await api.stop()


class TestAdvertisedUrl:
    @pytest.mark.asyncio
    async def test_url_follows_the_port_actually_bound(
        self, bot: MagicMock, tmp_path: Path
    ) -> None:
        """AC2 — the walked-to port, not the one that was asked for."""
        with socket.socket() as taken:
            taken.bind(("127.0.0.1", 0))
            taken.listen(1)
            base = taken.getsockname()[1]
            with patch("c_lord.main.DEFAULT_API_PORT", base):
                api = await build_api_server(bot, default_channel_id=1, data_dir=tmp_path)
                assert api is not None
                try:
                    assert await start_api_server(api) is True
                    assert api.port != base
                    assert api_endpoint.current() == f"http://127.0.0.1:{api.port}"
                finally:
                    await api.stop()
        assert api_endpoint.current() is None

    def test_explicit_url_wins(self, monkeypatch: pytest.MonkeyPatch) -> None:
        """A reverse proxy in front of the API is the operator's to name."""
        api_endpoint.advertise("0.0.0.0", 8123)
        monkeypatch.setenv("CLORD_API_URL", "https://api.example.test")
        assert api_endpoint.current() == "https://api.example.test"

    def test_wildcard_host_is_advertised_as_loopback(self) -> None:
        api_endpoint.advertise("0.0.0.0", 8123)
        assert api_endpoint.current() == "http://127.0.0.1:8123"


def _typed_command(mock_run: MagicMock) -> str:
    parts = [
        c[0][0][-1] for c in mock_run.call_args_list if "send-keys" in c[0][0] and "-l" in c[0][0]
    ]
    return "".join(parts)


class TestClaudeIsToldTheUrl:
    def _start(self) -> str:
        mgr = TmuxSessionManager(mapping_path="")
        mgr._available = True
        mgr._thread_to_window[12345] = "work1"
        with patch("c_lord.tmux._run") as mock_run:
            mock_run.return_value = MagicMock(returncode=0, stdout="12345\n")
            assert mgr.start_claude(12345, "hello", "sonnet") is True
        return _typed_command(mock_run)

    def test_bound_url_is_in_the_env_prefix(self) -> None:
        """AC2 — RED before: no CLORD_API_URL reached the session at all."""
        api_endpoint.advertise("127.0.0.1", 8093)
        cmd = self._start()
        # The env(1) prefix: after the prelude's last ";", before the binary.
        env_prefix = cmd.rsplit(";", 1)[-1].split(" claude --", 1)[0]
        assert env_prefix.lstrip().startswith("env -u CLAUDECODE"), cmd
        assert "CLORD_API_URL=http://127.0.0.1:8093" in env_prefix, cmd

    def test_no_url_when_the_api_is_down(self) -> None:
        cmd = self._start()
        assert "CLORD_API_URL" not in cmd
