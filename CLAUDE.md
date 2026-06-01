# c-lord (c-lord)

Discord frontend for Claude Code CLI. **This is a framework (OSS library), not a personal bot.**

**略称: c-lord** (c-lord)

## Framework vs Instance

- **c-lord** (this repo) = reusable OSS framework. No personal config, no secrets, no server-specific logic.
- Personal instances (e.g. a personal Discord bot) install this as a package and import the Cog. The instance repo handles server-specific config, additional Cogs, and secrets.
- When adding features: if it's useful to anyone → add here. If it's personal workflow → add in the instance repo.

### Zero-Config Principle (Critical)

**Consumers must get new features by updating the package alone — no code changes required.**

- New features should be enabled by default (auto-discovery, sensible defaults)
- New constructor parameters must have backward-compatible defaults (`= None`)
- If a feature requires consumers to wire something up, the design is wrong — fix it in c-lord
- Consumers should NEVER need to copy, wrap, or subclass c-lord Cogs. If they do, c-lord is missing an extension point

## Architecture

- **Python 3.10+** with discord.py v2
- **Cog pattern** for modular features
- **Repository pattern** for data access (SQLite via aiosqlite)
- **asyncio.subprocess** for Claude Code CLI invocation (never shell=True)

## Key Design Decisions

1. **Claude pushes its own answer via Skill, not scraped from TUI** (#53): Each session dir gets a `.claude/skills/discord-reply/SKILL.md` (with `thread_id`, `api_url`, optional `Authorization: Bearer` baked in) that tells Claude to `curl POST /api/reply` at the end of every turn. c-lord no longer extracts Claude's response from `tmux capture-pane`. The tmux pane is kept solely for human visibility (`tmux attach -t <session>:<work>`) and for `send-keys` input. **This is what structurally prevents the TUI-chrome-leak class of bugs** (#23, #27, #28, #29, #30, #32, #34, #35, #39, #41, #43, #45, #49, #50): there is no text-from-TUI codepath that reaches Discord anymore, so a new chrome element can no longer leak. The legacy `USE_SKILL_REPLY` env remains as an opt-out switch (`USE_SKILL_REPLY=0`) but disabling it does **not** restore the old scrape path — it simply stops the skill from being injected, leaving Claude with no path to Discord. See `c_lord/skills/`.
2. **Thread = Session**: Each Discord thread maps 1:1 to a Claude Code session ID. Replies in a thread continue the same session via `--resume`.
3. **Emoji reactions for status**: Non-intrusive progress indication on the user's message. Debounced to avoid Discord rate limits.
4. **Tool-use embeds are still driven by the tmux event stream**: `tmux_runner.py` still polls `capture-pane` and emits SYSTEM / RESULT / tool-use / permission / plan / elicitation / todo events. Only the ASSISTANT text events were removed (#53). So Discord still gets live "Bash(...)" / "Read(...)" embeds, status emoji, plan-approval buttons, etc. — none of that goes through the (removed) text-post path.
5. **Installable package**: `c_lord` is a proper Python package. Consumers install via `uv add git+...` or `pip install git+...`, not by copying files.
6. **Shared run helper**: `cogs/_run_helper.py` centralizes Claude CLI execution logic used by both ClaudeChatCog and SkillCommandCog.
7. **REST API as the control plane**: Claude Code subprocesses communicate back to c-lord via REST API (`CLORD_API_URL` env var), not via stdout markers or special output formats. This makes the interface explicit, testable, and usable by external systems (GitHub Actions, etc.). See `ext/api_server.py`.
8. **SQLite-backed dynamic scheduler**: Scheduled tasks are stored in `scheduled_tasks` DB table and executed by a single `discord.ext.tasks` master loop (every 30s). Tasks are registered at runtime via REST API — no code changes needed to add new tasks. `discord.ext.tasks` decorators are only used for the master loop, not per-task (they're static/compile-time constructs).
9. **Claude handles "what", c-lord handles "when"**: For scheduled tasks, c-lord only manages the schedule. All domain logic (what to check, how to deduplicate, what to post) lives in the Claude prompt. No GitHub/AzureDevOps-specific code in c-lord itself.
10. **Per-channel tmux sessions** (auto-derived from `/clord-init` binding): When a channel is bound to a repo via `/clord-init`, the tmux session name is auto-derived from the repo URL (`derive_session_name()` → e.g. `https://github.com/yousan/c-lord` → `c-lord`). All threads in that channel share that session, with one window per thread (`work1`, `work2`, ...). Channels without a binding fall back to the global default `clord` session. Implemented in `c_lord/cogs/channel_repo.py::resolve_tmux_manager()` (cached per channel) and consumed by `claude_chat`, `webhook_trigger`, `scheduler` cogs. **Do not assume a single global session — always go through `resolve_tmux_manager(channel_id)`.**

### Why REST API over stdout markers for Claude→c-lord communication

Alternative considered: Claude embeds `<!-- c-lord:schedule {...} -->` in response text; c-lord parses stdout.

**Rejected because**: fragile text parsing, untestable, can't be triggered externally, implicit side effect from output.

**REST API chosen because**: clean interface, independently testable, usable by external systems, already an established c-lord pattern (`ext/api_server.py`). Claude uses its Bash tool to `curl $CLORD_API_URL/api/tasks`.

## Development

### Setup

```bash
git clone https://github.com/yousan/c-lord.git
cd c-lord
uv sync --dev
```

### Running Tests

```bash
uv run pytest tests/ -v --cov=c_lord
```

All tests must pass before submitting a PR. CI runs on Python 3.10, 3.11, and 3.12.

### Linting & Formatting

```bash
uv run ruff check c_lord/    # lint
uv run ruff format c_lord/   # format
```

CI enforces both `ruff check` and `ruff format --check`. Fix all issues before pushing.

### Running (standalone)

```bash
cp .env.example .env
# Edit .env with your Discord bot token and channel ID
uv run python -m c_lord.main
```

### E2E Testing (Discord)

Bot 再起動は積極的に行ってよい。新しいコードで Bot を再起動し、Discord 上で動作確認する。

```bash
# 1. Bot 再起動
# 注意: 同一ホストで staging clone (c-lord-parallel-3 等) も動かしている場合、
# 素の pgrep だと staging も巻き添えで kill される。本番だけ狙うなら venv パスで絞る:
#   pgrep -f "/home/yousan/c-lord/.venv/bin/python3 -m c_lord.main" | xargs -r kill
pgrep -f "c_lord.main" | xargs kill 2>/dev/null; sleep 2
nohup uv run python -m c_lord.main > /tmp/clord-bot.log 2>&1 &

# 2. E2E テスト実行（要 .env: DISCORD_BOT_TOKEN / DISCORD_CHANNEL_ID / E2E_TEST_WEBHOOK_URL）
uv run pytest -m e2e tests/e2e/ -v
```

E2E テストは `tests/e2e/` 配下に pytest として実装され、`@pytest.mark.e2e` マーカーで分離されている。デフォルトの `pytest` 実行 (CI 含む) からは除外され、`-m e2e` を明示した時だけ走る。`.env` に必要なキーが揃っていない場合はテストごと skip される。

**カバーしているフロー**:
- `tests/e2e/test_attach_command.py` — `!attach` テキストコマンド (webhook → process_commands)
- `tests/e2e/test_message_flow.py` — スレッドメッセージ → Claude 応答 + TUI chrome leak / OGP suppression 回帰

**Webhook セットアップ**: Discord → Server Settings → Integrations → Webhooks で `DISCORD_CHANNEL_ID` のチャンネルに Webhook を作成し、`.env` の `E2E_TEST_WEBHOOK_URL` に設定する。

テキストコマンド (`!attach` 等) は Webhook 経由で E2E テスト可能。`process_commands` が Webhook メッセージを処理するよう `ClaudeDiscordBot` でオーバーライド済み。

### Debugging Discord from a Claude session

c-lord のクローン (parallel worktree, 別ディレクトリの clone 等) で動かしている Claude から Discord をデバッグ目的で参照・操作したいときの手順。bot 本体や bot が spawn した子プロセスではなく、**手動で起動した Claude / 別マシンの Claude** が対象。

**前提と制約**:
- `c_lord/cogs/_run_helper.py` は **bot が spawn する子 Claude の env から `DISCORD_BOT_TOKEN` を strip する** (security audit に記載)。bot 経由で立った Claude は環境変数からは token を読めない
- 手動で `claude` コマンドを叩いて立ち上げた tmux window 内 Claude には strip が掛からないので、**bot の `.env` ファイル**を直接読めばよい
- Discord MCP plugin (`plugin:discord:discord`) は別チャンネルへ `Missing Access` で失敗することが多い。**メッセージの読み取りは MCP に頼らず、`.env` の bot token を読んで Discord REST API を `curl` で叩く**のが確実 — MCP が `Missing Access` を返しても**そこで諦めず curl にフォールバックすること**。MCP は c-lord のスタック外（利用者環境にある保証もない）なので読み取りの基盤にしない。読み取り経路を skill + API として c-lord 内に正式実装する作業は #259、設計方針は #234 を参照

**Token 取得**: 各 clone の作業ディレクトリ直下の `.env` を見るのが基本。本体 (bot を起動している c-lord clone) 以外の並行作業 clone (`c-lord-parallel`, `c-lord-parallel-2`, ...) には **`.env` を本体に symlink する規約** にしている:

```bash
# 並行 clone を新規作成したら一度だけ
ln -s /home/yousan/c-lord/.env /path/to/your-clone/.env
# (パスは運用に合わせて。canonical な bot の .env を指す symlink)
```

これで以降 `.env` (相対パス) を読めば全 clone から同じ token に届く。bot 本体 clone は実ファイル、並行 clone は symlink。

**読み取り (任意の thread / channel のメッセージ取得)**:

```bash
# clone のルートで実行 (実ファイル / symlink どちらでも OK)
TOKEN=$(grep '^DISCORD_BOT_TOKEN=' .env | cut -d= -f2-)

# 直近 N 件のメッセージ
curl -s -H "Authorization: Bot $TOKEN" \
  -H "User-Agent: DiscordBot (https://github.com/yousan/c-lord, 1.0)" \
  "https://discord.com/api/v10/channels/<THREAD_OR_CHANNEL_ID>/messages?limit=10" \
  | python3 -c "import sys,json; [print(f\"{m['author']['username']}: {m['content'][:200]}\") for m in reversed(json.load(sys.stdin))]"

# 個別メッセージ
curl -s -H "Authorization: Bot $TOKEN" \
  -H "User-Agent: DiscordBot/1.0" \
  "https://discord.com/api/v10/channels/<CH>/messages/<MSG>"
```

**投稿 (デバッグ通知を Discord に送る)**:

```bash
curl -s -X POST -H "Authorization: Bot $TOKEN" \
  -H "User-Agent: DiscordBot/1.0" -H "Content-Type: application/json" \
  -d '{"content":"debug: ..."}' \
  "https://discord.com/api/v10/channels/<THREAD_ID>/messages"
```

**bot 内 spawn 子 Claude (`c-lord-sessions/<ch>/<thr>/` cwd) の場合**: env は strip されているが、ファイル読みは strip 対象外なので絶対パス (`cat /path/to/c-lord/.env`) で token を取得可能。並行 clone と違い session_dir には symlink を入れていないため絶対パス必須。

**代替: c-lord REST API (`ext/api_server.py`)**:
api_server をオプトインで有効化してある環境では `POST /api/threads/{thread_id}/messages` で同等の操作が可能 (詳細は `docs/COMMANDS.md`)。bot を再起動せずに有効化する手段はないため、デバッグ目的では上の curl が手軽。

## Debugging & Troubleshooting

Bot の挙動が怪しいとき、最初に見るべき情報源は **bot ログ** と **Discord 上のメッセージ** の 2 つ。前者は `nohup` 経由で `/tmp/clord-bot.log` に出ているのが運用上の標準 (上の "Running (standalone)" 参照)。後者は `.env` を読んで Discord REST API を curl で叩く ("Debugging Discord from a Claude session" 参照 — Claude Code から実行可)。

### ログの読み方

すべてのログは Python `logging` 経由で `c_lord/utils/logger.py` で設定された StreamHandler に流れる。フォーマットは `%(asctime)s [%(levelname)s] %(name)s: %(message)s`。

**構造化コンテキスト**: 重要な処理ポイントには `log_ctx()` ヘルパー (`c_lord/utils/logger.py`) で `[thread=<id> session=<short> task=<id> channel=<id>]` 形式の prefix が付く。これで `grep "thread=12345"` すると 1 スレッドの一連の処理を抽出できる。`session=` は UUID-shaped (≥32 文字) のときは先頭セグメントだけに省略される。

**主要な入口/出口ログ**:
- `_run_helper.py:run_claude_with_config` — `run_claude: enter` / `run_claude: exit` (Claude 実行 1 回ごと)
- `cogs/scheduler.py:_run_task` — `_run_task: enter` / `_run_task: exit` (スケジュール実行ごと)
- `cogs/scheduler.py:_master_loop` — `SchedulerCog: N task(s) due (ids=[...])` (30 秒ごと、due があるときのみ)
- `cogs/webhook_trigger.py:on_message` — `Webhook trigger matched` (CI/CD webhook 着弾時)

**典型的な調査フロー**:
1. Discord で問題のスレッド ID を取得 (URL の末尾、または右クリック → Copy ID)
2. `grep "thread=<ID>" /tmp/clord-bot.log` で当該スレッドの処理ログを抽出
3. `enter` と `exit` の対応関係 / エラー stacktrace の有無を確認
4. `session=<short>` を別途 grep すると Claude CLI 側のセッションスコープも追える

新規にログを書くときは、**thread / session / task / channel のいずれかが文脈上ある場合は必ず `log_ctx()` を使う**。素の文字列 interpolation を増やすと grep 性が落ちる。

### セッションライフサイクル

c-lord で 1 つの「セッション」が辿る状態遷移:

1. **作成** — ユーザーがチャンネルにメッセージ → `ClaudeChatCog` がスレッドを作成
2. **session_dir セットアップ** — `session_dir.py` が `c-lord-sessions/<channel_id>/<thread_id>/` に repo を git clone (channel が `/clord-init` で repo に bind されている場合のみ)
3. **tmux window 作成** — `tmux.py::resolve_tmux_manager(channel_id)` で per-channel session を取得し、新規 window (`work1`, `work2`, ...) を立てる
4. **Claude CLI 起動 + Skill 注入** — `claude/tmux_runner.py` が tmux window 内で `claude` を起動 (`send-keys`)。同時に `session_dir.py` が `<session_dir>/.claude/skills/discord-reply/SKILL.md` を注入 — Claude はこれを読み取り、応答末尾で `curl POST /api/reply` で **自身が** Discord へ最終回答を投稿する (#53)。
5. **ツール embed / 状態 emoji** — `tmux_runner` は capture-pane を polling して SYSTEM / RESULT / tool-use / permission / plan / elicitation / todo events を yield。 `EventProcessor` がそれぞれの embed/reaction を Discord に post する。**最終回答テキストはここを通らない** — Skill 経由のみ。
6. **応答完了** — `RESULT` event で `EventProcessor.finalize()` を呼び、reaction 更新 + registry から unregister
7. **継続** — 同じスレッドへの reply は session_id を `--resume` で渡して同一セッションを継続
8. **クリーンアップ (任意)** — `_cleanup_session_dir` / `_cleanup_tmux_session` (現状はコマンド経由で明示的にトリガ。スレッド close 時の自動クリーンアップは未実装)

**よくある問題と確認手順**:

| 症状 | 最初に見るべき場所 | 典型的な原因 |
|------|-----------------|------------|
| スレッドが作られない | bot ログの `on_message` 周辺、`DISCORD_CHANNEL_ID` が一致しているか | Intent 不足 / channel ID 設定ミス |
| 応答が返ってこない | `grep "thread=<ID>"` で `run_claude: enter` はあるか / `exit` まで届くか | tmux window 作成失敗、Claude CLI hang、timeout |
| 同一セッションのはずが別セッション扱い | `_run_helper` で `session_id=` ログを確認、DB の `sessions` テーブル | repository から session_id が読めていない |
| Webhook trigger が無視される | `Webhook trigger matched` ログの有無 | webhook_id allowlist / channel_ids 不一致、prefix mismatch |
| Scheduler が動かない | `SchedulerCog: N task(s) due` の有無 (30 秒間隔) | `next_run_at` が未来、`scheduled_tasks` が空 |
| tmux session が見つからない | `tmux ls` で session 名を確認 | channel に `/clord-init` 未実行 → fallback `clord` を期待してしまう。常に `resolve_tmux_manager(channel_id)` 経由で取得 |

これでも切り分かないときは Discord 側の状態 (リアクション・スレッド存在・最終メッセージの author) を上の curl で確認するのが速い。

## Code Conventions

### Style

- **Formatter/Linter**: ruff (config in `pyproject.toml`)
- **Type hints**: Required on all function signatures
- **Python**: 3.10+ — use `from __future__ import annotations` in every file
- **Line length**: 100 characters max
- **Imports**: Sorted by ruff (`I` rule). Use `TYPE_CHECKING` for type-only imports

### Error Handling

- Use `contextlib.suppress(discord.HTTPException)` for Discord API calls that may fail (reactions, message edits)
- Never silently swallow errors in business logic — log them
- CLI subprocess errors should yield a `StreamEvent` with `error` field, not raise exceptions

### Security (Critical — Auto-Enforced)

This project runs arbitrary Claude Code sessions. Security is non-negotiable.

**Before every commit**, run the security audit (see `.claude/skills/security-audit/SKILL.md`):

- **Always `create_subprocess_exec`**: Never use `shell=True`. The prompt is a direct argument, not shell-interpolated.
- **`--` separator**: Always use `--` before the prompt argument to prevent flag injection
- **Session ID validation**: Strict regex `^[a-f0-9\-]+$` before passing to `--resume`
- **Skill name validation**: Strict regex `^[\w-]+$` before passing to Claude
- **Environment stripping**: `DISCORD_BOT_TOKEN` and other secrets are removed from the subprocess env so Claude's Bash tool can't read them
- **No `dangerously_skip_permissions` by default**: This flag exists for advanced users who understand the risk

If you modify `tmux_runner.py`, `_run_helper.py`, or any Cog, the security audit is **mandatory** before committing.

### Naming

- Files: `snake_case.py`
- Classes: `PascalCase` (e.g., `ClaudeRunner`, `StatusManager`)
- Functions/methods: `snake_case`
- Private: prefix with `_` (e.g., `_build_args`, `_run_helper.py`)
- Constants: `UPPER_SNAKE_CASE`

### Testing (TDD Enforced)

**All new features and bug fixes MUST follow TDD: write tests FIRST, then implement.**

1. **RED**: Write a failing test → `uv run pytest tests/test_xxx.py -v` → confirm it FAILS
2. **GREEN**: Write minimal code to pass → confirm it PASSES
3. **REFACTOR**: Clean up, keeping tests green
4. **VERIFY**: `uv run ruff check c_lord/ && uv run pytest tests/ -v --cov=c_lord`

See `.claude/skills/tdd/SKILL.md` for detailed patterns per module type.

- Use `pytest` with `pytest-asyncio` (auto mode)
- Test files go in `tests/` mirroring the source structure
- Pure logic (parser, chunker, types): 90%+ coverage
- Discord-dependent code (Cogs, StatusManager): use mocks, 30%+ coverage
- **Never write implementation code without a corresponding test**

## Project Structure

```
c_lord/          # Installable Python package
  __init__.py            # Public API exports
  protocols.py           # Shared protocols (DrainAware)
  main.py                # Standalone entry point
  bot.py                 # Discord Bot class
  session_dir.py         # Git clone based session directory management
  tmux.py                # Tmux session management wrapper
  cogs/
    claude_chat.py       # Main chat Cog (thread creation, message handling)
    skill_command.py     # /skill slash command with autocomplete
    webhook_trigger.py   # Webhook → Claude Code task execution (CI/CD)
    auto_upgrade.py      # Webhook → package upgrade + restart
    scheduler.py         # Scheduled task executor (SQLite-backed, master loop)
    _run_helper.py       # Shared Claude CLI execution logic (DRY)
  claude/
    config.py            # ClaudeConfig dataclass (CLI settings)
    tmux_runner.py       # tmux pane runner — yields SYSTEM/RESULT/tool-use/
                         # permission/plan/elicitation/todo events ONLY.
                         # ASSISTANT text events are no longer yielded (#53).
    types.py             # Type definitions for SDK messages
  skills/                # Per-session SKILL.md generator (#52, #53)
    discord_reply.py     # SKILL.md template (curl POST /api/reply pattern)
    injector.py          # Writes SKILL.md into <session_dir>/.claude/skills/
  database/
    models.py            # SQLite schema
    repository.py        # Session CRUD operations
    notification_repo.py # Scheduled notification CRUD (REST API)
    task_repo.py         # Scheduled task CRUD (SchedulerCog)
  discord_ui/
    status.py            # Emoji reaction status manager (debounced)
    embeds.py            # Discord embed builders
                         # NOTE: chunker.py / streaming_manager.py /
                         # tui_strip.py / progress_buffer.py were removed in
                         # #53 — the scrape→post path they served is gone.
  ext/
    api_server.py        # REST API server (always on when bot starts).
                         # POST /api/reply is the path Claude uses (via the
                         # injected skill) to post its final answer.
  utils/
    logger.py            # Logging setup
tests/                   # pytest test suite
pyproject.toml           # Package metadata + dependencies
uv.lock                  # Dependency lock file
CONTRIBUTING.md          # Contribution guidelines
```

### Adding a New Cog

1. Create `c_lord/cogs/your_cog.py`
2. If it runs Claude CLI, use `_run_helper.run_claude_in_thread()` — don't duplicate the streaming logic
3. Export from `c_lord/cogs/__init__.py`
4. Add to `c_lord/__init__.py` public API
5. Write tests in `tests/test_your_cog.py`

### Adding a New Discord UI Component

1. Add to the appropriate file in `c_lord/discord_ui/`
2. Export from `__init__.py` if it's part of the public API
3. Test edge cases (empty strings, very long strings, Unicode, code blocks)

## 開発の行動規範 (Working Conduct) — Issue/PR の前提

このリポジトリで AI が作業するときの上位ルール。Issue/PR/証跡の各規律はこの上に乗る。
背景と経緯は `docs/DESIGN_DECISIONS.md` → "Working agreements" を参照。

- **質問にはまず答える。質問を黙って作業に変換しない。** 「なぜここで〜なの？」「これは仕様？バグ？」は**診断の依頼**であって作業指示ではない。spec か bug かを、**実機の挙動の証跡**（スクショ / メッセージ URL / 観測）と**経緯 (Why)** で答えてから作業に入る。「コードが X だから」は挙動の説明であって仕様判定の答えにはならない（コードは意図を語れない）。
- **自走の範囲＝合意済みの作業。** 「やる」と決まった作業は **PR → 手動QA → マージ → 本番デプロイ → 本番実測**まで自走してよい（途中で逐一停止しなくてよい）。ただし起点の問いはまだ作業ではない → 診断して合意してから着手する。
- **柔らかい依頼（「ここ直ってる？」）は「答え→行動」の順で分離する。** 例:「直っていません。これは仕様ではなくバグです。→ 直しました: PR #NNN / Evidence: <URL>」。先頭で事実に答えれば、相手が「答えだけ欲しかった」場合も意図のズレに気づける。安価で可逆なら先回り可。**設計変更・破壊操作・不可逆な判断は推察で走らず一行確認**。
- **迎合しない・鵜呑みにしない。** 事実ベースで判断し、報告と食い違えば指摘する。ユーザーの仮説も自分のデータで確認し、違えばその判断を優先して伝える。
- **コードより先に実機の挙動を見る。** 内部実装を熟知した開発者目線ではなく、**利用者目線で「使って不便か」**を確認する。これが無いと、その Issue/PR が何のためにあるか（Why）が掴めない。
- **報告は簡潔・日本語・URL はクリッカブルに。** 完了は `#NNN done / PR #MMM / Evidence: <URL>` を1行で。実機未確認なら「テスト緑・実機未確認」と正直に書く（過大申告しない）。
- **Issue/PR はテンプレートに従う** (`.github/ISSUE_TEMPLATE/`, `.github/pull_request_template.md`)。テンプレが各規律を書式で強制する。

## Definition of Done (DoD) — single source of truth

**A change is "done" only when every box below is checked. This list is the one
authoritative completion definition; the PR template
(`.github/pull_request_template.md`) mirrors it verbatim. If the two ever
disagree, this section wins — fix the template.**

Why this exists: CI only runs `ruff` + `pyright` + `pytest`, all of which pass
with mocked tests. Real behavior (tmux, Discord, restart continuity) is **not**
exercised by CI. So "green CI" ≠ "works". The boxes below force the evidence CI
cannot produce. See `docs/DESIGN_DECISIONS.md` → "DoD & merge gate" for the
incident that motivated this.

**Label-based exemptions** (enforced by `dod-gate` CI — see `.github/workflows/dod-gate.yml`):

- `no-runtime-change` or `documentation` label: **exempts TDD evidence and Staging verification** (items 2 and 4 below). Use for pure-docs, CI/tooling, or provably no-behavior-change commits.
- **Closes discipline (item 6) is always enforced**, regardless of labels.

A PR may be merged only when:

- [ ] **Every Acceptance Criterion of the linked Issue is copied into the PR body and checked.** Not "most" — every one.
- [ ] **TDD evidence**: the new test failed before the change (RED) and passes after (GREEN). Paste the RED failure line.
- [ ] **Detection bug fixture rule**: if the fix targets a TUI prompt detection function (`_has_permission_prompt`, `_is_yn_prompt`, `_has_unknown_interactive`), add a real captured pane snapshot to `tests/fixtures/panes/` **before** writing the fix. The fixture must reproduce the bug (i.e. the broken detection returns a wrong value on that snapshot).
- [ ] **`pytest` + `ruff check` + `ruff format --check` + `pyright` all green** (CI covers this).
- [ ] **Staging behavior verified** (unless the [skip exceptions](#動作確認スキーム-必須) apply): RED reproduced + GREEN confirmed **on staging** (not just mocks), with log excerpts pasted under a `## Staging Evidence` heading (timestamp / thread / branch hash, clickable URLs). **Discord 側の証跡はユーザー提供のスクリーンショットが主**（サーバ上の AI は GUI を持たず Discord クライアントの画面を撮れない）。AI は **tmux ペインキャプチャ + REST 取得メッセージ本文**で補強する。長いログは `<details>` で畳む。**An empty Staging Evidence section = not done.**
- [ ] **No unrelated changes** in the diff; self-review done (`git diff main` が当該変更だけかを確認)。
- [ ] **Closes gating**: use `Closes #N` / `Resolves #N` **only if this PR satisfies 100% of that Issue's ACs**. If any AC is deferred, use `Refs #N` instead and leave the Issue open with a comment naming what remains. **キーワードは箇条書き (`- `) の中に入れず独立行に正確に書く**（リスト内だと GitHub が拾わない）。Never let a partial PR auto-close an Issue.

If you cannot check a box, the change is not done — do not merge. State the
blocker in the PR instead of silently dropping it.

### Issue authoring rules (write the Issue so it *can't* be half-done)

Most "completed but actually missing half" failures start at the Issue, not the
PR. When you (or Opus) author an Issue:

- **修正の前に必ず Issue 化し、ブランチを紐付ける。** 勝手に直し始めない（診断と合意が先）。
- **Why を必ず書く（背骨）。** 「この修正/実装は何のためか」を利用者目線で明記する。Issue は**クローズ後も「どういう考えでそうなったか」を辿る記録**なので、症状だけでなく意図を残す。Why が無いと次の判断ができない。
- **証跡（スクショ）を Issue にも残す。** テキストログだけでは表出しない問題（レイアウト・UI・ランプ状態など）があるため、**スクリーンショット**（理想は tmux の動作 × Discord の動作を合成した画像）で「リアルな問題」を示す。ユーザー提供のスクショは可能な限り Issue に入れる。網羅性を優先し、長いログ/コードは `<details><summary>` で畳んで可読性を保つ。
- **本文は常に「現在の真実」に保つ。** 相談で方針が変わったら 概要/原因/AC を実態に書き換える。ただし**重要な書き換えは日付つきコメントで「何を・なぜ変えたか」を一次記録として残す**（後続コメントがどの版の本文を前提にしていたか辿れるように）。積み残しは新規 Issue 乱立ではなく**再オープン＋コメント**で残す。
- **One Issue = one concern.** Do not bundle a bug fix with a design/enhancement task, or two unrelated behaviors, in one Issue. Bundling lets an agent satisfy the easy/testable half, write `Closes`, and auto-close the rest into oblivion. Split into separate Issues.
- **Acceptance Criteria must be binary and unambiguous** — each AC is a checkbox that is objectively true or false (a command to run, an observable output, a state to assert). No "should probably", no "consider", no open options ("A案 or B案") left in the AC. Decide before filing; move discussion out of the AC list.
- **Keep scope narrow.** If the Issue's title needs an "and", it is probably two Issues. A wide守備範囲 is where definitions go soft and items leak.
- **Deferred work is escalated, never implied.** If something is explicitly out of scope, say so in the Issue and link the follow-up. Silence is not a decision.

## Git & PR Workflow

- **Branch from `main`**: `feature/description`, `fix/description`, `docs/description`
- **CI must pass**: All 3 Python versions x (ruff check + ruff format + pytest)
- **No direct push to main**: Always create a PR
- **Squash merge preferred**: Keeps main history clean
- **Commit style**: `<type>: <description>` — types: feat, fix, refactor, docs, test, chore, security
- **Required checks**: `dod-gate` and `test (3.10/3.11/3.12)` are required status checks on `main`. **A PR with a red `dod-gate` cannot be merged — not by humans, not by sub-Claude.** If `dod-gate` is red, fix the PR body or the issue before merging. `enforce_admins=true` means the admin account is also blocked.

### Standard Development Flow (Mandatory)

Issue → branch → PR → **動作確認 + セルフレビュー** → merge → prod deploy。動作確認なしで merge しないこと。「直ったか分からない」状態を残さないのがルール。

1. **Issue 起票 / 受領** — 何を直すか / 何を作るかを明文化。バグなら**再現条件**を Issue に書く
2. **ブランチ作成 + TDD で実装** — 失敗テスト → 実装 → グリーン
3. **PR 作成** — Closes #N で Issue と連動、CI green を確認
4. **動作確認 (E2E on staging)** ← **必須** — 下記のスキーム
5. **セルフレビュー** — diff を読み返す / 不要な変更がないか / セキュリティ監査 (`security-audit` skill)
6. **Merge** (squash + delete branch) — **only after every [Definition of Done](#definition-of-done-dod--single-source-of-truth) box is checked.** A green CI is necessary but not sufficient.
7. **Prod redeploy** — `cd /home/yousan/c-lord && git pull && pgrep -f "/home/yousan/c-lord/.venv/bin/python3 -m c_lord.main" | xargs -r kill && sleep 3 && nohup uv run python -m c_lord.main > /tmp/clord-bot.log 2>&1 &`
   - ⚠️ **本番 venv のパスで絞ること。** 素の `pgrep -f c_lord.main` は staging clone (`c-lord-parallel-3` 等) のプロセスにもマッチするため、本番再起動のつもりで staging を巻き添えで kill してしまう。`/home/yousan/c-lord/.venv` を含むパスで絞れば本番プロセスだけに当たる (staging は `c-lord-parallel-3/.venv` なので除外される)。staging 側の再起動コマンド (下記 [動作確認スキーム](#動作確認スキーム-必須)) が `pgrep -f "c-lord-parallel-3.*c_lord.main"` で絞っているのと同じ要領。

### 動作確認スキーム (必須)

**ルール**: バグ修正 / 機能追加の PR は必ず staging 環境で「**修正前 = 再現できる**」「**修正後 = 再現しない (グリーン)**」を webhook 経由で確認する。これを通らないものはマージしない。

**前提**: staging 環境 (本番と独立した bot / channel) が `/home/yousan/c-lord-parallel-3` で常時稼働している。詳細は memory `project_staging_env.md` 参照。本番 (`/home/yousan/c-lord`) は kill しない。staging bot は共有リソースで、再起動・ブランチ切替の調整漏れは他セッションの検証を中断させるため、下記レシピ末尾の **原状復帰** を必ず実行する。

**手順** (バグ修正の例):
```bash
# 1. RED 再現 — staging 上で問題が発生することを webhook 経由で確認
curl -X POST -H "Content-Type: application/json" \
  -d '{"content":"<bug を再現する入力>"}' \
  "$E2E_TEST_WEBHOOK_URL?wait=true&thread_id=$E2E_TEST_THREAD_ID"
# → /tmp/clord-bot-staging.log で問題発生をログ確認
# → Discord 上で症状を確認 (REST API で fetch_messages)

# 2. 修正実装 + ユニットテスト

# 3. staging bot を新コードで再起動 (単一インスタンスで起動)
#    ⚠️ パターン kill は実行中の自分のシェル (eval 中の `c_lord.main` 文字列) にも当たって自滅することがある。
#       残存 count が 0 にならないときは PID 直指定 (kill <PID>) が安全。
pgrep -f "c-lord-parallel-3.*c_lord.main" | xargs -r kill; sleep 3
echo "remaining: $(pgrep -f 'c-lord-parallel-3.*c_lord.main' | wc -l)"   # ← 0 を確認
nohup uv run python -m c_lord.main > /tmp/clord-bot-staging.log 2>&1 &
sleep 2; echo "running: $(pgrep -f 'c-lord-parallel-3.*c_lord.main' | wc -l)"  # ← 1 を確認 (二重起動防止)

# 4. GREEN 確認 — 同じ webhook 入力で問題が再現しないこと
curl -X POST ... (上と同じ)
# → ログ + Discord 上の応答が期待通りであることを確認

# 5. PR 本文の "Test plan" にこの再現→修正のログ抜粋を貼る

# 6. 原状復帰 — 検証で切り替えていたなら idle ブランチに戻して上記 (#3) を再実行
#    idle ブランチは memory project_staging_env.md の "Default branch when idle" 参照
git checkout fix/wire-max-concurrent-sessions   # 例
```

**機能追加の場合**: RED の代わりに「実装前は存在しない挙動」「実装後は期待挙動」を webhook + ログで観測する。例: 構造化ログ追加 PR では `grep "thread=<id>" /tmp/clord-bot-staging.log` で **before = ヒットしない / after = enter/exit ペアが出る** を比較。

**スキップしてよい例外**:
- 純粋なドキュメント PR (CLAUDE.md / README のみ)
- 純粋なリファクタで挙動が変わらないことが自明 (それでも `pytest` は必須)

それ以外で staging 検証を省略するときは PR 本文に省略理由を書く。

## AI Agent Configuration

This project ships AI agent configs for all major tools:

| File | Tool | Purpose |
|------|------|---------|
| `CLAUDE.md` | Claude Code | Project context (this file) |
| `AGENTS.md` | OpenAI Codex | Symlink → CLAUDE.md |
| `.github/copilot-instructions.md` | GitHub Copilot | Condensed instructions |
| `.cursorrules` | Cursor | IDE-specific rules |

### Skills (`.claude/skills/`)

Project-specific skills that help AI agents work effectively on this codebase:

| Skill | Purpose |
|-------|---------|
| `tdd` | **Enforced** test-driven development — write tests FIRST, then implement |
| `verify` | Pre-commit quality gate (lint + format + test + security) |
| `add-cog` | Step-by-step guide to scaffold a new Cog |
| `security-audit` | Security checklist specific to subprocess/injection threats |
| `python-quality` | Python coding patterns and project conventions |
| `test-guide` | Testing patterns, mocking Discord objects, coverage goals |

### Commands (`.claude/commands/`)

| Command | Usage |
|---------|-------|
| `/verify` | Run full verification pipeline |
| `/new-cog <name>` | Scaffold a new Cog with tests |

### Hooks (`.claude/settings.json`)

- **PostToolUse (Edit/Write)**: Auto-format `.py` files with ruff after editing

## What Does NOT Belong Here

- Personal bot configuration (tokens, channel IDs, user IDs)
- Server-specific Cogs or workflows
- Direct Anthropic API calls (we use Claude Code CLI, not the API)
- Heavy dependencies that most users won't need
- Anything that requires secrets to import the package
