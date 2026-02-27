# c-lord User Guide

A guide for Discord users interacting with the c-lord bot.

**[日本語版はこちら](ja/USER_GUIDE.md)**

---

## Architecture Overview

A single bot serves multiple Discord channels, each linked to a different repository where Claude Code works.

```
Bot (c-lord process)                                     ← one instance
│
├── Access control: @claude-operator role
│   └── Only users with this role can interact with the bot
│
├── #project-a (channel)
│   │
│   ├── Repository: github.com/user/project-a.git
│   │   └── Linked via /clord-init (stored in DB)
│   │
│   ├── tmux session: "project-a"
│   │   ├── work1 ── Claude Code CLI for Thread 101
│   │   └── work2 ── Claude Code CLI for Thread 102
│   │
│   └── session_dir: ~/c-lord-sessions/project-a/
│       ├── 101/ ── git clone of project-a (for Thread 101)
│       └── 102/ ── git clone of project-a (for Thread 102)
│
├── #project-b (channel)
│   │
│   ├── Repository: github.com/user/project-b.git
│   │
│   ├── tmux session: "project-b"
│   │   └── work1 ── Claude Code CLI for Thread 201
│   │
│   └── session_dir: ~/c-lord-sessions/project-b/
│       └── 201/ ── git clone of project-b (for Thread 201)
│
└── #general (no repository linked)
    └── /clord → error "No repository configured"
```

### How Things Relate

| Relationship | Mapping | Linked by |
|-------------|---------|-----------|
| Channel : Repository | 1:1 | `/clord-init` (stored in DB) |
| Channel : tmux session | 1:1 | Auto-generated from channel name |
| Thread : tmux window | 1:1 | `@thread_id` (tmux window option) |
| Thread : session_dir | 1:1 | `~/c-lord-sessions/{project}/{thread_id}/` |
| Thread : Claude session | 1:1 | DB (`sessions` table) |

### Setup Flow

```
Admin: /clord-init repo:https://github.com/user/project-a.git
  ↓
Stored in DB: channel_id → repo URL, tmux session name, session_dir path
  ↓
User: /clord Fix the auth bug
  ↓
Bot automatically:
  1. Creates Thread (Discord)
  2. git clone (session_dir)
  3. Creates tmux window (work1, work2, ...)
  4. Launches Claude Code CLI
```

> **Note:** `/clord-init` and role-based access control are planned features. Currently, a single channel and repository are configured via `.env` (`DISCORD_CHANNEL_ID` / `SESSION_SOURCE_REPO`).

---

## Starting a Session

### `/clord <prompt>`

The primary way to start a Claude Code session. Use it in a channel that has a repository linked.

```
/clord Fix the login bug in auth.py
```

The bot creates a new thread and Claude begins working. All subsequent messages in that thread continue the same session.

### `/clord-attach <window>` / `!attach <window>`

Attach an existing tmux window to the current thread. Use this inside a thread that was created manually (e.g. for an already-running Claude Code CLI session).

```
/clord-attach work1
!attach work1
```

After attaching, the bot will respond to messages in that thread.

### Opt-in Only

The bot **only responds in threads it knows about** — threads created via `/clord`, attached via `!attach` or `/clord-attach`, or spawned via the REST API. It will never interfere with threads created by other bots or for human conversations.

---

## Interacting in a Thread

### Sending Messages

Type normally in the thread. Each message is sent to Claude as a new prompt. If Claude is already processing a previous message, the new message interrupts it (sends SIGINT) and starts fresh with your new instruction.

### Attachments

- **Text files** (`.txt`, `.md`, `.csv`, `.json`, `.xml`, etc.) — automatically appended to the prompt. Up to 5 files, 50 KB each, 100 KB total.
- **Images** (`.png`, `.jpg`, etc.) — downloaded and passed to Claude via `--image`. Up to 4 images, 5 MB each.

### Status Indicators (Emoji Reactions)

The bot adds emoji reactions to your message to show what Claude is doing:

| Emoji | Meaning |
|-------|---------|
| 🧠 | Thinking / reasoning |
| 🛠️ | Reading files |
| 💻 | Editing code |
| 🌐 | Web search |

Reactions are removed when the step completes.

---

## Interactive Features

### Questions (AskUserQuestion)

When Claude needs your input, Discord buttons or a select menu appear. Pick an option and Claude continues with your answer. Buttons survive bot restarts.

### Plan Mode

When Claude proposes a plan, an embed shows the full plan text with **Approve** / **Cancel** buttons. Claude only proceeds after you approve. Auto-cancels after 5 minutes.

### Tool Permissions

