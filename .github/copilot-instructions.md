# c-lord — Copilot Instructions

Discord frontend for Claude Code CLI. Python 3.10+ with discord.py v2.

## Key Rules

- **Type hints required** on all function signatures
- **`from __future__ import annotations`** in every Python file
- **ruff** for linting and formatting (line-length: 100)
- **pytest** with pytest-asyncio (auto mode) for testing
- **Never use `shell=True`** in subprocess calls — always `create_subprocess_exec`
- **Validate all user input** before passing to CLI (regex for session IDs, skill names)
- **`--` separator** before user prompts in CLI args to prevent flag injection
- **`contextlib.suppress`** for non-critical Discord API failures
- **Logging** via `logging.getLogger(__name__)`, never `print()`

## Architecture

- `c_lord/claude/` — CLI subprocess runner and stream-json parser
- `c_lord/cogs/` — Discord.py Cogs. Use `_run_helper.run_claude_in_thread()` for CLI execution
- `c_lord/database/` — SQLite session persistence (aiosqlite)
- `c_lord/discord_ui/` — Status reactions, message chunking, embeds
- `tests/` — pytest suite

## Testing (TDD Enforced)

**Write tests FIRST, then implement.** Follow RED → GREEN → REFACTOR for all new features and bug fixes.

```bash
uv run pytest tests/ -v --cov=c_lord
uv run ruff check c_lord/
uv run ruff format --check c_lord/
```

## Security (Mandatory Pre-Commit)

This project spawns Claude Code CLI as a subprocess. All user input flows through to CLI args.
Never interpolate user input into shell commands. Strip secrets from subprocess env.
Run security audit before committing changes to runner.py, _run_helper.py, or any Cog.
