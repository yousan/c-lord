"""#1120 — tmux のセッション名は完全一致で指す。

``tmux -t <名前>`` は**前方一致**なので、``foo`` のセッションがまだ無くて
``foobar`` があると、``foo`` のスレッドの窓が ``foobar`` の中に作られ、
/tmux-list にも2回出ていた。``-t =<名前>`` なら完全一致。

分離した tmux サーバ（``-L``）だけを使う。既定ソケットには触らない (#701)。
"""

from __future__ import annotations

import re
import subprocess
from pathlib import Path
from unittest.mock import AsyncMock, MagicMock

import pytest

import c_lord.tmux as tmux_mod
from c_lord.tmux import TmuxSessionManager

SOCK = "clord-test-1120"
_orig_run = tmux_mod._run


def _run_isolated(args: list[str]) -> subprocess.CompletedProcess[str]:
    if args and args[0] == "tmux":
        args = ["tmux", "-L", SOCK, *args[1:]]
    return _orig_run(args)


def _t(*a: str) -> subprocess.CompletedProcess[str]:
    return subprocess.run(
        ["tmux", "-L", SOCK, "-f", "/dev/null", *a], capture_output=True, text=True
    )


@pytest.fixture(autouse=True)
def _iso(monkeypatch: pytest.MonkeyPatch, tmp_path: Path):
    if subprocess.run(["which", "tmux"], capture_output=True).returncode != 0:
        pytest.skip("tmux not installed")
    monkeypatch.setattr(tmux_mod, "_run", _run_isolated)
    monkeypatch.delenv("TMUX", raising=False)
    monkeypatch.setenv("XDG_CACHE_HOME", str(tmp_path / "cache"))
    subprocess.run(["tmux", "-L", SOCK, "kill-server"], capture_output=True)
    yield
    subprocess.run(["tmux", "-L", SOCK, "kill-server"], capture_output=True)


def _windows() -> list[str]:
    return _t(
        "list-windows", "-a", "-F", "#{session_name}:#{window_name}:#{@thread_id}"
    ).stdout.split()


def test_window_for_foo_lands_in_its_own_session(tmp_path: Path) -> None:
    _t("new-session", "-d", "-s", "foobar", "-n", "w1")
    mgr = TmuxSessionManager(session_name="foo")
    mgr.create_session(111, str(tmp_path))
    sessions = _t("list-sessions", "-F", "#{session_name}").stdout.split()
    where = _windows()
    assert "foo" in sessions, f"no 'foo' session was created; windows: {where}"
    assert any(w.startswith("foo:") and w.endswith(":111") for w in where), where
    assert not any(w.startswith("foobar:") and w.endswith(":111") for w in where), where


def test_a_missing_foo_does_not_list_foobars_windows(tmp_path: Path) -> None:
    _t("new-session", "-d", "-s", "foobar", "-n", "w3")
    assert TmuxSessionManager(session_name="foo").list_sessions() == []


async def test_tmux_list_does_not_show_a_window_twice_1120() -> None:
    """``x/foo`` と ``x/foobar`` を bind、tmux には ``foobar`` だけ → 窓は1回。"""
    from c_lord.cogs.session_manage import SessionManageCog

    _t("new-session", "-d", "-s", "foobar", "-n", "w3")
    bot = MagicMock()
    bot.get_cog = MagicMock(return_value=None)
    authorizer = MagicMock()
    authorizer.is_allowed = MagicMock(return_value=True)
    cog = SessionManageCog(bot=bot, repo=MagicMock(), authorizer=authorizer)
    cog._get_all_bindings = AsyncMock(return_value=[{"channel_id": 1}, {"channel_id": 2}])
    managers = {1: TmuxSessionManager(session_name="foo"), 2: TmuxSessionManager("foobar")}
    cog._resolve_tmux_manager = AsyncMock(side_effect=lambda cid, thread_id: managers[cid])

    sent: list[dict] = []

    async def respond(content=None, *, embed=None, ephemeral=False, **kw):  # noqa: ANN001, ANN202
        sent.append({"content": content, "embed": embed})

    async def ack(*, ephemeral=False):  # noqa: ANN001, ANN202
        return None

    await cog._tmux_list_impl(respond=respond, ack=ack)
    embed = sent[-1]["embed"]
    assert [embed.title] + [f.name for f in embed.fields] == ["🖥️ Tmux Windows (1)", "`w3`"]


def test_every_session_target_in_tmux_py_is_exact() -> None:
    """``c_lord/tmux.py`` でセッション名を ``-t`` に渡す箇所はすべて ``=`` 付き。"""
    src = Path(tmux_mod.__file__).read_text(encoding="utf-8")
    loose = re.findall(r'"-t",\s*self\.session_name\b', src)
    loose += re.findall(r'f"\{self\.session_name\}:', src)
    assert loose == [], loose
