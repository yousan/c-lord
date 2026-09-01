"""``discord-read`` skill template (Issue #259).

Lets the Claude session **read** messages from any Discord channel / thread /
guild the c-lord bot participates in, using the bot's own token via a plain
``curl`` against the Discord REST API — **not** the ``plugin:discord:discord``
MCP tool.

Why this exists (#454): when Claude tries to read another channel it tends to
reach for the MCP ``fetch_messages`` tool, which is gated by the plugin's own
``access.json`` allowlist and fails with ``Missing Access`` / ``not
allowlisted`` on channels the bot is otherwise a member of. The documented
curl fallback only lived in c-lord's own ``CLAUDE.md``, so it never reached
sessions whose cwd is a *different* project. Injecting this skill puts the
fallback in front of Claude regardless of cwd.

Design (spawn-read, see #259 comment 2026-06-22):
- The skill does **not** stand up or call a c-lord REST endpoint — reading is
  stateless, so a one-shot curl is enough and avoids the multi-tenant pitfall
  of an always-on localhost API (loopback TCP is not UID-scoped).
- The bot **token is never baked into this file**. SKILL.md is written inside
  the cloned user repo working tree, so a literal token could be committed and
  pushed. Instead the skill tells Claude to read the token at runtime from
  c-lord's ``.env`` (``{env_path}``) into a shell variable — file permissions
  (same Unix user) are the isolation boundary, matching #234's stance that the
  same-UID Claude can read ``.env`` anyway. The token therefore never appears
  as a literal in the command text that the #71 transcript mirror would echo
  to Discord.
"""

from __future__ import annotations

# NOTE: keep the curl examples aligned with the documented fallback in the
# project ``CLAUDE.md`` ("Debugging Discord from a Claude session"). The token
# is always read into a shell variable first; never interpolate it as a
# literal.

_DISCORD_READ_SKILL_WITH_PATH = """\
---
name: discord-read
description: |
  Read messages from any Discord channel, thread, or guild the c-lord bot is
  in. Use this whenever you need to look at Discord history that is NOT the
  current thread's incoming message — e.g. "check what the error said in that
  other channel", "read the linked message", "what was posted in #ops". Do
  NOT use the `plugin:discord:discord` MCP tool for this; it is gated by a
  separate allowlist and fails with `Missing Access` on channels the bot can
  otherwise see. This skill reads via the bot's own token over plain HTTPS.
---

# discord-read

Read Discord messages using the **c-lord bot token** and the Discord REST API.

## Do NOT use the MCP plugin

`plugin:discord:discord` (`fetch_messages`) is gated by its own `access.json`
allowlist and returns `Missing Access` / `not allowlisted` for channels the
c-lord bot is nonetheless a member of. **If you already tried MCP and it was
rejected, do not give up — fall back to the curl below.** Prefer this curl
from the start.

## Read the token (runtime, into a variable — never print it)

The bot token lives in c-lord's `.env`. Read it into a shell variable; never
echo, log, write it to a file, or commit it:

```bash
TOKEN=$(grep '^DISCORD_BOT_TOKEN=' {env_path} | cut -d= -f2-)
```

## Fetch the last N messages of a channel / thread

```bash
curl -s -H "Authorization: Bot $TOKEN" \\
  -H "User-Agent: DiscordBot (https://github.com/yousan/c-lord, 1.0)" \\
  "https://discord.com/api/v10/channels/<CHANNEL_OR_THREAD_ID>/messages?limit=20"
```

Returns a JSON array (newest first). Pipe through `jq` or `python3 -m json.tool`
to read it; each element has `.author.username` and `.content`.

## Fetch a single message

```bash
curl -s -H "Authorization: Bot $TOKEN" \\
  -H "User-Agent: DiscordBot/1.0" \\
  "https://discord.com/api/v10/channels/<CHANNEL_ID>/messages/<MESSAGE_ID>"
```

## Rules

1. Always read the token into a variable first; use `$TOKEN` in the curl.
   Never put the literal token in a command, a file, or your reply.
2. A Discord thread ID works as a channel ID — `/channels/<THREAD_ID>/messages`
   reads a thread.
3. Scope: you can read any channel/guild the **bot** is a member of (the bot
   token is cross-guild within its memberships). This is intentional.
4. This skill is for **reading**. To post your answer, use `discord-reply`.
"""

_DISCORD_READ_SKILL_NO_PATH = """\
---
name: discord-read
description: |
  Read messages from any Discord channel, thread, or guild the c-lord bot is
  in. Use this whenever you need to look at Discord history that is NOT the
  current thread's incoming message. Do NOT use the `plugin:discord:discord`
  MCP tool for this; it is gated by a separate allowlist and fails with
  `Missing Access`. This skill reads via the bot's own token over plain HTTPS.
---

# discord-read

Read Discord messages using the **c-lord bot token** and the Discord REST API.

## Do NOT use the MCP plugin

`plugin:discord:discord` (`fetch_messages`) is gated by its own `access.json`
allowlist and returns `Missing Access` / `not allowlisted` for channels the
c-lord bot is nonetheless a member of. **If you already tried MCP and it was
rejected, do not give up — fall back to the curl below.**

## Read the token (runtime, into a variable — never print it)

The bot token is in c-lord's `.env` file (the same `.env` the bot was started
with — search upward from the c-lord working directory if unsure). Read
`DISCORD_BOT_TOKEN` into a shell variable; never echo, log, write it to a
file, or commit it:

```bash
TOKEN=$(grep '^DISCORD_BOT_TOKEN=' /path/to/c-lord/.env | cut -d= -f2-)
```

## Fetch the last N messages of a channel / thread

```bash
curl -s -H "Authorization: Bot $TOKEN" \\
  -H "User-Agent: DiscordBot (https://github.com/yousan/c-lord, 1.0)" \\
  "https://discord.com/api/v10/channels/<CHANNEL_OR_THREAD_ID>/messages?limit=20"
```

## Rules

1. Always read the token into a variable first; use `$TOKEN` in the curl.
   Never put the literal token in a command, a file, or your reply.
2. A Discord thread ID works as a channel ID.
3. Scope: any channel/guild the **bot** is a member of (cross-guild within its
   memberships). This is intentional.
4. This skill is for **reading**. To post your answer, use `discord-reply`.
"""


def render_discord_read_skill(env_path: str | None = None) -> str:
    """Render the discord-read SKILL.md body.

    Args:
        env_path: Absolute path to c-lord's ``.env`` file, from which Claude
            reads ``DISCORD_BOT_TOKEN`` at runtime. When known, it is baked
            into the curl example so the fallback is copy-pasteable. When
            ``None``, a generic variant is rendered that still instructs
            Claude to read the token from c-lord's ``.env``.

    The token value itself is **never** substituted into the body — only the
    path. Claude reads the token into a shell variable at runtime.
    """
    if env_path:
        return _DISCORD_READ_SKILL_WITH_PATH.format(env_path=env_path)
    return _DISCORD_READ_SKILL_NO_PATH
