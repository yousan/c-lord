"""``discord-reply`` skill template.

The template is rendered with the session's ``thread_id``, ``api_url`` and
optional ``api_secret`` substituted in. The resulting SKILL.md is written to
``<session_dir>/.claude/skills/discord-reply/SKILL.md`` so Claude finds it via
its skill auto-discovery.
"""

from __future__ import annotations

# NOTE: when editing the curl examples, keep them strictly aligned with the
# ``POST /api/reply`` contract in ``c_lord/ext/api_server.py``. The endpoint
# accepts JSON only (no multipart) and ``progress_file`` must be an absolute
# path readable by the c-lord process. ``tests/test_skills_contract.py``
# parses these examples and round-trips them through the real endpoint.

DISCORD_REPLY_SKILL_NO_AUTH = """\
---
name: discord-reply
description: |
  Post your final answer to the Discord thread bridged to this Claude
  session. ALWAYS call this skill at the end of every response — it is the
  only way the user sees what you produced. Sending text to stdout / the
  terminal does NOT reach Discord.
---

# discord-reply

This Claude session is bridged to Discord thread `{thread_id}`.
Use the c-lord REST API to post your reply.

The endpoint accepts **JSON only** (no multipart). Attachments are passed by
**absolute filesystem path** — c-lord reads the file on the server side.

## Plain text reply

```bash
curl -s -X POST "{api_url}/api/reply" \\
  -H "Content-Type: application/json" \\
  -d @- <<'JSON'
{{
  "thread_id": {thread_id},
  "content": "YOUR FINAL ANSWER (Discord markdown OK)"
}}
JSON
```

## Reply with a progress.txt attachment

Write the detail to a file first, then pass its **absolute path** in the same
JSON body. Do NOT use multipart (`-F`) — it will not work.

```bash
cat > /tmp/progress-{thread_id}.txt <<'LOG'
step 1 ...
step 2 ...
LOG

curl -s -X POST "{api_url}/api/reply" \\
  -H "Content-Type: application/json" \\
  -d @- <<JSON
{{
  "thread_id": {thread_id},
  "content": "done — details attached",
  "progress_file": "/tmp/progress-{thread_id}.txt"
}}
JSON
```

## Rules

1. Call this skill exactly once per turn, at the very end, with your final answer.
2. Do not include intermediate progress in `content` — write it to a file and
   pass the absolute path via `progress_file`.
3. Send your full answer in `content` — c-lord splits messages longer than
   Discord's 2000-char limit into multiple posts automatically, so do NOT
   truncate or cut off your answer to fit. For very long detail dumps (logs,
   large diffs) prefer writing to a file and passing `progress_file`.
4. Markdown is supported (bold, lists, code fences). URLs render as text only
   (no preview embeds, by design).
5. Lead with what the user sees, not code. When you finished a task, start the
   reply with what changed *from the user's point of view* (what to do, and how
   the result looks here in Discord), THEN put implementation/CI detail
   (function names, test counts, file paths) after it. The reader may not read
   code — they should grasp "what changed for me" in one glance. If nothing
   changes for the user (internal-only work), say so explicitly.
"""

DISCORD_REPLY_SKILL_WITH_AUTH = """\
---
name: discord-reply
description: |
  Post your final answer to the Discord thread bridged to this Claude
  session. ALWAYS call this skill at the end of every response — it is the
  only way the user sees what you produced. Sending text to stdout / the
  terminal does NOT reach Discord.
---

# discord-reply

This Claude session is bridged to Discord thread `{thread_id}`.
Use the c-lord REST API to post your reply.

The endpoint accepts **JSON only** (no multipart) and is protected by Bearer
auth — every request must include the `Authorization` header below.
Attachments are passed by **absolute filesystem path** — c-lord reads the
file on the server side.

## Plain text reply

```bash
curl -s -X POST "{api_url}/api/reply" \\
  -H "Authorization: Bearer {api_secret}" \\
  -H "Content-Type: application/json" \\
  -d @- <<'JSON'
{{
  "thread_id": {thread_id},
  "content": "YOUR FINAL ANSWER (Discord markdown OK)"
}}
JSON
```

## Reply with a progress.txt attachment

Write the detail to a file first, then pass its **absolute path** in the same
JSON body. Do NOT use multipart (`-F`) — it will not work.

```bash
cat > /tmp/progress-{thread_id}.txt <<'LOG'
step 1 ...
step 2 ...
LOG

curl -s -X POST "{api_url}/api/reply" \\
  -H "Authorization: Bearer {api_secret}" \\
  -H "Content-Type: application/json" \\
  -d @- <<JSON
{{
  "thread_id": {thread_id},
  "content": "done — details attached",
  "progress_file": "/tmp/progress-{thread_id}.txt"
}}
JSON
```

## Rules

1. Call this skill exactly once per turn, at the very end, with your final answer.
2. Do not include intermediate progress in `content` — write it to a file and
   pass the absolute path via `progress_file`.
3. Send your full answer in `content` — c-lord splits messages longer than
   Discord's 2000-char limit into multiple posts automatically, so do NOT
   truncate or cut off your answer to fit. For very long detail dumps (logs,
   large diffs) prefer writing to a file and passing `progress_file`.
4. Markdown is supported (bold, lists, code fences). URLs render as text only
   (no preview embeds, by design).
5. Lead with what the user sees, not code. When you finished a task, start the
   reply with what changed *from the user's point of view* (what to do, and how
   the result looks here in Discord), THEN put implementation/CI detail
   (function names, test counts, file paths) after it. The reader may not read
   code — they should grasp "what changed for me" in one glance. If nothing
   changes for the user (internal-only work), say so explicitly.
6. The Bearer token above must be sent with every request. Do not log or
   echo it.
"""

# Kept as an alias for backward-compat with tests that imported the old name.
DISCORD_REPLY_SKILL = DISCORD_REPLY_SKILL_NO_AUTH


def render_discord_reply_skill(
    thread_id: int,
    api_url: str,
    api_secret: str | None = None,
) -> str:
    """Render the SKILL.md body with per-session values substituted.

    Args:
        thread_id: Discord thread ID the session is bridged to.
        api_url: Base URL of the c-lord REST API.
        api_secret: Bearer token required by the API server. When set, the
            curl examples include the ``Authorization`` header. When None
            (the default), the auth-less template is rendered.
    """
    if api_secret:
        return DISCORD_REPLY_SKILL_WITH_AUTH.format(
            thread_id=thread_id,
            api_url=api_url,
            api_secret=api_secret,
        )
    return DISCORD_REPLY_SKILL_NO_AUTH.format(thread_id=thread_id, api_url=api_url)
