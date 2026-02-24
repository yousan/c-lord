---
name: tdd
description: Enforce test-driven development — write tests FIRST, then implement. Mandatory for new features and bug fixes.
---

# TDD — Test-Driven Development (Enforced)

**This is not a suggestion. This is the workflow.**

When adding features or fixing bugs in c-lord, you MUST write tests before writing implementation code. No exceptions.

## When to Activate

- Adding any new feature or functionality
- Fixing a bug (write a test that reproduces the bug FIRST)
- Refactoring code (ensure existing tests pass, add missing ones)
- Adding a new Cog, parser rule, UI component, or database operation

## The Cycle: RED → GREEN → REFACTOR

### Step 1: RED — Write Failing Tests

Write tests that describe the desired behavior. Run them. **They must fail.**

```bash
uv run pytest tests/test_new_feature.py -v
# Expected: FAILED (because the code doesn't exist yet)
```

If tests pass before you write any implementation, your tests are wrong.

### Step 2: GREEN — Write Minimal Implementation

Write the **minimum code** to make the tests pass. No more.

```bash
uv run pytest tests/test_new_feature.py -v
# Expected: PASSED
```

### Step 3: REFACTOR — Clean Up

Improve the implementation while keeping all tests green. Then run the full suite:

```bash
uv run pytest tests/ -v --cov=c_lord --cov-report=term-missing
```

### Step 4: VERIFY — Lint + Format + Full Suite

```bash
uv run ruff check c_lord/
uv run ruff format c_lord/
uv run pytest tests/ -v --cov=c_lord
```

## What to Test First (by module type)

### New Parser Rule

```python
# tests/test_parser.py — Write THIS first
def test_parse_new_event_type():
    line = '{"type":"new_type","data":"value"}'
    event = parse_line(line)
    assert event is not None
    assert event.message_type == MessageType.ASSISTANT

def test_parse_new_event_type_missing_data():
    line = '{"type":"new_type"}'
    event = parse_line(line)
    assert event is not None  # Should handle gracefully

# THEN implement in c_lord/claude/parser.py
```

### New Cog Command

```python
# tests/test_new_cog.py — Write THIS first
from unittest.mock import AsyncMock, MagicMock

def test_rejects_unauthorized_user():
    cog = MyCog(bot=MagicMock(), allowed_user_ids={111})
    assert cog._is_authorized(999) is False

def test_accepts_authorized_user():
    cog = MyCog(bot=MagicMock(), allowed_user_ids={111})
    assert cog._is_authorized(111) is True

def test_validates_input_format():
    # Test that invalid input is rejected before reaching CLI
    assert not re.match(r"^[\w-]+$", "invalid; rm -rf /")

# THEN implement in c_lord/cogs/my_cog.py
```

### New Chunker Behavior

```python
# tests/test_chunker.py — Write THIS first
def test_chunk_with_nested_code_blocks():
    text = "```python\n```nested```\n```"
    chunks = chunk_message(text)
    # Verify code blocks are not broken
    for chunk in chunks:
        assert chunk.count("```") % 2 == 0  # Even number = balanced

def test_chunk_unicode_content():
    text = "日本語テスト 🎉" * 500
    chunks = chunk_message(text)
    assert all(len(c.encode("utf-8")) <= 8000 for c in chunks)

# THEN implement the fix in c_lord/discord_ui/chunker.py
```

### Bug Fix

```python
# tests/test_bug_123.py — Write THIS first
def test_session_id_with_uppercase_rejected():
    """Bug #123: Session IDs with uppercase hex were accepted."""
    runner = ClaudeRunner()
    with pytest.raises(ValueError):
        runner._build_args("hello", session_id="ABC-DEF")

# Confirm the test FAILS with current code
# THEN fix the regex in runner.py
# THEN confirm the test PASSES
```

### Database Operation

```python
# tests/test_repository.py — Write THIS first
async def test_save_overwrites_existing(repo):
    await repo.save(thread_id=123, session_id="old-id")
    await repo.save(thread_id=123, session_id="new-id")
    result = await repo.get_session_id(thread_id=123)
    assert result == "new-id"

async def test_concurrent_saves(repo):
    import asyncio
    tasks = [
        repo.save(thread_id=i, session_id=f"session-{i}")
        for i in range(100)
    ]
    await asyncio.gather(*tasks)
    # All should succeed without errors

# THEN implement or verify in c_lord/database/repository.py
```

## Coverage Requirements

| Module | Target | Rationale |
|--------|--------|-----------|
| parser.py | 90%+ | Pure logic, no dependencies, easy to test |
| chunker.py | 90%+ | Pure logic, edge cases are critical |
| types.py | 90%+ | Data structures and enums |
| repository.py | 90%+ | Real SQLite, no mocking needed |
| runner.py | 60%+ | Test args/env building; subprocess is harder |
| Cogs | 30%+ | Discord mocking is heavy; test validation logic |
| status.py | 30%+ | Async + Discord API mocking |
| **Overall** | **50%+** | **And always increasing** |

## Anti-Patterns (Don't Do This)

### Writing implementation first, tests later
```
❌ "I'll write the code first and add tests after"
✅ "I'll write the test first, watch it fail, then implement"
```

### Testing implementation details
```python
# ❌ Bad: Testing internal state
assert runner._process is not None

# ✅ Good: Testing observable behavior
args = runner._build_args("hello", None)
assert args[-1] == "hello"
```

### Skipping the RED step
```
❌ Writing a test that passes immediately
✅ Writing a test, running it, confirming it FAILS, then implementing
```

### Over-mocking
```python
# ❌ Bad: Mocking everything
mock_everything = MagicMock()
result = function_under_test(mock_everything)
# What are you even testing?

# ✅ Good: Mock only external boundaries
real_runner = ClaudeRunner(command="claude")
args = real_runner._build_args("test prompt", None)
assert "--" in args
```

## Quick Reference

```bash
# Run specific test file
uv run pytest tests/test_parser.py -v

# Run specific test
uv run pytest tests/test_parser.py::test_parse_system_message -v

# Run with coverage
uv run pytest tests/ -v --cov=c_lord --cov-report=term-missing

# Stop on first failure
uv run pytest tests/ -x

# Show local variables on failure
uv run pytest tests/ -v --tb=short -l
```
