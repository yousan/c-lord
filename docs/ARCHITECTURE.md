# c-lord Architecture

How the pieces of c-lord fit together today: which process does what, what travels between
Discord, tmux and Claude Code, and where to look in the code.

> **一次情報は `CLAUDE.md`。** モジュールの一覧は `CLAUDE.md` の "Project Structure"、
> それぞれの設計判断の経緯は "Key Design Decisions" と [`DESIGN_DECISIONS.md`](DESIGN_DECISIONS.md)、
> 利用者から見た個々の動きは [`specs/`](specs/README.md) にある。この文書は全体のつながりを説明する。
> #53 以前の構成（`claude -p` を毎回起動して stream-json を読む `ClaudeRunner`）の説明は、
> #724 と #1315 で取り除いた。

## What c-lord is

c-lord is a thin UI layer that bridges Discord messages to the Claude Code CLI. It has no AI logic of its own — all intelligence comes from Claude Code's existing capabilities (CLAUDE.md, skills, tools, memory, MCP servers). The bridge's responsibility is: accept user input from Discord, type it into an interactive `claude` running in a tmux pane, and deliver Claude's answer back to Discord **by reading Claude Code's own transcript** (`~/.claude/projects/<slug>/*.jsonl`), not by scraping the TUI (#71/#712).

Three ideas explain most of what follows.

1. **One Discord thread = one Claude Code session = one tmux window.** The thread is the
   conversation; the window holds a `claude` process that stays resident between turns.
2. **Input is typed, output is read.** c-lord types the prompt into the pane with `tmux send-keys`.
   It never reads the answer back off the screen: Claude Code writes every turn to a jsonl
   transcript, and c-lord tails that file.
3. **The pane is only watched for *state*.** `capture-pane` is polled to learn that a turn has
   ended, that a menu is open, or that something is blocking — never for answer text.

## The parts and what flows between them

```
 Discord ── channel, threads, reactions, buttons
    │  messages, button clicks              ▲  answer text, attachments,
    ▼                                       │  turn progress line (jsonl)
 ┌─ discord.py Bot (bot.py) ──────────────────────────────────────────────┐
 │                                                                        │
 │   ClaudeChatCog / SkillCommandCog / WebhookTriggerCog / SchedulerCog   │
 │                    │                                                   │
 │                    ▼                                                   │
 │          ┌──────────────────┐   ┌──────────────────────────┐           │
 │          │ _run_helper.py   │   │ TranscriptMirrorCog      │           │
 │          │ + EventProcessor │   │ (transcript/mirror.py)   │           │
 │          │ (status lamp,    │   │ tails the jsonl → posts  │           │
 │          │  ask bridge,     │   │ the answer (the ONLY     │           │
 │          │  errors, DB)     │   │ delivery path, #712)     │           │
 │          └────────┬─────────┘   └────────────▲─────────────┘           │
 │                   ▼                          │                         │
 │          ┌──────────────────┐                │                         │
 │          │ tmux_runner.py   │ yields SYSTEM / RESULT only              │
 │          │ + tmux.py        │ (#723)         │                         │
 │          └────────┬─────────┘                │                         │
 └───────────────────┼──────────────────────────┼─────────────────────────┘
                     │ send-keys / capture-pane │ reads
                     ▼                          │
 ┌─ tmux window (one per thread) — resident ────┼─────────────────────────┐
 │  claude --session-id <uuid>  /  --resume <uuid>                        │
 │   └─ writes ~/.claude/projects/<slug>/<uuid>.jsonl ────────────────────┘
 │  Claude Code runs here with everything the host gives it: the repo's   │
 │  CLAUDE.md, skills, tools, memory, MCP servers. c-lord adds none.      │
 └────────────────────────────────────────────────────────────────────────┘
```

Beside this main loop sits the **REST API** (`ext/api_server.py`), a control plane that Claude
sessions and outside systems call into (`$CLORD_API_URL`) to register scheduled tasks, send
notifications or start a session. It is not part of how an answer is delivered.

## Modules by layer

`CLAUDE.md` lists every file. The tables below name the ones that carry the architecture.

### Starting up

| Module | What it does |
|--------|--------------|
| `main.py` | The standalone entry point (`python -m c_lord.main`, or `c-lord start`). Reads `.env`, builds a `ClaudeConfig`, the bot and the API server, calls `setup_bridge()`, runs the startup sweep of old sessions, then connects. |
| `setup.py` | `setup_bridge(bot, config, …)` registers every Cog and starts every background loop in one call. A consumer who embeds c-lord in their own bot calls this and nothing else — which is what lets a new Cog reach them through a package upgrade alone. |
| `bot.py` | `ClaudeDiscordBot`, a small `commands.Bot` subclass: intents (message content, guilds), slash-command sync, and a `process_commands` that lets webhook messages reach text commands. Used in standalone mode. |
| `cli.py` | The `c-lord` command: `setup` (a wizard that writes `.env`), `start`, `version`. |
| `__init__.py` | The public API — what consumers import: `setup_bridge`, `ClaudeConfig`, the Cogs, the repositories, `TmuxClaudeRunner`, the value types. |

### Cogs (`cogs/`) — where Discord meets the session

| Module | Class | What it does |
|--------|-------|--------------|
| `claude_chat.py` | `ClaudeChatCog` | Core message handler. `/clord <prompt>` (and `spawn_session()`, which `POST /api/spawn` also uses) creates the thread for a new conversation; `on_message` continues the session for replies in a thread c-lord already tracks. A plain message typed in the channel itself starts nothing — threads are opened on purpose, not by accident. Manages concurrency via `asyncio.Semaphore`. Provides `/clear` slash command, which types Claude Code's own `/clear` into the pane (#803). |
| `skill_command.py` | `SkillCommandCog` | `/skill` (and its text twin `!skill`). Reads `SKILL.md` frontmatter under `~/.claude/skills/`, reloading at most every 60 s, and offers the names as Discord autocomplete. Opens a thread and runs `/<name> [args]` in it through `_run_helper`. |
| `_run_helper.py` | `run_claude_with_config()` | Shared function extracted to avoid duplicating the Claude CLI run logic between ClaudeChatCog and SkillCommandCog. Drives one turn: iterates the runner's events and hands them to `EventProcessor` (status lamp, ask bridge, error embeds, session persistence). **It never posts the answer text** — that is the transcript mirror's job (#712). |
| `event_processor.py` | `EventProcessor` | Turns the runner's SYSTEM / RESULT events into Discord side effects: saves the session record, bridges `pane_ask` menus to buttons, sets the 🟢/🟡/❌ lamp, posts error / usage-limit embeds. |
| `transcript_mirror.py` | `TranscriptMirrorCog` | `start_for(thread_id, working_dir)` tails the thread's Claude Code transcript (`transcript/mirror.py`) and posts assistant text, attachments (`SendUserFile`) and the turn progress line. The **only** path Claude's answer takes to Discord (#71/#712). |
| `channel_repo.py` | `ChannelRepoCog` | `/clord-init` and `/clord-thread-init` bind a channel or a thread to a git repository. `resolve_manager()` / `resolve_tmux_manager(channel_id, thread_id=…)` turn that binding into the session-dir manager and the tmux session a thread uses. |
| `session_manage.py` | `SessionManageCog` | The operator commands: `/clord-status`, `/workspace-*`, `/tmux-*`, `/model` and friends. |
| `webhook_trigger.py`, `scheduler.py`, `auto_upgrade.py` | — | Turns that no human typed: a webhook line that selects a server-side prompt, a row in `scheduled_tasks` that came due, a package upgrade. The first two run Claude through the same `_run_helper`. |

### Claude CLI (`claude/`)

| Module | Class/Function | What it does |
|--------|----------------|--------------|
| `tmux_runner.py` | `TmuxClaudeRunner` | Per-turn runner. Starts `claude` in the thread's tmux window if it is not running (`tmux.start_claude`), otherwise types the prompt into the resident pane (`tmux.send_input`). Polls `capture-pane` only to detect turn end, open menus and blocking prompts; **yields SYSTEM (`session_id` / `pane_ask` / `unknown_tui_prompt`) and RESULT (done / error / usage limit) events only** (#723). No answer text is read off the pane. |
| `config.py` | `ClaudeConfig` | The settings a consumer hands to `setup_bridge()`: `command`, `model`, `permission_mode`, `working_dir`, `timeout_seconds`, `dangerously_skip_permissions`, `effort`. It holds configuration only; execution is the runner's. |
| `types.py` | `StreamEvent`, `MessageType`, `AskQuestion`, `UsageLimit`, … | The value types a runner event is made of. Several `StreamEvent` fields (`tool_use`, `todo_list`, `permission_request`, `elicitation`, `is_compact`) are kept for import compatibility but nothing sets or reads them (#723/#1316). |

### tmux / Transcript Layer (`tmux.py`, `transcript/`)

| Module | Class/Function | What it does |
|--------|----------------|--------------|
| `tmux.py` | `TmuxSessionManager` | One tmux session per repo, one window per thread (`create_session`). `start_claude` launches `claude --session-id <uuid>` (or `--resume <claimed uuid>`) with secrets `env -u`'d (#353/#773); `send_input` types the prompt as it is — a leading `/`/`!` gets one space so it stays text, and no invisible marker (#861). |
| `transcript/claim.py` | — | Records the uuid c-lord gave the session in `<project_dir>/.clord-session`, so the mirror knows which jsonl belongs to the thread (#773). |
| `transcript/mirror.py` | — | Tails that jsonl and turns assistant entries into posts; `cogs/transcript_mirror.py` sends them, split by `discord_ui/reply_chunker.py`. |
| `transcript/formatter.py` | — | Renders one jsonl event as Discord text — including the `🔧 Bash: …` lines folded into `progress.txt`. |
| `session_dir.py` | `SessionDirManager` | Gives each thread its own git clone (`<base>/<channel_id>/<thread_id>/`) and refreshes it every turn, together with the co-author commit hook (#518) and the injected `discord-read` skill (#259). |

### Database (`database/`)

SQLite through `aiosqlite`, one repository class per table. Every method opens its own
connection and closes it again; there is no pool to manage.

| Module | Class/Function | What it does |
|--------|----------------|--------------|
| `models.py` | `init_db()` | Creates and migrates the schema. The central table is `sessions`: `thread_id` (primary key), `session_id`, `working_dir`, `model`, the thread's topic and state, and `created_at` / `last_used_at` stamped with `datetime('now', 'localtime')`. |
| `repository.py` | `SessionRepository` | CRUD operations. `get()` by thread_id, `save()` with upsert, `delete()`, `cleanup_old()` for age-based cleanup (marks rows swept rather than deleting them — #818; `get()` and the lists skip swept rows, `get_swept()` returns them). Each operation opens and closes its own `aiosqlite` connection (simple, no connection pooling). |
| `channel_repo.py`, `thread_repo.py` | — | Which repository a channel / a thread is bound to. |
| `task_repo.py`, `notification_repo.py` | — | Scheduled tasks (`SchedulerCog`) and scheduled notifications (REST API). |
| the others | — | State that must survive a restart: pending questions, pending resumes, bridged menus, settings, the dev-environment link. |

### Discord UI (`discord_ui/`)

| Module | Class/Function | What it does |
|--------|----------------|--------------|
| `status.py` | `StatusManager` | Emoji reaction lamp on the user's trigger message: 🟢 running (turn start, kept through thinking/tools) → 🟡 waiting (turn done), with ❌ error / ⏳⚠️ stall as temporary overrides. Applied immediately (no debounce) — the lamp changes only a couple of times per turn, and reactions use a different rate-limit bucket than thread renames. This replaced the per-turn thread-name lamp that saturated Discord's ~2/10min rename limit (#246); the thread-name 🟢/🟡 is now a slow, poll-driven sidebar view that is **off by default** (#329 — opt in with `CLORD_THREAD_LAMP=1`, see `docs/specs/thread-lamp.md`). Includes stall detection: soft (⏳) at 10s, hard (⚠️) at 30s. There is no compaction lamp: the `set_compact()` (🗜️) that used to live here was never reached — nothing sets `StreamEvent.is_compact` (#753) — and was removed in #1316. The compaction users actually see is the mirror's one-line `🗜️ コンテキストを圧縮しました` (#628). |
| `reply_chunker.py` | — | Splits the mirrored answer into Discord-sized messages (fence-aware). |
| `embeds.py` | `session_start_embed()`, `ask_embed()`, etc. | Discord embed builders. Color-coded: blurple for info, green for success, red for error. Consistent visual language across all bot output. There are no tool-use / thinking / todo / plan / permission / elicitation embeds: nothing sets the `StreamEvent` fields they rendered, so none was ever posted (#723), and the builders — with `tool_timer.py`, `permission_view.py`, `plan_view.py` and `elicitation_view.py` — were removed in #1316. Tool activity reaches Discord through the jsonl mirror instead: the turn progress line (`turn_progress.py`) and the `progress.txt` attachment. |
| `ask_handler.py`, `ask_view.py` | — | The ask bridge: a menu Claude Code shows in the pane (AskUserQuestion, plan approval) becomes Discord buttons, and the click is typed back into the pane (#166/#251). |
| `authorization.py` | `Authorizer`, `AuthorizedViewMixin` | The one rule for who may drive c-lord, shared by every message gate, command and button (#466/#713). See [`SECURITY.md`](SECURITY.md). |
| `turn_progress.py` | — | The self-updating `⚙️ 作業中 …` line that fills a long silence during a turn (#539). |

### REST API and logging

| Module | What it does |
|--------|--------------|
| `ext/api_server.py` | The control plane. Started on every run; if its port is taken it logs a WARNING and the bot carries on without it. Endpoints cover notifications, scheduled tasks, the lounge, `POST /api/spawn` and posting into a thread — see [`COMMANDS.md`](COMMANDS.md). Who may call it is decided by `ext/peer_uid.py` (same Unix user) or a bearer secret (#457). |
| `utils/logger.py` | `setup_logging()` installs the root handler with the timestamped format and keeps discord.py's own loggers at `WARNING`. `log_ctx()` builds the `[thread=… session=…]` prefix that makes one thread's history greppable. |

## What happens in a turn

### A new conversation

```
1. A person runs `/clord <prompt>` in a channel (or its text twin `!clord`, or a caller
   uses POST /api/spawn). A plain message in the channel does not start a session.
   │
2. ClaudeChatCog — authorized? → spawn_session()
   ├── Create Discord thread (name = topic read off the prompt —
   │   one line, no markdown/URLs, … when cut; #721)
   ├── post the prompt into it as the seed message (the lamp needs a message to sit on)
   │
3. _run_claude()
   ├── semaphore: all slots busy → post "⏳ Waiting for a free session slot..."
   ├── StatusManager on the trigger (seed) message → 🟢
   ├── session_dir: git clone into c-lord-sessions/<ch>/<thread>/
   │   (+ prepare-commit-msg co-author hook, #518)
   ├── resolve_tmux_manager(channel_id, thread_id=…) → tmux window w<N>
   ├── TranscriptMirrorCog.start_for(thread_id, working_dir)  ← delivery
   │
4. run_claude_with_config() → EventProcessor
   │
5. TmuxClaudeRunner.run(prompt)
   ├── claude not running in the window:
   │     tmux.start_claude → send-keys `claude --session-id <uuid> … -- <prompt>`
   │     (uuid recorded in .clord-session, #773; secrets env -u'd, #353)
   ├── claude already resident:
   │     tmux.send_input → send-keys the prompt (no marker, #861)
   ├── poll capture-pane (turn end / menus / blocking prompts only)
   │
6. Events yielded by the runner (these two kinds only — #723):
   ├── SYSTEM {session_id}          → save session record to DB
   ├── SYSTEM {pane_ask}            → AskUserQuestion / plan menu → Discord buttons
   ├── SYSTEM {unknown_tui_prompt}  → tell the thread an unknown prompt is blocking
   ├── RESULT {done | error | usage limit} → lamp 🟡 / ❌, error embed
   │
   Meanwhile, independently of the runner:
   └── TranscriptMirrorCog reads <uuid>.jsonl → posts the answer text,
       attachments, and the "⚙️ 作業中" progress line (#712)
   │
7. Turn ends
   ├── Lamp 🟢 → 🟡 on the trigger message
   └── claude stays resident in the pane (no process is killed)
```

Permission prompts never reach Discord: the runner accepts them inside the pane.

### A reply in an existing thread

```
1. A person replies in the thread
   │
2. on_message() → _handle_thread_reply()
   ├── repo.get(thread_id) → session record
   │
3. _run_claude() — same as above (semaphore, lamp, session_dir, window, mirror)
   │
4. TmuxClaudeRunner.run(prompt)
   ├── claude still resident in the window (the usual case):
   │     send_input → the prompt is typed into the same conversation
   ├── pane has no claude (bot restart, window gone, …):
   │     start_claude → `claude --resume <uuid from .clord-session>` (#773)
   │
5. Same event flow as above; the answer arrives via the jsonl mirror
```

### A skill

```
1. /skill name:<skill> [args]
   │
2. SkillCommandCog
   ├── authorized? → skill name matches ^[\w-]+$ ? → is it one of the loaded skills?
   ├── acknowledge the interaction, open a thread named "/<skill> [args]"
   │
3. run_claude_with_config(prompt="/<skill> [args]")
   │
4. The turn runs exactly like a new conversation. Because the text is sent as a
   command, Claude Code itself resolves "/<skill>" to the skill.
```

### Turns nobody typed

`WebhookTriggerCog` (a webhook message that starts with a configured trigger prefix),
`SchedulerCog` (a `scheduled_tasks` row that is due; one master loop checks every 30 s) and
`POST /api/spawn` all end in the same place: a thread, a tmux window, a mirror, and
`run_claude_with_config()`. What differs is where the prompt comes from — server-side
configuration, the database, or the API caller — and that there is no person to mention, so
notices fall back to the owner (`notify_policy.py`).

## Concurrency and lifetimes

```
        MAX_CONCURRENT_SESSIONS (default 3)
        ┌──────────────────────────────┐
        │  asyncio.Semaphore(N)        │   = turns running at once, NOT resident claude
        │                              │     processes (that is CLORD_MAX_RESIDENT_WORKSPACES,
        └──────────────┬───────────────┘     #576 — released when the turn ends)
           ┌───────────┼───────────┐
           ▼           ▼           ▼
       thread A     thread B     thread C        (a 4th turn waits for a slot)
       window w1    window w2    window w3
       claude       claude       claude          each stays resident after its turn
```

- **A turn holds a slot; a session does not.** The semaphore bounds how many turns run at the
  same moment. A turn that cannot get a slot tells its thread it is waiting. When the turn ends
  the slot is released, but the `claude` process stays in its window.
- **One runner per thread.** `ClaudeChatCog` builds a `TmuxClaudeRunner` for the thread's window
  on each turn and keeps the running ones in `_active_runners`, keyed by thread id, so that Stop,
  `/stop` and a newer message can reach the turn in flight.
- **Residents are bounded by time and by memory, not by the semaphore.** Idle workspaces are put
  to sleep (4 h, `idle_sleep.py`), stopped (7 d, `idle_stop.py`), and capped as a backstop
  (`resident_cap.py`); see [`specs/workspace-vocabulary.md`](specs/workspace-vocabulary.md).
- **Nothing blocks the event loop.** Discord and SQLite I/O are async; `tmux` and `git` are
  blocking subprocess calls with a timeout, made through `asyncio.to_thread`.
- **All windows live in one tmux server.** Whatever the session name, killing the server kills
  every thread's Claude at once — which is why verification against real tmux must use
  `tmux -L <socket>` ([`STAGING.md`](STAGING.md)).

## Extending c-lord

### If you embed c-lord in your own bot

1. **Call `setup_bridge()`.** It registers c-lord's Cogs on your `commands.Bot` and returns the
   repositories it created. Add your own Cogs next to them; you should never need to copy, wrap
   or subclass c-lord's.
2. **Configure through `ClaudeConfig`** (model, permission mode, working directory, timeout,
   effort) and through the environment variables in `.env.example`.
3. **Run Claude from your own Cog** with `run_claude_with_config(RunConfig(...))` — a thread, a
   `TmuxClaudeRunner` and a prompt are the minimum. `run_claude_in_thread()` remains as the older
   positional form of the same call.
4. **Import only what you use.** The package root exports the individual Cogs, repositories and
   types.

### If you change c-lord itself

1. **A new Cog** — follow `CONTRIBUTING.md` and the `add-cog` skill; register it in
   `setup_bridge()`; run Claude through `_run_helper`, never a copy of it.
2. **How a tool is shown**: tool activity is rendered from the jsonl by `transcript/formatter.py` (the `progress.txt` lines) and `discord_ui/turn_progress.py` (the progress line) — not from `ToolCategory` / `TOOL_CATEGORIES` in `types.py`, which nothing reads any more (#1316).
3. **Something new the pane can say** (a prompt, a banner, an end-of-turn shape) — teach
   `tmux_runner.py` to recognise it from a real captured pane (`tests/fixtures/panes/`), yield it
   as a SYSTEM or RESULT event, and handle it in `EventProcessor`.
4. **Something new the transcript can say** (a tool, an attachment, a system row) — handle the
   jsonl event in `transcript/formatter.py` / `transcript/mirror.py`;
   [`specs/mirrored-events.md`](specs/mirrored-events.md) lists what is mirrored and what is not.
5. **A new embed** — add the builder to `discord_ui/embeds.py` and post it from `EventProcessor`
   or the Cog that owns the moment.

## What depends on what

```
c_lord/
  __init__.py ──────────┬──→ claude/tmux_runner.py ──→ tmux.py
                        ├──→ claude/config.py, claude/types.py
                        ├──→ cogs/claude_chat.py ──→ _run_helper.py ──→ event_processor.py
                        │                          └→ transcript_mirror.py ──→ transcript/,
                        │                                                     discord_ui/reply_chunker.py
                        ├──→ cogs/skill_command.py ──→ _run_helper.py
                        ├──→ database/repository.py
                        ├──→ discord_ui/status.py
                        └──→ discord_ui/embeds.py, setup.py

  main.py ──→ bot.py, setup.py (wires the Cogs), models, repository, logger

Outside the package:
  discord.py      gateway, commands, app_commands
  aiosqlite       async SQLite
  python-dotenv   reads .env (standalone mode)
  aiohttp         the REST API (optional extra "api")
  Pillow, emoji   table / pane images (optional extra "table")
  tmux            the resident claude panes
  claude          the Claude Code CLI itself
```

The layering to keep: `cogs/` is where a session's state meets Discord rendering —
`_run_helper.py` / `EventProcessor` for runner events, `TranscriptMirrorCog` for the answer text.
The runner (`claude/tmux_runner.py`, `tmux.py`) never posts to Discord. `discord_ui/` builds what
Discord shows from value types (`claude/types.py`) and reaches a pane only through the runner or
tmux manager it is handed — the ask bridge typing a clicked answer is the main case. Keeping it
that way is what lets the runner be tested without Discord mocks and most UI components without
tmux.