When Claude needs permission to run a tool, an embed shows the tool name and input with **Allow** / **Deny** buttons. Auto-denies after 2 minutes.

### TodoWrite Progress

When Claude tracks tasks with `TodoWrite`, a single embed is posted and updated in-place:
- ✅ Completed
- 🔄 In progress
- ⬜ Pending

---

## Slash Commands

| Command | Description | Status |
|---------|-------------|--------|
| `/clord <prompt>` | Start a new Claude Code session | Available |
| `/clord-attach <window>` | Attach a tmux window to the current thread | Available |
| `/stop` | Stop the current session (preserves it for resume) | Available |
| `/clear` | Reset the Claude Code session for this thread | Available |
| `/clord-init <repo>` | Link a repository to the current channel | Planned |
| `/skill <name> [args]` | Run a Claude Code skill (with autocomplete) | In progress |
| `/sessions` | List sessions (filter by origin, time window) | In progress |
| `/sync-sessions` | Import CLI sessions as Discord threads | In progress |
| `/resume-info` | Show the CLI command to continue this session in a terminal | In progress |
| `/model-show` | Show current model (global + per-thread) | In progress |
| `/model-set <model>` | Change model for all new sessions | In progress |
| `/upgrade` | Trigger bot upgrade (if enabled) | In progress |

> **Status legend:**
> - **Available** — usable in the current version
> - **In progress** — exists in the codebase but not yet enabled in the standard startup (`main.py`); will be available in a future release
> - **Planned** — designed but not yet implemented

### Text Commands

| Command | Description | Status |
|---------|-------------|--------|
| `!attach <window>` | Attach a tmux window (same as `/clord-attach`) | Available |

---

## Access Control

> **Note:** Role-based access control is a planned feature. Currently, access is restricted via `DISCORD_OWNER_ID` in `.env`.

Access to the bot is controlled by a Discord role.

- A server admin creates a `@claude-operator` role
- Only users with this role can run `/clord`, `/clord-init`, etc.
- Messages from users without the role are ignored by the bot
- Add/remove the role via Discord server settings (no bot restart needed)

---

## Session Lifecycle

```
/clord "Fix the bug"
    ↓
[Thread created] ← Bot responds here
    ↓
Send follow-up messages → Claude continues in same session
    ↓
/stop → Session paused (send a new message to resume)
    ↓
Bot restart → Session auto-resumes
```

### What Happens Under the Hood

```
/clord "Fix the bug"
    ↓
1. DB session record created (thread_id → session_id)
2. session_dir created: ~/c-lord-sessions/project-a/{thread_id}/
   └── git clone https://github.com/user/project-a.git
3. tmux window created: project-a:work1
   └── @thread_id = {thread_id}
4. Claude Code CLI launched (runs inside tmux window)
    ↓
Message sent to thread
    ↓
5. session_id retrieved from DB
6. Input sent to tmux window
7. Claude Code output streamed to thread
```

### Timeout

Sessions time out after a configurable period of inactivity (default: 5 minutes). An embed with elapsed time and guidance is shown. Send a new message to start a fresh session in the same thread.

### Interrupting

Send a new message while Claude is working. The current operation is interrupted (SIGINT) and Claude starts with your new instruction. No need to `/stop` first.

### Bot Restart

If the bot restarts (upgrade, maintenance, etc.), active sessions are automatically marked for resume. When the bot comes back online, sessions pick up where they left off.

---

## Observability

The session-complete embed shows:

- **Token usage** — Input, output, and cache tokens with hit rate
- **Context usage** — Percentage of context window used; ⚠️ warning above 83.5%
- **Compact detection** — Notification when context compaction occurs
- **Hard stall** — Warning after 30 seconds of no activity (extended thinking or compression)

---

## tmux Session Management

Each Claude Code session runs inside a tmux window. Server administrators with SSH access can inspect and interact with sessions directly.

```bash
# List tmux sessions per project
tmux ls
# project-a: 2 windows (created ...)
# project-b: 1 windows (created ...)

# Attach to a project's sessions
tmux attach -t project-a

# Switch between windows (work1, work2, ...)
# Ctrl-b + n (next), Ctrl-b + p (prev), Ctrl-b + 1 (by number)
```

Each window shows the Claude Code CLI terminal, so you can see what Claude is doing in real time.

---

## Tips

1. **One thread, one task** — Each thread is an independent Claude Code session. Use separate threads for separate tasks.
2. **Be specific** — The first message to `/clord` sets the context. A clear, detailed prompt gets better results.
3. **Peek via tmux** — If you have server access, `tmux attach` lets you watch Claude work in real time.
4. **Channel = project** — Each channel is linked to a repository. Make sure you `/clord` in the right channel.
