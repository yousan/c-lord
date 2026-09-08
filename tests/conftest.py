"""Shared pytest fixtures for c_lord tests.

These fixtures are automatically available to all test files in this directory.
Class-level fixtures with the same name take precedence (pytest scoping rules).
"""

from __future__ import annotations

import os
import shutil
import subprocess
import tempfile
from pathlib import Path
from unittest.mock import AsyncMock, MagicMock

import discord
import pytest

from c_lord.claude.types import MessageType, StreamEvent


@pytest.fixture
def thread() -> MagicMock:
    """A MagicMock discord.Thread with send and id set."""
    t = MagicMock(spec=discord.Thread)
    t.id = 12345
    msg = MagicMock(spec=discord.Message)
    msg.edit = AsyncMock()
    t.send = AsyncMock(return_value=msg)
    return t


@pytest.fixture
def runner() -> MagicMock:
    """A MagicMock runner (TmuxClaudeRunner) with interrupt() wired up."""
    r = MagicMock()
    r.interrupt = AsyncMock()
    return r


@pytest.fixture
def repo() -> MagicMock:
    """A MagicMock SessionRepository with async save/get."""
    r = MagicMock()
    r.save = AsyncMock()
    r.get = AsyncMock(return_value=None)
    return r


def make_async_gen(events: list[StreamEvent]):
    """Return an async generator factory that yields the given events.

    Usage::

        runner.run = make_async_gen([event1, event2])
        async for e in runner.run("prompt"):
            ...
    """

    async def gen(*args, **kwargs):
        for e in events:
            yield e

    return gen


def simple_events(session_id: str = "sess-1") -> list[StreamEvent]:
    """Return a minimal sequence: SYSTEM + RESULT (no tool use)."""
    return [
        StreamEvent(message_type=MessageType.SYSTEM, session_id=session_id),
        StreamEvent(
            message_type=MessageType.RESULT,
            is_complete=True,
            text="Done.",
            session_id=session_id,
            cost_usd=0.01,
            duration_ms=500,
        ),
    ]


@pytest.fixture(autouse=True)
def _isolated_prompt_dir(tmp_path_factory, monkeypatch):
    """Keep staged prompt files (#529) out of the shared temp directory.

    ``start_claude`` writes the prompt to a file that the *pane* is meant to
    delete once it has read it. Under test there is no pane, so without this
    every call would leave one behind in the system temp dir.
    """
    directory = tmp_path_factory.mktemp("clord-prompts")
    monkeypatch.setattr("c_lord.tmux._prompt_file_dir", lambda: directory)


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
    directory = tempfile.mkdtemp(prefix="clord-tests-tmux-")
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
