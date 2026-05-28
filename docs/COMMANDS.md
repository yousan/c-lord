# Command Reference

All commands available to Discord users and API consumers.

> For a comprehensive guide with architecture diagrams, session lifecycle, and tips, see the **[User Guide](USER_GUIDE.md)**.

## Architecture Overview

```
Bot → Channel (= 1 repo + 1 tmux session) → Thread (= 1 tmux window)
```

- **Channel ↔ Repository**: Each Discord channel is bound to a git repository via `/clord-init`. This binding is stored in the database.
- **Thread ↔ Session**: Each Discord thread maps 1:1 to a Claude Code session. Replies in a thread continue the same session via `--resume`.
- **Unbound channels**: Running `/clord` in a channel without a `/clord-init` binding will return an error directing the user to configure the binding first.
- **Execution mode**: Claude Code runs exclusively in tmux TUI mode. The legacy subprocess mode was removed in v1.x.

## Slash Commands

### Chat & Sessions

| Command | Description | Where |
|---------|-------------|-------|
| `/clord <prompt>` | Start a new Claude Code session | Channel or thread |
| `/stop` | Stop the active session (session is preserved for resume) | Thread only |
| `/clear` | Reset the session — next message starts fresh | Thread only |
| `/clord-attach <window>` | Attach this thread to an existing tmux window | Thread only |

**`/clord`** creates a new thread and sends your prompt to Claude Code. If used inside an existing thread, it continues the same session.

**`/stop`** gracefully interrupts the running process. The session is saved — just send another message in the thread to resume.

**`/clord-attach`** links a thread to a tmux window so you can interact with the same Claude Code session from both Discord and the terminal.

### Skills

| Command | Description | Where |
|---------|-------------|-------|
| `/skill <name> [args]` | Run a Claude Code skill | Channel or thread |

Skills are predefined prompts stored in `~/.claude/skills/`. The `name` parameter supports autocomplete — start typing to filter available skills.

### Channel Configuration

| Command | Description | Where |
|---------|-------------|-------|
| `/clord-init` | Show all channel-to-repo bindings | Any channel |
| `/clord-init repo:<url>` | Bind this channel to a git repository | Any channel |
| `/clord-init remove:True` | Remove the binding for this channel | Any channel |
| `/clord-thread-init` | Show thread-level binding for this thread | Thread only |
| `/clord-thread-init repo:<url>` | Bind this thread to a git repository (overrides channel binding) | Thread only |
| `/clord-thread-init remove:True` | Remove the thread-level binding | Thread only |

Requires **Manage Server** permission. When a channel is bound to a repo, all sessions started in that channel automatically use that repo as their working directory. A thread-level binding set via `/clord-thread-init` takes precedence over the channel binding.

### Model Management

| Command | Description | Where |
|---------|-------------|-------|
| `/model show` | Show the current Claude model | Anywhere |
| `/model set <model>` | Change the global model for new sessions | Anywhere |

Available models: `haiku` (fast), `sonnet` (balanced, default), `opus` (powerful).

### Session Management

| Command | Description | Where |
|---------|-------------|-------|
| `/resume-info` | Show the CLI command to resume this thread's session | Thread only |
| `/sessions` | List all known sessions | Anywhere |

**`/resume-info`** displays `claude --resume <session_id>` so you can continue the conversation from your terminal.

### Workspace Management

| Command | Description | Where |
|---------|-------------|-------|
| `/session-dirs` | List all active session directories | Anywhere |
| `/session-cleanup [dry_run]` | Remove clean orphaned session directories | Anywhere |
| `/tmux-list` | List all active tmux windows | Anywhere |
| `/workspace-delete` | Delete the tmux window and session directory for this thread | Thread only |

### Upgrade

| Command | Description | Where |
|---------|-------------|-------|
| `/upgrade` | Manually trigger a package upgrade | Anywhere |

Only available when the bot operator has enabled the upgrade slash command.

---

## Text Commands

| Command | Description | Example | Slash equivalent |
|---------|-------------|---------|------------------|
| `!clord <prompt>` | Start a new session (channel) / continue (thread) | `!clord build X` | `/clord` |
| `!attach <window>` | Attach this thread to a tmux window | `!attach work13` | `/clord-attach` |
| `!skill <name> [args]` | Run a Claude Code skill | `!skill recall` | `/skill` |
| `!stop` | Stop the active session (preserved for resume) | `!stop` | `/stop` |
| `!clear` | Reset the session — next message starts fresh | `!clear` | `/clear` |

