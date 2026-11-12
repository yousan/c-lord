# c-lord

[![CI](https://github.com/yousan/c-lord/actions/workflows/ci.yml/badge.svg)](https://github.com/yousan/c-lord/actions/workflows/ci.yml)
[![Python 3.10 – 3.12](https://img.shields.io/badge/python-3.10%20%7C%203.11%20%7C%203.12-3776AB.svg)](pyproject.toml)
[![MIT license](https://img.shields.io/badge/license-MIT-green.svg)](LICENSE)

**Run multiple Claude Code sessions in parallel from Discord — each thread in its own git clone, so sessions never share a working tree.**

c-lord turns a Discord server into the control room for the Claude Code sessions running on your own machine. Give each task its own thread, walk away, and check on all of them from your phone, laptop, or terminal — nothing gets cut off, and everything that happened stays in the thread.

<p align="center">
  <img src="docs/assets/demo-mock.svg" alt="Mock-up: a Discord channel with four Claude Code threads, one open showing a request, a message typed in the terminal, the reply with files, and a terminal attached to the same session (all names fictional)" width="760">
</p>

## Why c-lord

- 🎛️ **Discord becomes the control room for many Claude Code sessions.** One thread is one real, interactive Claude Code running in tmux on your machine — the same thing you'd use in a terminal. Drop files into the thread and Claude gets them; files Claude makes come back as attachments. Want the keyboard? `tmux attach` to that very session.
- ⏳ **No time limit.** A long job is never stopped for taking long. c-lord only gives up on a turn when Claude's screen has been frozen for 5 minutes, and even then it doesn't kill Claude.
- 🧪 **A whole separate workspace per thread.** Each thread works in its own clone of your repo, so it can run its own backend and frontend together, and its own `docker compose` stack, without touching another thread's. c-lord knows which containers belong to which thread and stops them with it. (Turn it on by binding the channel to a repo with `/clord-init`; host ports are still shared, so give each stack its own.)
- 📜 **Everything is kept in Discord.** Replies, tool activity, files — even what you type straight into the tmux pane — land in the thread. Your team can follow along, and another AI agent can read the history to pick up the work.
- 🔒 **You choose who can drive it.** Out of the box only the owner of the Discord app can use it (talking to c-lord runs commands on your machine). Open it to a role or a list of people when you want.

**Get started:** [Quick start](#quick-start) · [How it works](#the-big-idea-parallel-sessions-each-in-its-own-clone) · [Commands](docs/COMMANDS.md) · [Configuration](#configuration) · [Architecture](#architecture)

English · [日本語](docs/ja/README.md) · [简体中文](docs/zh-CN/README.md) · [한국어](docs/ko/README.md) · [Español](docs/es/README.md) · [Português](docs/pt-BR/README.md) · [Français](docs/fr/README.md)

> **Based on [claude-code-discord-bridge](https://github.com/ebibibi/claude-code-discord-bridge) by [@ebibibi](https://github.com/ebibibi) (Masahiko Ebisuda).** This project was originally created by ebibibi and is used here under the MIT License. Thank you for the great foundation.

> c-lord is an independent open-source project. Anthropic does not make, sponsor or endorse it; "Claude" and "Claude Code" are Anthropic's trademarks. c-lord only drives the Claude Code CLI that you installed and logged in to yourself.

**On this page:** [How it works](#the-big-idea-parallel-sessions-each-in-its-own-clone) · [Quick start](#quick-start) · [Working in a thread](#working-in-a-thread) · [Workspaces](#workspaces) · [Commands](#commands) · [Configuration](#configuration) · [Scheduled tasks](#scheduled-tasks) · [GitHub and CI](#github-and-ci-automation) · [REST API](#rest-api) · [As a library](#using-c-lord-as-a-library) · [Architecture](#architecture) · [Security](#security) · [Development](#development) · [How it is built](#how-this-project-is-built)

## The Big Idea: Parallel Sessions, Each in Its Own Clone

Each Discord thread is its own Claude Code session, running in its own tmux window on your server. Send tasks to several threads and they run side by side; close your laptop and they keep going; reply in a thread to pick up where it left off.

What keeps parallel sessions from clobbering each other is **isolation**: when a channel is bound to a repository (`/clord-init`), every thread works in its own independent `git clone`. Sessions never share a working tree, so one thread's uncommitted edits can never show up in another's. They meet only through the git remote — branches, pushes and PRs — like human contributors do.

```text
Thread A (feature)   ──→  Claude Code  (own clone)  ─┐
Thread B (PR review) ──→  Claude Code  (own clone)   ├─→  git remote (branches · PRs)
Thread C (docs)      ──→  Claude Code  (own clone)  ─┘
```

**Sessions do not coordinate with each other today.** Earlier versions of this README described four coordination mechanisms; this is their actual state ([#758](https://github.com/yousan/c-lord/issues/758)):

| Mechanism | Actual state |
|---|---|
| **Concurrency notice** (instructions to Claude about working in parallel) | **Not built** — sessions run in the tmux TUI (#53), which has no per-turn system-prompt channel, so it was never delivered; c-lord stopped building it (#766) |
| **Active session registry** | Exists in memory and is used by `/workspace-cleanup`, but **sessions don't see it** (nothing sends it to them) |
| **Coordination channel** | **Off by default.** With `COORDINATION_CHANNEL_ID` set, it posts a one-line notice when a session's turn ends; there is no start event |
| **AI Lounge** | The REST endpoints work, but **nothing tells Claude the lounge exists**, so in practice nothing is posted — see [AI Lounge](#ai-lounge-not-delivered-to-sessions) |

So: separate working trees, not live coordination. Anything outside the clones — the same remote branch, a shared port or database, a staging bot — can still collide. Give each thread its own branch.

### AI Lounge (not delivered to sessions)

> **Status: not wired up** ([#758](https://github.com/yousan/c-lord/issues/758)). The lounge was designed to be injected into every session as ephemeral system context. Since sessions run in the tmux TUI (#53) there is no per-turn channel for that, so c-lord does not build it (#766): Claude is never told the lounge exists, and in practice nothing gets posted.

The REST endpoints still work, so you can use the lounge yourself — for example by describing it in your repository's `CLAUDE.md`:

```bash
# Post a note (stored in SQLite; also forwarded to the lounge channel, which defaults to COORDINATION_CHANNEL_ID)
curl -X POST "http://localhost:8080/api/lounge" \
  -H "Content-Type: application/json" \
  -d '{"message": "Starting auth refactor on feature/oauth", "label": "feature dev"}'

# Read recent notes
curl "http://localhost:8080/api/lounge"
```

## Quick start

You need a Linux machine (WSL2 works) that stays on, with these installed:

- Python 3.10 or newer and [uv](https://docs.astral.sh/uv/)
- `git` and `tmux`
- the [Claude Code CLI](https://docs.anthropic.com/en/docs/claude-code), already logged in — run `claude` once by hand to make sure
- a Discord server you are allowed to add a bot to

### Step 1 — Create the Discord bot

1. Open the [Discord Developer Portal](https://discord.com/developers/applications) and make a **New Application**.
2. On its **Bot** page, switch on **Message Content Intent** (under *Privileged Gateway Intents*) and copy the bot **token**.
3. Under **OAuth2 → URL Generator**, tick the scopes `bot` and `applications.commands`, then these bot permissions:

   | Permission | Used for |
   |---|---|
   | View Channel | seeing the channel at all (see the note below) |
   | Send Messages, Send Messages in Threads | replies |
   | Create Public Threads | one thread per session |
   | Read Message History | catching up on messages posted while the bot was away |
   | Add Reactions, Manage Messages | the status lamp on your message, and clearing it |
   | Manage Webhooks *(optional)* | sub-agent messages under their own name (`CLORD_SUBAGENT_VOICES=1`) |

4. Open the generated URL and add the bot to your server.

> **Private / permission-locked channels:** if a channel denies **View Channel**
> to `@everyone` (a private channel), the server-wide invite above is *not* enough
> — Discord's channel-level deny overrides it. You must add the **bot's role**
> (or the bot member) to that channel's permission overrides and grant at least
> **View Channel**, **Create Public Threads**, **Send Messages**, and **Send
> Messages in Threads**. Granting "Manage Threads" alone does **not** help.
>
> When the bot is missing these on a channel, **`/clord` replies (ephemerally)
> with the exact permissions to grant** (#443) — a slash command reaches the bot
> whether or not it can see the channel. Fix the permissions before going on:
> without **View Channel** the bot receives no messages from that channel, so
> what you write in a thread there would never reach Claude.

### Step 2 — Run the setup wizard

The wizard writes `.env` for you; there is nothing to edit by hand.

```bash
# Straight from GitHub, nothing installed:
uvx --from "git+https://github.com/yousan/c-lord.git" c-lord setup

# Or from a clone (this is also what you want for running it as a service):
git clone https://github.com/yousan/c-lord.git
cd c-lord
uv run c-lord setup
```

It asks six things, in this order:

1. **Claude Code CLI** — it only checks that `claude` can be run, and tells you if not.
2. **Bot token** — the token is checked against the Discord API (a token pasted as `Bot xxx` is stored as `xxx`; after 3 empty or invalid answers it stops without writing `.env`).
3. **Channel** — it lists the text channels the bot can see; pick one by number (no ID copying), or paste a numeric channel ID (a channel URL, or digits like `²`, is asked again). This becomes `DISCORD_CHANNEL_ID`, the bot's home channel.
4. **Your Discord user ID** — optional; this is who gets @-mentioned. A username instead of the numeric ID is asked again, since `c-lord start` refuses it.
5. **Working directory** — the default directory for runs that have no clone of their own.
6. **Model** — `sonnet`, `opus` or `haiku`.

Then it writes `.env` (use `--env PATH` to put it somewhere else) and offers to start the bot on the spot.

### Start / Stop

```bash
c-lord start    # start the bot (reads .env in current dir)
c-lord start --env /path/to/.env   # custom .env location
c-lord version  # print the build that is running, e.g. v1.4.183-bd80c47e-20260908
```

In a clone, prefix these with `uv run`. A bot started this way runs in the foreground; Ctrl-C stops it. To keep it running, see [Run as a service](#run-as-a-service-systemd---user--survives-reboot).

> **Config precedence (#324):** keys defined in the `.env` file always win over
> inherited process environment variables — the directory you start from
> determines the bot's identity. Keys *absent* from the file still fall back to
> the process env, so env-var-only setups keep working.

### Step 3 — Bind a repository and start a session

Sessions are started with a slash command, never by a plain message typed into the channel itself (those are ignored, so a channel can stay a normal place to talk).

```text
/clord-init repo:https://github.com/you/your-project.git     ← once per channel
/clord Fix the flaky login test                               ← opens a thread and starts Claude there
```

`/clord-init` records which repository the channel works on. By default Discord offers it only to members with the **Manage Server** permission. From then on every `/clord` opens a new thread, clones that repository for it, and starts Claude Code in the clone. Everything you write **inside that thread** goes to the same Claude session. To use a different repository for one thread, name it: `/clord repo:<url> <prompt>` — that also works in a channel with no binding.

The host clones with its own git credentials, so a private repository needs an SSH key or credential helper set up there. When a clone fails, the thread shows git's own error and what to check ([spec](docs/specs/workspace-setup-failure.md)).

### Run as a Service (systemd --user — survives reboot)

To keep c-lord running in the background and **start it automatically when the
host reboots**, install it as a `systemd --user` service. From your clone:

```bash
bash scripts/install-systemd.sh
```

This installs [`deploy/c-lord.service`](deploy/c-lord.service) verbatim as
`~/.config/systemd/user/c-lord.service`, writes the host-specific values (this
clone's path, your `uv`, your `PATH`) to the drop-in
`c-lord.service.d/10-install.conf`, enables it, and turns on **linger** so the
service starts on boot even when no one is logged in. Re-run it after
`deploy/c-lord.service` changes. Then manage it with standard systemd:

```bash
systemctl --user restart c-lord.service     # after a `git pull`
systemctl --user stop    c-lord.service
systemctl --user status  c-lord.service
journalctl --user -u c-lord.service -f      # live logs
```

Once the unit is installed, **systemd is the only thing that starts this clone's
bot.** Don't kill it and start it by hand — systemd would try to respawn it, hit
the single-instance lock, give up (`Start request repeated too quickly`), and
leave the bot running unsupervised. `bash scripts/staging.sh restart|stop` knows
this: in a clone that a user unit's `WorkingDirectory=` points at, it calls
`systemctl --user restart|stop` instead of killing, and errors out (without
killing) when `systemctl --user` is unavailable. `bash scripts/staging.sh status`
shows whether each bot process is inside the unit's cgroup.

Why `--user` instead of a system service: c-lord drives the user's `tmux`,
`~/.claude` session files, `uv`, and the `claude` CLI, so it runs cleanest as
**you**, with your environment — a root/system service would have to re-create
all of that via `User=`/`Environment=`. `loginctl enable-linger "$USER"` is what
makes a user service survive logout and start at boot; the installer runs it for
you.

> **Manual install:** copy [`deploy/c-lord.service`](deploy/c-lord.service), fix
> the two `CHANGE-ME` paths (clone dir + `uv`), then
> `systemctl --user daemon-reload && systemctl --user enable --now c-lord.service && loginctl enable-linger "$USER"`.

> **WSL note:** if `systemctl --user` reports `Failed to connect to bus`, the
> user D-Bus session isn't up. Ensure `systemd=true` under `[boot]` in
> `/etc/wsl.conf` and reboot WSL (`wsl --shutdown`), or restart the bot by
> killing it and letting `Restart=always` respawn it (`pkill -f c_lord.main` is
> unsafe with multiple clones — kill by PID).

## Working in a thread

What follows is what you actually see in Discord. Each item links to the page that defines how it should behave; the full index is [docs/specs/](docs/specs/README.md), and the day-to-day guide is [docs/USER_GUIDE.md](docs/USER_GUIDE.md).

### Talking to Claude

- **One thread, one conversation.** A reply in the thread continues the same Claude Code session (`--resume`), with its context intact — today, tomorrow, or after the bot restarted.
- **A new message interrupts.** If Claude is still working when you write again, the running turn is interrupted and your new instruction is what runs. No `/stop` first.
- **Stopping.** `/stop`, or the **⏹ Stop** button that sits at the bottom of the thread while a turn runs, stops the turn and the shell commands Claude left running. The conversation is kept; your next message continues it ([spec](docs/specs/stop-button.md)).
- **No length limit on what you send.** Long pastes are delivered whole. If a message really cannot be delivered, the thread says so — it never ends an empty turn in silence ([spec](docs/specs/input-delivery.md)).
- **Files in.** Attach up to 10 files to a message. Each is saved inside the thread's clone (`.clord/attachments/…`, kept out of git) and Claude is told the path ([spec](docs/specs/attachments.md)).
- **Files out.** A file Claude hands over is attached to the thread under its real name, up to 25 MB by default ([spec](docs/specs/user-file-delivery.md)).
- **Long answers arrive complete.** An answer over Discord's 2000-character limit is split into several messages without breaking code blocks ([spec](docs/specs/long-reply-split.md)).
- **The terminal is still there.** `tmux attach` to the thread's window and type; what you type there shows up in the thread as yours, and Claude's answer follows as usual ([spec](docs/specs/mirrored-events.md)).
- **Messages posted while the bot was down are not lost.** When it reconnects or restarts, it reads back what arrived in its threads meanwhile and runs it, with one line explaining the delay ([spec](docs/specs/gateway-backfill.md)).
- **Thread names stay yours** — c-lord never re-summarises a thread's name on its own. A thread keeps the name it was opened with (only the `W<N> │` prefix and the `#<issue>` number are added around it); run `/thread-rename` in the thread when you want a fresh summary of the recent conversation (sonnet). `CLORD_AUTO_TOPIC=1` restores the old auto-summary (#705)

### Knowing what is going on

- **Real-time status** — Emoji reaction lamp on your message: 🟢 running while Claude works, 🟡 waiting when it's your turn (❌ on error, ⏳ / ⚠️ if it goes quiet for 10 s / 30 s). Reactions stay responsive under heavy use. A 🟢/🟡 lamp in the thread name is off by default (#329); set `CLORD_THREAD_LAMP=1` for that slower, eventually-consistent sidebar view (#246)
- **Tool activity** — Read from Claude Code's own transcript, not posted tool by tool: a turn that goes quiet shows one progress line (`⚙️ 作業中 5:56 · 🔧 Bash(…) · ツール 61 件`, see *Turn progress line* below), and the turn's tool calls and progress sentences are attached to the answer as `progress.txt` (#723)
- **Turn progress line** — When a turn goes quiet for 90 s, one subtext line appears (`⚙️ 作業中 5:56 · 🔧 Bash(…) · ツール 61 件`), refreshes in place every 15 s, and disappears the moment real output returns; says `⏳ 待機中` when even tool activity has stopped. When Claude writes a progress sentence (the pane's "●" line), the line shows the newest one instead of the tool (#883) — those sentences also land in `progress.txt`, never as messages of their own. Never posted outside a turn. Opt out with `CLORD_TURN_PROGRESS=0` (#539)
- **Context usage** — Each turn ends with a one-line footer (`📊 7% context (69k/1.0M) · …`): context window percentage (input + cache tokens, excluding output), model, versions and cost; it turns into a ⚠️ warning above 83.5%, when auto-compact may run next turn (`docs/specs/context-footer.md`)
- **Compact notice** — When Claude Code compacts the context, the thread gets a one-line `🗜️ コンテキストを圧縮しました` instead of the raw continuation summary (#628)
- **Hard stall lamp** — After 30 s of no activity the reaction on your message turns ⚠️ (extended thinking or context compression can be this quiet too); no message is posted to the thread (#473)
- **Turn-end ping** — When the turn is over, the thread @-mentions whoever asked for it, so you are notified. A turn that produced no answer says so instead of claiming to be finished ([spec](docs/specs/turn-end-notification.md)).
- **Timeout notifications** — Embed with elapsed time and resume guidance, raised only when Claude is genuinely wedged (pane frozen for the whole window *and* not idle at its prompt); a normally-finished turn never triggers it (#541), and neither does a question/plan menu left waiting for your answer (#751)
- **Usage limit and login** — Hitting the plan's usage limit is reported as exactly that, with the time it resets ([spec](docs/specs/usage-limit.md)). When Claude Code's login expires, the owner gets one DM naming the host and the threads that stopped, and can log back in from that DM ([spec](docs/specs/login-alert.md)).
- **📊 Session Status board** — Off by default (#761). Set `CLORD_SESSION_STATUS_BOARD=1` to keep one live board in `DISCORD_CHANNEL_ID` listing each session as 🟢 Auto-processing / 🟡 Waiting for input. While it is off, a starting bot posts nothing to the channel and retires any board earlier starts left behind; the turn-end `🟡 Claude has finished … @you` ping in the thread is unaffected. See `docs/specs/session-status-board.md`

### When Claude asks you something

- **Questions** — When Claude calls `AskUserQuestion`, the choices appear in the thread as buttons or a select menu, and Claude carries on with what you picked. The buttons keep working after a bot restart ([spec](docs/specs/ask-menu-lifecycle.md)).
- **Plan Mode** — When Claude calls `ExitPlanMode`, the approval menu Claude Code shows is bridged to Discord the same way as `AskUserQuestion`: the choices appear as buttons, and Claude proceeds with the one you pick (#251)
- **Tool permission prompts** — Not shown on Discord: c-lord accepts Claude Code's permission prompt in the pane so the turn keeps going. What Claude may do is decided by the permission mode / settings the session runs with, not by a per-tool button

### Splitting work

- **Sub-agent log** — Work split across sub-agents inside one thread stays on Discord: a `🤖 サブエージェント「…」開始` / `終了 · 6:12 · ツール 48 件` line per run, the end line carrying `subagent-*.md` (full report, assignment, tool calls), the start line's copy refreshed while it runs. `CLORD_SUBAGENT_VOICES=1` adds each agent's messages and report under its own name (#971, `docs/specs/subagent-log.md`)

**When to open a new thread, and when to split work inside one** (#969): open a new thread only when the work needs its own branch, its own dev server, or its own conversation with you. To split one piece of work — research, reviews, sorting — ask Claude to use sub-agents inside the thread: no extra `claude` process (≈285 MB each) is started, and each sub-agent's start, end and full report still land in the thread ([sub-agent log](docs/specs/subagent-log.md)). Agent Teams (`CLAUDE_CODE_EXPERIMENTAL_AGENT_TEAMS`) is experimental and its teammates do not come back when c-lord restarts or resumes the session; do not use its split-pane display (`--teammate-mode tmux`), which adds panes to the window c-lord drives.

### Skills, models, commits

- **Skills** — `/skill` runs one of the Claude Code skills in `~/.claude/skills/`. The name autocompletes, and a skill added while the bot is running shows up within a minute. In a channel it opens a new thread; in a thread it continues that session.
- **Runtime model switching** — `/model` shows and changes the model and effort at session, channel or global scope (`include-existing` also moves open sessions from their next turn)
- **Who asked for a commit** — Commits Claude makes in a thread carry `Co-authored-by:` trailers for the Discord user who asked for that turn and for Claude. It is done by a git hook, not by asking Claude to remember; `CLORD_COAUTHOR=0` turns it off ([spec](docs/specs/commit-coauthors.md)).

## Workspaces

A **workspace** is everything that belongs to one thread: its Claude session, its clone, and any docker environment started from that clone.

- **One git clone per thread** — With a channel bound to a repo (`/clord-init`), each thread works in its own independent `git clone`; sessions share nothing but the git remote
- **Three levels of "off", each including the one before.** None of these operations deletes the conversation history or a docker volume ([vocabulary](docs/specs/workspace-vocabulary.md)):

  | | What stops | On its own | By hand |
  |---|---|---|---|
  | **Sleep** | the `claude` process (docker keeps running) | after 4 idle hours (`CLORD_IDLE_SLEEP_HOURS`) | — the next message wakes it without a word |
  | **Stop** | + the docker environment | after 7 idle days (`CLORD_IDLE_STOP_DAYS`) | `/workspace-stop`, undone by `/workspace-start` |
  | **Delete** | + the clone itself | after 30 unused days, see below | `/workspace-delete` |

- **A cap on how many stay awake.** Over `CLORD_MAX_RESIDENT_WORKSPACES` (worked out from the host's memory by default), the longest-idle workspaces are put to sleep. Starting a new one is never made to wait ([spec](docs/specs/resident-cap.md)).
- **Channel status** — `/clord-status` lists this channel's sessions with directory size, the `tmux attach` target, and the `claude --resume` command; `show_all` includes closed sessions (`docker ps -a` style). Supersedes the old `/sessions`, `/session-dirs`, `/resume-info`.
- **Workspace cleanup** — `/workspace-cleanup` reclaims clone directories that no running session is using (`dry_run` to preview); a directory holding work (uncommitted changes, unpushed commits, ignored files such as `.env`) is never removed, and the reply says which (#1016)
- **30-day tidy-up, announced** — At every start, workspaces unused for **30 days** are tidied. The period follows Claude Code's own transcript retention (`cleanupPeriodDays`), so by then Claude has usually forgotten the conversation anyway. The clone is removed only if it holds no uncommitted work; the record linking the thread to its session is kept. A thread that is still open gets a 🧹 notice saying what happened and what survived, and is then archived; a thread already closed is tidied without a post, so it stays closed. Writing in such a thread later reconnects to whatever is still on disk, or starts a new conversation in the same thread. Details: [Why a thread loses its record](docs/COMMANDS.md#why-a-thread-loses-its-record-the-30-day-sweep).
- **Clones share git objects.** Clones of the same repository borrow from one local mirror, so a new thread's clone takes seconds and little disk. Nothing changes in how you use git; `CLORD_GIT_MIRRORS=0` turns it off ([spec](docs/specs/git-object-mirrors.md)).

## Commands

Every command is described in **[docs/COMMANDS.md](docs/COMMANDS.md)**. The ones you will use most:

| Command | Where | What it does |
|---|---|---|
| `/clord-init repo:<url>` | channel | Bind the channel to a repository (no argument: show the bindings) |
| `/clord <prompt>` | channel | Open a thread and start a session; `repo:<url>` picks a repository for that thread |
| `/stop` | thread | Stop the running turn; the conversation is kept |
| `/clear` · `/compact` | thread | Start over in the same thread · summarise the context to free the window |
| `/skill <name> [args]` | channel or thread | Run a Claude Code skill |
| `/model` | anywhere | Show or change model and effort for the session, the channel or everything |
| `/clord-status` | anywhere | This channel's sessions, with the `tmux attach` target and `claude --resume` command |
| `/workspace-stop` · `/workspace-start` · `/workspace-delete` · `/workspace-cleanup` | thread / anywhere | The [workspace](#workspaces) operations |
| `/claude-restart` · `/resync` · `/tmux-screenshot` | thread | Restart the Claude process · reconnect the thread to its pane · post a picture of the pane |
| `/thread-rename` | thread | Re-summarise the thread's name |
| `/clord-reattach` | thread | Reconnect a tidied thread to what is still on disk |
| `/version` | anywhere | Which build is running |

Most slash commands also exist as a text command with `!` in front (`!clord`, `!stop`, …). Those are what a webhook can send — Discord does not let webhooks use slash commands — and they are how scripts and other agents start work: a webhook message beginning with `!clord`, posted to a channel that is bound to a repository, opens a session.

## Configuration

Everything is set in `.env`. Only the first two are required. [.env.example](.env.example) is a commented starting point, not the full list: some of the variables in the tables below are not in it, and it holds a few (for the test and monitoring tools) that are not listed here.

**Connection**

| Variable | What it does | Default |
|---|---|---|
| `DISCORD_BOT_TOKEN` | The bot's token | (required) |
| `DISCORD_CHANNEL_ID` | Channel ID for Claude chat. Like every ID setting here (`DISCORD_OWNER_ID`, `COORDINATION_CHANNEL_ID`, `EXPECTED_BOT_USER_ID`), ASCII digits only — a link, a name or `²` stops `c-lord start` with the setting's name and value (#1084); spaces around the digits are ignored (#1194) | (required) |
| `EXPECTED_BOT_USER_ID` | The bot's own user ID. If the process logs in as a different bot it exits at once — worth setting when several bots run on one host | (off) |

**Claude Code**

| Variable | What it does | Default |
|---|---|---|
| `CLAUDE_COMMAND` | The Claude Code executable to run | `claude` |
| `CLAUDE_MODEL` | Model for new sessions. `/model` changes it at runtime | `sonnet` |
| `CLAUDE_EFFORT` | Reasoning effort passed as `--effort` (`low` / `medium` / `high` / `xhigh` / `max`). Unset, no flag is passed | (CLI default) |
| `CLAUDE_PERMISSION_MODE` | Permission mode the sessions run in | `acceptEdits` |
| `CLAUDE_WORKING_DIR` | Directory for runs that bring no clone of their own (a scheduled task or webhook trigger without `working_dir`) | current directory |
| `CLORD_SESSION_PLUGINS` | `0` stops c-lord from leaving out the Discord plugin and unneeded language-server plugins when it starts a session ([spec](docs/specs/session-plugins.md)) | `true` |

**Who may use it**

| Variable | What it does | Default |
|---|---|---|
| `DISCORD_OWNER_ID` | User ID allowed to drive the bot, and @-mentioned when Claude needs input. Unset, the bot allows only the Discord application's owner (#713) | (optional) |
| `CLORD_ALLOWED_ROLE` | Discord role name whose members may drive the bot (OR with `DISCORD_OWNER_ID`) | (optional) |
| `CLORD_ALLOW_ANYONE` | `1` lets **every** member of the server drive the bot — which means running shell commands on the host. Warned about at startup | `false` |
| `CLORD_NOTIFY_DENIED` | `0` stops the DM the application owner gets when someone not allowed posts in a bot thread (the sender never sees anything; the refusal is still logged — #346) | `true` |
| `CLORD_HOST_LABEL` | What the login-outage DM calls this machine (#1309 — tell a second host apart) | host name |
| `CLORD_LOGIN_RELAY` | `0` keeps the login-outage DM but drops logging back in from it (sign-in link + code reply, #1310) | `true` |

**Limits and workspaces**

| Variable | What it does | Default |
|---|---|---|
| `MAX_CONCURRENT_SESSIONS` | Max **turns running at once**. Not a cap on resident `claude` processes — the semaphore wraps turn execution only and is released when the turn ends, while the tmux pane lives on. For the resident cap see `CLORD_MAX_RESIDENT_WORKSPACES` (#576). Must be a whole number **≥ 1** — `0`, negatives and non-numbers stop startup with the key named (#1119). A number of more than 18 digits is ignored with a WARNING naming the key, and the default is used (#1256). | `3` |
| `CLORD_MAX_RESIDENT_WORKSPACES` | Max workspaces holding a live `claude` at once. Over the cap, the longest-idle ones are put to sleep; creating a new workspace is never blocked. Unlike the idle TTLs this default **is** host-dependent, so it is computed from `MemTotal` (cgroup limit first) rather than shipped as a constant. `0` disables. See `docs/specs/resident-cap.md`. | auto (`max(2, MemTotal_GiB × 0.4 / 0.45)`) |
| `SESSION_TIMEOUT_SECONDS` | Session inactivity timeout. Whole number **≥ 1**, otherwise startup stops with the key named (#1119). More than 18 digits is ignored with a WARNING naming the key, and the default is used (#1256) | `300` |
| `CLORD_IDLE_SLEEP_HOURS` | Idle hours before a workspace sleeps (fractions allowed). `0` disables | `4` |
| `CLORD_IDLE_STOP_DAYS` | Idle days before a workspace is stopped. `0` disables | `7` |
| `CLORD_ORPHAN_SWEEP_DAYS` | Days before a clean, untouched clone that no record points at is removed. `0` disables | `30` |
| `SESSION_DIR_BASE` | Where the per-thread clones live. Do not move an existing one: `--resume` is keyed by the directory path | `~/.c-lord/<instance>/sessions` |
| `CLORD_INSTANCE` | The `<instance>` above | the clone's directory name |
| `CLORD_GIT_MIRRORS` | `0` stops clones of one repository from sharing git objects | `true` |

**Notifications and events**

| Variable | What it does | Default |
|---|---|---|
| `CLORD_OWNER_FALLBACK` | How far the owner fallback goes for turns nobody human asked for (webhook / CI / scheduler): `all` (turn-end 🟡 + pauses + failures), `blocked` (pauses + failures — quiet when it works, loud when it breaks), `off` (never, failures included) | `blocked` |
| `CLORD_EVENT_LOG` | Append one JSON line per turn end (`completion` / `blocked` / `failure`) to this file, after the last reply is posted — for a dispatching agent or CI to `tail -F` instead of polling the thread. Server-side only; see [docs/specs/turn-end-events.md](docs/specs/turn-end-events.md) (#917) | (off) |
| `CLORD_EVENT_WEBHOOK_URL` / `CLORD_EVENT_WEBHOOK_SECRET` | POST the same event as JSON to this URL, with `X-Clord-Signature: sha256=<HMAC-SHA256 of the body>`. Nothing is sent without the secret. Failures are retried a few times and never block the turn (#917) | (off) |
| `CLORD_EVENT_DEFER_MAX_SECONDS` | How long a `completion` is held while background sub-agents are still running before it is sent anyway | `3600` |
| `COORDINATION_CHANNEL_ID` | Channel that gets a one-line notice when a session's turn ends; also the default AI Lounge channel. A number longer than a Discord id (20 digits) is ignored with a startup WARNING naming the key, as if it were unset (#1256) | (optional) |

**Sub-agents**

| Variable | What it does | Default |
|---|---|---|
| `CLORD_SUBAGENT_LOG` | `0` stops the sub-agent log: one `🤖 … 開始` / `終了` line per run of a sub-agent, the end line carrying `subagent-*.md` (the full report, the assignment and every tool call), the start line's copy of it replaced while the agent works. See [docs/specs/subagent-log.md](docs/specs/subagent-log.md) (#971) | `true` |
| `CLORD_SUBAGENT_VOICES` | `1` also posts, **under the speaker's name**, the parent's assignments, every `SendMessage` between the parent and its agents, and each final report — so who told whom what can be followed in the thread. Uses a webhook when the bot has Manage Webhooks (not required: without it the name goes in front of the text). Tool calls stay in the attachment (#971) | `false` |
| `CLORD_SUBAGENT_PROGRESS_SECONDS` | How often a running sub-agent's attached progress file is replaced (only when it changed; minimum 10) | `60` |

**What the thread shows**

| Variable | What it does | Default |
|---|---|---|
| `CLORD_RENDER_TABLE_IMAGES` | Set to `1`, `true`, or `yes` to render GFM pipe tables as PNG images attached to Discord messages | (optional) |
| `CLORD_SHOW_URL_EMBEDS` | Set to `1`/`true`/`yes`/`on` to let Discord expand OGP/link-preview cards for URLs in Claude's replies. Off by default — replies stay compact (no preview card). | `false` |
| `CLORD_AUTO_TOPIC` | Set to `1`/`true`/`yes`/`on` to let c-lord summarise a **new** thread's name with an LLM (haiku) on its first message — the pre-#705 behaviour. Off by default: the thread keeps the name it was opened with, and `/thread-rename` (sonnet) re-summarises on demand. | `false` |
| `CLORD_SESSION_STATUS_BOARD` | Set to `1`/`true`/`yes`/`on` to show the 📊 Session Status board in `DISCORD_CHANNEL_ID` (#761). Off by default: a start posts nothing there and retires boards earlier starts left. The turn-end ping in the thread is not affected. | `false` |
| `CLORD_THREAD_RETITLE` | Set to `1`/`true`/`yes`/`on` to re-title a thread mid-conversation when an LLM judges the work changed (#121). Off by default (#414) — it renamed threads too eagerly. | `false` |
| `CLORD_THREAD_LAMP` | `1` adds a 🟢/🟡 lamp to the thread name (the sidebar view). Off by default because Discord limits thread renames hard; the reaction lamp on your message is always on ([spec](docs/specs/thread-lamp.md)) | `false` |
| `CLORD_TURN_PROGRESS` | `0` turns off the turn progress line; `CLORD_TURN_PROGRESS_QUIET_SECONDS` changes how long the thread must be quiet first | `true` / `90` |
| `CLORD_COAUTHOR` | `0` stops adding `Co-authored-by:` trailers to Claude's commits | `true` |

**Process and tmux**

| Variable | What it does | Default |
|---|---|---|
| `CLORD_TMUX_SCREENSHOT_ROWS` | Height, in rows, that `/tmux-screenshot` grows the window to before capturing. `0` captures it as it is | `100` |
| `CLORD_INSTANCE_ID` | Tells apart c-lord instances that share one tmux server. Set it only when two instances start from the same directory | the start directory's real path |
| `CLORD_SHUTDOWN_TIMEOUT_SECONDS` | After SIGTERM/SIGINT, how long the process may take to exit before it dumps its stacks to the log and exits with status 70. `0` disables | `10` |

The REST API has its own settings (`CLORD_API_HOST`, `CLORD_API_PORT`, `CLORD_API_URL`, `CLORD_API_SECRET`, `CLORD_API_ALLOW_ANY_PEER`) — see [REST API](#rest-api).

### URL Link Previews (CLORD_SHOW_URL_EMBEDS)

When Claude's reply contains a URL (e.g. a GitHub Issues link), Discord would normally expand it into a tall OGP preview card (title, description, image), pushing the actual answer down and cluttering the thread. By default c-lord **suppresses** these previews (the `SUPPRESS_EMBEDS` flag is set on each reply), so the link stays inline and the answer reads cleanly. Set `CLORD_SHOW_URL_EMBEDS=true` to restore Discord's default link-preview expansion.

### Table Image Rendering (CLORD_RENDER_TABLE_IMAGES)

When enabled, Markdown pipe tables in Claude's responses are rendered as PNG images using [Pillow](https://python-pillow.github.io/) and attached to the Discord message alongside the text. Each cell is split into text and emoji runs: **text is drawn with a CJK-capable font and emoji are drawn in full color** from a color emoji font, so 🟢/🔴 status lamps keep their color.

**Every message the mirror posts is covered — progress updates as well as the final answer** (#683). Discord draws no Markdown tables at all, so a table it does not render is a column of raw `|` characters, and intermediate messages stay in the thread after the turn ends. Applying this only to the final answer therefore left the reader unable to predict which tables would be readable.

**The image rides on the message the table is in.** A reply longer than 2000 characters is split into several messages; each table's PNG is attached to the message that contains that table (a table the split cuts between two rows is drawn whole, on the message where it starts). Before #750 every image went to the *last* message, so a table in the first half stayed as raw pipes there while its picture hung under an unrelated paragraph further down. `progress.txt` still goes on the last message.

**At most 10 tables per message become images.** That is Discord's attachment ceiling, and exceeding it fails the whole send — text included — so tables past the tenth stay as raw markdown rather than costing the message. The limit is per message, not per turn: each progress message carries its own allowance. When the reply also carries `progress.txt`, that file takes one slot and the tables get the other nine.

**Inline markdown inside cells is collapsed to plain text** before drawing — the image cannot be made interactive, so leaving the raw syntax in would just leak noise. `[label](url)` / `![alt](url)` become `label` (the URL is unclickable in an image and only adds clutter), and `**bold**` / `*italic*` / `` `code` `` keep only their inner text. Underscore emphasis (`_x_`, `__x__`) is intentionally left untouched so identifiers such as `_is_allowed` / `__init__` are not mangled.

**`~~strike~~` is the exception: it is drawn as a real strikethrough rule.** A picture *can* show a line through the text, and Claude uses strikethrough in tables to mean "this row is the one being dropped" — Discord strikes it in the message body, so collapsing it to plain text in the image would make the two disagree and invert the meaning of the table. The rule follows the text across wrapped lines and applies to part of a cell (`~~a~~ b`) as well as the whole of it.

**Installation:**

```bash
uv add "c-lord[table]"
# or: pip install "c-lord[table]"
```

> The same `[table]` extra also powers **`/tmux-screenshot`** — a debug command
> that posts a PNG of the current tmux pane (ANSI colors, layout, and status
> lamps preserved). Without the extra, the command replies with an install hint
> instead of an image.

**Font support (CJK text + color emoji):**

c-lord renders text and emoji separately, picking the first font found in each group:

| Font file path | Group / Notes |
|---|---|
| `~/.local/share/fonts/NotoSansJP.ttf` | **Text** — recommended for Japanese |
| `/usr/share/fonts/opentype/noto/NotoSansCJK-Regular.ttc` | Text — covers JP / ZH / KO |
| `/usr/share/fonts/truetype/dejavu/DejaVuSans.ttf` | Text — last-resort (Latin only) |
| `~/.local/share/fonts/NotoColorEmoji.ttf` | **Color emoji** — 🟢🔴🟡✅⚠️ render in full color |
| `/usr/share/fonts/truetype/noto/NotoColorEmoji.ttf` | Color emoji — alternative system path |
| `~/.local/share/fonts/NotoEmoji-Regular.ttf` | Monochrome emoji — fallback if no color font (no color, but no tofu) |

If no color emoji font is present, c-lord falls back to a monochrome emoji font (glyph shapes, no color), then to drawing the raw character. Use the **CBDT** Noto Color Emoji — Pillow renders it with `embedded_color=True`.

> **Seeing empty boxes (□) instead of Japanese text in a screenshot?** That's a
> missing font on *this* machine/user, not a bug — none of the paths above
> were found, so rendering fell back to a Latin-only font. It's a per-user,
> per-host install (`~/.local/share/fonts/`), so it won't carry over to a
> different Linux user account or a fresh host even if it already works
> elsewhere.
>
> **If you're not comfortable running terminal commands yourself, that's
> fine — just hand the block below to your AI assistant** (your Claude Code
> session, or the c-lord Claude thread itself) and ask it to run it for you.
> It's written to be safe for an AI to execute unattended: it only writes to
> your own home directory, needs no `sudo`, and touches nothing outside
> `~/.local/share/fonts/`.

**Quick install for Japanese + color emoji (Linux):**
```bash
mkdir -p ~/.local/share/fonts
# Japanese text
curl -Lo ~/.local/share/fonts/NotoSansJP.ttf \
  https://github.com/notofonts/noto-cjk/raw/main/Sans/OTF/Japanese/NotoSansCJK-Regular.ttc
# Color emoji (CBDT)
curl -Lo ~/.local/share/fonts/NotoColorEmoji.ttf \
  https://github.com/googlefonts/noto-emoji/raw/main/fonts/NotoColorEmoji.ttf
fc-cache -fv
```

Long cells are wrapped to a bounded width (`MAX_COL_WIDTH`, display-width aware so CJK counts double) so wide tables stay readable on mobile.

> **Note for other CJK languages (Chinese, Korean, Arabic, Thai, etc.):** install a font that covers the target script (e.g. Noto Sans SC for Simplified Chinese, Noto Sans KR for Korean) and add its path to the `_JP_FONT_PATHS` list in `c_lord/discord_ui/table_renderer.py`. Contributions adding multi-language font detection are welcome.

## Scheduled tasks

A scheduled task is a prompt c-lord runs for you at a fixed interval. c-lord keeps only the *when*; everything about *what* — what to check, what to post, how not to repeat itself — is in the prompt. Tasks live in SQLite and are added, changed and removed through the [REST API](#rest-api) while the bot runs, so neither a code change nor a restart is involved. The usual way to make one is to ask Claude in a thread to register it; Claude has the API address in `CLORD_API_URL`:

```bash
curl -X POST "$CLORD_API_URL/api/tasks" \
  -H "Content-Type: application/json" \
  -d '{
        "name": "weekly-deps",
        "prompt": "Check for outdated dependencies and open an issue if there are any",
        "interval_seconds": 604800,
        "channel_id": 123456789012345678
      }'
```

`name`, `prompt`, `interval_seconds` and `channel_id` are all required, and the channel must be bound to a repository with `/clord-init`; `working_dir` and `run_immediately` are optional. `PATCH /api/tasks/{id}` with `{"enabled": false}` pauses a task without deleting it. One loop wakes every 30 seconds and starts whatever is due; a task whose previous run is still going is skipped, not doubled.

Each run posts a `🔄 **[Scheduled]** <task name>` message, opens a thread on it, creates a tmux window in the task's `working_dir`, and runs Claude there — the answer lands in the thread like any other turn. What a scheduled run should look like (and what counts as a bug) is written up in [docs/specs/scheduled-tasks.md](docs/specs/scheduled-tasks.md).

## GitHub and CI automation

Anything that can post to a Discord webhook can hand work to Claude. There are two ways, depending on who writes the prompt.

**The sender writes the prompt.** A webhook message that starts with `!clord`, in a channel bound to a repository, opens a session exactly as `/clord` would. Holding the webhook URL is the permission, so treat the URL as a secret.

**The prompt is fixed on your side.** `WebhookTriggerCog` maps a message prefix to a prompt you defined in code. The webhook can only choose *which* trigger fires; it cannot supply text for Claude to act on. This is the safer choice for CI. The cog is not registered by default — add it in [your own bot](#using-c-lord-as-a-library):

```python
from c_lord import ClaudeConfig, WebhookTrigger, WebhookTriggerCog

docs_sync = WebhookTrigger(
    prompt="Read the latest commits on main and update the docs to match. Open a PR.",
    working_dir="/srv/checkouts/my-project",
    timeout=900,  # seconds
)

await bot.add_cog(
    WebhookTriggerCog(
        bot=bot,
        runner=ClaudeConfig(model="sonnet"),
        triggers={"🔄 docs-sync": docs_sync},      # message prefix → what to run
        channel_ids={123456789012345678},          # only listen here
        allowed_webhook_ids={234567890123456789},  # only this webhook
    )
)
```

A `WebhookTrigger` skips Claude Code's permission checks unless you pass `dangerously_skip_permissions=False`, because nobody is there to answer a prompt — which is one more reason to restrict `channel_ids` and `allowed_webhook_ids`.

**What happens on a trigger:** the webhook message gets a thread, Claude runs in its own tmux window in the trigger's `working_dir` (or the runner's), and its answer is posted into that thread. If the window cannot be created (e.g. tmux is unavailable on the host), the run does not start: the thread says why, the webhook message gets ❌, and the bot owner is mentioned (#629).

### Example: Automated Documentation Sync (implement in your own bot)

> **Note:** This is a pattern you can implement in your own bot — c-lord does not bundle documentation-sync workflows. For a reference implementation, see [EbiBot](https://github.com/ebibibi/discord-bot).

On every push to `main`, your bot can trigger Claude Code to:
1. Pull the latest changes and analyze the diff
2. Update English documentation
3. Translate to Japanese (or any target languages)
4. Create a PR with a bilingual summary
5. Enable auto-merge — merges automatically when CI passes

**GitHub Actions (add to your repo):**

```yaml
# .github/workflows/docs-sync.yml
on:
  push:
    branches: [main]
jobs:
  ask-claude:
    # Claude's own docs commits carry [docs-sync]; without this line they would fire the workflow again
    if: "!contains(github.event.head_commit.message, '[docs-sync]')"
    runs-on: ubuntu-latest
    steps:
      - name: Post the trigger to Discord
        env:
          WEBHOOK: ${{ secrets.DISCORD_WEBHOOK_URL }}
        run: |
          curl --fail -sS -H "Content-Type: application/json" \
            --data '{"content": "🔄 docs-sync"}' "$WEBHOOK"
```

The bot side is the `WebhookTriggerCog` shown above, with `🔄 docs-sync` as the trigger. When Claude's own PR merges into `main`, that push would fire the workflow again. The `if:` line stops the loop, provided your prompt tells Claude to put `[docs-sync]` in its commit messages.

### Hearing back when a turn ends

A script that dispatched work does not have to poll the thread. With `CLORD_EVENT_LOG` or `CLORD_EVENT_WEBHOOK_URL` set, c-lord emits one event per finished turn — `completion`, `blocked` or `failure` — after the last reply has been posted ([spec](docs/specs/turn-end-events.md)).

### Upgrading the bot from a webhook

`AutoUpgradeCog` is for a bot that has c-lord installed as a package. Like `WebhookTriggerCog`, you add it yourself:

```python
from c_lord import AutoUpgradeCog, UpgradeConfig

await bot.add_cog(
    AutoUpgradeCog(
        bot,
        UpgradeConfig(
            package_name="c-lord",
            working_dir="/srv/my-bot",
            restart_command=["systemctl", "--user", "restart", "my-bot.service"],
            restart_approval=True,
            slash_command_enabled=True,
        ),
    )
)
```

When a webhook message starting with `trigger_prefix` (default `🔄 upgrade`) arrives, the cog opens a thread and reports each step there:

1. It runs `uv lock --upgrade-package <package_name>` and then `uv sync` in `working_dir` (replace either with `upgrade_command` / `sync_command`).
2. It notes which threads have a turn running, then waits for running work to finish — every cog that exposes an `active_count` property (the `DrainAware` protocol) is asked, your own included. After `drain_timeout` (300 s) it stops waiting.
3. It runs `restart_command`. Without one, nothing is restarted and the thread says the upgrade is complete.

After the restart, the threads noted in step 2 get a quiet one-line notice that c-lord restarted. Claude is **not** prompted again: the `claude` processes live in tmux and were never stopped, so there is nothing to resume (#406).

`upgrade_approval=True` asks before step 1, and `restart_approval=True` asks once the new version is installed, before steps 2 and 3; either is answered with a ✅ reaction in the upgrade thread or the Approve button. `slash_command_enabled=True` adds `/upgrade`, which starts the same pipeline without a webhook. Only users allowed to drive the bot can run it. It works in a channel or in a thread — run in a thread, the upgrade thread is opened in the parent channel — and if an upgrade is already running it tells you so.

A c-lord that runs from its own clone needs none of this: `git pull && systemctl --user restart c-lord.service`.

## REST API

c-lord's control plane for notifications, scheduled tasks and spawning
sessions. It starts on every run — it is not tied to how answers are delivered
(#712/#543). Requires aiohttp:

```bash
uv add "c-lord[api]"
```

It binds `CLORD_API_HOST:CLORD_API_PORT` (default `127.0.0.1:8080`). With
`CLORD_API_PORT` **unset**, a taken port is not a problem: the API moves to the
next free one (8081, 8082, … then any free port) and logs where it went, so two
clones on one host both get an API with nothing to configure (#258). A port you
**set** is used as-is: if it is taken the bot logs a WARNING and runs without the
API rather than failing to start. A value that is not ASCII digits (`²`, a word)
is ignored with a WARNING naming `CLORD_API_PORT`, as if it were unset; spaces
around the digits are ignored (#1218). A number past 65535 is a port you set that
cannot be used: a WARNING names `CLORD_API_PORT` and the bot runs without the API
(#1237). Likewise an entry of `CLORD_TRUSTED_BOT_IDS` that is not ASCII digits, or
is longer than a Discord id (20 digits), is ignored with a startup WARNING, and the other ids in
the list keep working. That includes full-width digits (`１２３`), which used to be
read as the id 123 — write the id in ASCII digits.

Claude in every session is told where the API is as the `CLORD_API_URL`
environment variable — the port that was actually bound, so it never has to be
kept in step with `CLORD_API_PORT` by hand. Set `CLORD_API_URL` yourself only to
point sessions somewhere else (a reverse proxy, say). When the API is not
running, sessions get no `CLORD_API_URL` at all.

**It only serves the Unix user running the bot** (#457). `POST /api/spawn` starts
a Claude Code session, so reaching this port is equivalent to a shell as that
user — and loopback is a *network* boundary, not a *UID* one: every account on a
multi-user host can connect to `127.0.0.1`. So the peer's UID is read from
`/proc/net/tcp` and checked against the bot's own, `/api/health` included;
everyone else gets `403`. Nothing to configure — your own calls keep working as
they are. To let another user or another host in, set `CLORD_API_SECRET` and
have them send `Authorization: Bearer …`. See [docs/SECURITY.md](docs/SECURITY.md#rest-api-exposure-712-457).

| Method | Path | What it does |
|---|---|---|
| GET | `/api/health` | Liveness check |
| POST | `/api/spawn` | Open a new thread and start Claude in it with `prompt` (optional `channel_id`, `thread_name`). Answers as soon as the thread exists |
| POST | `/api/threads/{thread_id}/messages` | Post `content` into a thread |
| POST | `/api/threads/{thread_id}/sleep` | Put that thread's workspace to sleep, as the idle sleep would |
| POST | `/api/notify` | Post a notification (`message`, optional `title`, `color`, `channel_id`) |
| POST | `/api/schedule` | Post a notification later (`message`, `scheduled_at` in ISO 8601) |
| GET | `/api/scheduled` | List notifications that are still waiting |
| DELETE | `/api/scheduled/{id}` | Cancel one |
| POST | `/api/tasks` | Register a [scheduled task](#scheduled-tasks) |
| GET | `/api/tasks` | List the tasks |
| PATCH | `/api/tasks/{id}` | Change a task: `enabled`, `prompt`, `interval_seconds`, `working_dir` |
| DELETE | `/api/tasks/{id}` | Delete a task |
| POST | `/api/mark-resume` | Record a thread before you restart the bot yourself, so it gets the restart notice on the next start |
| GET | `/api/lounge` | Read recent AI Lounge messages (sessions aren't told about the lounge, so nothing posts here unless you set that up — #758) |
| POST | `/api/lounge` | Post a message to the AI Lounge (with optional `label`) — not called by c-lord's sessions on their own (#758) |

Every field, status code and edge case is in [docs/COMMANDS.md](docs/COMMANDS.md#rest-api). Two examples, run as the bot's own Unix user so no header is needed:

```bash
# Start a session from a script — the answer comes back with the thread's ID
curl -X POST http://127.0.0.1:8080/api/spawn \
  -H "Content-Type: application/json" \
  -d '{"prompt": "Review the newest open PR and summarise the risks", "thread_name": "PR review"}'

# Tell the home channel that something happened
curl -X POST http://127.0.0.1:8080/api/notify \
  -H "Content-Type: application/json" \
  -d '{"title": "CI", "message": "Nightly build is green"}'
```

## Using c-lord as a library

c-lord is an ordinary Python package, and `c-lord start` is just one program built on it. To put it inside a bot of your own — with your own cogs next to it — install it and call `setup_bridge()`:

```bash
uv add git+https://github.com/yousan/c-lord.git
```

```python
import asyncio
import os

from c_lord import ClaudeConfig, setup_bridge
from c_lord.bot import ClaudeDiscordBot


async def main() -> None:
    channel_id = int(os.environ["DISCORD_CHANNEL_ID"])
    owner_id = int(os.environ["DISCORD_OWNER_ID"])

    bot = ClaudeDiscordBot(channel_id=channel_id, owner_id=owner_id)
    config = ClaudeConfig(model="sonnet", permission_mode="acceptEdits")

    async with bot:
        await setup_bridge(
            bot,
            config,
            claude_channel_id=channel_id,
            allowed_user_ids={owner_id},
        )
        # await bot.add_cog(YourCog(bot))   ← your own cogs go here
        await bot.start(os.environ["DISCORD_BOT_TOKEN"])


asyncio.run(main())
```

`setup_bridge()` registers c-lord's standard cogs — nine with the call above: chat, channel-to-repository binding, skills, workspace commands, the 30-day tidy-up, the scheduler, the transcript mirror, the login relay and `/version`. That is deliberate: a standard feature added to c-lord later reaches your bot when you update the package, with no change to your code. `WebhookTriggerCog` and `AutoUpgradeCog` are not among them; add those two yourself with `bot.add_cog()`, as shown above.

```bash
uv lock --upgrade-package c-lord && uv sync
```

Use `ClaudeDiscordBot` as the bot class — c-lord's cogs read things it carries, such as the home channel. The standalone entry point, [`c_lord/main.py`](c_lord/main.py), is the reference for the rest: reading `.env`, the single-instance locks, starting the REST API and shutting down on a signal.

## Architecture

### How Discord maps to tmux

The "Discord thread = Claude Code session" idea sits on top of a tmux layout. Knowing where a thread actually lives makes debugging and integration much easier (you can `tmux attach` to watch a session live):

| Unit | tmux | Mapping |
|------|--------------|---------|
| Repository a thread is bound to | tmux session | 1:1 |
| Thread | tmux window | 1:1 |

- **Sessions are only ever born in a channel.** `/clord` opens a *new* thread; inside a thread it continues that thread's session, offers to reconnect one whose record was swept, or refuses if the thread was never c-lord's (#551). An ordinary conversation thread cannot be turned into a Claude session — there is no command that adopts one, by design. See [docs/COMMANDS.md](docs/COMMANDS.md#chat--sessions).
- **1 repository = 1 tmux session — not 1 channel.** The session name comes from the repo a thread is bound to: its own when it has one (`/clord-thread-init`, or `/clord repo:<url>`), otherwise the channel's (`/clord-init`); unbound channels fall back to the default `clord` session. So a channel whose threads are bound to different repos is spread over several sessions, and two channels bound to the same repo share one. Where a given thread lives — and why it is decided this way — is spelled out in [docs/specs/tmux-layout.md](docs/specs/tmux-layout.md).
- **1 thread = 1 tmux window = 1 Claude Code session.** Each thread gets its own window (`w1`, `w2`, …) running one Claude Code session. So your back-and-forth in a thread *is* the back-and-forth with that Claude session — replies continue it via `--resume`. Window numbers are unique **within a session**, not within a channel.

For the "why" behind this design, see [docs/PHILOSOPHY.md](docs/PHILOSOPHY.md).

### How an answer reaches Discord

```text
you, in a Discord thread                      Claude Code, in that thread's tmux window
        │                                                    │
        │  your message is typed into the pane  ────────────▶│
        │                                                    │  works: reads, edits, runs tools
        │                                                    ▼
        │                          ~/.claude/projects/<dir>/<session>.jsonl
        │                          (the transcript Claude Code writes by itself)
        │                                                    │
        ◀──── c-lord tails that file and posts what it says ─┘
```

c-lord does not read Claude's answer off the terminal, and it does not ask Claude to post it. It reads the transcript Claude Code already keeps and forwards that. Screen-scraping kept leaking bits of the terminal UI into Discord; asking Claude to post meant a whole turn went missing whenever it forgot. The transcript has neither problem, and it is the only route an answer takes.

The pane is still watched, but not for text: to notice that a turn ended, to turn a menu Claude Code shows (`AskUserQuestion`, plan approval) into Discord buttons, to accept Claude Code's permission prompts, and to report a prompt c-lord does not recognise. [docs/ARCHITECTURE.md](docs/ARCHITECTURE.md) walks through all of this, and [docs/DESIGN_DECISIONS.md](docs/DESIGN_DECISIONS.md) records why.

### Where things are

```text
c_lord/
  cli.py            the `c-lord` command: setup, start, version
  main.py           standalone entry point — reads .env, takes the locks, starts everything
  setup.py          setup_bridge(): registers the standard cogs in one call
  bot.py            ClaudeDiscordBot
  tmux.py           tmux sessions and windows; starting claude, typing into the pane
  session_dir.py    one git clone per thread
  claude/           per-turn runner over the tmux pane, CLI settings, context usage
  transcript/       tailing Claude Code's transcript and shaping it for Discord; sub-agent log
  cogs/             chat, channel↔repo binding, skills, workspace commands, scheduler,
                    webhook triggers, auto-upgrade, transcript mirror
  discord_ui/       status lamp, question buttons, turn progress line, table and pane images
  database/         SQLite: sessions, bindings, scheduled tasks, notifications, pending menus
  ext/api_server.py REST API
  skills/           the discord-read skill placed in each session
deploy/             the systemd unit
scripts/            install-systemd.sh, staging.sh, evidence and release tooling
docs/               guides, and docs/specs/ — how each feature should behave
```

### What c-lord is, and is not

- **A bridge, not an AI.** c-lord has no prompts, tools or memory of its own. All of that is Claude Code's, and whatever you have set up for Claude Code — `CLAUDE.md`, skills, MCP servers, hooks — works in a thread because it is the same program.
- **The real CLI, not the API.** Each thread runs the interactive `claude` you would run in a terminal. c-lord never calls the Anthropic API.
- **tmux stays.** c-lord does not replace tmux; it lets you manage tmux from Discord. The pane is always there to attach to.
- **Sessions that outlive connections.** Claude runs on the server. Your laptop sleeping, your train entering a tunnel, or the bot restarting does not stop it.
- **You judge from Discord, so Discord has to show enough to judge.** Progress, results, files and failures all land in the thread, and a failure says what failed and what to do next rather than going quiet.
- **Nothing to configure for the common case.** New features are on by default with sensible settings, and updating the package is all a consumer does.

The reasoning behind these is in [docs/PHILOSOPHY.md](docs/PHILOSOPHY.md) (Japanese).

## Security

Talking to c-lord means running commands on the machine it lives on. The defaults assume that.

- **User authorization** — `allowed_user_ids` / `CLORD_ALLOWED_ROLE` restrict who can invoke Claude. **With neither configured, only the Discord application's own owner may use the bot** — talking to c-lord runs shell commands on its host, so "not configured" is not "everyone" (#713). `CLORD_ALLOW_ANYONE=1` opens it to the whole server on purpose (and says so at startup). See [docs/specs/authorization-default.md](docs/specs/authorization-default.md)
- **The REST API answers only the bot's own Unix user** unless you set `CLORD_API_SECRET` (see [REST API](#rest-api)).
- **No shell in between.** Every process c-lord starts is started with an argument list (`asyncio.create_subprocess_exec`), never through a shell, and a prompt is passed after a `--` so it cannot be read as a flag.
- **Checked before use.** A session ID must look like one before it reaches `--resume`; skill names and repository URLs are validated the same way.
- **The bot token does not leak into sessions by inheritance.** It is removed from the tmux environment and from the `claude` command line's environment. This prevents accidents, not a determined session: `.env` is a file the same Unix user can read.

[docs/SECURITY.md](docs/SECURITY.md) has the threat model and the risks that are accepted on purpose.

## Development

```bash
git clone https://github.com/yousan/c-lord.git
cd c-lord
uv sync --dev
make setup                                # once per clone: turns on the pre-commit hook

uv run pytest tests/ -v --cov=c_lord
uv run ruff check c_lord/ && uv run ruff format --check c_lord/
uv run pyright c_lord/
```

CI runs those checks on Python 3.10, 3.11 and 3.12. They use mocks, so green CI does not show that a change works against a real Discord and a real tmux — which is why a change also has to be shown working on a staging bot before it merges. [CONTRIBUTING.md](CONTRIBUTING.md) explains what a pull request needs; [docs/STAGING.md](docs/STAGING.md) explains the staging bots.

## How this project is built

**This codebase is developed by [Claude Code](https://docs.anthropic.com/en/docs/claude-code)**, Anthropic's AI coding agent. The original foundation, [`claude-code-discord-bridge`](https://github.com/ebibibi/claude-code-discord-bridge), was created by [@ebibibi](https://github.com/ebibibi) (Masahiko Ebisuda) and provided this project with its initial architecture under the MIT License. This derivative is now maintained by [@yousan](https://github.com/yousan), who defines requirements, reviews pull requests, and approves all changes — Claude Code does the implementation.

**Thank you, [@ebibibi](https://github.com/ebibibi).** c-lord started from the code of [claude-code-discord-bridge](https://github.com/ebibibi/claude-code-discord-bridge), which Masahiko Ebisuda published under the MIT License. Its copyright notice and license text are kept in [NOTICE](NOTICE).

Day to day that looks like this. A change starts as an Issue with acceptance criteria. Claude Code — usually running in a c-lord thread, so c-lord is built with c-lord — writes a failing test, makes it pass, shows the change working on a staging bot with screenshots, and opens a pull request. Two required checks guard `main`, and a pull request merges only when every item of the [Definition of Done](CLAUDE.md#definition-of-done-dod--single-source-of-truth) is met.

c-lord's own history begins on 2026-02-22 with a copy of that upstream project. Bug reports and pull requests are welcome, from people and from agents alike.

## Inspired by

- [claude-code-discord-bridge](https://github.com/ebibibi/claude-code-discord-bridge) by [@ebibibi](https://github.com/ebibibi) — The upstream foundation this project is based on. See [EbiBot](https://github.com/ebibibi/discord-bot) for ebibibi's personal bot built on the same framework (docs sync, push notifications, Todoist watchdog, scheduled health checks, GitHub Actions CI/CD).
- [OpenClaw](https://github.com/openclaw/openclaw) — Emoji status reactions, message debouncing, fence-aware chunking
- [claude-code-discord-bot](https://github.com/timoconnellaus/claude-code-discord-bot) — CLI spawn + stream-json approach
- [claude-code-discord](https://github.com/zebbern/claude-code-discord) — Permission control patterns
- [claude-sandbox-bot](https://github.com/RhysSullivan/claude-sandbox-bot) — Thread-per-conversation model

## License

[MIT](LICENSE). The copyright notice and license of the code c-lord started from are in [NOTICE](NOTICE).
