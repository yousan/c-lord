"""``discord-reply`` skill template.

The template is rendered with the session's ``thread_id`` and ``api_url``
substituted in. The resulting SKILL.md is written to
``<session_dir>/.claude/skills/discord-reply/SKILL.md`` so Claude finds it via
its skill auto-discovery.
"""

from __future__ import annotations

DISCORD_REPLY_SKILL = """\
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

## Reply with progress.txt attachment

When you have a long progress log that would clutter the main answer,
write it to a file and attach it:

```bash
curl -s -X POST "{api_url}/api/reply" \\
  -F "thread_id={thread_id}" \\
  -F "content=YOUR FINAL ANSWER" \\
  -F "progress=@/path/to/progress.txt"
```

## Rules

1. Call this skill exactly once per turn, at the very end, with your final answer.
2. Do not include intermediate progress in `content` — put it in `progress.txt`
   if you want it preserved.
3. Keep `content` under ~1800 characters (Discord limit is 2000; leave room
   for chrome). If your answer is longer, paginate or attach it.
4. Markdown is supported (bold, lists, code fences). URLs render as text only
   (no preview embeds, by design).
"""


def render_discord_reply_skill(thread_id: int, api_url: str) -> str:
    """Render the SKILL.md body with per-session values substituted."""
    return DISCORD_REPLY_SKILL.format(thread_id=thread_id, api_url=api_url)
