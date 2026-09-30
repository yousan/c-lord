# Architecture

> **この文書の一部はまだ #53 以前の構成を説明しています。** #724 で **Overview の図・
> Claude CLI 層 / Discord UI 層の表・Data Flow・Dependency Graph** はいまの経路
> （tmux ペイン常駐 + jsonl transcript ミラー配信 + ask bridge）に描き直しました。
> **Concurrency Model / Extension Points はまだ当時の `ClaudeRunner` 前提の記述**が残っています
> （いまの実体は `claude/tmux_runner.py` の `TmuxClaudeRunner`）。
>
> **今のモジュール構成（あるべき動き／実態）を知りたいときは、`CLAUDE.md` の
> "Project Structure" と "Key Design Decisions" を一次情報として参照してください。**

## Overview

c-lord is a thin UI layer that bridges Discord messages to the Claude Code CLI. It has no AI logic of its own — all intelligence comes from Claude Code's existing capabilities (CLAUDE.md, skills, tools, memory, MCP servers). The bridge's responsibility is: accept user input from Discord, type it into an interactive `claude` running in a tmux pane, and deliver Claude's answer back to Discord **by reading Claude Code's own transcript** (`~/.claude/projects/<slug>/*.jsonl`), not by scraping the TUI (#71/#712).

```
┌─────────────────────────────────────────────────────────┐
│                    Discord (Gateway)                     │
│  ┌──────────┐  ┌──────────┐  ┌──────────────────────┐  │
│  │ Channel   │  │ Threads  │  │ Reactions / Buttons  │  │
│  └─────┬────┘  └────┬─────┘  └──────────┬───────────┘  │
└────────┼────────────┼───────────────────┼───────────────┘
         │            │                   ▲  answer text, attachments,
         ▼            ▼                   │  turn progress line (jsonl)
┌──────────────────────────────────────────────────────────────┐
│              discord.py Bot (bot.py)                          │
│  ┌────────────────┐  ┌──────────────────┐                     │
│  │ ClaudeChatCog  │  │ SkillCommandCog  │                     │
│  └───────┬────────┘  └───────┬──────────┘                     │
│          └─────────┬─────────┘                                │
│                    ▼                                          │
│          ┌──────────────────┐   ┌──────────────────────────┐  │
│          │ _run_helper.py   │   │ TranscriptMirrorCog      │  │
│          │ + EventProcessor │   │ (transcript/mirror.py)   │  │
│          │ (status lamp,    │   │ tails the jsonl → posts  │  │
│          │  ask bridge,     │   │ the answer (the ONLY     │  │
│          │  errors, DB)     │   │ delivery path, #712)     │  │
│          └────────┬─────────┘   └────────────▲─────────────┘  │
│                   ▼                          │                │
│          ┌──────────────────┐                │                │
│          │ tmux_runner.py   │ yields SYSTEM / RESULT only     │
│          │ + tmux.py        │ (#723)         │                │
│          └────────┬─────────┘                │                │
└───────────────────┼──────────────────────────┼────────────────┘
                    │ send-keys / capture-pane │ reads
                    ▼                          │
┌──────────────────────────────────────────────┼──────────┐
│  tmux window (one per thread) — resident     │          │
│  claude --session-id <uuid>  /  --resume <uuid>         │
│   └─ writes ~/.claude/projects/<slug>/<uuid>.jsonl ─────┘
│  ┌─────────────────────────────────────────────────┐    │
│  │ CLAUDE.md, skills, tools, memory, MCP servers   │    │
│  │ (all inherited from the host environment)       │    │
│  └─────────────────────────────────────────────────┘    │
└─────────────────────────────────────────────────────────┘
```

## Module Responsibilities

### Entry Points

| Module | Role |
|--------|------|
| `main.py` | Standalone entry point. Loads `.env`, initializes DB, creates components, starts bot. For users who run this as their only bot. |
| `__init__.py` | Public API surface. Exports `ClaudeChatCog`, `ClaudeRunner`, `SessionRepository`, and all types needed by consumers who embed this into their own bot. |
| `bot.py` | `ClaudeDiscordBot` — minimal `commands.Bot` subclass. Configures intents (message_content, guilds), stores `channel_id`, syncs slash commands on ready. Only used in standalone mode. |

### Cogs Layer (`cogs/`)

| Module | Class | Role |
|--------|-------|------|
| `claude_chat.py` | `ClaudeChatCog` | Core message handler. Listens for `on_message` in the configured channel and its child threads. Creates threads for new conversations, resumes sessions for thread replies. Manages concurrency via `asyncio.Semaphore`. Provides `/clear` slash command, which types Claude Code's own `/clear` into the pane (#803). |
| `skill_command.py` | `SkillCommandCog` | Provides `/skill` and `/skills` slash commands. Scans `~/.claude/skills/` at startup, parses YAML frontmatter from `SKILL.md` files, offers Discord autocomplete. Creates a thread and delegates to `_run_helper`. |
| `_run_helper.py` | `run_claude_with_config()` | Shared function extracted to avoid duplicating the Claude CLI run logic between ClaudeChatCog and SkillCommandCog. Drives one turn: iterates the runner's events and hands them to `EventProcessor` (status lamp, ask bridge, error embeds, session persistence). **It never posts the answer text** — that is the transcript mirror's job (#712). |
| `event_processor.py` | `EventProcessor` | Turns the runner's SYSTEM / RESULT events into Discord side effects: saves the session record, bridges `pane_ask` menus to buttons, sets the 🟢/🟡/❌ lamp, posts error / usage-limit embeds. |
| `transcript_mirror.py` | `TranscriptMirrorCog` | `start_for(thread_id, working_dir)` tails the thread's Claude Code transcript (`transcript/mirror.py`) and posts assistant text, attachments (`SendUserFile`) and the turn progress line. The **only** path Claude's answer takes to Discord (#71/#712). |

### Claude CLI Layer (`claude/`)

| Module | Class/Function | Role |
|--------|---------------|------|
| `tmux_runner.py` | `TmuxClaudeRunner` | Per-turn runner. Starts `claude` in the thread's tmux window if it is not running (`tmux.start_claude`), otherwise types the prompt into the resident pane (`tmux.send_input`). Polls `capture-pane` only to detect turn end, open menus and blocking prompts; **yields SYSTEM (`session_id` / `pane_ask` / `unknown_tui_prompt`) and RESULT (done / error / usage limit) events only** (#723). No answer text is read off the pane. |
| `types.py` | `StreamEvent`, `ToolUseEvent`, `SessionState`, enums | Type definitions. `MessageType` (system/assistant/user/result), `ContentBlockType` (text/tool_use/tool_result), `ToolCategory` (read/edit/command/web/think/other). `TOOL_CATEGORIES` maps Claude Code tool names to categories. `ToolUseEvent.display_name` provides human-readable descriptions. |

### tmux / Transcript Layer (`tmux.py`, `transcript/`)

| Module | Class/Function | Role |
|--------|---------------|------|
| `tmux.py` | `TmuxSessionManager` | One tmux session per repo, one window per thread (`create_session`). `start_claude` launches `claude --session-id <uuid>` (or `--resume <claimed uuid>`) with secrets `env -u`'d (#353/#773); `send_input` types the prompt with a zero-width-space marker (#71). |
| `transcript/claim.py` | — | Records the uuid c-lord gave the session in `<project_dir>/.clord-session`, so the mirror knows which jsonl belongs to the thread (#773). |
| `transcript/mirror.py` | — | Tails that jsonl and turns assistant entries into posts; `cogs/transcript_mirror.py` sends them, split by `discord_ui/reply_chunker.py`. |

### Database Layer (`database/`)

| Module | Class/Function | Role |
|--------|---------------|------|
| `models.py` | `init_db()` | Schema definition and initialization. Single `sessions` table with `thread_id` (PK), `session_id`, `working_dir`, `model`, timestamps. Uses `datetime('now', 'localtime')` for timestamps. |
| `repository.py` | `SessionRepository` | CRUD operations. `get()` by thread_id, `save()` with upsert, `delete()`, `cleanup_old()` for age-based cleanup (marks rows swept rather than deleting them — #818; `get()` and the lists skip swept rows, `get_swept()` returns them). Each operation opens and closes its own `aiosqlite` connection (simple, no connection pooling). |

### Discord UI Layer (`discord_ui/`)

| Module | Class/Function | Role |
|--------|---------------|------|
| `status.py` | `StatusManager` | Emoji reaction lamp on the user's trigger message: 🟢 running (turn start, kept through thinking/tools) → 🟡 waiting (turn done), with ❌ error / ⏳⚠️ stall as temporary overrides. Applied immediately (no debounce) — the lamp changes only a couple of times per turn, and reactions use a different rate-limit bucket than thread renames. This replaced the per-turn thread-name lamp that saturated Discord's ~2/10min rename limit (#246); the thread-name 🟢/🟡 is now a slow, poll-driven sidebar view that is **off by default** (#329 — opt in with `CLORD_THREAD_LAMP=1`, see `docs/specs/thread-lamp.md`). Includes stall detection: soft (⏳) at 10s, hard (⚠️) at 30s. **`set_compact()` (🗜️) is still in the code but unreachable** — it only runs when a SYSTEM event carries `StreamEvent.is_compact`, and nothing sets that field, so no 🗜️ reaction has ever been added (#753). The compaction users actually see is the mirror's one-line `🗜️ コンテキストを圧縮しました` (#628). |
| `reply_chunker.py` | — | Splits the mirrored answer into Discord-sized messages (fence-aware). |
| `embeds.py` | `session_start_embed()`, `ask_embed()`, etc. | Discord embed builders. Color-coded: blurple for info, green for success, red for error, yellow for tool use. Consistent visual language across all bot output. **`tool_use_embed()` (and `tool_timer.py`, which renders it) is currently unreachable** — nothing sets `StreamEvent.tool_use` any more, so no tool-use embed has ever been posted (#723). Tool activity reaches Discord through the jsonl mirror instead: the turn progress line (`turn_progress.py`) and the `progress.txt` attachment. |

### Utilities (`utils/`)

| Module | Function | Role |
|--------|----------|------|
| `logger.py` | `setup_logging()` | Configures root logger with timestamp format. Silences discord.py's verbose logging (`WARNING` level). |

## Data Flow

### New Conversation

```
1. User sends message in configured channel
   │
2. on_message() in ClaudeChatCog
   │
3. _handle_new_conversation()
   ├── Create Discord thread (name = topic read off the message —
   │   one line, no markdown/URLs, … when cut; #721)
   │
4. _run_claude()
   ├── Check semaphore (post "waiting" if full)
   ├── StatusManager on the user's message → 🟢
   ├── session_dir: git clone into c-lord-sessions/<ch>/<thread>/
   │   (+ prepare-commit-msg co-author hook, #518)
   ├── resolve_tmux_manager(channel_id, thread_id=…) → tmux window w<N>
   ├── TranscriptMirrorCog.start_for(thread_id, working_dir)  ← delivery
   │
5. run_claude_with_config() → EventProcessor
   │
6. TmuxClaudeRunner.run(prompt)
   ├── claude not running in the window:
   │     tmux.start_claude → send-keys `claude --session-id <uuid> … -- <prompt>`
   │     (uuid recorded in .clord-session, #773; secrets env -u'd, #353)
   ├── claude already resident:
   │     tmux.send_input → send-keys the prompt (+ ZWSP marker, #71)
   ├── poll capture-pane (turn end / menus / blocking prompts only)
   │
7. Events yielded by the runner (these two kinds only — #723):
   ├── SYSTEM {session_id}          → save session record to DB
   ├── SYSTEM {pane_ask}            → AskUserQuestion / plan menu → Discord buttons
   ├── SYSTEM {unknown_tui_prompt}  → tell the thread an unknown prompt is blocking
   ├── RESULT {done | error | usage limit} → lamp 🟡 / ❌, error embed
   │
   Meanwhile, independently of the runner:
   └── TranscriptMirrorCog reads <uuid>.jsonl → posts the answer text,
       attachments, and the "⚙️ 作業中" progress line (#712)
   │
8. Turn ends
   ├── Lamp 🟢 → 🟡 on the trigger message
   └── claude stays resident in the pane (no process is killed)
```

### Thread Reply (Session Resume)

```
1. User replies in existing thread
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

### Skill Execution

```
1. User invokes /skill goodmorning
   │
2. SkillCommandCog.run_skill()
   ├── Validate skill name (regex)
   ├── Look up in loaded skills list
   ├── Defer interaction
   ├── Create thread named "/goodmorning"
   │
3. run_claude_in_thread(prompt="/goodmorning", session_id=None)
   │
4. Same streaming flow as new conversation
   └── Claude Code interprets "/goodmorning" as a skill invocation
```

## Concurrency Model

```
                    ┌──────────────────┐
                    │   Semaphore(N)   │  N = MAX_CONCURRENT_SESSIONS (default 3)
                    │                  │  = turns running at once, NOT resident claude
                    │                  │  processes (that is CLORD_MAX_RESIDENT_WORKSPACES,
                    │                  │  #576 — released when the turn ends)
                    └────────┬─────────┘
                             │
          ┌──────────────────┼──────────────────┐
          ▼                  ▼                  ▼
   ┌─────────────┐   ┌─────────────┐   ┌─────────────┐
   │ Thread #1   │   │ Thread #2   │   │ Thread #3   │
   │ Runner (A)  │   │ Runner (B)  │   │ Runner (C)  │
   │ claude proc │   │ claude proc │   │ claude proc │
   └─────────────┘   └─────────────┘   └─────────────┘
```

- Each Claude CLI invocation gets its own `ClaudeRunner` instance via `clone()`.
- The `_active_runners` dict tracks runners by thread_id for kill-on-demand (`/clear`).
- The semaphore prevents resource exhaustion — excess requests queue with a "waiting" message.
- All I/O is async (asyncio subprocess, aiosqlite), so the event loop is never blocked.

## Extension Points

### For Framework Consumers (Package Users)

1. **Custom Cogs**: Import `ClaudeChatCog` and `SkillCommandCog`, add to your own `commands.Bot`. Add your own Cogs alongside them.
2. **Custom Runner Configuration**: `ClaudeRunner` accepts `command`, `model`, `permission_mode`, `working_dir`, `timeout_seconds`, `allowed_tools`, `dangerously_skip_permissions`.
3. **Selective Imports**: `__init__.py` exports individual components — use only what you need. Import `parse_line` and `chunk_message` for custom pipelines.
4. **run_claude_in_thread()**: Can be called from any Cog or async context. Needs a `Thread`, `ClaudeRunner`, `SessionRepository`, and a prompt.

### For Framework Contributors

1. **New Cog**: Follow CONTRIBUTING.md pattern. Use `_run_helper.run_claude_in_thread()` for Claude execution.
2. **New Tool Category**: Add to `ToolCategory` enum, update `TOOL_CATEGORIES` mapping in `types.py`, add emoji in `status.py` `CATEGORY_EMOJI` and `embeds.py` `CATEGORY_ICON`.
3. **New Event Type**: Add to `MessageType` enum, add `_parse_xxx()` function in `parser.py`, handle in `_run_helper.py` event loop.
4. **New Embed Type**: Add builder function in `embeds.py`, call from `_run_helper.py`.

## Dependency Graph

```
c_lord/
  __init__.py ──────────┬──→ claude/tmux_runner.py ──→ tmux.py
                        ├──→ claude/types.py
                        ├──→ cogs/claude_chat.py ──→ _run_helper.py ──→ event_processor.py
                        │                          └→ transcript_mirror.py ──→ transcript/,
                        │                                                     discord_ui/reply_chunker.py
                        ├──→ cogs/skill_command.py ──→ _run_helper.py
                        ├──→ database/repository.py
                        ├──→ discord_ui/status.py
                        └──→ discord_ui/embeds.py

  main.py ──→ bot.py, setup.py (wires the Cogs), models, repository, logger

External:
  discord.py (Gateway, commands, app_commands)
  aiosqlite (async SQLite)
  tmux (the resident claude panes)
  python-dotenv (env loading, standalone mode only)
```

Key design constraint: `claude/` and `discord_ui/` have zero dependencies on each other. The `cogs/` layer is the only place where CLI state meets Discord rendering — `_run_helper.py` / `EventProcessor` for runner events, `TranscriptMirrorCog` for the answer text. This keeps the runner testable without Discord mocks and the UI components testable without tmux.
