# Command Reference

> **#574 で改名**: 操作名は「終了」→「**停止**」、コマンドは `/close-workspace` → `/workspace-stop`、`/reopen-workspace` → `/workspace-start`、スレッド名のマーカーは `[終了]` → `[停止]` になりました。**旧コマンド名はエイリアスとして動き続けます**（利用者側の変更は不要）。既存スレッドに残る `[終了]` も引き続き正しく解釈されます。理由: 7日で自動発火するようになるため、何も失っていないのに「終了」と言われると誤解を招くから。詳細は [workspace-vocabulary.md](specs/workspace-vocabulary.md)。

> **#578 で改名**: `/restart-claude` → **`/claude-restart`**、`/session-cleanup` → **`/workspace-cleanup`**。**旧名はエイリアスとして動き続けます**（利用者側の変更は不要 — パッケージを更新するだけ）。理由: コマンド名の規約が3つ並立していたので、既に多数派だった**名詞先頭**に揃えたから。`session-cleanup` の「セッション」は #571 で `session_id` / tmux に予約した語で、このコマンドが消すのは**作業ディレクトリ**なので `workspace` に改めました。規約そのものは下の[コマンド命名規約](#コマンド命名規約)にあります。

All commands available to Discord users and API consumers.

> For a comprehensive guide with architecture diagrams, session lifecycle, and tips, see the **[User Guide](USER_GUIDE.md)**.

## Architecture Overview

```
Bot → Channel (= 1 repo) → Thread (= 1 tmux window)
                             └─ in the tmux session of the repo THAT THREAD is bound to
```

The tmux session follows the **repository**, not the channel: a channel whose threads
are bound to different repos spans several sessions, and two channels on the same repo
share one. See [specs/tmux-layout.md](specs/tmux-layout.md).

