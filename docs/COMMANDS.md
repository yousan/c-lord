# Command Reference

All commands available to Discord users and API consumers.

> For a comprehensive guide with architecture diagrams, session lifecycle, and tips, see the **[User Guide](USER_GUIDE.md)**.

## Architecture Overview

```
Bot → Channel (= 1 repo + 1 tmux session) → Thread (= 1 tmux window)
```

- **Channel ↔ Repository**: Each Discord channel is bound to a git repository via `/clord-init`. This binding is stored in the database.
- **Thread ↔ Session**: Each Discord thread maps 1:1 to a Claude Code session. Replies in a thread continue the same session via `--resume`.
- **Unbound channels**: Running `/clord` in a channel without a `/clord-init` binding returns an error directing the user to configure the binding first — *unless* `repo:` names one explicitly (#514), which works with no binding at all.
- **Channels another instance owns** (#522): the `!text` twins reach **every** c-lord instance that can read the channel, so an instance that is neither bound to the channel nor watching it as its `DISCORD_CHANNEL_ID` **says nothing at all** — no warning, no thread. Without this, every bystander bot in a shared server answers the same `!clord` with its own "not bound" warning. Slash commands are unaffected: their replies are ephemeral, so only the invoker sees them.
- **Execution mode**: Claude Code runs exclusively in tmux TUI mode. The legacy subprocess mode was removed in v1.x.

## Slash Commands

### Chat & Sessions

| Command | Description | Where |
|---------|-------------|-------|
| `/clord <prompt>` | Start a new Claude Code session | Channel or thread |
| `/clord repo:<url> <prompt>` | Start a session on a **specific** repository | Channel only |
| `/stop` | Stop the active session (session is preserved for resume) | Thread only |
| `/clear` | Reset the session — next message starts fresh | Thread only |
| `/compact [instructions]` | Compact (summarize) the session context to free the window | Thread only |
| `/clord-attach <window>` | Attach this thread to an existing tmux window | Thread only |

**`/clord`** creates a new thread and sends your prompt to Claude Code. If used inside an existing thread, it continues the same session.

**`repo:` is optional** (#514). Leave it out and the thread uses the channel's `/clord-init` repository, as before. Give it and the new thread is cloned from *that* repository instead — no `/clord-init` needed, and no separate `/clord-thread-init` step:

```
/clord repo:git@github.com:yousan/dotclaude.git prompt:Claude 5 系に対応する
```

The option autocompletes with the channel's default (shown first) and every repository the bot already knows. Derived URLs are accepted — a PR or issue link is normalized to the repository root. The thread's tmux session follows the chosen repository too (#427).

`repo:` only applies when a thread is being **created**. Inside an existing thread it is refused with a pointer to `/clord-thread-init`, because that thread's working copy is already cloned and would not change.

**`/stop`** gracefully interrupts the running process. The session is saved — just send another message in the thread to resume.

**`/compact`** fires the Claude Code TUI's built-in `/compact` for this thread's session, compressing the conversation history into a summary so the context window is freed **without losing continuity** (unlike `/clear`, which discards the session). Pass optional `instructions` to focus the summary (e.g. `/compact keep the open tasks and decisions`). Note: a plain `/compact` typed as a normal message does **not** work under `CLORD_BRIDGE_MODE=jsonl` (the leading-slash note below) — this command exists precisely because it sends `/compact` via the zero-width-space-free `send_literal` path.

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
| `/model set <model>` | Change the global model for new sessions. Pick a tier alias (`sonnet`/`opus`/`haiku`, each resolves to the latest of that tier) or type any model ID (e.g. `claude-fable-5`) — the CLI validates it | Anywhere |

Available models: `haiku` (fast), `sonnet` (balanced, default), `opus` (powerful).

### Session Management

| Command | Description | Where |
|---------|-------------|-------|
| `/clord-status` | List **this channel's** live sessions — size, attach, resume | Anywhere |
| `/clord-status show_all:true` | Also include closed sessions (like `docker ps -a`) | Anywhere |

**`/clord-status`** is the single per-channel status view. Each row shows the window number `#` (sorted ascending), the `status`, the thread `topic`, the directory `size`, and `used` (time since last activity). Above the table is a copyable `tmux attach -t <session>:work<#>` command (substitute the `#`). The Claude-Code session id (`cc-session`, for `claude --resume <id>` from a terminal) is shown only in the `all` view, at the right edge. By default it lists only **live** sessions (like `docker ps`); `show_all` adds **closed** ones (`/close-workspace`'d — tmux window gone, session dir kept, still using disk). Sessions whose working dir was deleted (`/workspace-delete`) are a footer count only. It **supersedes the removed `/sessions`, `/session-dirs`, and `/resume-info`** (#363).

**Status values** (observed live at call time, not the polled DB state):

| status | meaning |
|--------|---------|
| `run` | tmux window exists and Claude is executing (🟢) |
| `wait` | tmux window exists, turn done, waiting for your input (🟡) |
| `err` | tmux window exists, an error is visible in the pane (🔴) |
| `closed` | no tmux window but the session dir still exists (still uses disk). Two ways to get here: `/close-workspace`'d (thread renamed `[終了] …`; a message is held and offers a 再開 button, #512) or the pane merely died (bot restart / tmux-server death — a message auto-resumes it via `--continue`, #270) (⚪) |

### Workspace Management

| Command | Description | Where |
|---------|-------------|-------|
| `/session-cleanup [dry_run]` | Remove clean orphaned session directories | Anywhere |
| `/tmux-list` | List all active tmux windows | Anywhere |
| `/tmux-screenshot` | Post a PNG screenshot of this thread's current tmux pane (debug) | Thread only |
| `/close-workspace` | **終了**: close the tmux window, keep the session (see below) | Thread only |
| `/reopen-workspace` | Reopen a 終了 thread so messages run again | Thread only |
| `/workspace-delete` | Delete the tmux window and session directory for this thread | Thread only |

**`/close-workspace` = 終了 (#271, #512).** It kills the tmux window and archives
the thread but **keeps** the session directory, transcript, and DB row, so the
conversation can be picked up later. What you see:

- the thread is renamed **`[終了] #<issue> <topic>`** — the `W<N> │` window prefix
  is dropped because the window it named is exactly what was just killed
- writing in the thread afterwards **does not run Claude**. c-lord replies with
  「⏹️ このスレッドは終了しています」 plus a **▶️ 再開する** button; pressing it
  reopens the session and then runs the message you just sent, so nothing has to
  be retyped. `/reopen-workspace` does the same without the button (but does not
  re-send your message).

This is deliberately different from a session whose pane merely *died* (bot
restart, `kill -9`, tmux-server death): that one is not "終了", carries no marker,
and still auto-resumes on the next message via `--continue` (#270).

Use `/workspace-delete` instead when you want the disk back — that one is not
resumable.

### Mirror Recovery

| Command | Description | Where |
|---------|-------------|-------|
| `/resync` | Reconnect this thread's Discord mirror to its tmux pane | Thread only |
| `/resync-channel` | Reconnect the mirror for every thread in this channel | Channel or thread |
| `/restart-claude` | Restart the Claude process for this thread (keeps the conversation) | Thread only |

**`/resync`** is a safety valve for when the tmux→Discord *mirror* feels out of sync — a selection menu's buttons never appeared, or a tool embed looks stale. It re-projects the **current** tmux state onto Discord: (1) re-bridges any stranded TUI menu so its buttons (re)appear, and (2) posts a fresh PNG snapshot of the pane so you can see the live state. It does this immediately, without waiting for the 60s menu-watchdog sweep or a bot restart.

It does **not** touch the Claude process or the conversation — the session is untouched. `/resync-channel` runs the same reconnect for every thread in the channel's tmux session and reports how many it touched.

If the thread's work session is **stopped** (no tmux window — e.g. after a bot restart or a tmux-server death), `/resync` and `/tmux-screenshot` no longer dead-end with a bare "No tmux window found." Instead they tell you the session is stopped and that **sending a message auto-restores it** (the on-disk conversation resumes via `--continue`, announced with a "🔄 …会話を復元して続けます" notice — #270 / #465). This is what keeps a restart from leaving you stuck (#464).

**Screenshot height (#471)**: `/tmux-screenshot` (and the `/resync` PNG snapshot) show **more history than the live ~40-row window**. Claude's TUI keeps no scrollback, so before capturing, c-lord transiently grows the window so Claude redraws more of the conversation, captures the taller screen, then restores the exact original size (the human's attached view is unchanged). The default height is **100 rows**; override it with `CLORD_TMUX_SCREENSHOT_ROWS` (rows), or set it to `0` to capture the current window as-is.

**`/restart-claude`** restarts the Claude *process* for this thread **while keeping the conversation**. Use it when the process is wedged (e.g. a stuck turn silently blocks further input). It kills the active runner and the tmux window so the old/stuck process is gone, but — unlike `/clear` — it does **not** reset the session. Your next message then resumes the same conversation via `--continue`, so the context survives. (The fresh process spawns on that next message through the normal reply path, which is what keeps session setup correct.)

**The three recovery commands, by what they touch:**

| Command | Discord mirror | Claude process | Conversation/context |
|---------|----------------|----------------|----------------------|
| `/resync` | reconnect | untouched | kept |
| `/restart-claude` | — | restarted | **kept** (via `--continue`) |
| `/clear` | — | killed | **wiped** (fresh session) |

### Upgrade

| Command | Description | Where |
|---------|-------------|-------|
| `/upgrade` | Manually trigger a package upgrade | Anywhere |

Only available when the bot operator has enabled the upgrade slash command.

---

## Text Commands

| Command | Description | Example | Slash equivalent |
|---------|-------------|---------|------------------|
| `!clord [repo:<url>] <prompt>` | Start a new session (channel) / continue (thread) | `!clord repo:git@github.com:yousan/dotclaude.git build X` | `/clord` |
| `!attach <window>` | Attach this thread to a tmux window | `!attach w13` | `/clord-attach` |
| `!skill <name> [args]` | Run a Claude Code skill | `!skill recall` | `/skill` |
| `!stop` | Stop the active session (preserved for resume) | `!stop` | `/stop` |
| `!clear` | Reset the session — next message starts fresh | `!clear` | `/clear` |
| `!compact [instructions]` | Compact (summarize) the session context | `!compact keep open tasks` | `/compact` |
| `!model-show` | Show the current Claude model | `!model-show` | `/model show` |
| `!clord-status [all]` | List this channel's sessions (`all` = include closed) | `!clord-status all` | `/clord-status` |
| `!tmux-list` | List active tmux windows | `!tmux-list` | `/tmux-list` |
| `!tmux-screenshot` | Post a PNG screenshot of this thread's tmux pane | `!tmux-screenshot` | `/tmux-screenshot` |
| `!resync` | Reconnect this thread's Discord mirror to tmux | `!resync` | `/resync` |
| `!resync-channel` | Reconnect the mirror for every thread in the channel | `!resync-channel` | `/resync-channel` |
| `!restart-claude` | Restart the Claude process (keeps the conversation) | `!restart-claude` | `/restart-claude` |
| `!clord-init [repo\|remove]` | Bind / unbind / show channel→repo | `!clord-init https://…` | `/clord-init` |
| `!clord-thread-init [repo\|remove]` | Bind / unbind / show thread→repo | `!clord-thread-init remove` | `/clord-thread-init` |
| `!model-set <model>` | Change the global Claude model | `!model-set opus` | `/model set` |
| `!session-cleanup [dry]` | Remove clean orphaned session dirs (`dry` = preview) | `!session-cleanup dry` | `/session-cleanup` |
| `!close-workspace` | 終了: close the tmux window, keep the session | `!close-workspace` | `/close-workspace` |
| `!reopen-workspace` | Reopen a 終了 thread | `!reopen-workspace` | `/reopen-workspace` |
| `!workspace-delete` | Delete this thread's tmux window + session dir | `!workspace-delete` | `/workspace-delete` |

> **Manage-Server note.** `/clord-init` and `/clord-thread-init` are gated by
> Discord's *Manage Server* permission. Their `!text` twins have **no** such
> Discord-level gate — they are gated only by `_is_allowed` (owner/role
> allowlist). Keep the allowlist restricted in production.

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
