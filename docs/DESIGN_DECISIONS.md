# Design Decisions

This document captures the "why" behind key architectural choices. Each decision includes the alternatives considered and the reasoning for the chosen approach.

## 1. CLI Subprocess, Not Direct API

**Decision:** Invoke `claude -p --output-format stream-json` as a subprocess rather than calling the Anthropic API directly.

**Alternatives considered:**
- Direct Anthropic API (HTTP/SDK) — full control over requests, lower latency
- Claude Code as a library import — tighter integration, shared memory

**Why CLI:**
- **Inherits everything for free**: CLAUDE.md, skills, tools, memory, MCP servers, hooks — all the features that make Claude Code powerful work automatically because we spawn the real CLI
- **No feature tracking burden**: When Claude Code adds new features, they work immediately through the bridge without any code changes
- **Proven stability**: The CLI handles all the complex tool orchestration, permission management, and error recovery. Reimplementing this via direct API would be thousands of lines of fragile code
- **Claude Code is the product**: We're building a UI for Claude Code, not an alternative to it

**Trade-offs accepted:**
- Higher latency (subprocess spawn overhead ~200ms)
- Larger resource footprint (one OS process per session)
- Parsing stream-json instead of getting structured responses directly

## 2. Thread = Session (1:1 Mapping)

**Decision:** Each Discord thread maps to exactly one Claude Code session via `--resume`.

**Alternatives considered:**
- Single shared session across all threads — simpler, but no conversation isolation
- Session per user — better isolation, but can't have parallel conversations
- Session per channel — too coarse, mixes different topics

**Why thread = session:**
- **Natural UX**: Discord users already understand threads as "one conversation about one thing"
- **Visual history**: Scroll up in a thread to see the full conversation — no separate history viewer needed
- **Session resume**: `--resume session_id` continues exactly where you left off, including Claude Code's internal state (opened files, tool history, etc.)
- **Parallel work**: Different threads = different projects = different Claude Code sessions running concurrently

**Implementation:** SQLite `sessions` table with `thread_id` as primary key, `session_id` as the Claude Code session identifier. Simple UPSERT on save.

## 3. Shared Run Helper (_run_helper.py)

**Decision:** Extract Claude CLI execution logic into a shared function used by both `ClaudeChatCog` and `SkillCommandCog`.

**Alternatives considered:**
- Duplicate the streaming logic in each Cog — simpler initially, but maintenance nightmare
- Base class inheritance — too rigid, Cogs have different `on_message` vs slash command patterns
- Event-based pub/sub — over-engineered for two consumers

**Why shared function:**
- **DRY without coupling**: Both Cogs call `run_claude_in_thread()` but remain independent in how they receive input (message listener vs slash command)
- **Single place to fix bugs**: Status emoji logic, chunking, error handling — all in one place
- **Easy to extend**: New Cogs that need Claude execution just call the same function (e.g. a consumer docs-sync Cog can implement its own streaming logic on top of the shared function for different requirements)

## 4. Emoji Reactions for Status

**Decision:** Use Discord message reactions (🧠🛠️💻🌐✅❌) on the user's original message to show Claude's current activity.

**Alternatives considered:**
- Editing a "status message" in the thread — more visible but hits Discord rate limits fast
- Typing indicator — limited to "is typing", can't show what's happening
- Embed updates — rich info but very noisy in the thread

**Why reactions:**
- **Non-intrusive**: Doesn't clutter the thread with status updates
- **Glanceable**: One emoji tells you what's happening without reading text
- **Low API cost**: Adding/removing reactions is lightweight compared to message edits
- **Mobile-friendly**: Reactions are prominently visible in Discord mobile

**Implementation details:**
- Debounced at 700ms to avoid rate limits during rapid tool switches
- Only one status emoji at a time (old removed before new added)
- Stall detection: ⏳ after 10s of no activity, ⚠️ after 30s
- Cleanup on completion — reactions removed after brief display of ✅/❌

## 5. Fence-Aware Message Chunking

**Decision:** Never split a Discord message inside a code fence. If forced to split, properly close the fence and reopen it in the next chunk.

**Alternatives considered:**
- Naive split at 2000 chars — breaks code blocks, ugly rendering
- Split only at paragraph boundaries — may produce very uneven chunks
- Render to image — overkill, not searchable

**Why fence-aware:**
- **Claude Code outputs lots of code**: A bridge for a coding tool must handle code blocks correctly
- **Broken fences look terrible**: Half a code block renders as monospace plain text, confusing users
- **Proper reopening**: `\`\`\`python\n...` is closed with `\`\`\`` and reopened in the next chunk with `\`\`\`python\n`, preserving syntax highlighting

**Split preference order:**
1. Paragraph break (blank line) — cleanest visual break
2. Line break — keeps lines intact
3. Hard split at limit — last resort, fence repair kicks in

