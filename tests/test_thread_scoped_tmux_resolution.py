"""A thread bound to its own repo must get its own tmux session (#600, #427).

``/clord-thread-init`` lets one thread live in a different repository from its
parent channel. The thread's window then lives in *that repo's* tmux session, so
anything reaching for the thread's pane has to resolve with ``thread_id`` —
resolving by ``parent_id`` alone silently returns the *channel's* session, and
every keystroke aimed at the thread lands nowhere.

That is exactly #427, and #600 is the same accident a second time: two paths that
send a menu answer back to the TUI were still resolving without ``thread_id``::

    c_lord/cogs/transcript_mirror.py:268   resolve_tmux_manager(parent_id)
    c_lord/thread_state_sync.py:791        resolve_tmux_manager(parent_id)

Production effect: a thread stalled for two days while the same ❓ question was
re-posted six times — the answer never reached Claude, so the menu never closed,
so the watchdog kept re-bridging it.

Because "remember to pass it" has now failed twice, ``thread_id`` is made a
**required keyword argument**: a caller that genuinely is not thread-scoped has
to say ``thread_id=None`` on purpose. These tests pin that contract.
"""

from __future__ import annotations

import inspect

import pytest


class TestThreadIdIsStructurallyRequired:
    """AC6: the signature, not vigilance, is what prevents a third incident."""

    def test_resolve_tmux_manager_requires_thread_id(self) -> None:
        from c_lord.cogs.channel_repo import ChannelRepoCog

        sig = inspect.signature(ChannelRepoCog.resolve_tmux_manager)
        param = sig.parameters.get("thread_id")
        assert param is not None, "resolve_tmux_manager must take thread_id"
        assert param.default is inspect.Parameter.empty, (
            "thread_id must have NO default — #427 and #600 were both caused by it "
            "quietly defaulting to None and resolving the parent channel's session"
        )
        assert param.kind is inspect.Parameter.KEYWORD_ONLY, (
            "thread_id must be keyword-only so a call site cannot pass it positionally "
            "by accident, and so its intent is visible at every call"
        )

    @pytest.mark.parametrize(
        "module_path, cog_name",
        [
            ("c_lord.cogs.claude_chat", "ClaudeChatCog"),
            ("c_lord.cogs.skill_command", "SkillCommandCog"),
            ("c_lord.cogs.session_manage", "SessionManageCog"),
            ("c_lord.cogs.webhook_trigger", "WebhookTriggerCog"),
        ],
    )
    def test_wrappers_also_require_thread_id(self, module_path: str, cog_name: str) -> None:
        """A wrapper that defaults thread_id re-opens the same hole one level up."""
        import importlib

        cog = getattr(importlib.import_module(module_path), cog_name)
        resolver = getattr(cog, "_resolve_tmux_manager", None)
        if resolver is None:
            pytest.skip(f"{cog_name} has no _resolve_tmux_manager wrapper")
        param = inspect.signature(resolver).parameters.get("thread_id")
        assert param is not None, f"{cog_name}._resolve_tmux_manager must take thread_id"
        assert param.default is inspect.Parameter.empty, (
            f"{cog_name}._resolve_tmux_manager must not default thread_id — that is the "
            "hole #600 fell through"
        )


class TestNoCallerOmitsThreadId:
    """Every call site must state its intent, including the ones that pass None."""

    def _call_sites(self) -> list[tuple[str, int, str]]:
        """Each call as one string, continuation lines folded in.

        The call is often wrapped across lines by the formatter, so a per-line
        scan reports a false positive on a call whose ``thread_id=`` sits on the
        next line.
        """
        import pathlib

        root = pathlib.Path(__file__).resolve().parent.parent / "c_lord"
        out = []
        for path in root.rglob("*.py"):
            lines = path.read_text().splitlines()
            for i, line in enumerate(lines, 1):
                if "resolve_tmux_manager(" not in line or "def " in line:
                    continue
                # Prose is not a call site: a comment explaining what
                # resolve_tmux_manager() does has no thread_id to state, and
                # flagging it would push authors toward vaguer comments.
                hash_at = line.find("#")
                if 0 <= hash_at < line.index("resolve_tmux_manager("):
                    continue
                call = line.strip()
                # Fold following lines until the call's parentheses balance.
                j = i
                while call.count("(") > call.count(")") and j < len(lines):
                    call += " " + lines[j].strip()
                    j += 1
                out.append((str(path.relative_to(root)), i, call))
        return out

    def test_every_call_passes_thread_id_explicitly(self) -> None:
        """Including ``thread_id=None`` — silence is what caused both incidents."""
        offenders = [
            f"{p}:{n}  {src}" for p, n, src in self._call_sites() if "thread_id" not in src
        ]
        assert not offenders, (
            "these resolve_tmux_manager calls do not state thread_id; a thread bound "
            "to its own repo would silently get the parent channel's session "
            "(#427/#600):\n  " + "\n  ".join(offenders)
        )

    def test_the_two_regressed_sites_are_thread_scoped(self) -> None:
        """The exact pair from #600 must resolve with the thread, not just the parent."""
        wanted = {"cogs/transcript_mirror.py", "thread_state_sync.py"}
        seen = {
            p: src
            for p, _n, src in self._call_sites()
            if p in wanted and "resolve_tmux_manager(" in src
        }
        assert wanted <= seen.keys(), f"expected a call in each of {wanted}, saw {seen.keys()}"
        for path, src in seen.items():
            assert "thread_id=" in src, (
                f"{path} still resolves without thread_id — this is the #600 bug: the "
                "menu answer would be sent into the parent channel's tmux session"
            )
