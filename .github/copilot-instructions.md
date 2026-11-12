# c-lord — Copilot Instructions

c-lord lets people use Claude Code from Discord. It is a reusable framework (an installable Python
package), not a personal bot. This file is the condensed form of `CLAUDE.md`, which is authoritative.

## Rules for any code you suggest

- Python 3.10+ and discord.py v2. Start every file with `from __future__ import annotations` and
  put type hints on every function signature.
- Format and lint with **ruff** (line length 100). Type-only imports go under `TYPE_CHECKING`.
- Use `logging.getLogger(__name__)`, never `print()`; add `log_ctx()` when a thread, session, task
  or channel is in scope.
- External commands: `create_subprocess_exec` with an argument list. **Never `shell=True`.**
- Put the **`--` separator** before a prompt or any other user-supplied positional argument, so it
  cannot be read as a flag.
- **Validate user input** before it reaches the CLI: session IDs (`^[a-f0-9\-]+$`) and skill names
  (`^[\w-]+$`) by regex.
- Keep secrets out of child environments — new secret variables belong in `SENSITIVE_ENV_KEYS`
  (`c_lord/tmux.py`).
- Non-critical Discord API failures (reactions, message edits): `contextlib.suppress(discord.HTTPException)`.
  Errors in business logic are logged, not swallowed.
- New constructor parameters need a backward-compatible default (`= None`): consumers must get
  features by upgrading the package, with no code change on their side.
- No personal configuration, secrets, or direct Anthropic API calls in this repository.

## Layout

- `c_lord/claude/` — tmux pane runner for the Claude Code CLI, its config and value types
- `c_lord/cogs/` — discord.py Cogs. Run Claude through `c_lord/cogs/_run_helper.py`; do not duplicate it
- `c_lord/transcript/` — reads Claude Code's jsonl transcript and mirrors the answer to the thread
  (the only delivery path)
- `c_lord/tmux.py`, `c_lord/session_dir.py` — one tmux window and one git clone per Discord thread
- `c_lord/database/` — SQLite persistence (aiosqlite) behind repository classes
- `c_lord/discord_ui/` — status reactions, reply splitting, embeds, buttons
- `c_lord/ext/api_server.py` — REST API control plane
- `tests/` — the pytest suite (`tests/e2e/` needs a live bot and is skipped by default)

## Tests come first (TDD is enforced)

For every feature and bug fix: write a failing test, then the implementation, then refactor
(RED → GREEN → REFACTOR). pytest with pytest-asyncio in auto mode.

```bash
uv run pytest tests/ -v --cov=c_lord
uv run ruff check c_lord/
uv run ruff format --check c_lord/
```

## Security check before committing

Discord messages end up as input to a Claude Code session running on the host, so user text must
never be interpolated into a shell command. Changes to `c_lord/claude/tmux_runner.py`,
`c_lord/cogs/_run_helper.py` or any Cog require the security audit in
`.claude/skills/security-audit/SKILL.md` before they are committed.
