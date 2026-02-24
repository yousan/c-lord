Run the full verification pipeline before committing:

1. Run `uv run ruff check c_lord/` — fix any lint errors
2. Run `uv run ruff format c_lord/` — ensure formatting
3. Run `uv run pytest tests/ -v --cov=c_lord --cov-report=term-missing` — all tests must pass
4. Run a security scan: search for `shell=True`, hardcoded secrets, and unvalidated user input in the codebase
5. Verify the public API imports work: `python -c "from c_lord import ClaudeRunner, ClaudeChatCog"`

Report results for each step. If any step fails, fix the issue and re-run that step before proceeding.