## 6. Installable Package, Not Monolith

**Decision:** Ship `c_lord` as a proper Python package installable via `uv add git+...` or `pip install git+...`.

**Alternatives considered:**
- Monolithic bot — easier to start, harder to customize
- Docker image — isolated but heavyweight for a Python script
- Copy-paste template — no upgrade path

**Why package:**
- **Separation of concerns**: The framework handles Discord↔CLI bridging. The consumer handles project-specific config, secrets, and custom Cogs
- **Upgrade path**: `uv lock --upgrade-package c-lord && uv sync` gets you the latest framework without touching your custom code
- **No conflict**: Your bot's `pyproject.toml` pins the framework version. Multiple bots can use different versions
- **Real-world validation**: Consumer bots demonstrate this works — they import `ClaudeChatCog`, `ClaudeRunner`, `SkillCommandCog`, and add their own Cogs (reminders, watchdog, docs-sync, auto-upgrade) without touching the framework

## 7. No Custom AI Logic

**Decision:** The bridge has zero AI logic. No prompt engineering, no tool definitions, no memory management, no system prompts.

**Alternatives considered:**
- Add a system prompt to shape Claude's Discord responses — tempting but wrong
- Define custom tools for Discord-specific actions — scope creep
- Build a memory/context system — duplicating Claude Code's built-in memory

**Why no AI logic:**
- **Claude Code already handles all of this**: CLAUDE.md defines behavior, skills define capabilities, memory provides context, tools provide actions
- **Less to maintain**: Every piece of AI logic we add is something we need to keep updated as Claude Code evolves
- **Predictable behavior**: What you see in the terminal is what you get in Discord. No hidden system prompts changing Claude's behavior
- **Single source of truth**: Your CLAUDE.md is the only configuration needed. No bridge-specific config files, no "Discord mode" settings

## 8. SQLite for Session Storage

**Decision:** Use SQLite (via aiosqlite) for the thread-to-session mapping, with one connection per operation.

**Alternatives considered:**
- In-memory dict — simpler but lost on restart
- Redis — overkill for simple key-value with one user
- PostgreSQL — way overkill
- JSON file — no concurrent access safety

**Why SQLite:**
- **Persistent across restarts**: Session mappings survive bot restarts, so you can continue conversations
- **Zero infrastructure**: No external database server needed
- **aiosqlite**: Async wrapper prevents blocking the event loop
- **Simple schema**: One table, one primary key. No joins, no migrations framework needed
- **Good enough performance**: Single-user bot with dozens of sessions — SQLite handles this trivially

**One connection per operation:** Rather than maintaining a connection pool, each repository method opens and closes its own connection. This is slightly less efficient but eliminates connection lifecycle management and works perfectly for the low query volume of this application.

## 9. Clone Pattern for Runner Instances

**Decision:** `ClaudeRunner.clone()` creates a fresh instance with the same config but no active subprocess.

**Alternatives considered:**
- Reuse the same runner instance — sharing subprocess state between sessions is dangerous
- Create new `ClaudeRunner` manually — duplicates config everywhere
- Factory pattern — over-engineered for what's essentially a copy constructor

**Why clone:**
- **Safety**: Each session gets its own subprocess. No shared state, no race conditions
- **Simplicity**: `runner.clone()` copies all config fields. One line of code
- **Trackability**: `_active_runners[thread_id] = runner` maps each thread to its runner for `/clear` to kill the right subprocess

## 10. Separate Webhook and Chat Message Paths

**Decision:** Webhook messages (`message.webhook_id` present) and regular user messages (`message.author.bot` check) are handled by completely separate Cogs with no overlap.

**Alternatives considered:**
- Single Cog handling both — simpler code structure but muddled responsibility
- Middleware pattern — Discord.py doesn't have middleware, would be non-idiomatic

**Why separate:**
- **ClaudeChatCog**: Filters on `message.author.bot == False` (skips all bots including webhooks)
- **Webhook Cogs** (like docs-sync): Filter on `message.webhook_id` being present (only webhooks)
- **No conflict**: These filters are mutually exclusive — a message can't be both a non-bot user message and a webhook message
- **Different security models**: User messages go through `allowed_user_ids` check. Webhook messages check for fixed trigger strings
- **Different behaviors**: User messages create interactive Claude sessions. Webhook messages trigger predefined automated workflows

## 11. Stall Detection in Status Manager

**Decision:** If no tool activity is detected for 10 seconds, show ⏳. After 30 seconds, show ⚠️.

