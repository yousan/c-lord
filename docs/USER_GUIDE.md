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
│   │   ├── w1 ── Claude Code CLI for Thread 101
│   │   └── w2 ── Claude Code CLI for Thread 102
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
│   │   └── w1 ── Claude Code CLI for Thread 201
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
| Channel : tmux session | 1:1 | Auto-generated from repo name |
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
  3. Creates tmux window (w1, w2, ...)
  4. Launches Claude Code CLI
```

> **Important:** Channels must be bound to a repository via `/clord-init` before `/clord` can be used. Unbound channels will receive an error directing the user to run `/clord-init` first.

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
/clord-attach w1
!attach w1
```

After attaching, the bot will respond to messages in that thread.

### Opt-in Only

The bot **only responds in threads it knows about** — threads created via `/clord`, attached via `!attach` or `/clord-attach`, or spawned via the REST API. It will never interfere with threads created by other bots or for human conversations.

---

## Interacting in a Thread

### Sending Messages

Type normally in the thread. Each message is sent to Claude as a new prompt. If Claude is already processing a previous message, the new message interrupts it (sends SIGINT) and starts fresh with your new instruction.

There is **no length limit** on what you send — long pastes and large text attachments are delivered to Claude in full (they are split internally on the way into the tmux pane; you see no difference). If a message genuinely cannot be delivered, the bot says so with the input size and how to recover — it never silently finishes an empty turn. See [あるべき動き: 送ったメッセージが Claude に届くこと](specs/input-delivery.md).

### Attachments

Attach any file — text, image, PDF, archive. Each one is **saved into the session's checkout** and Claude is given its path, so "read the file I attached" works the way you'd expect. Nothing is inlined into the prompt, so file size does not affect whether your message gets through.

- Up to **10 files per message**, each up to Discord's own upload limit (100 MB).
- Saved under `.clord/attachments/<message-id>/` inside the session directory, and git-excluded so Claude's commits never pick them up.
- If a file **cannot** be handed over (too large, too many, download failed, or the channel has no repository bound), the bot says so in the thread, naming the file and the reason — it is never dropped silently.

See [あるべき動き: 添付ファイル](specs/attachments.md).

### Status Indicators (Emoji Reactions)

The bot adds a single emoji reaction to your message to show the turn status:

| Emoji | Meaning |
|-------|---------|
| 🟢 | Running — Claude is working (thinking or running tools) |
| 🟡 | Waiting — the turn finished; it's your turn |
| ❌ | The turn ended in an error |
| ⚠️ | No activity for a while — possible stall (extended thinking or compaction) |
| 🗜️ | Compacting context |

