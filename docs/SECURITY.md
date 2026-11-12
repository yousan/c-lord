# Security Model — what c-lord defends, and what it does not

This document states the trust boundary c-lord draws, the mechanisms that hold it, and the risks
that are accepted on purpose. The pre-commit checklist that goes with it is the `security-audit`
skill (`.claude/skills/security-audit/SKILL.md`).

## Threat Model

c-lord spawns Claude Code CLI subprocesses that can execute arbitrary code on the host machine (read/write files, run commands, make network requests). This is **by design** — Claude Code's value comes from its ability to interact with the development environment.

That fixes what c-lord can and cannot promise. It cannot make a running session harmless. What it
is responsible for is narrower:

> **Only people who are allowed to may start or steer a session — and putting Discord in front of
> Claude Code must not open any door that the Claude Code CLI does not already open by itself.**

### What We Protect Against

| Threat | How it is held |
|--------|----------------|
| Unauthorized users invoking Claude | One `Authorizer` shared by every gate — messages, buttons, `/skill`, `/clord-init`. **Unconfigured, it allows only the Discord application's owner** (#713) |
| Unauthorized users clicking decision buttons (answer a question / approve a plan / Stop) | Same allowlist enforced in every View via `AuthorizedViewMixin.interaction_check` (#466) |
| A prompt being executed by a shell | c-lord never builds a shell command out of message text. A new session receives its prompt through a `0600` temp file read into a quoted variable and passed as one positional argument (`TmuxSessionManager.start_claude`); later messages are typed into the resident pane literally (`tmux send-keys -l`). c-lord's own child processes are started from argument lists — no `shell=True` anywhere |
| A prompt being read as command-line flags | `--` precedes the prompt on the `claude` command line and the text on `send-keys`, so `--dangerously-skip-permissions` or `--resume …` inside a message stays text |
| A crafted session ID turning into a flag or a path | Only a value shaped like a UUID is ever written to a session claim or handed to `--resume` (`is_session_id()` in `c_lord/transcript/claim.py`) |
| A crafted skill name | `/skill` accepts `^[\w-]+$` only, and then only a name that matches a skill it has loaded |
| A repository URL that makes `git` run a command | `validate_repo_url()` refuses `ext::` transports, a leading `-` and embedded newlines; the clone passes `--` before the URL (#514/#923) |
| Secrets *inherited* by tmux panes | `SENSITIVE_ENV_KEYS` (`c_lord/tmux.py`) is removed from every tmux client env, marked `set-environment -r` on bot-managed sessions, and `env -u`'d on the `claude` command line (#353) |
| A process started inside a pane silently becoming a second production bot | Same mechanism — this inheritance is what caused the #322 contamination incidents |
| Claude refusing to start because it believes it is nested | `CLAUDECODE` is removed from the launch environment (`env -u CLAUDECODE`), so the session does not mistake the bot's environment for an enclosing Claude Code |

### What We Do NOT Protect Against

| Scenario | Why it is out of scope |
|----------|------------------------|
| Claude Code reading and writing files on the host | That is the product. A session that could not touch the checkout would be useless |
| Claude Code talking to the network | Likewise — web search, package installs, `git push` and API calls all need it |
| Claude Code editing its own configuration (CLAUDE.md, memory, settings) | Expected behaviour of the CLI, not something the bridge adds |
| Someone with admin rights on the Discord server | They can already grant themselves the role or the channel. The server's own permissions are the control there |
| Someone with physical or shell access to the host | Ordinary host security; nothing c-lord could add |
| Claude reading the bot token from c-lord's `.env` | Accepted (#259). Same-UID processes can already read `.env`; the spawn-read skill relies on that. See below. |
| A process running as the **same** Unix user driving the REST API | Accepted (#457). It can already read `.env` and every session directory. Access from **another** Unix user is blocked — see [REST API exposure](#rest-api-exposure-712-457). |

**The boundary is drawn at Discord, not at the CLI.** Deciding who may start and steer a session is c-lord's job. Once a session runs, it has whatever the Unix user running the bot has — treat "may talk to c-lord" as "may run commands on this host".

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
- The `SKILL.md` itself — with this host's `.env` *path* in it — is kept out of
  the user's commits: the injector lists it in the clone's `.git/info/exclude`
  (#779), so `git add -A` does not pick it up. Before #779 it was committed
  into c-lord's own public repository by an unrelated PR (#704).
- The Discord thread history c-lord writes into the checkout on a reattach
  (`.claude/clord-thread-history.md`, #538/#862) — the whole conversation,
  other participants included — gets the same `.git/info/exclude` line (#882).

> Note: the env stripping described under "Environment Isolation" below **is**
> implemented (#353, closing the #458 drift), but it does **not** make the token
> secret from the session — the spawn-read path reads it from the `.env` *file*,
> not from an environment variable. The accepted-risk reasoning above therefore
> does not depend on it, and stripping is not a substitute for it.

## Input Validation

Text from Discord reaches four places where it could be mistaken for something other than text.
Each has one check, in one place.

### The prompt (`c_lord/tmux.py`)

A turn's prompt takes one of two routes, and neither lets it be interpreted:

- **Starting `claude`** (`start_claude`). The launch is a command line typed into the pane's shell,
  so the prompt is never spliced into it. It is written to a private temp file, read into a shell
  variable and deleted before `claude` runs, and passed as `-- "$CLORD_PROMPT"`: one quoted
  positional argument after the `--` separator. A message such as `$(rm -rf ~)` is therefore not
  expanded, and one such as `--dangerously-skip-permissions` is not a flag. If the temp file cannot
  be written, the fallback is the same argument single-quoted, with embedded quotes escaped.
  The other values that reach that line from outside the code (the API URL, plugin settings) go
  through `shlex.quote`, and a model name set from Discord is validated first (below).
- **Typing into a resident `claude`** (`send_input`). The text is sent with
  `tmux send-keys -l -t <window> -- <text>` — literal keys, in pieces, with tmux itself invoked
  from an argument list. When the text begins with one of Claude Code's mode characters (`/`, `!`,
  `#`, `&`, `?`) a single space is put in front, so an ordinary message cannot become a slash
  command or a shell escape; the dedicated commands (`/compact`, `/skill` …) are the only way to
  send one deliberately.

c-lord's own subprocesses (`tmux`, `git`, `du`, `claude -p` for the context probe) are started with
`asyncio.create_subprocess_exec` or `subprocess.run` on an argument list. Nothing uses a shell.

### Session IDs (`c_lord/transcript/claim.py`)

c-lord names the session it starts (`claude --session-id <uuid>`) and records that name in
`<project_dir>/.clord-session`; a later start resumes with `--resume <recorded uuid>` (#773).
`is_session_id()` accepts only the 8-4-4-4-12 hexadecimal UUID shape, both when a claim is written
and when it is read back, so a damaged or planted claim cannot become a flag or a path such as
`../../etc/passwd`.

### Skill names (`c_lord/cogs/skill_command.py`)

`/skill` runs `/<name> [args]` in the session. The name must match `^[\w-]+$` — letters, digits,
underscore, hyphen — and must be one of the skills c-lord has loaded from `~/.claude/skills/`;
anything else is answered with `Invalid skill name` or `not found` and nothing is run.

### Repository URLs and model names

`/clord-init` and `/clord repo:` put a user-supplied string in front of `git clone`, which runs
before Claude starts and so outside any tool-permission prompt. `validate_repo_url()`
(`c_lord/database/channel_repo.py`) refuses `ext::…` (git would execute the rest), anything
starting with `-` (git would read an option), and anything containing a line break. A model name
given to `/model` must match `^[A-Za-z][A-Za-z0-9._-]{0,63}$` before it is stored or passed to
`--model`.

## Environment Isolation

### Secrets are kept out of the tmux environment (#353)

```python
# c_lord/tmux.py
SENSITIVE_ENV_KEYS = (
    "DISCORD_BOT_TOKEN",
    "CLORD_API_SECRET",
    "CLORD_EVENT_WEBHOOK_SECRET",  # #917: signing key of the turn-end event POST
    "CLORD_EVENT_WEBHOOK_URL",
)
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

### What stays in the environment

Only the keys above (and `CLAUDECODE`) are removed. Everything else the bot was started with —
`PATH`, `HOME`, and `ANTHROPIC_API_KEY` if you use one — is inherited by the pane, because Claude
Code cannot work without it. The API key in particular is left in place deliberately: it is how
Claude Code authenticates. To make a session use a different key or account, configure Claude Code
itself; c-lord has no setting for it.

## Authorization Model

### Who may drive c-lord

There is one rule, held by one object: the `Authorizer` (`c_lord/discord_ui/authorization.py`),
built once in `setup_bridge()` and handed to every Cog. A message, a slash or text command and a
button click all ask the same instance.

- When `allowed_user_ids` is set (standalone: `DISCORD_OWNER_ID`): only those Discord users can
- When `CLORD_ALLOWED_ROLE` is set: members holding that role can (OR logic)
- When **neither** is set: only the Discord application's own owner can — the
  account that created the bot, which Discord already knows, so nothing has to
  be configured for the bot to be usable *by you* and unusable by everyone else
  (#713). Until the bot has logged in and asked Discord, nobody passes.
- `CLORD_ALLOW_ANYONE=1` restores "anyone in the server", with a startup warning
- A refused message is never answered in the channel — the sender is not even
  told a bot is reading. The application owner gets a DM instead (who, where,
  first lines, link; once a day per person, in memory), and every refusal is
  logged. The DM never names who *is* allowed. `CLORD_NOTIFY_DENIED=0` turns
  the DM off (#346, `docs/specs/authorization-default.md`)
- Logging back in from the DM (#1310) types text into a pane, so it is the
  narrowest gate in the project: only a DM from the owner (`DISCORD_OWNER_ID`,
  else the application owner), only while the `clord-login` window is waiting
  (15 minutes), only one space-free token, sent with `send-keys -l` into that
  window and nowhere else. The sign-in link and the code stay in the DM: no
  thread, channel or log line carries either (the log records the code's
  length). `CLORD_LOGIN_RELAY=0` turns it off (`docs/specs/login-alert.md`)
- The same check applies to `SkillCommandCog`, `ChannelRepoCog` and
  `SessionManageCog` — they hold the same `Authorizer` instance rather than a
  copy of the rule. Every slash / text command goes through it except the
  read-only `/version`, `/model` (arguments are gated inside, #918), `/thread-archive show`; an AST test
  (`tests/test_command_authorization_coverage.py`) fails CI for a new command
  that skips it (#781)

**Why the default is not "everyone"**: being able to talk to c-lord is being
able to run shell commands on the host that runs it. A fail-open default means
someone who follows the README and starts the bot has handed that to every
member of the server without being told. See
[specs/authorization-default.md](specs/authorization-default.md).

#### Interactive buttons enforce the same allowlist (#466)

The allowlist gates not only *messages* but every interactive button c-lord
posts — AskUserQuestion choices, plan-approval choices (the same buttons —
#251), the **Stop** and **Reopen** buttons, and the auto-upgrade **Approve**
button. (Tool-permission prompts never reach Discord: c-lord accepts them in
the pane, so there is no Allow/Deny button to guard.) Because c-lord threads are *public*, any
server member who can see the channel could otherwise click these and decide
on the session owner's behalf (e.g. approve a plan → let Claude carry it
out).

Each `discord.ui.View` mixes in `AuthorizedViewMixin`
(`c_lord/discord_ui/authorization.py`), whose `interaction_check` consults the
same `Authorizer` (built from `allowed_user_ids` / `allowed_role_name`). A
non-allowlisted click runs no callback and gets an ephemeral "権限がありません"
notice. When no allowlist is configured, the rule is the same as everywhere
else — the application owner only (#713), not everyone. The allowlist is built once in `ClaudeChatCog` and shared with the
in-session Views (via `RunConfig`), the persistent-view restore path
(`bot.py`), and `AutoUpgradeCog` — so configuring the allowlist alone protects
every button, no extra wiring.

### Which channels and threads are acted on

Being allowed is necessary, not sufficient — c-lord also has to consider the place its own.

- A plain message in the channel starts nothing. A conversation is opened deliberately: `/clord`,
  `/skill`, or `POST /api/spawn`.
- Inside a thread, a message is run only when the thread has a session record, or is a former
  c-lord thread that can be reconnected or started again from its repository binding
  (#538/#556/#862). Other threads under the same channel are left alone, silently.
- Text commands (`!stop`, `!clord` …) are delivered to every bot that can read the channel, so each
  instance answers only where it can point at its own record of the channel or thread — the
  configured `DISCORD_CHANNEL_ID` or a `/clord-init` binding (#596, `c_lord/command_gate.py`).

### Messages that do not come from a person

`is_message_authorized()` (`c_lord/command_gate.py`) decides these, in this order:

- **Webhook messages are accepted.** Holding the webhook URL is the authorization (#507/#508) —
  this is what CI triggers, E2E tests and one Claude handing work to another rely on. Treat a
  channel's webhook URL as a credential that can start sessions.
- **Except c-lord's own voice.** The webhook c-lord uses to speak for a sub-agent is recorded in
  `~/.c-lord/voice-webhooks.json` before its first post and is never obeyed; otherwise everything
  an agent said would come back to the parent as an order (#971).
- **Other bots are refused**, unless their ID is listed in `CLORD_TRUSTED_BOT_IDS`.
- c-lord's own ordinary posts are bot messages like any other, so its output does not come back
  in as input.

## Webhook Security (Consumer Cog Pattern)

A webhook that should *do one fixed thing* — as opposed to holding a conversation — belongs in a
trigger Cog. `WebhookTriggerCog` (`c_lord/cogs/webhook_trigger.py`) is the built-in one and the
model to follow:

- it looks only at messages that have a `webhook_id`, optionally only from `allowed_webhook_ids`
  and only in `channel_ids`;
- the message merely *selects* a trigger by its prefix. The prompt that runs is the one configured
  on the server (`WebhookTrigger.prompt`) — the text after the prefix is not executed;
- one run per trigger at a time (a lock per prefix);
- a message a trigger Cog claims is left to it, so it is not also run as a conversation (#862).

When building custom Cogs that respond to webhooks (for example, a docs-sync trigger), follow this pattern:

1. **Require `message.webhook_id`**, so the path cannot be reached by an ordinary user message.
2. **Match a fixed trigger string**, so the sender chooses *which* predefined action runs and
   nothing more.
3. **Keep the prompt in your own code or configuration**, never assembled from the webhook's
   content.

Note that `WebhookTrigger.dangerously_skip_permissions` defaults to `True`: a trigger runs
unattended. That is one more reason its prompt must not be influenced by the message.

## Database Security

- The SQLite files hold c-lord's bookkeeping: the thread → session mapping with each thread's
  working directory and topic, channel / thread → repository bindings, settings, questions waiting
  for an answer, scheduled tasks and notifications (including their prompt text), and lounge
  messages.
- c-lord writes no token or secret into them. What people type — a task prompt, a repository URL —
  is stored as given, so do not put credentials in either.
- Values always travel as `?` parameters. The only SQL assembled with string formatting is column
  lists and conditions fixed in the code.
- `cleanup_old()` for age-based sweeping — since #818 it marks rows 片付け済み (`closed_reason = 'swept'`) instead of deleting them; the heavy data (session dirs) is what gets removed

## REST API exposure (#712, #457)

The control-plane API (`c_lord/ext/api_server.py`) starts on **every** run — it
used to be gated on the retired skill-push delivery path, so the default
configuration had no API at all (#543). Since `POST /api/spawn` starts a Claude
Code session, the listener is privileged: **reaching it is equivalent to a shell
as the user running the bot.**

**`127.0.0.1` is a network boundary, not a UID boundary.** Loopback TCP ports
are not isolated per Unix user, so on a multi-user host every account could
reach the port — and before #457 the API answered them. Measured on the
maintainer's host on 2026-09-14, with `CLORD_API_SECRET` unset: `GET
/api/health` → 200, `GET /api/tasks` → 200 (scheduled-task prompt bodies), `POST
/api/spawn` → 400 `prompt is required` — i.e. an unauthenticated caller reached
request validation. That host has eleven human accounts and a second c-lord
running under a different user.

**The gate (#457).** Every request must satisfy one of:

1. **Same Unix user.** The peer's UID is read from `/proc/net/tcp`(`6`) by
   matching the connection's 4-tuple (`c_lord/ext/peer_uid.py`) and compared
   with the bot's own UID. On macOS, which has no `/proc`, the same 4-tuple is
   looked up with `lsof` (#899): only the socket whose *local* end is the peer
   counts (the bot's own accepted socket names the peer as its *remote* end and
   must never be mistaken for the caller). `root` also passes — it can read `.env`, `ptrace` the
   process, or simply become that user, so refusing it would protect nothing.
2. **`Authorization: Bearer <CLORD_API_SECRET>`**, when a secret is configured.
   This is how you deliberately let *another* user or another host in.

Everything else gets **403**, including `/api/health` — it used to answer 200 to
anyone, which turned a port scan into an inventory of every c-lord on the host.
Denials are logged at WARNING (sampled per peer UID, `c_lord/log_sampler.py`).

Notes and limits:

- **Same UID is not isolated, by design.** Any process running as the bot's user
  — including every Claude session it spawns — can drive the API. That is the
  same trust boundary as the `.env` file it can already read (see *Accepted
  risks* below). #457 closes the *cross-UID* hole only.
- **A same-UID caller is served without the header even when a secret is set.**
  Demanding it would protect nothing (that user can read the secret) and would
  break the control plane: `CLORD_API_SECRET` is in `SENSITIVE_ENV_KEYS`, so a
  Claude session cannot read it from its environment (#353).
- **If the peer's UID cannot be proven** — no `/proc` on Linux, no usable
  `lsof` on macOS (missing, timed out, no matching socket, or rows that
  disagree), any other OS, or a peer from another host — the request is
  refused. A non-root `lsof` cannot see other users' processes, so on macOS a
  caller from another account finds no row and is refused the same way. Set `CLORD_API_SECRET` for those
  callers, or `CLORD_API_ALLOW_ANY_PEER=1` to serve unverified local callers;
  the escape hatch logs a WARNING naming itself at every start.
- **Every start logs who is served**, e.g.
  `REST API access: serving uid=1000 only, no secret configured`.
- It binds `CLORD_API_HOST`, **default `127.0.0.1`** — loopback only. Do not
  bind `0.0.0.0` unless something else (firewall, reverse proxy with auth) is in
  front of it; a remote peer cannot be UID-verified and will need the secret.
- **`/api/reply`'s `progress_file` attaches only files under c-lord's session
  dirs** (`SESSION_DIR_BASE`, symlinks and `..` resolved). Anything else is
  posted without the attachment and logged at WARNING (#1039) — a value that is
  not a path at all (e.g. a NUL in it) included, never a 500 (#1105). The check
  and the open are one act: the file is opened by walking from the session-dir
  base one component at a time with `openat` + `O_NOFOLLOW`, so swapping a
  directory for an outward symlink after the check fails the open instead of
  attaching the outside file (#1106). The directories on the way are opened for search
  only (`O_PATH` on Linux, `O_SEARCH` where Python offers it), so a directory one may pass
  through but not list (mode `0311`) does not hide the file under it (#1185; where Python offers neither, such a directory still hides it). The logged value is `repr`'d so a newline
  in it cannot forge a log line (#1107). Without this, a
  caller let in with `CLORD_API_SECRET` could have the bot attach its own `.env`
  to a thread. This narrows one direct read path; it is not a sandbox — the same
  caller can still start a session with `POST /api/spawn` (see above).
- One bot per port. A port collision is logged as a WARNING and the bot runs
  without the API — it does not silently attach to another bot's listener. (A
  listener belonging to *another user* cannot serve your callers either: they
  are the ones who get refused.)

## Deployment Recommendations

1. **Run it on a Discord server you control.** Everyone who can post where c-lord listens is one
   allowlist mistake away from a shell on the host.
2. **Give it its own channel(s)** rather than a general chat, and keep the webhook URLs of those
   channels as private as a password.
3. **Set `allowed_user_ids`**: Always set this in production — don't rely solely on channel permissions. Left unset the bot falls back to its application owner, which is safe but implicit; naming the allowlist makes it reviewable. Never set `CLORD_ALLOW_ANYONE=1` on a host you care about
4. **Do not count on permission prompts as a checkpoint.** `permission_mode` (default
   `acceptEdits`, `CLAUDE_PERMISSION_MODE`) is passed to `claude --permission-mode`, but a tool
   permission prompt that appears in the pane is accepted by c-lord and never shown in Discord.
   What a session may do is bounded by the Unix account the bot runs as and by Claude Code's own
   settings on that host — choose those accordingly.
5. **Leave `dangerously_skip_permissions` off** unless you have decided you want it. It is never
   the default for conversations.
6. **Read the log.** Each turn is bracketed by `run_claude: enter` / `exit (outcome=…)`, refused
   messages and refused API calls are logged, and each start logs who the REST API serves.
7. **Keep dependencies updated**: `uv lock --upgrade-package c-lord && uv sync`
8. **Leave the REST API on loopback** (`CLORD_API_HOST`, default `127.0.0.1`) — see above.

## Security Audit Checklist

Go through this before merging a change to `c_lord/tmux.py`, `c_lord/claude/tmux_runner.py`,
`c_lord/cogs/_run_helper.py` or any Cog. The `security-audit` skill has the longer form, with the
commands to run.

- [ ] No subprocess is started through a shell (`shell=True`, `create_subprocess_shell`, `os.system`), and nothing is `eval`'d
- [ ] `--` comes before every user-supplied positional argument
- [ ] Anything added to the `claude` launch line typed into the pane is `shlex.quote`d or passed through a variable
- [ ] Every external input is validated where it enters (session IDs, skill names, repository URLs, model names, channel IDs)
- [ ] `SENSITIVE_ENV_KEYS` (`c_lord/tmux.py`) covers any new secret variables
- [ ] SQL values are bound with `?`; no user-influenced string is formatted into a query
- [ ] The shared `Authorizer` is consulted in any new message handler, button or command — never a fresh copy of the rule (#713: the two copies that existed kept the fail-open default after it was fixed for the other two)