**Why:**
- **Extended thinking**: Claude Code sometimes thinks for 10-30+ seconds before acting. Without stall detection, the user sees 🧠 forever and wonders if the bot is frozen
- **Network issues**: If the subprocess hangs or the stream stalls, stall indicators help distinguish "thinking" from "broken"
- **Soft vs hard**: Two levels let users know the difference between "this is taking a while" and "something might be wrong"
- **Non-blocking**: The stall monitor runs as an asyncio task alongside the event stream, not blocking any I/O

## 12. DoD & merge gate (single source of truth)

**Decision:** CLAUDE.md carries one authoritative "Definition of Done" list,
the PR template mirrors it verbatim, and a PR may close an Issue only when 100%
of that Issue's Acceptance Criteria are met. Issues must be one-concern with
binary, unambiguous ACs.

**Why (the incident that motivated this):**

While fixing `/clear`, we filed Issue #123 bundling two things: a concrete bug
(Part 1: `/clear` left the tmux window alive on idle threads, so the next
message resumed old context) and an open-ended design task (Part 2: bot-restart
loses context because `start_claude` never uses `--resume`/`--continue`, and the
stored `session_id` is a synthetic `tmux-{thread_id}` so `--resume` cannot work
anyway). PR #124 fixed Part 1 only, wrote `Closes #123`, and on merge GitHub
auto-closed the Issue as "completed". Part 2 silently vanished.

Root cause was **not** lax rules in prose — CLAUDE.md already said staging
verification was mandatory. The cause was a gap between the *enforced* gate and
the *aspirational* prose:

- The only hard gate (CI) runs `ruff`/`pyright`/`pytest`, all of which pass with
  **mocked** tests. Real tmux/Discord behavior is never exercised. Green CI ≠ works.
- Staging verification was honor-system, and the **PR template didn't even list
  it** — so the artifact the author actually fills out never surfaced it.
- The Definition of Done was scattered across the TDD section, the flow section,
  the staging section, and the PR template, and they disagreed. The weakest one won.
- A single multi-concern Issue + `Closes #N` auto-close let a partial PR mark the
  whole Issue done with no check that all ACs were satisfied.

**Fix:** Move the strictness from prose into the artifact the author touches.
One DoD list; the PR template is a verbatim copy; ACs are copied into the PR and
all must be checked; `Closes` is gated on 100% AC completion (else `Refs`);
Issues are kept one-concern with binary ACs so they cannot be half-satisfied.

## 13. Restarting the bot: filter by venv path, kill by PID, launch detached

**Decision:** Process control for the prod/staging bots follows three rules:

1. **Target by venv path, never the bare module name.** Stop prod with
   `pgrep -f "/home/yousan/c-lord/.venv/bin/python3 -m c_lord.main"` (staging:
   `c-lord-parallel-3/.venv/...`). The unfiltered `pgrep -f c_lord.main` matches
   *every* c-lord on the host.
2. **Count with `ps`, not `pgrep -fc`.** Use
   `ps -eo pid,lstart,args | grep "<venv-path>/bin/python3 -m c_lord.main" | grep -v grep`.
   A healthy env has exactly one python (with one `uv run` parent).
3. **Launch detached with `setsid -f`, never a session-tracked background runner.**
   `setsid -f bash -c 'cd <dir> && exec uv run python -m c_lord.main >> <log> 2>&1' < /dev/null`
   reparents the daemon to `systemd --user` so it outlives the launching session.

**Why (the incident that motivated this):**

During the #62 / #182 prod redeploy, following the documented
`pgrep -f c_lord.main | xargs kill` plus a session-tracked background launch
produced **2–3 duplicate prod bot instances** that fought over the same Discord
token. Each new same-token gateway login forces Discord to close the previous
one, which the bot handles as a graceful `bot_shutdown` — so the instances
churned, dropped sessions, and double-posted (Discord rate-limited the
duplicated thread renames).

Three compounding root causes, none of them "the bot is buggy":

- **`pgrep -f c_lord.main` matched the operator's own shell.** When the kill
  command's own command line contains the string `c_lord.main`, `pkill`/`pgrep`
  match the shell running it. `pkill` then killed its own shell mid-command
  (observed as exit code 144), leaving the kill half-done — so the next launch
  stacked an instance on top of survivors.
- **The bot was launched via a session-tracked background runner.** Because the
  daemon never exits, the task never "completes"; it lingers as a child of the
  launching session and can be retried/duplicated, and it dies if that session ends.
- **Repeated launches while kills were incomplete** stacked up. The unfiltered
  pattern also meant a "prod restart" could take down the staging clone, and
  vice-versa.

**Fix:** The prod-restart command in CLAUDE.md is scoped to the prod venv path
(so it can't hit the staging clone), the E2E-restart snippet carries the same
caution, and the operational rules above (kill by explicit PID, count with
`ps`, launch with `setsid -f`) are the standard procedure. Net: one bot per
env, no token churn, no cross-clone collateral.
