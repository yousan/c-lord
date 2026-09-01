# Security Model

## Threat Model

c-lord spawns Claude Code CLI subprocesses that can execute arbitrary code on the host machine (read/write files, run commands, make network requests). This is **by design** — Claude Code's value comes from its ability to interact with the development environment.

The bridge's security goal is:

> **Ensure that only authorized users can trigger Claude Code sessions, and that the bridge layer itself does not introduce additional attack surfaces beyond what Claude Code CLI already exposes.**

### What We Protect Against

| Threat | Mitigation |
|--------|-----------|
| Unauthorized users invoking Claude | `allowed_user_ids` allowlist in `ClaudeChatCog` and `SkillCommandCog` |
| Unauthorized users clicking decision buttons (Allow/Approve/Stop) | Same allowlist enforced in every View via `AuthorizedViewMixin.interaction_check` (#466) |
| Shell injection via user prompts | `create_subprocess_exec` (no shell), `--` separator before prompt arg |
| Flag injection via prompts | `--` separator prevents `-p`, `--resume` etc. in prompt text |
| Session hijacking via crafted IDs | Strict regex validation: `^[a-f0-9\-]+$` |
| Skill name injection | Strict regex validation: `^[\w-]+$` |
| Secrets *inherited* by tmux panes | `SENSITIVE_ENV_KEYS` (`c_lord/tmux.py`) is removed from every tmux client env, marked `set-environment -r` on bot-managed sessions, and `env -u`'d on the `claude` command line (#353) |
| A process started inside a pane silently becoming a second production bot | Same mechanism — this inheritance is what caused the #322 contamination incidents |
| Nesting detection bypass | `CLAUDECODE` env var stripped — subprocess won't think it's already inside Claude Code |

### What We Do NOT Protect Against

| Scenario | Why |
|----------|-----|
| Claude Code accessing host filesystem | This is Claude Code's core functionality — restricting it defeats the purpose |
| Claude Code making network requests | Same as above — Claude Code needs internet access for web search, API calls, etc. |
| Claude Code modifying its own config | This is expected behavior (CLAUDE.md, memory files, etc.) |
| Discord server admin abuse | If someone has admin on your Discord server, they already have control |
| Physical access to the host | Out of scope — standard server security applies |
| Claude reading the bot token from c-lord's `.env` | Accepted (#259). Same-UID processes can already read `.env`; the spawn-read skill relies on that. See below. |

**The security boundary is at the Discord layer, not the CLI layer.** Once a session starts, Claude Code has full CLI-level access. The bridge's job is to ensure only the right person can start sessions.

### Accepted risk: `discord-read` skill exposes the bot token to the session (#259)

The injected `discord-read` skill (`c_lord/skills/discord_read.py`) tells Claude
to read other Discord channels by reading `DISCORD_BOT_TOKEN` from c-lord's
`.env` at runtime and `curl`-ing the Discord REST API (instead of the MCP
plugin, which fails with `Missing Access` on channels the bot can otherwise
see — #454).

This means the bot token is **readable by the Claude session** (it `grep`s the
`.env` file). We accept this:

- It exposes nothing new: any process running as the same Unix user can already
  `cat` the `.env` file. Hiding the token from a same-UID Claude is security
  theater (see #234) — the real isolation boundary is the OS file permissions
  on `.env`, which separate different Unix users.
- The skill **never bakes the literal token into a file**. Only the `.env`
  *path* is written into `SKILL.md` (which lives inside the cloned user repo
  working tree, where a literal token could be `git commit`-ed and pushed). The
  token is read into a shell variable at runtime, so it does not appear in the
  command text that the transcript mirror (#71) echoes to Discord.

> Note: the env stripping described under "Environment Isolation" below **is**
> implemented (#353, closing the #458 drift), but it does **not** make the token
> secret from the session — the spawn-read path reads it from the `.env` *file*,
> not from an environment variable. The accepted-risk reasoning above therefore
> does not depend on it, and stripping is not a substitute for it.

## Input Validation

### Prompt Handling (runner.py)

```python
# All arguments passed as a list — no shell interpolation
args = [self.command, "-p", "--output-format", "stream-json", ...]

# -- separator prevents the prompt from being interpreted as flags
args.append("--")
args.append(prompt)

# Spawned without shell
self._process = await asyncio.create_subprocess_exec(*args, ...)
```

Why this matters:
- A prompt like `--dangerously-skip-permissions` won't be interpreted as a flag
- A prompt like `$(rm -rf /)` won't be shell-expanded
- `create_subprocess_exec` passes arguments directly to the exec syscall

### Session ID Validation (runner.py)

```python
if not re.match(r"^[a-f0-9\-]+$", session_id):
    raise ValueError(f"Invalid session_id format: {session_id!r}")
```

Session IDs come from Claude Code CLI output and are stored in SQLite. Before passing back via `--resume`, they're validated against a strict hex-and-hyphens pattern.

### Skill Name Validation (skill_command.py)

```python
if not re.match(r"^[\w-]+$", name):
    await interaction.response.send_message(f"Invalid skill name: `{name}`", ephemeral=True)
    return
```

Skill names are passed to Claude Code as `/{name}`. The regex ensures only alphanumeric characters, underscores, and hyphens are allowed.

## Environment Isolation

### Secrets are kept out of the tmux environment (#353)

```python
# c_lord/tmux.py
SENSITIVE_ENV_KEYS = ("DISCORD_BOT_TOKEN", "CLORD_API_SECRET")
```

A tmux **server** inherits the environment of whichever process first runs a
tmux command, and every window it creates inherits that. When the bot wins that
race, every pane can read the token with a plain `printenv`. Three layers keep
that from happening:

1. **`_tmux_client_env()`** removes the keys from the environment of every tmux
   command c-lord runs, so a server *c-lord* starts is born clean. This is the
   source fix — the leak lives in `_create_global_session`'s plain
   `tmux new-session` fallback, taken whenever `systemd-run` is unavailable
   (containers, CI) or its unit fails.
2. **`_strip_sensitive_env()`** marks the keys `set-environment -r` on each
   bot-managed session, which repairs a server that was *already* started dirty
   — by a c-lord older than this fix, or by any other token-holding process.
   Scoped per session on purpose: a server-global mark would also reach the
   unrelated sessions sharing the host's tmux server.
3. **`start_claude()`** adds `env -u <key>` to the `claude` command line, so the
   Claude process is clean even in a session nobody marked.

Panes that are **already running** keep the environment they started with —
that cannot be fixed retroactively. They age out as windows are recreated.

**What this does and does not buy.** It does *not* make the token secret from
the session: the `.env` file is readable by the same Unix user, and the injected
`discord-read` skill (#259) tells Claude to read it from there — see the
accepted risk above, and #234. What it prevents is **accidental inheritance**:
a process started inside a pane picking the production token out of its
environment without anyone intending it. That is exactly what happened in #322,
where a staging bot became a second production bot. Treat it as
contamination-prevention, not confidentiality.

`CLAUDECODE` is separately removed (`env -u CLAUDECODE`) so the spawned Claude
does not think it is nested inside another Claude Code.

### What's NOT Stripped

General environment variables (PATH, HOME, ANTHROPIC_API_KEY, etc.) are passed through because Claude Code needs them to function. The `ANTHROPIC_API_KEY` is intentionally available — Claude Code uses it for API calls. If you need to restrict which API key Claude Code uses, configure it via Claude Code's own settings, not this bridge.

## Authorization Model

### User-Level Authorization

```python
class ClaudeChatCog(commands.Cog):
    def __init__(self, ..., allowed_user_ids: set[int] | None = None):
        self._allowed_user_ids = allowed_user_ids

    async def on_message(self, message):
        if message.author.bot:
            return
        if self._allowed_user_ids is not None and message.author.id not in self._allowed_user_ids:
            return
```

- When `allowed_user_ids` is set: only listed Discord user IDs can invoke Claude
- When `allowed_user_ids` is `None`: all users in the channel can invoke Claude (for trusted private servers)
- The same check applies to `SkillCommandCog`

#### Interactive buttons enforce the same allowlist (#466)

The allowlist gates not only *messages* but every interactive button c-lord
posts — tool-permission **Allow/Deny**, plan **Approve/Cancel**, MCP
elicitation, AskUserQuestion choices, the **Stop** button, and the
auto-upgrade **Approve** button. Because c-lord threads are *public*, any
server member who can see the channel could otherwise click these and decide
on the session owner's behalf (e.g. approve a tool permission → let Claude run
something).

Each `discord.ui.View` mixes in `AuthorizedViewMixin`
(`c_lord/discord_ui/authorization.py`), whose `interaction_check` consults the
same `Authorizer` (built from `allowed_user_ids` / `allowed_role_name`). A
non-allowlisted click runs no callback and gets an ephemeral "権限がありません"
notice. When no allowlist is configured, everyone may click (zero-config —
unchanged). The allowlist is built once in `ClaudeChatCog` and shared with the
in-session Views (via `RunConfig`), the persistent-view restore path
(`bot.py`), and `AutoUpgradeCog` — so configuring the allowlist alone protects
every button, no extra wiring.

### Channel-Level Authorization

Both Cogs only respond to messages in the configured channel (`channel_id`) and its child threads. Messages in other channels are silently ignored.

### Bot Messages

`message.author.bot` check ensures bot messages (including webhook messages) don't trigger Claude sessions. This prevents infinite loops if Claude's output triggers another bot.

## Webhook Security (Consumer Cog Pattern)

When building custom Cogs that respond to webhooks (for example, a docs-sync trigger), follow this pattern:

```python
# Only respond to webhook messages
if not message.webhook_id:
    return

# Fixed trigger string — no arbitrary command execution
if message.content.strip() != "🔄 expected-trigger":
    return

# Hardcoded behavior — webhook cannot inject commands
prompt = HARDCODED_PROMPT  # Server-side, not from webhook
```

Key principles:
1. **Check `webhook_id`** — distinguishes webhooks from regular users
2. **Fixed trigger strings** — webhook cannot specify what to do, only trigger predefined actions
3. **Hardcoded prompts** — all Claude Code prompts are defined server-side, never from webhook content

## Database Security

- SQLite database stores `thread_id` → `session_id` mappings only
- No user data, no messages, no secrets stored
- Parameterized queries throughout (`?` placeholders, no string formatting)
- `cleanup_old()` method for age-based data removal

## Deployment Recommendations

1. **Private Discord server**: Run the bot on a server only you have access to
2. **Dedicated channel**: Use a specific channel for Claude interactions, not a general chat
3. **Set `allowed_user_ids`**: Always set this in production — don't rely solely on channel permissions
4. **Review Claude Code permissions**: Configure `permission_mode` and `allowed_tools` to restrict Claude Code's capabilities as needed
5. **Don't use `dangerously_skip_permissions`**: This flag exists for power users who understand the implications. It disables Claude Code's built-in safety prompts
6. **Monitor the bot**: Check logs regularly. Claude Code sessions are logged with timing and cost data
7. **Keep dependencies updated**: `uv lock --upgrade-package c-lord && uv sync`

## Security Audit Checklist

Before merging changes to `runner.py`, `_run_helper.py`, or any Cog:

- [ ] No `shell=True` in any subprocess call
- [ ] `--` separator present before user-supplied arguments
- [ ] All external input validated (session IDs, skill names, channel IDs)
- [ ] `SENSITIVE_ENV_KEYS` (`c_lord/tmux.py`) covers any new secret variables
- [ ] No string formatting in SQL queries (use `?` placeholders)
- [ ] `allowed_user_ids` check present in any new message handler
- [ ] No new `os.system()`, `subprocess.run(shell=True)`, or `eval()` calls
