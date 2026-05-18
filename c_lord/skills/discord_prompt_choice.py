"""``discord-prompt-choice`` skill template (Issue #63).

When Claude needs to present the user with a multiple-choice question
(numbered options, yes/no, "which of these N approaches?"), it must call
this skill so the choices reach Discord. Post-#58 the capture-pane scrape
path is gone, so TUI-rendered choice prompts no longer leak through —
the only push channel is the skill REST API.

The template is rendered with the session's ``thread_id``, ``api_url`` and
optional ``api_secret`` substituted in. The result is written to
``<session_dir>/.claude/skills/discord-prompt-choice/SKILL.md`` so Claude
finds it via skill auto-discovery, alongside ``discord-reply``.
"""

from __future__ import annotations

# NOTE: keep the curl JSON aligned with ``POST /api/prompt-choice`` in
# ``c_lord/ext/api_server.py``. ``tests/test_skills_contract.py`` parses
# these examples and round-trips them through the real endpoint.

DISCORD_PROMPT_CHOICE_SKILL_NO_AUTH = """\
---
name: discord-prompt-choice
description: |
  Present a numbered choice / option menu to the user on Discord. Use this
  whenever you would otherwise ask the user to pick between alternatives
  ("which approach? 1, 2, or 3?", "yes / no / cancel", "should I refactor
  or rewrite?"). Without this skill the choice never reaches Discord and
  the session stalls. The user's reply arrives in the next turn via the
  normal Discord-thread → tmux input path.
---

# discord-prompt-choice

This Claude session is bridged to Discord thread `{thread_id}`.
Use the c-lord REST API to post a choice prompt.

## When to use

Whenever your response would otherwise be a question with discrete options —
post the options via this skill instead of just listing them in text. The
user replies with the label (e.g. `1`) in the Discord thread, and that
reply arrives as your next turn's input.

## Endpoint

`POST {api_url}/api/prompt-choice` (JSON only).

```bash
curl -s -X POST "{api_url}/api/prompt-choice" \\
  -H "Content-Type: application/json" \\
  -d @- <<'JSON'
{{
  "thread_id": {thread_id},
  "question": "Which approach do you prefer?",
  "choices": [
    {{"label": "1", "description": "Refactor in place"}},
    {{"label": "2", "description": "Rewrite from scratch"}},
    {{"label": "3", "description": "Leave as-is for now"}}
  ]
}}
JSON
```

## Schema

- `thread_id` (int, required) — pinned for this session.
- `question` (string, required) — the prompt shown to the user.
- `choices` (array, required, non-empty) — each element has:
  - `label` (string, required) — what the user types to pick this option.
  - `description` (string, optional) — short explanation of the option.

## Rules

1. Call this skill **instead of** writing "1. ... 2. ... please reply with
   a number" in `discord-reply`. The two skills are mutually exclusive in a
   given turn — pick one based on whether you need a decision or not.
2. After calling this skill, finish your turn. The user's choice arrives as
   the next turn's prompt; resume work then.
3. Keep `label` short (one token, ideally a single digit or word). It is
   what the user types.
4. Up to ~8 choices fit comfortably on Discord. If you have more, group
   them first.
"""

DISCORD_PROMPT_CHOICE_SKILL_WITH_AUTH = """\
---
name: discord-prompt-choice
description: |
  Present a numbered choice / option menu to the user on Discord. Use this
  whenever you would otherwise ask the user to pick between alternatives
  ("which approach? 1, 2, or 3?", "yes / no / cancel", "should I refactor
  or rewrite?"). Without this skill the choice never reaches Discord and
  the session stalls. The user's reply arrives in the next turn via the
  normal Discord-thread → tmux input path.
---

# discord-prompt-choice

This Claude session is bridged to Discord thread `{thread_id}`.
Use the c-lord REST API to post a choice prompt. The endpoint is protected
by Bearer auth — every request must include the `Authorization` header.

## When to use

Whenever your response would otherwise be a question with discrete options —
post the options via this skill instead of just listing them in text. The
user replies with the label (e.g. `1`) in the Discord thread, and that
reply arrives as your next turn's input.

## Endpoint

`POST {api_url}/api/prompt-choice` (JSON only).

```bash
curl -s -X POST "{api_url}/api/prompt-choice" \\
  -H "Authorization: Bearer {api_secret}" \\
  -H "Content-Type: application/json" \\
  -d @- <<'JSON'
{{
  "thread_id": {thread_id},
  "question": "Which approach do you prefer?",
  "choices": [
    {{"label": "1", "description": "Refactor in place"}},
    {{"label": "2", "description": "Rewrite from scratch"}},
    {{"label": "3", "description": "Leave as-is for now"}}
  ]
}}
JSON
```

## Schema

- `thread_id` (int, required) — pinned for this session.
- `question` (string, required) — the prompt shown to the user.
- `choices` (array, required, non-empty) — each element has:
  - `label` (string, required) — what the user types to pick this option.
  - `description` (string, optional) — short explanation of the option.

## Rules

1. Call this skill **instead of** writing "1. ... 2. ... please reply with
   a number" in `discord-reply`. The two skills are mutually exclusive in a
   given turn — pick one based on whether you need a decision or not.
2. After calling this skill, finish your turn. The user's choice arrives as
   the next turn's prompt; resume work then.
3. Keep `label` short (one token, ideally a single digit or word). It is
   what the user types.
4. Up to ~8 choices fit comfortably on Discord. If you have more, group
   them first.
5. The Bearer token above must be sent with every request. Do not log it.
"""


def render_discord_prompt_choice_skill(
    thread_id: int,
    api_url: str,
    api_secret: str | None = None,
) -> str:
    """Render the prompt-choice SKILL.md body with per-session values.

    Args:
        thread_id: Discord thread ID the session is bridged to.
        api_url: Base URL of the c-lord REST API.
        api_secret: Bearer token required by the API server. When set, the
            curl examples include the ``Authorization`` header.
    """
    if api_secret:
        return DISCORD_PROMPT_CHOICE_SKILL_WITH_AUTH.format(
            thread_id=thread_id,
            api_url=api_url,
            api_secret=api_secret,
        )
    return DISCORD_PROMPT_CHOICE_SKILL_NO_AUTH.format(thread_id=thread_id, api_url=api_url)
