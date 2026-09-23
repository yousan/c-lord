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
TOKEN=$(grep '^DISCORD_BOT_TOKEN=' /home/yousan/c-lord/.env | cut -d= -f2-)
```

## Fetch the last N messages of a channel / thread

```bash
curl -s -H "Authorization: Bot $TOKEN" \
  -H "User-Agent: DiscordBot (https://github.com/yousan/c-lord, 1.0)" \
  "https://discord.com/api/v10/channels/<CHANNEL_OR_THREAD_ID>/messages?limit=20"
```

Returns a JSON array (newest first). Pipe through `jq` or `python3 -m json.tool`
to read it; each element has `.author.username` and `.content`.

## Fetch a single message

```bash
curl -s -H "Authorization: Bot $TOKEN" \
  -H "User-Agent: DiscordBot/1.0" \
  "https://discord.com/api/v10/channels/<CHANNEL_ID>/messages/<MESSAGE_ID>"
```

## Rules

1. Always read the token into a variable first; use `$TOKEN` in the curl.
   Never put the literal token in a command, a file, or your reply.
2. A Discord thread ID works as a channel ID — `/channels/<THREAD_ID>/messages`
   reads a thread.
3. Scope: you can read any channel/guild the **bot** is a member of (the bot
   token is cross-guild within its memberships). This is intentional.
4. This skill is for **reading**. You do not need to post your answer
   anywhere — c-lord mirrors your reply into the thread on its own.
