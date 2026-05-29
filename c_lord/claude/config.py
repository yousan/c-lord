"""Configuration for Claude Code CLI invocation.

Replaces the former ``ClaudeRunner`` class.  ``ClaudeRunner`` combined
configuration (model, timeout, permission mode) with subprocess execution
logic.  Now that tmux is the sole execution backend, this dataclass holds
only the configuration; ``TmuxClaudeRunner`` handles execution.
"""

from __future__ import annotations

from dataclasses import dataclass


@dataclass
class ClaudeConfig:
    """Settings for Claude Code CLI invocation.

    Consumers create one ``ClaudeConfig`` and pass it to ``setup_bridge()``.
    Individual Cogs read ``model``, ``timeout_seconds``, etc. from this object
    when constructing per-thread ``TmuxClaudeRunner`` instances.
    """

    command: str = "claude"
    model: str = "sonnet"
    permission_mode: str = "acceptEdits"
    working_dir: str | None = None
    timeout_seconds: int = 300
    dangerously_skip_permissions: bool = False
    # Reasoning effort for the session, passed to the CLI as ``--effort``.
    # c-lord raises this above the CLI's own default: every session gets the
    # deepest level the flag accepts.  Valid values: low, medium, high, xhigh,
    # max (the ``--effort`` flag rejects anything else; ``ultracode``/``auto``
    # are only reachable via ``CLAUDE_CODE_EFFORT_LEVEL`` / the ``/effort``
    # command, not this flag).  Override per instance or via ``CLAUDE_EFFORT``.
    effort: str = "max"
