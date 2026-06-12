# c-lord (c-lord)

Discord frontend for Claude Code CLI. **This is a framework (OSS library), not a personal bot.**

**略称: c-lord** (c-lord)

## 理念 (Philosophy) — 迷ったらここに照らす

C-lord が「何のため・誰のどの痛みを解決するか」を定めた理念は **[docs/PHILOSOPHY.md](docs/PHILOSOPHY.md)** にある。機能や仕様の良し悪しで迷ったとき、コードや好みではなく**まずこの理念に照らして判断する**。理念と現実がぶつかったら「理念を見直す / 例外として記録する / 実装上の制約として切り分ける」のいずれかを明示的に選ぶ。下の各規律（行動規範・DoD 等）はこの理念の手段side。

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
3. **Emoji reactions for status** (#246): The per-turn lamp is a single reaction on the user's trigger message — 🟢 running (kept through thinking/tools) → 🟡 waiting (turn done), with ❌ error / ⏳⚠️ stall / 🗜️ compact as temporary overrides. Applied immediately (no debounce). Reactions use a different Discord rate-limit bucket than thread renames, so this replaced the per-turn thread-name lamp that saturated the ~2-renames-per-10-min limit (#241); the thread-name 🟢/🟡 is now the slow, poll-driven sidebar view. See `docs/specs/thread-lamp.md`.
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
# 1. Bot 再起動 — 必ず scripts/staging.sh を使う (#327)。
#    pgrep パターン kill / nohup uv run は事故パターンとして禁止 (docs/STAGING.md 参照)
bash scripts/staging.sh restart

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

**Token 取得**: **本番 `.env` を絶対パスで直接読む** (`/home/yousan/c-lord/.env`。パスは運用に合わせて)。

⚠️ **旧規約(並行 clone の `.env` を本体に symlink)は廃止された** (#326)。symlink された `.env` は「その clone で bot を起動すると本番トークンで第2の本番 bot が立つ」地雷だったため (#322 根因)、各並行 clone (`c-lord-parallel`, `c-lord-parallel-2`, `c-lord-issue63`) の `.env` は現在**無効なセンチネル値を持つ実ファイル**になっており、bot を起動しても Discord ログインに失敗して即終了する。**新しい並行 clone を作るときも symlink を張らないこと**(token はどこからでも上の絶対パスで読める)。

**読み取り (任意の thread / channel のメッセージ取得)**:

```bash
# どの clone / session_dir からでも、本番 .env を絶対パスで読む (#326 以降 symlink は無い)
TOKEN=$(grep '^DISCORD_BOT_TOKEN=' /home/yousan/c-lord/.env | cut -d= -f2-)

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

**bot 内 spawn 子 Claude (`c-lord-sessions/<ch>/<thr>/` cwd) の場合**: env は strip されているが、ファイル読みは strip 対象外なので、上と同じく絶対パス (`cat /home/yousan/c-lord/.env`) で token を取得可能。

**代替: c-lord REST API (`ext/api_server.py`)**:
api_server をオプトインで有効化してある環境では `POST /api/threads/{thread_id}/messages` で同等の操作が可能 (詳細は `docs/COMMANDS.md`)。bot を再起動せずに有効化する手段はないため、デバッグ目的では上の curl が手軽。

## Debugging & Troubleshooting

Bot の挙動が怪しいとき、最初に見るべき情報源は **bot ログ** と **Discord 上のメッセージ** の 2 つ。前者のパスは `bash scripts/staging.sh status` が表示する (per-run ログ + 最新への symlink `/tmp/clord-bot-<clone名>.log`。旧運用の固定パス `/tmp/clord-bot.log` / `/tmp/clord-bot-staging.log` は名残り)。後者は `.env` を読んで Discord REST API を curl で叩く ("Debugging Discord from a Claude session" 参照 — Claude Code から実行可)。

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
  - **起点の問い・診断質問は、内容の大小に関わらず必ず〈回答＋合意〉を先に通す。例外を作らない。** 「軽微だから」「明らかにバグだから」という理由での勝手な着手も禁止する（その曖昧さこそが再発の元 — "軽微" "明らか" を判定する基準は無く、抜け道になる）。合意が取れたら、あとは下の「自走の範囲」どおり自走してよい。
  - **止めて答える分、回答自体は手短に速く返す**（長考で待たせない — 回答までの時間も体験に影響する）。
- **自走の範囲＝合意済みの作業。** 「やる」と決まった作業は **PR → 手動QA → マージ → 本番デプロイ → 本番実測**まで自走してよい（途中で逐一停止しなくてよい）。ただし起点の問いはまだ作業ではない → 診断して合意してから着手する。
- **柔らかい依頼（「ここ直ってる？」）は「答え→行動」の順で分離する。** 例:「直っていません。これは仕様ではなくバグです。→ 直しました: PR #NNN / Evidence: <URL>」。先頭で事実に答えれば、相手が「答えだけ欲しかった」場合も意図のズレに気づける。安価で可逆なら先回り可。**設計変更・破壊操作・不可逆な判断は推察で走らず一行確認**。
- **迎合しない・鵜呑みにしない。** 事実ベースで判断し、報告と食い違えば指摘する。ユーザーの仮説も自分のデータで確認し、違えばその判断を優先して伝える。
- **動きを変えたら「あるべき動き」の記述も一緒に直す。** 利用者から見た動きを変える PR は、その動きを説明したドキュメント（仕様・README・docs の該当箇所）も同じ PR で更新する。変えないなら「動きは変えていない」と一言書く（PR 本文に `no-user-visible-change`）。これをしないと「あるべき動き」の記述が実態とズレ（drift し）、後から読んだ人が古い説明を信じてしまう。実際に `docs/ARCHITECTURE.md` / `README.md` には #53 で消したモジュール（`runner.py` / `chunker.py` など）の記述がしばらく残っていた。詳しい書式は DoD のチェック項目を参照。
- **コードより先に実機の挙動を見る。** 内部実装を熟知した開発者目線ではなく、**利用者目線で「使って不便か」**を確認する。これが無いと、その Issue/PR が何のためにあるか（Why）が掴めない。
- **報告は簡潔・日本語・URL はクリッカブルに。** 完了は `#NNN done / PR #MMM / Evidence: <URL>` を1行で。実機未確認なら「テスト緑・実機未確認」と正直に書く（過大申告しない）。
- **完了報告は「これがこうなる」形で書く（利用者目線を先頭・実装/CI を従）。** Discord への完了報告は、まず**「どこで何を操作すると、Discord 上の見え方がどう変わるか」**を先頭に書く。実装の詳細（関数名・`pytest 37/37`・`dod-gate green`・行番号）はその**後ろ（従）**に置く。コードを読まない利用者でも「自分にとって何が変わったか」が一読で分かるようにするため。利用者の見え方が変わらない変更（内部リファクタ等）は `no-user-visible-change` と明記する。
  - 良い例: 「スレッドに長文を送ると、最後まで複数メッセージで届くようになりました（前は途中で切れていました）。実装: チャンク分割を `_split_reply` に追加 / PR #MMM / pytest green」
  - 悪い例: 「`_split_reply` を追加し pytest 37/37 green、dod-gate green」（利用者から見て何が変わったか分からない）
- **証跡はスクショを主にする（利用者から見た挙動/UI を変えるとき）。** 「直った / こう変わった」を示す証跡は、テキストログではなく**スクショ（Discord 実画面 and/or tmux ペイン）を主証跡**にする。テキストでは表出しない問題（レイアウト・ボタンの出/不出・ランプ状態・別バブル/同一バブル）があるため。利用者に見えない変更（純リファクタ等）はスクショ不要（`no-user-visible-change`）。**Discord 実画面は AI 自身が撮る（#243）**: `scripts/discord_evidence_shot.sh` にメッセージリンク/チャンネル URL を渡す（bot ホスト上でのみ動く。セットアップ・制約は `docs/discord-evidence-capture.md`）。人間提供のスクショは AI のキャプチャが届かない場面（テストアカウント未参加のサーバ・ログイン失効時など）の補助。原因側の tmux ペイン PNG（#286）とペアで A=結果 / C=原因 を揃える（#287）。注釈／before-after 並置の補助ツールは #310。
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

- `no-runtime-change` or `documentation` label: **waives the DoD-completion checklist below** (notably the **TDD evidence**, **Staging verification**, and the **evidence-link requirement**). Use for pure-docs, CI/tooling, or provably no-behavior-change commits.
- **`Closes` discipline is always enforced**, regardless of labels: if a PR uses `Closes`/`Resolves #N`, that Issue's Acceptance Criteria must be 100% met (else use `Refs #N`).
- **Evidence-link required** (#391): a PR **without** an exempt label must contain a 証跡 image (`![...](URL)`) or a Release-asset URL in its body, or `dod-gate` fails. Text logs alone do not pass — paste an actual screenshot/URL (`scripts/evidence_upload.py`), or apply `no-runtime-change`/`documentation` if evidence is genuinely N/A.

<!-- 参照はルール名で固定（番号で指さない）。項目を挿入すると番号がずれ免除対象がずれるため。dod-gate.yml の実挙動: ラベルあり → DoD 完了要件 (Rule 1) + 証跡リンク必須 (Rule 3) を免除 / `Closes` 規律 (Rule 2) は常時適用。 -->

A PR may be merged only when:

- [ ] **Every Acceptance Criterion of the linked Issue is copied into the PR body and checked.** Not "most" — every one.
- [ ] **TDD evidence**: the new test failed before the change (RED) and passes after (GREEN). Paste the RED failure line.
- [ ] **Detection bug fixture rule**: if the fix targets a TUI prompt detection function (`_has_permission_prompt`, `_is_yn_prompt`, `_has_unknown_interactive`), add a real captured pane snapshot to `tests/fixtures/panes/` **before** writing the fix. The fixture must reproduce the bug (i.e. the broken detection returns a wrong value on that snapshot).
- [ ] **`pytest` + `ruff check` + `ruff format --check` + `pyright` all green** (CI covers this).
- [ ] **Staging behavior verified** (unless the [skip exceptions](#動作確認スキーム-必須) apply): RED reproduced + GREEN confirmed **on staging** (not just mocks), with log excerpts pasted under a `## Staging Evidence` heading (timestamp / thread / branch hash, clickable URLs). **Discord 側の証跡は AI が `scripts/discord_evidence_shot.sh` で撮った実画面スクショが主**（#243。bot ホスト上で実行）。tmux ペインキャプチャ（#286）+ REST 取得メッセージ本文で補強し、人間提供スクショは AI のキャプチャが届かない場面の補助とする。**証跡 PNG は `scripts/evidence_upload.py` で GitHub Release アセット（prerelease タグ `evidence`）にアップロードし、その URL を PR/Issue 本文に貼る（git ツリーには commit しない — バイナリで履歴が膨らむ＆業界的にも off-repo + リンクが定石）。証跡は催促される前に貼る（必須）。Discord CDN の添付 URL は期限付きなので直貼りしない**（詳細は `docs/discord-evidence-capture.md`、#390）。長いログは `<details>` で畳む。**An empty Staging Evidence section = not done.** 挙動/UI を変える報告は**スクショを主証跡**にする（テキストログは補助。証跡規約 #291 → #350 で更新）。
- [ ] **動きを変えたら「あるべき動き」も更新する**: 利用者から見た動きを変える PR は、対応する「あるべき動き」を説明したドキュメント（`docs/specs/` などの仕様・`README.md`・`docs/` の該当箇所）も同じ PR で更新する。動きを変えないなら PR 本文に `no-user-visible-change` と明記する（更新不要であることをはっきり宣言する）。**狙い**: ドキュメントが実際の動きとズレる（drift する）のを防ぎ、「あるべき動き」を信用できる状態に保つ。
- [ ] **No unrelated changes** in the diff; self-review done (`git diff main` が当該変更だけかを確認)。
- [ ] **Closes gating**: use `Closes #N` / `Resolves #N` **only if this PR satisfies 100% of that Issue's ACs**. If any AC is deferred, use `Refs #N` instead and leave the Issue open with a comment naming what remains. **キーワードは箇条書き (`- `) の中に入れず独立行に正確に書く**（リスト内だと GitHub が拾わない）。Never let a partial PR auto-close an Issue.

If you cannot check a box, the change is not done — do not merge. State the
blocker in the PR instead of silently dropping it.

### Issue authoring rules (write the Issue so it *can't* be half-done)

Most "completed but actually missing half" failures start at the Issue, not the
PR. When you (or Opus) author an Issue:

- **修正の前に必ず Issue 化し、ブランチを紐付ける。** 勝手に直し始めない（診断と合意が先）。
- **Why を必ず書く（背骨）。** 「この修正/実装は何のためか」を利用者目線で明記する。Issue は**クローズ後も「どういう考えでそうなったか」を辿る記録**なので、症状だけでなく意図を残す。Why が無いと次の判断ができない。
- **証跡（スクショ）を Issue にも残す。** テキストログだけでは表出しない問題（レイアウト・UI・ランプ状態など）があるため、**スクリーンショット**（理想は tmux の動作 × Discord の動作を合成した画像）で「リアルな問題」を示す。**問題発生中（RED）の実画面は AI 自身が `scripts/discord_evidence_shot.sh` で撮って Issue に添付する**（yousan からメッセージリンクをもらう、または REST で該当箇所を特定する）。ユーザー提供のスクショもあれば併せて入れる。網羅性を優先し、長いログ/コードは `<details><summary>` で畳んで可読性を保つ。
- **「あるべき見た目」はデザインカンプで絵にして合意する（#316）。** Discord 上の見た目・挙動を**提案**する Issue/設計では、`scripts/discord_mockup.py` で手組み spec から Discord 風モック PNG を描き、Issue に添付して「**ゴールはこれ**」を絵で合意する（散文 spec では yousan が裁定できない — #287 P1）。スクショ（上項）が「実際こうだった」の証跡なのに対し、こちらは「**こうあるべき**」のモック。使い方は `docs/design-comp-mockup.md`。
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
7. **Prod redeploy** — `cd /home/yousan/c-lord && git pull && bash scripts/staging.sh restart`
   - スクリプトは実行ディレクトリの bot **だけ**を `/proc/<pid>/cwd` で同定して PID 直 kill する(staging を巻き添えにしない)。pgrep パターン kill は禁止 — 事故パターン一覧は `docs/STAGING.md` 参照。
   - ⚠️ 本番が supervisor (systemd --user / start-clord.sh --guard) 配下で動いている場合は supervisor 経由で再起動すること(手動 kill+起動は #195 の二重 bot 事故の元)。

### 動作確認スキーム (必須)

**ルール**: バグ修正 / 機能追加の PR は必ず staging 環境で「**修正前 = 再現できる**」「**修正後 = 再現しない (グリーン)**」を webhook 経由で確認する。これを通らないものはマージしない。

**前提**: staging 環境 (本番と独立した bot / channel) が `/home/yousan/c-lord-parallel-3` で常時稼働している。本番 (`/home/yousan/c-lord`) は kill しない。staging bot は共有リソース — 借用前の占有確認と検証後の**原状復帰**を必ず行う。

**手順の詳細は [docs/STAGING.md](docs/STAGING.md) が唯一の正**(レイアウト・占有プロトコル・禁止事項・増設手順を含む)。骨子:

```bash
cd /home/yousan/c-lord-parallel-3
export CLORD_LEASE_OWNER="<セッション識別子>"
bash scripts/staging.sh status                          # 0. 占有・状態確認
bash scripts/staging.sh borrow --purpose "PR #NNN 検証"  # 1. 借用 (他人の有効リース中は拒否される)
bash scripts/staging.sh restart main                    # 2. RED — 修正前コードで再現 (webhook 経由)
bash scripts/staging.sh restart <fix-branch>            # 3. GREEN — 修正後コードで再現しない
bash scripts/staging.sh restart main                    # 4. 原状復帰 (idle ブランチ = main)
bash scripts/staging.sh release                         # 5. 返却
```

webhook での再現入力・前提条件 (`E2E_TEST_THREAD_ID` のスレッドに sessions レコードが必要、等) は STAGING.md の「検証レシピ」を参照。PR 本文の "Staging Evidence" には RED→GREEN のログ抜粋に加え、**RED / GREEN それぞれの Discord 実画面スクショ**を主証跡として貼る — `scripts/discord_evidence_shot.sh "<検証スレッドの URL>" -o red.png` のように撮り、`scripts/evidence_upload.py red.png green.png --issue <N>` で `evidence` リリースにアップロードして出力された URL を参照する (#390。git には commit しない)。

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