The reaction flips 🟢 → 🟡 each turn. Because reactions and thread renames use
different Discord rate limits, this lamp stays responsive even under heavy use;
the 🟢/🟡 in the **thread name** is a slower, eventually-consistent sidebar view
(#246).

---

## Interactive Features

### Questions (AskUserQuestion)

When Claude needs your input, Discord buttons or a select menu appear. Pick an option and Claude continues with your answer. Buttons survive bot restarts.

### Plan Mode

When Claude proposes a plan, an embed shows the full plan text with **Approve** / **Cancel** buttons. Claude only proceeds after you approve. Auto-cancels after 5 minutes.

### Tool Permissions

When Claude needs permission to run a tool, an embed shows the tool name and input with **Allow** / **Deny** buttons. Auto-denies after 2 minutes.

### @-mention when your input is needed

All of the prompts above (**AskUserQuestion / plan approval / tool permission / MCP elicitation**) pause the turn mid-flight, blocking on your answer. When one appears, c-lord posts a message that `@`-mentions **the person who sent that turn**, so you get a push notification even if your thread notifications are set to "mentions only" (a Discord embed on its own never pushes).

- The mention targets **whoever posted the turn** (the thread creator for the first prompt, or the replier for a follow-up).
- For turns with no human poster (webhook / scheduled runs) or turns driven directly in the tmux pane, the mention falls back to `DISCORD_OWNER_ID` when set (no mention when it is unset).

### TodoWrite Progress

When Claude tracks tasks with `TodoWrite`, a single embed is posted and updated in-place:
- ✅ Completed
- 🔄 In progress
- ⬜ Pending

### Progress folding (`progress.txt`)

The intermediate embeds a turn produces — session start, thinking, tool use and
tool results, the todo list — are useful while the turn is in flight and noise
once it is over. At the end of a turn they are folded into a single
`progress.txt` transcript and the originals are deleted.

- A turn that produced **nothing worth reading** (no tool use, no thinking — the
  session-start banner alone does not count) posts **no `progress.txt` at all**.
  The intermediate embeds are still cleaned up, so the thread simply ends with
  the answer (#542).
- When `progress.txt` is posted on its own rather than attached to a message
  that already has text, it carries a one-line caption saying what the file is.

---

## Slash Commands

| Command | Description | Status |
|---------|-------------|--------|
| `/clord <prompt>` | Start a new Claude Code session | Available |
| `/clord-attach <window>` | Attach a tmux window to the current thread | Available |
| `/clord-init <repo>` | Link a repository to the current channel | Available |
| `/stop` | Stop the current session (preserves it for resume) | Available |
| `/clear` | Reset the Claude Code session for this thread | Available |
| `/skill <name> [args]` | Run a Claude Code skill (with autocomplete) | Available |
| `/clord-status [show_all]` | List this channel's sessions — size, attach, resume (supersedes `/sessions`, `/session-dirs`, `/resume-info`) | Available |
| `/sync-sessions` | Import CLI sessions as Discord threads | Available |
| `/model show` | Show current model (global + per-thread) | Available |
| `/model set <model>` | Change model for all new sessions | Available |
| `/session-cleanup` | Remove clean orphaned session directories | Available |
| `/tmux-list` | List all active tmux windows | Available |
| `/close-workspace` | 終了: close the tmux window, keep the session (thread renamed `[終了] …`; a later message is held and offers a 再開 button) | Available |
| `/reopen-workspace` | Reopen a 終了 thread so messages run again | Available |
| `/workspace-delete` | Delete the tmux window and session directory for this thread | Available |
| `/upgrade` | Trigger bot upgrade (if enabled) | Available |

### Text Commands

| Command | Description | Status |
|---------|-------------|--------|
| `!attach <window>` | Attach a tmux window (same as `/clord-attach`) | Available |

---

## Access Control

Access to the bot is controlled by a Discord role.

- A server admin creates a `@claude-operator` role (configurable via `CLORD_ALLOWED_ROLE`)
- Only users with this role can run `/clord`, `/clord-init`, etc.
- Messages from users without the role are ignored by the bot
- Add/remove the role via Discord server settings (no bot restart needed)
- `DISCORD_OWNER_ID` can also be used to always allow a specific user

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
Bot restart → tmux session keeps running; observers re-attach and a quiet
              "C-lord を再起動しました" notice is posted. Claude is NOT
              re-prompted (#406) — send a new message to continue.
    ↓
tmux session gone (killed together with the bot / tmux-server death):
              the next message recreates the window and resumes the prior
              conversation from the on-disk transcript (claude --continue),
              announced with "🔄 …会話を復元して続けます" so the replayed
              context reads as a restore, not a broken bot (#464).
```

### Threads c-lord has no record of

A thread whose session record is gone (deleted workspace, rebuilt database, a thread
created by another host) **cannot** be resumed. c-lord no longer swallows those
messages: the message gets a ⚠️ reaction, the thread gets a one-time notice saying it
did **not** reach Claude, and the notice names the way forward — `/clord <task>` starts
a fresh session right there. The stopped-session hint from `/tmux-screenshot` and
`/resync` says the same thing rather than promising a resume that cannot happen (#538).
See [specs/session-resume.md](specs/session-resume.md).

### What Happens Under the Hood

```
/clord "Fix the bug"
    ↓
1. DB session record created (thread_id → session_id)
2. session_dir created: ~/c-lord-sessions/project-a/{thread_id}/
   └── git clone https://github.com/user/project-a.git
3. tmux window created: project-a:w1
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

A session times out only when Claude is genuinely wedged: the tmux pane has
stopped changing for the whole inactivity window (default: 5 minutes) **and**
Claude is not sitting idle at its input prompt. An embed with elapsed time and
guidance is shown; send a new message to start a fresh session in the same thread.

A turn that finished normally never produces this embed, even though its pane
goes completely silent afterwards — in the default `jsonl` bridge mode the answer
is delivered by the transcript mirror, so pane silence after an answer is the
expected steady state, not a hang (#541).

### Interrupting

Send a new message while Claude is working. The current operation is interrupted (SIGINT) and Claude starts with your new instruction. No need to `/stop` first.

### Bot Restart

If the bot restarts (upgrade, maintenance, etc.), active sessions are automatically marked for resume. When the bot comes back online, sessions pick up where they left off.

---

## Observability

The session-complete embed shows:

- **Token usage** — Input, output, and cache tokens with hit rate
- **Context usage** — Percentage of context window used; warning above 83.5%
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

# Switch between windows (w1, w2, ...)
# Ctrl-b + n (next), Ctrl-b + p (prev), Ctrl-b + 1 (by number)
```

Each window shows the Claude Code CLI terminal, so you can see what Claude is doing in real time.

---

## Tips

1. **One thread, one task** — Each thread is an independent Claude Code session. Use separate threads for separate tasks.
2. **Be specific** — The first message to `/clord` sets the context. A clear, detailed prompt gets better results.
3. **Peek via tmux** — If you have server access, `tmux attach` lets you watch Claude work in real time.
4. **Channel = project** — Each channel is linked to a repository. Make sure you `/clord` in the right channel.
