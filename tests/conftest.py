"""Shared pytest fixtures for c_lord tests.

What lives here applies to every test file without an import:

* ``thread`` / ``runner`` — the two doubles a ``RunConfig`` needs.
* the autouse fixtures below — each one keeps a piece of process-global or
  host-global state (home directory, tmux socket, authorization, …) from
  leaking between tests or out of the test run.
"""

from __future__ import annotations

import os
import shutil
import subprocess
import sys
import tempfile
from collections.abc import AsyncIterator, Callable
from pathlib import Path
from unittest.mock import AsyncMock, MagicMock

import discord
import pytest

from c_lord.claude.types import MessageType, StreamEvent

#: The id every ``thread`` double answers to, so a test can name it without the fixture.
THREAD_ID = 12345
#: The session id the default scripted turn of ``runner`` reports.
SESSION_ID = "0a1b2c3d-0000-4000-8000-000000000001"


@pytest.fixture(autouse=True)
def _isolated_claude_home(tmp_path_factory, monkeypatch) -> None:
    """Keep ``~/.claude`` out of the test run (#773).

    ``start_claude`` now records which session it started in
    ``~/.claude/projects/<slug>/.clord-session``, derived from the pane's cwd.
    Unit tests hand it fake pane paths, so without this every run would leave
    claims in the developer's real home **and read each other's** — three tmux
    tests started failing on a second run because an earlier test's claim turned
    their ``--continue`` into a ``--resume``.

    Redirects ``Path.home()`` rather than ``$HOME``: the environment is
    inherited by the real ``tmux`` server and shells that a few tests drive, and
    a home that does not exist breaks them (``test_real_tmux_...`` typed into a
    pane whose shell never came up).  A test that sets ``$HOME`` itself still
    wins — that is a deliberate choice about where home is, and this fixture
    only supplies the default.
    """
    real_home = os.environ.get("HOME")
    tmp_home = tmp_path_factory.mktemp("home")

    def _home(cls: type[Path]) -> Path:
        current = os.environ.get("HOME")
        return tmp_home if current == real_home else Path(str(current))

    monkeypatch.setattr(Path, "home", classmethod(_home))


def scripted_turn(*events: StreamEvent) -> Callable[..., AsyncIterator[StreamEvent]]:
    """Build a stand-in for ``runner.run`` that yields *events* and then ends.

    The real runner is an async generator, so a plain ``AsyncMock`` cannot play
    it; this keeps each test's "what the pane reported" a flat list of events.
    """

    async def _run(*_args: object, **_kwargs: object) -> AsyncIterator[StreamEvent]:
        for event in events:
            yield event

    return _run


def shortest_turn(session_id: str = SESSION_ID) -> list[StreamEvent]:
    """The events of the shortest clean turn: the session is named, then it completes."""
    return [
        StreamEvent(message_type=MessageType.SYSTEM, session_id=session_id),
        StreamEvent(message_type=MessageType.RESULT, is_complete=True, session_id=session_id),
    ]


@pytest.fixture
def thread() -> MagicMock:
    """A ``discord.Thread`` double: every send succeeds and returns a message."""
    double = MagicMock(spec=discord.Thread)
    double.id = THREAD_ID
    double.send = AsyncMock(return_value=MagicMock(spec=discord.Message))
    return double


@pytest.fixture
def repo() -> MagicMock:
    """A SessionRepository double: writes succeed, and nothing is on record yet."""
    double = MagicMock()
    double.save = AsyncMock()
    double.get = AsyncMock(return_value=None)
    return double


@pytest.fixture
def runner() -> MagicMock:
    """A MagicMock runner (TmuxClaudeRunner) with interrupt() wired up."""
    double = MagicMock()
    double.interrupt = AsyncMock()
    # Left unscripted, a turn is the shortest clean one: the session is named,
    # then the turn completes.
    double.run = scripted_turn(*shortest_turn())
    return double


@pytest.fixture(autouse=True)
def _isolated_prompt_dir(tmp_path_factory, monkeypatch):
    """Keep staged prompt files (#529) out of the shared temp directory.

    ``start_claude`` writes the prompt to a file that the *pane* is meant to
    delete once it has read it. Under test there is no pane, so without this
    every call would leave one behind in the system temp dir.
    """
    directory = tmp_path_factory.mktemp("clord-prompts")
    monkeypatch.setattr("c_lord.tmux._prompt_file_dir", lambda: directory)