- **Channel ↔ Repository**: Each Discord channel is bound to a git repository via `/clord-init`. This binding is stored in the database.
- **Thread ↔ Session**: Each Discord thread maps 1:1 to a Claude Code session. Replies in a thread continue the same session via `--resume`.
- **Unbound channels**: Running `/clord` in a channel without a `/clord-init` binding returns an error directing the user to configure the binding first — *unless* `repo:` names one explicitly (#514), which works with no binding at all.
- **Channels another instance owns** (#522): the `!text` twins reach **every** c-lord instance that can read the channel, so an instance that is neither bound to the channel nor watching it as its `DISCORD_CHANNEL_ID` **says nothing at all** — no warning, no thread. Without this, every bystander bot in a shared server answers the same `!clord` with its own "not bound" warning. Slash commands are unaffected: their replies are ephemeral, so only the invoker sees them.
- **Execution mode**: Claude Code runs exclusively in tmux TUI mode. The legacy subprocess mode was removed in v1.x.

## コマンド命名規約

> **新しいコマンドを足すときは、まずここを読む。** 規約はドキュメントに書くだけでは
> 守られないので、`tests/test_command_naming.py` が **名前そのもの** を検査する。
> 外れた名前は CI で落ちる。

#540 の棚卸しで、24 コマンドに **規約が3つ並立** していることが分かった（目的語-動詞 9本 /
動詞-目的語 4本 / 目的語なし 10本）。#578 で、既に多数派だった **名詞先頭** に揃えた。
名前は次の3つのどれかに当てはめる。

### 1. `<目的語>-<動詞>` — そのリソースのライフサイクル操作

**名詞が先頭。** Discord のオートコンプリートは**前方一致**なので、`/workspace` と打てば
`workspace-start` / `workspace-stop` / `workspace-delete` / `workspace-cleanup` が
**揃って**出る。動詞先頭（`close-workspace`）だと同じ仲間が `c` と `r` に散り、
「このリソースに何ができるか」が一覧できない。

目的語に使ってよい語は、**利用者に見えている実体**だけ:

| 目的語 | 実体 | 例 |
|---|---|---|
| `clord` | bot 自身 — 起動・接続・設定 | `/clord-init` `/clord-status` `/clord-attach` `/clord-reattach` `/clord-thread-init` |
| `claude` | Claude の**プロセス** | `/claude-restart` |
| `workspace` | スレッドに紐づく作業一式 | `/workspace-start` `/workspace-stop` `/workspace-delete` `/workspace-cleanup` |
| `tmux` | tmux セッション / ウィンドウ | `/tmux-list` `/tmux-screenshot` |
| `thread` | Discord スレッド | `/thread-archive` |
| `model` | Claude のモデル設定 | `/model show` `/model set` |

**`session` は目的語に使わない。** #571 で「セッション」は Claude の `session_id` と
tmux セッション**だけ**を指す語に予約した。利用者が名指しで操作する目的語にこの語を
使うと、1語が3つの実体を指す状態に逆戻りする。だから #578 で `/session-cleanup` は
`/workspace-cleanup` になった（消えるのは**作業ディレクトリ**であって、会話でも
`session_id` でもない）。用語の確定版は
[workspace-vocabulary.md](specs/workspace-vocabulary.md)。

### 2. 目的語なし — 「いま・ここ」への即時操作

**目的語なしは例外ではなくルール。** 目的語が無いことに
「**このスレッドで、いま起きていることへの即時操作**」という意味を持たせる。

`stop` / `clear` / `compact` / `resync` / `skill` / `upgrade` / `version`

これで `/stop` と `/workspace-stop` が**衝突ではなく役割分担**として共存できる:

- `/stop` — いま走っている**ターン**を止める（ワークスペースはそのまま動いている）
- `/workspace-stop` — この**ワークスペース**を停止する（Claude も docker も止まる）

`/resync` に channel 版が無いのも同じ規約の帰結。`/resync-channel` は #619 で削除された
（60秒の menu watchdog が全セッションを自動で回しているため）。残った `/resync` は
「いま・ここ（このスレッド）のミラーを繋ぎ直す」即時操作なので、目的語なしが正しい。

### 3. 旧名エイリアス

**改名しても旧名は必ず残す。** 利用者の指が覚えているし、Zero-Config Principle により
利用者側の作業は「パッケージを更新するだけ」であるべきだから。エイリアスは新しい実装を
**そのまま呼ぶ**（実装を2つ持たない — 2つあると必ずズレる）。

| 旧名 | 新名 | いつ |
|---|---|---|
| `/close-workspace` | `/workspace-stop` | #574 |
| `/reopen-workspace` | `/workspace-start` | #574 |
| `/restart-claude` | `/claude-restart` | #578 |
| `/session-cleanup` | `/workspace-cleanup` | #578 |
| `!attach` | `/clord-attach` | （テキスト版のみ） |

旧名はスラッシュ／テキストの両方で動き続ける。オートコンプリートには
`(旧名) /... と同じです` と出るので、どちらが正なのかは一覧で分かる。

## Slash Commands

### Chat & Sessions

| Command | Description | Where |
|---------|-------------|-------|
| `/clord <prompt>` | Start a new Claude Code session | Channel; in a thread, **c-lord's own only** |
| `/clord repo:<url> <prompt>` | Start a session on a **specific** repository | Channel only |
| `/stop` | Stop the active session (session is preserved for resume) | Thread only |
| `/clear` | Reset the session — next message starts fresh | Thread only |
| `/compact [instructions]` | Compact (summarize) the session context to free the window | Thread only |
| `/clord-attach <window>` | Attach this thread to an existing tmux window | Thread only |
| `/clord-reattach` | Reconnect this thread to the Claude session still on disk (when its record was swept) | Thread only |

**`/clord`** creates a new thread and sends your prompt to Claude Code. Inside a thread, what it does depends on whether that thread is c-lord's (#551):

| The thread | `/clord` does |
|---|---|
| has a session | continues it, as before |
| **was** c-lord's but lost its record (the [30-day sweep](#why-a-thread-loses-its-record-the-30-day-sweep)) | offers **🔗 再接続する** — reconnects to what is on disk, rather than starting over |
| was never c-lord's | **refuses, and changes nothing** |

```
⚠️ このスレッドは c-lord のスレッドではないため、ここでセッションを開始できません。
続けるには:
・新しく始める → チャンネルで /clord prompt:<やること>
```

Before this, `/clord` checked only whether *a repository* was reachable from the thread — true of every thread under a bound channel, human conversations included — and then cloned a session dir, opened a tmux window and wrote the session record. From that point every message in that thread went to Claude, and only someone who knew `/workspace-stop` could undo it.

**There is deliberately no command that adopts an existing thread.** A c-lord thread is one c-lord created from a channel. To start work from an existing discussion, run `/clord` in the channel — it opens a fresh thread. Reconnecting (middle row) is not an exception to this: it only ever reattaches to a session dir that is already on disk, so a thread c-lord never touched has nothing for it to find.

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
| `/clord-thread-init repo:<url>` | Change this thread's repository (overrides channel binding) | **c-lord threads only** |
| `/clord-thread-init remove:True` | Remove the thread-level binding | Thread only |

Requires **Manage Server** permission. When a channel is bound to a repo, all sessions started in that channel automatically use that repo as their working directory. A thread-level binding set via `/clord-thread-init` takes precedence over the channel binding.

`/clord-thread-init repo:<url>` **changes the repository of a thread that is already c-lord's** — it does not turn a thread into one (#551). Binding an ordinary conversation thread used to be step one of the takeover described under `/clord` above, so it is refused on the same test. To start on a different repository, use `/clord repo:<url> prompt:<...>` in the channel, which opens a new thread already bound to it. Showing the binding (no arguments) and `remove:True` still work anywhere — neither can turn a thread into a session.

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

**`/clord-status`** is the single per-channel status view. Each row shows the `attach` target — the **measured** `session:window` that thread is actually in, copy-pasteable as-is — plus the `status`, the thread `topic`, the directory `size`, and `used` (time since last activity). Rows are still ordered by window number ascending. Since #615 a channel's threads can sit in **several** tmux sessions (one per bound repo), so the target is per row and there is no single channel-wide session to print; the old `<session>:work<#>` template pointed at windows that did not exist (#616). A `closed` session has no window, so its `attach` cell is `-`. The Claude-Code session id (`cc-session`, for `claude --resume <id>` from a terminal) is shown only in the `all` view, at the right edge. By default it lists only **live** sessions (like `docker ps`); `show_all` adds **closed** ones (`/workspace-stop`'d — tmux window gone, session dir kept, still using disk). Sessions whose working dir was deleted (`/workspace-delete`) are a footer count only. It **supersedes the removed `/sessions`, `/session-dirs`, and `/resume-info`** (#363).

**Status values** (observed live at call time, not the polled DB state):

| status | meaning |
|--------|---------|
| `run` | tmux window exists and Claude is executing (🟢) |
| `wait` | tmux window exists, turn done, waiting for your input (🟡) |
| `err` | tmux window exists, an error is visible in the pane (🔴) |
| `closed` | no tmux window but the session dir still exists (still uses disk). Two ways to get here: `/workspace-stop`'d (thread renamed `[終了] …`; a message is held and offers a 再開 button, #512) or the pane merely died (bot restart / tmux-server death — a message auto-resumes it via `--continue`, #270) (⚪) |

### Workspace Management

| Command | Description | Where |
|---------|-------------|-------|
| `/workspace-cleanup [dry_run]` | Reclaim the working directories of workspaces nothing is using | Anywhere |
| `/tmux-list` | List all active tmux windows | Anywhere |
| `/tmux-screenshot` | Post a PNG screenshot of this thread's tmux pane — a stopped workspace is restored first (debug) | Thread only |
| `/workspace-stop` | **停止**: close the tmux window, keep the session (see below) | Thread only |
| `/workspace-start` | Reopen a 停止 thread so messages run again | Thread only |
| `/workspace-delete` | Delete the tmux window and session directory for this thread | Thread only |

**`/workspace-stop` = 終了 (#271, #512).** It kills the tmux window and archives
the thread but **keeps** the session directory, transcript, and DB row, so the
conversation can be picked up later. What you see:

- the thread is renamed **`[終了] #<issue> <topic>`** — the `W<N> │` window prefix
  is dropped because the window it named is exactly what was just killed
- writing in the thread afterwards **does not run Claude**. c-lord replies with
  「⏹️ このスレッドは終了しています」 plus a **▶️ 再開する** button; pressing it
  reopens the session and then runs the message you just sent, so nothing has to
  be retyped. `/workspace-start` does the same without the button (but does not
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
| `/claude-restart` | Restart the Claude process for this thread (keeps the conversation) | Thread only |

**`/resync`** is a safety valve for when the tmux→Discord *mirror* feels out of sync — a selection menu's buttons never appeared, or a tool embed looks stale. It re-projects the **current** tmux state onto Discord: (1) re-bridges any stranded TUI menu so its buttons (re)appear, and (2) posts a fresh PNG snapshot of the pane so you can see the live state. It does this immediately, without waiting for the 60s menu-watchdog sweep or a bot restart.

It does **not** touch the Claude process or the conversation — the session is untouched.

There is **no channel-wide twin**. `/resync-channel` existed until #619 and was removed: it swept a single tmux session, so threads bound to another repo were silently skipped — while the 60s menu watchdog already re-bridges stranded menus across **every** session on its own. Reconnecting is therefore two things: the watchdog (automatic, all sessions) and `/resync` (manual, this thread, plus a pane snapshot the watchdog does not post).

**`/tmux-screenshot` on a stopped workspace restores it and then takes the picture (#642).** No tmux window means no pixels to capture, and since [the 4-hour sleep](specs/workspace-sleep.md) any thread nobody touched for four hours is in exactly that state — so answering with "send a message and it will come back" made the command stop returning pictures at all. It now brings the workspace back the way a message would (`claude --continue`, but with **no prompt**, so no turn runs), captures the restored pane, and posts the PNG with a `-# 🔄 …復元してから撮影しました` line so the few seconds of waiting are accounted for. Two thread states are **not** restored, because a message would not restore them either: a `[終了]` thread (that state was your decision — it still offers **▶️ 再開する**) and a thread c-lord has no record of. If the restore itself fails, it says so instead of posting an empty screen.

If the thread's work session is **stopped** (no tmux window — e.g. after a bot restart or a tmux-server death), `/resync` (and `/tmux-screenshot`, for the two states above) no longer dead-ends with a bare "No tmux window found." It tells you what sending a message will *actually* do, which depends on the thread (#538):

- **The thread has a session record** → sending a message auto-restores it (the on-disk conversation resumes via `--continue`, announced with a "🔄 …会話を復元して続けます" notice — #270 / #465). This is what keeps a restart from leaving you stuck (#464).
- **The session was closed** (`[終了]` / `/workspace-stop`) → the message is held and a **▶️ 再開する** button is offered instead (#512).
- **c-lord has no record of the thread** (usually the [30-day sweep](#why-a-thread-loses-its-record-the-30-day-sweep); also a rebuilt DB, or a thread from another host) → the message did not run, and it is answered rather than dropped in silence: a ⚠️ reaction plus a one-time notice saying so. What the notice offers depends on **what is still on disk** (#538):

| Still on disk | The notice offers |
|---|---|
| checkout + transcript | **🔗 再接続する** — reconnects, and the next message continues the real conversation |
| checkout only (the common case) | **🔗 再接続する** — reconnects to the work; the thread's own history is written into the checkout so Claude can pick up where it left off |
| neither | no button: says nothing is left to reconnect to, and points at `/clord` in the channel |

  `/clord-reattach` does the same thing from a command, for when you already know what happened and would rather not send a message that will not run.

  **Reattaching only ever reconnects.** It never clones, never creates a session dir, and refuses a thread with nothing on disk — so it cannot be used to turn an ordinary thread into a Claude session (the door #551 closes).

The hint's wording and the rule that decides whether a message is accepted come from the same place, so they cannot drift apart again. See [specs/session-resume.md](specs/session-resume.md).

### Why a thread loses its record (the 30-day sweep)

The most common reason c-lord "has no record of the thread" is not a bug: **every startup deletes session records that have gone 30 days unused.** Until #554 that was completely silent — one `Cleaned up 3 old sessions` line in the bot log, without even the thread ids — so the first anyone heard of it was a month later:

> 古い C-lord セッションを続けようとしたところセッションが無い、って言われちゃった。消した覚えは無いはず。Discord 上にそういう事も書いてないし

The sweep still runs. What changed is that **each swept thread now gets a notice in the thread itself**, so the reason is where the question gets asked:

```
🧹 このスレッドは 30 日以上使われていなかったため、作業セッションの記録を整理しました。
・作業ディレクトリ（clone した内容）は残っています — 書きかけの成果物はディスク上にそのままあります
・会話の履歴は失われています（Claude Code 自身も既定 30 日で transcript を整理するため）
```

**The notice names the way back.** When the checkout survived, it offers `/clord-reattach` — the thread reconnects to the work still on disk rather than starting over (#538). When nothing survived it does not, because there would be nothing to reattach to.

**What is deleted is the record, not the work.** The row ties a Discord thread to its Claude session; the git clone under `c-lord-sessions/<channel>/<thread>/` is left alone. So a swept thread usually still has its checkout, half-finished edits included — which is why the notice inspects the disk instead of printing one fixed sentence. It reports three different situations:

| On disk | Notice says |
|---|---|
| clone + transcript | both survived |
| clone only (the common case) | the work is there, the conversation is not |
| neither | nothing left to reconnect to |

**Two cleaners run on the same schedule.** Claude Code expires its own transcripts under `~/.claude/projects/` via `cleanupPeriodDays` (default 30), independently of c-lord. That is why the middle row is the common one, and why c-lord cannot restore a conversation it never deleted — raise `cleanupPeriodDays` in your Claude Code settings if you want longer history.

**Screenshot height (#471)**: `/tmux-screenshot` (and the `/resync` PNG snapshot) show **more history than the live ~40-row window**. Claude's TUI keeps no scrollback, so before capturing, c-lord transiently grows the window so Claude redraws more of the conversation, captures the taller screen, then restores the exact original size (the human's attached view is unchanged). The default height is **100 rows**; override it with `CLORD_TMUX_SCREENSHOT_ROWS` (rows), or set it to `0` to capture the current window as-is.

**`/claude-restart`** restarts the Claude *process* for this thread **while keeping the conversation**. Use it when the process is wedged (e.g. a stuck turn silently blocks further input). It kills the active runner and the tmux window so the old/stuck process is gone, but — unlike `/clear` — it does **not** reset the session. Your next message then resumes the same conversation via `--continue`, so the context survives. (The fresh process spawns on that next message through the normal reply path, which is what keeps session setup correct.)

<a id="recovery-ladder"></a>

**どれを使えばいいか — 壊す量が少ない順** (#578)

止めたい対象がどれかで選ぶ。上から順に試せば、**必要以上に失わない**。

| 使うとき | コマンド | Discord ミラー | 走っているターン | Claude プロセス | 会話 | docker |
|---|---|---|---|---|---|---|
| 表示だけがおかしい | `/resync` | 繋ぎ直す | そのまま | そのまま | 残る | 動いたまま |
| いま走っているターンを止めたい | `/stop` | そのまま | **中断** | そのまま | 残る | 動いたまま |
| プロセスが固まって入力を受け付けない | `/claude-restart` | そのまま | 落とす | **再起動** | 残る（`--continue`） | 動いたまま |
| 文脈を捨ててやり直したい | `/clear` | そのまま | 落とす | 落とす | **消える** | 動いたまま |
| このスレッドの作業を畳みたい | `/workspace-stop` | そのまま | 落とす | 落とす | 残る | **停止** |
| ディスクも返したい | `/workspace-delete` | そのまま | 落とす | 落とす | 残る | 停止（作業ディレクトリも削除） |

重なって見える3つの違いは、**何を失うか**の一点に尽きる:

- **`/stop`** はターンだけを中断する。プロセスも会話もそのままなので、次のメッセージは
  何事もなかったように続く。**「今の作業をやめさせたい」はここで足りる**
- **`/claude-restart`** はプロセスを取り替える。`/stop` が効かない（＝プロセス自体が
  固まっていて中断を受け取れない）ときの次の一手。会話は `--continue` で引き継ぐので
  **文脈は失わない**
- **`/clear`** だけが**会話を捨てる**。だから「固まったから `/clear`」は、直したい問題に
  対して代償が大きすぎる — その用途は `/claude-restart` が引き受ける

`/workspace-stop` 以降はワークスペースのライフサイクル操作で、上の3つとは層が違う
（[workspace-vocabulary.md](specs/workspace-vocabulary.md)）。

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
| `!tmux-screenshot` | Post a PNG screenshot of this thread's tmux pane (restores a stopped workspace first) | `!tmux-screenshot` | `/tmux-screenshot` |
| `!resync` | Reconnect this thread's Discord mirror to tmux | `!resync` | `/resync` |
| `!claude-restart` | Restart the Claude process (keeps the conversation) | `!claude-restart` | `/claude-restart` |
| `!clord-init [repo\|remove]` | Bind / unbind / show channel→repo | `!clord-init https://…` | `/clord-init` |
| `!clord-thread-init [repo\|remove]` | Bind / unbind / show thread→repo | `!clord-thread-init remove` | `/clord-thread-init` |
| `!model-set <model>` | Change the global Claude model | `!model-set opus` | `/model set` |
| `!workspace-cleanup [dry]` | Reclaim unused workspaces' working dirs (`dry` = preview) | `!workspace-cleanup dry` | `/workspace-cleanup` |
| `!workspace-stop` | 停止: close the tmux window, keep the session | `!workspace-stop` | `/workspace-stop` |
| `!workspace-start` | Reopen a 停止 thread | `!workspace-start` | `/workspace-start` |
| `!workspace-delete` | Delete this thread's tmux window + session dir | `!workspace-delete` | `/workspace-delete` |

**旧名も同じように動きます** — スラッシュ・テキストの両方で。`!restart-claude` /
`!session-cleanup` / `!close-workspace` / `!reopen-workspace` はそれぞれ
`!claude-restart` / `!workspace-cleanup` / `!workspace-stop` / `!workspace-start`
と**同じ実装**を呼びます（[コマンド命名規約](#コマンド命名規約) の「旧名エイリアス」）。

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
