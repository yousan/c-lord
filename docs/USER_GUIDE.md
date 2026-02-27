# c-lord User Guide

A guide for Discord users interacting with the c-lord bot.

**[日本語版はこちら](ja/USER_GUIDE.md)**

---

## Starting a Session

### `/clord <prompt>`

The primary way to start a Claude Code session. Use it in any channel the bot monitors.

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
| `/skill <name> [args]` | Run a Claude Code skill (with autocomplete) | Planned |
| `/sessions` | List sessions (filter by origin, time window) | Planned |
| `/sync-sessions` | Import CLI sessions as Discord threads | Planned |
| `/resume-info` | Show the CLI command to continue this session in a terminal | Planned |
| `/model-show` | Show current model (global + per-thread) | Planned |
| `/model-set <model>` | Change model for all new sessions | Planned |
| `/worktree-list` | Show active session worktrees | Planned |
| `/worktree-cleanup` | Remove orphaned clean worktrees | Planned |
| `/upgrade` | Trigger bot upgrade (if enabled) | Planned |

> **Note:** Commands marked "Planned" are implemented in the codebase but not yet enabled in the standard startup (`main.py`). They will be available in a future release.

### Text Commands

| Command | Description | Status |
|---------|-------------|--------|
| `!attach <window>` | Attach a tmux window (same as `/clord-attach`) | Available |

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

## Thread Dashboard

A pinned embed in the bot's channel shows all active sessions at a glance:
- Which threads are active vs. waiting for input
- The thread owner is @-mentioned when Claude needs input

---

## Tips

1. **One thread, one task** — Each thread is an independent Claude Code session. Use separate threads for separate tasks.
2. **Be specific** — The first message to `/clord` sets the context. A clear, detailed prompt gets better results.
3. **Use skills** — `/skill` with autocomplete lets you run pre-defined workflows without typing long prompts.
4. **Check the dashboard** — The pinned thread dashboard shows what's happening across all sessions.
5. **Resume from terminal** — Use `/resume-info` to get the CLI command and continue a Discord session in your terminal.