@pytest.fixture(autouse=True)
def _fresh_typed_input_state():
    """Forget what c-lord typed / interrupted in earlier tests (#879).

    ``c_lord.tmux`` remembers per thread id what it last typed, so the next
    send can clear that prompt if an interrupt put it back in the box. Tests
    reuse thread ids, and that memory would leak one test's payload into the
    next one's scripted pane.
    """
    from c_lord import tmux

    tmux._last_typed.clear()
    tmux._interrupted_at.clear()
    yield
    tmux._last_typed.clear()
    tmux._interrupted_at.clear()


@pytest.fixture(scope="session", autouse=True)
def _isolated_tmux_socket():
    """Keep the whole test run off the fleet's tmux socket (#701).

    ``pytest`` is routinely run on the bot host while the fleet is live, and a
    handful of tests drive real tmux. Every tmux client — ours in
    ``c_lord.tmux._run`` included, since it inherits this process's environment
    — resolves its socket under ``$TMUX_TMPDIR``, so pointing that at a private
    directory puts a wall between the suite and ``/tmp/tmux-<uid>/default``,
    where the production windows live.

    This is the mechanical half of #701: the written rule ("isolate real tmux
    with ``-L``") only helps whoever reads it, and the incident it comes from
    was caused by someone who intended to isolate and simply used the wrong
    flag. A test that forgets is isolated anyway.

    ``TMUX_TMPDIR`` alone is NOT enough, and finding that out is half the value
    of this fixture: a client started inside a pane reads ``$TMUX`` (socket
    path, server pid, session) and talks to *that* server, ignoring
    ``TMUX_TMPDIR`` entirely. The suite is very often run from inside a c-lord
    pane, where ``$TMUX`` names the fleet's own socket — measured here, a bare
    ``tmux new-session`` under ``TMUX_TMPDIR`` alone still landed on the
    production server. So ``TMUX``/``TMUX_PANE`` are dropped too. (Same shape as
    the mistake in #701 itself: a flag that looks like isolation but is not.)

    Teardown kills only servers whose socket file sits inside that private
    directory, addressed by path (``-S``) so the command cannot resolve
    anywhere else.
    """
    # macOS's $TMPDIR (/var/folders/…/T/) is so long that the socket path
    # passes the 104-byte sun_path limit ("File name too long", #899).
    directory = tempfile.mkdtemp(
        prefix="clord-tests-tmux-", dir="/tmp" if sys.platform == "darwin" else None
    )
    previous = {name: os.environ.get(name) for name in ("TMUX_TMPDIR", "TMUX", "TMUX_PANE")}
    os.environ["TMUX_TMPDIR"] = directory
    os.environ.pop("TMUX", None)
    os.environ.pop("TMUX_PANE", None)
    try:
        yield directory
    finally:
        for name, value in previous.items():
            if value is None:
                os.environ.pop(name, None)
            else:
                os.environ[name] = value
        if shutil.which("tmux"):
            for socket_path in Path(directory).rglob("*"):
                if socket_path.is_socket():
                    subprocess.run(
                        ["tmux", "-S", str(socket_path), "kill-server"],
                        capture_output=True,
                    )
        shutil.rmtree(directory, ignore_errors=True)


@pytest.fixture(autouse=True)
def _reset_fallback_owner_ids():
    """Keep the process-global authorization state from leaking between tests.

    The resolved application owner (#713) and the published authorizer (#739)
    are process-global on purpose — one bot per process — so a test that sets
    either would otherwise decide who is authorized in every test after it.
    """
    from c_lord.discord_ui import authorization

    authorization.set_fallback_owner_ids(None)
    authorization.set_default_authorizer(None)
    try:
        yield
    finally:
        authorization.set_fallback_owner_ids(None)
        authorization.set_default_authorizer(None)


@pytest.fixture(autouse=True)
def _fresh_login_alert(tmp_path):
    """Give every test its own login outage (#1309).

    ``login_alert`` is process-global (one bot per process) and remembers an
    open outage; without this one test's refused turn would swallow the next
    test's DM.
    """
    from c_lord.login_alert import login_alert

    saved = (login_alert._state_path, login_alert._bot, login_alert._on_open, login_alert._on_close)
    login_alert._state_path = tmp_path / "login-alert.json"
    login_alert._state = None
    login_alert._dm = None
    login_alert._bot = None
    login_alert._on_open = []
    login_alert._on_close = []
    yield
    (
        login_alert._state_path,
        login_alert._bot,
        login_alert._on_open,
        login_alert._on_close,
    ) = saved
    login_alert._state = None
    login_alert._dm = None