Each text command is **functionally identical to its slash equivalent** — it
calls the same underlying handler. Text commands accept either prefix:

- `!skill recall` — the `!` prefix
- `@c-lord skill recall` — a bot mention (`when_mentioned_or`)

### Why text twins exist (E2E / manual QA)

Discord **slash commands cannot be invoked by a bot or a webhook** — only a
human clicking in the client can fire an application command. The webhook-based
E2E harness (`tests/e2e/`) and `E2E_TEST_WEBHOOK_URL`-driven manual QA therefore
have no way to trigger `/skill`, `/stop`, `/clear`, etc. The `!`-prefix / mention
twins are the webhook-invocable path, so these flows can be verified
automatically (see `tests/e2e/test_text_command_twins.py`).

> **Note (leading-slash is _not_ a substitute).** Typing `/skill-name` as an
> ordinary message does **not** run the skill: under `CLORD_BRIDGE_MODE=jsonl`
> c-lord prefixes every message sent to the Claude TUI with a zero-width-space
> marker, so the line no longer starts with `/` and the TUI does not treat it as
> a slash command. Use the `!`/mention twin instead.

> **Auth note.** A webhook author is not a real guild member, so commands gated
> by an allowlist/role (e.g. `!skill`) are denied for webhook callers when an
> allowlist is configured. Staging runs with an open allowlist so E2E works;
> production auth is unchanged.

---

## Access Control

The bot supports two authorization methods (OR logic — either one grants access):

1. **User ID** — Set `DISCORD_OWNER_ID` in `.env` to restrict commands to a specific user.
2. **Discord Role** — Set `CLORD_ALLOWED_ROLE` in `.env` to a role name (e.g., `claude-operator`). Any member with that role can use the bot.

If neither is configured, all users can use the bot.

---

## REST API

The optional REST API server allows external tools (Claude Code CLI, CI/CD, scripts) to interact with the bot programmatically.

**Base URL:** `http://127.0.0.1:8080` (configurable)
**Auth:** `Authorization: Bearer <CLORD_API_SECRET>` header (optional, except `/api/health`)

### Endpoints

| Method | Path | Description |
|--------|------|-------------|
| `GET` | `/api/health` | Health check |
| `POST` | `/api/notify` | Send immediate notification to Discord |
| `POST` | `/api/schedule` | Schedule a notification for later |
| `GET` | `/api/scheduled` | List pending notifications |
| `DELETE` | `/api/scheduled/{id}` | Cancel a pending notification |
| `POST` | `/api/tasks` | Register a scheduled Claude Code task |
| `GET` | `/api/tasks` | List all scheduled tasks |
| `PATCH` | `/api/tasks/{id}` | Update a task (enable/disable, prompt, interval) |
| `DELETE` | `/api/tasks/{id}` | Remove a scheduled task |
| `POST` | `/api/spawn` | Create a new thread and start Claude Code |
| `POST` | `/api/threads/{thread_id}/messages` | Post a message to a Discord thread |
| `POST` | `/api/mark-resume` | Mark a thread for resumption after restart |
| `GET` | `/api/lounge` | List recent AI Lounge messages |
| `POST` | `/api/lounge` | Post a message to the AI Lounge |

### Examples

Send a notification:

```bash
curl -X POST http://127.0.0.1:8080/api/notify \
  -H "Content-Type: application/json" \
  -H "Authorization: Bearer $CLORD_API_SECRET" \
  -d '{"message": "Build complete!", "title": "CI"}'
```

Spawn a new session:

```bash
curl -X POST http://127.0.0.1:8080/api/spawn \
  -H "Content-Type: application/json" \
  -H "Authorization: Bearer $CLORD_API_SECRET" \
  -d '{"prompt": "Review the latest PR and summarize changes"}'
```

Forward CLI input to a thread:

```bash
curl -X POST http://127.0.0.1:8080/api/threads/123456789/messages \
  -H "Content-Type: application/json" \
  -H "Authorization: Bearer $CLORD_API_SECRET" \
  -d '{"content": "Please also check the test coverage", "source": "cli"}'
```

Register a scheduled task:

```bash
curl -X POST http://127.0.0.1:8080/api/tasks \
  -H "Content-Type: application/json" \
  -H "Authorization: Bearer $CLORD_API_SECRET" \
  -d '{
    "name": "daily-review",
    "prompt": "Check for open PRs and post a summary",
    "interval_seconds": 86400,
    "channel_id": 123456789
  }'
```
