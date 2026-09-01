"""Secrets must not reach a tmux pane through the environment (#353).

A tmux *server* inherits the environment of whichever process first runs a tmux
command, and every window it later creates inherits that. When the bot wins that
race, every pane can read the production bot token with a plain ``printenv`` —
measured 6/6 panes in #362 (2026-06-11), and it was the supply of the #322
contamination incidents, where a staging bot started inside a pane silently
picked up the production token and became a second production bot.

Three independent layers, because each covers a case the others cannot:

1. ``_run`` drops the secrets from every tmux client invocation, so a server
   *c-lord* starts is born clean. Closes the source.
2. ``_ensure_session`` marks the secrets for removal in the session
   (``set-environment -r``), which repairs a server that was already started
   dirty — by an older c-lord, or by any other process holding the token.
3. ``start_claude`` unsets them on the ``claude`` command line, so the Claude
   process is clean even in a session nobody marked.

Note what this does *not* claim: the token stays readable at
``/home/yousan/c-lord/.env``, and the injected ``discord-read`` skill (#259)
tells Claude to read it from there. This is not confidentiality — it is
preventing *accidental inheritance*, which is what actually caused #322.
See #458 for the documentation drift this corrects.
"""

from __future__ import annotations

import os
from unittest.mock import MagicMock, patch

from c_lord.tmux import SENSITIVE_ENV_KEYS, TmuxSessionManager, _run


class TestRunDropsSecrets:
    """Layer 1: a server c-lord starts can never inherit the secrets."""

    def test_run_passes_an_env_without_the_secrets(self) -> None:
        with (
            patch.dict(
                os.environ,
                {"DISCORD_BOT_TOKEN": "fake-token", "CLORD_API_SECRET": "fake-secret"},
                clear=False,
            ),
            patch("c_lord.tmux.subprocess.run") as sp,
        ):
            sp.return_value = MagicMock(returncode=0, stdout="", stderr="")
            _run(["tmux", "new-session", "-d", "-s", "clord"])

        env = sp.call_args.kwargs["env"]
        for key in SENSITIVE_ENV_KEYS:
            assert key not in env, f"{key} would be inherited by a server c-lord starts"

    def test_run_keeps_everything_else(self) -> None:
        """Only the secrets go. TMUX_TMPDIR in particular decides which server we talk to."""
        with (
            patch.dict(
                os.environ,
                {"DISCORD_BOT_TOKEN": "fake-token", "TMUX_TMPDIR": "/tmp/xyz", "PATH": "/usr/bin"},
                clear=False,
            ),
            patch("c_lord.tmux.subprocess.run") as sp,
        ):
            sp.return_value = MagicMock(returncode=0, stdout="", stderr="")
            _run(["tmux", "list-sessions"])

        env = sp.call_args.kwargs["env"]
        assert env["TMUX_TMPDIR"] == "/tmp/xyz"
        assert env["PATH"] == "/usr/bin"

    def test_run_is_unharmed_when_the_secrets_are_not_set(self) -> None:
        with patch.dict(os.environ, {}, clear=False), patch("c_lord.tmux.subprocess.run") as sp:
            for key in SENSITIVE_ENV_KEYS:
                os.environ.pop(key, None)
            sp.return_value = MagicMock(returncode=0, stdout="", stderr="")
            result = _run(["tmux", "list-sessions"])
        assert result.returncode == 0


class TestSessionEnvRemovalMark:
    """Layer 2: repair a server that is already dirty."""

    @staticmethod
    def _mgr() -> TmuxSessionManager:
        mgr = TmuxSessionManager(session_name="clord", mapping_path="")
        mgr._available = True
        return mgr

    def test_ensure_session_marks_the_secrets_for_removal(self) -> None:
        mgr = self._mgr()
        with patch("c_lord.tmux._run") as run:
            run.return_value = MagicMock(returncode=0, stdout="", stderr="")
            assert mgr._ensure_session() is True

        marked = {
            c[0][0][-1]
            for c in run.call_args_list
            if "set-environment" in c[0][0] and "-r" in c[0][0]
        }
        assert marked == set(SENSITIVE_ENV_KEYS), f"marked={marked}"

    def test_mark_targets_this_session(self) -> None:
        mgr = self._mgr()
        mgr.session_name = "project-x"
        with patch("c_lord.tmux._run") as run:
            run.return_value = MagicMock(returncode=0, stdout="", stderr="")
            mgr._ensure_session()

        for call in run.call_args_list:
            argv = call[0][0]
            if "set-environment" in argv:
                assert argv[argv.index("-t") + 1] == "project-x"

    def test_mark_is_applied_to_a_session_that_already_existed(self) -> None:
        """The dirty server is usually one that predates the fix — mark it anyway."""
        mgr = self._mgr()
        with patch("c_lord.tmux._run") as run:
            # has-session succeeds => the session was already there
            run.return_value = MagicMock(returncode=0, stdout="", stderr="")
            mgr._ensure_session()
        assert [c for c in run.call_args_list if "set-environment" in c[0][0]]

    def test_mark_is_not_repeated_on_every_call(self) -> None:
        mgr = self._mgr()
        with patch("c_lord.tmux._run") as run:
            run.return_value = MagicMock(returncode=0, stdout="", stderr="")
            mgr._ensure_session()
            first = len([c for c in run.call_args_list if "set-environment" in c[0][0]])
            mgr._ensure_session()
            total = len([c for c in run.call_args_list if "set-environment" in c[0][0]])
        assert first == len(SENSITIVE_ENV_KEYS)
        assert total == first, "the removal mark is idempotent — do not re-issue it every turn"


class TestClaudeCommandUnsetsSecrets:
    """Layer 3: the Claude process is clean even in a session nobody marked."""

    def test_start_claude_unsets_the_secrets_on_the_command_line(self) -> None:
        mgr = TmuxSessionManager(session_name="clord", mapping_path="")
        mgr._available = True
        mgr._thread_to_window[42] = "@7"

        sent: list[str] = []

        def fake_run(argv: list[str]) -> MagicMock:
            if "send-keys" in argv:
                sent.append(" ".join(argv))
            if "show-option" in argv:
                return MagicMock(returncode=0, stdout="42\n", stderr="")
            return MagicMock(returncode=0, stdout="", stderr="")

        with patch("c_lord.tmux._run", side_effect=fake_run):
            mgr.start_claude(42, "hello", "sonnet")

        typed = " ".join(sent)
        for key in SENSITIVE_ENV_KEYS:
            assert f"-u {key}" in typed, f"claude is started without unsetting {key}: {typed[:400]}"
