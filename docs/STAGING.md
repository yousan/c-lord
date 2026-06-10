# STAGING.md — staging 運用の単一ソース (#327)

**staging の起動・停止・検証・原状復帰の手順はこのファイルと `scripts/staging.sh` が唯一の正**。
CLAUDE.md・メモリ・他ドキュメントに別レシピが書いてあったら、それは drift — このファイルを正として直すこと。
背景(なぜこの形になったか)は Issue #322(調査まとめ)と `docs/DESIGN_DECISIONS.md` §13 を参照。

## 環境レイアウト

| | 本番 (prod) | staging |
|---|---|---|
| clone | `/home/yousan/c-lord` | `/home/yousan/c-lord-parallel-3` |
| bot | `C-lord#8255` (`1475105094071750818`) | `C-lord-3#1206` (`1503195981142032405`) |
| channel | `.env` の `DISCORD_CHANNEL_ID` | `#c-lord-3` (`1503196656265597082`) |
| API port | 8087 | 8089 |
| tmux session | `c-lord` | `c-lord-parallel-3` |
| session dir | `c-lord-sessions/` | `c-lord-sessions-staging/` |
| ログ (最新) | `/tmp/clord-bot-c-lord.log`* | `/tmp/clord-bot-c-lord-parallel-3.log`* |
| ライフサイクル | **supervised**(手動 kill+nohup 禁止 — #195) | `scripts/staging.sh` で手動管理 |
| idle ブランチ | `main` | **`main`** |

\* `scripts/staging.sh` 導入後は per-run ログ `/tmp/clord-bot-<name>-<timestamp>.log` + 最新への symlink。
旧固定パス (`/tmp/clord-bot.log` / `/tmp/clord-bot-staging.log`) は旧手順の名残り。

> **idle ブランチについて**: かつて CLAUDE.md は `fix/wire-max-concurrent-sessions` を idle ブランチとしていたが、
> このブランチは staging DB のスキーマ前進 (`mirror_replied_uuid`, #232/#242) と不整合で**起動クラッシュする**。
> idle = `main` が現在の正(2026-06-10 改訂)。

## 安全原理(コードで強制されているもの)

1. **directory == identity** (#324): `.env` に書かれたキーは継承 env に常に勝つ。正しいディレクトリで起動すれば正しい bot になる。
2. **identity fail-fast** (#323): `.env` の `EXPECTED_BOT_USER_ID` と実ログイン identity が違えば bot は即 exit(1)。**新しい環境の .env には必ず設定すること。**
3. **センチネル .env** (#326): `c-lord-parallel` / `-2` / `c-lord-issue63` の `.env` は無効値。そこから bot は起動できない(本番 token の読み取りは `/home/yousan/c-lord/.env` を絶対パスで)。
4. **単一インスタンス flock**: 同一 data dir の二重起動 (#212) に加え、**同一トークンの二重起動**もホスト全域ロックで拒否される (#325, `~/.cache/c-lord/locks/token-<hash>.lock`)。別ディレクトリからでも同じ bot は2つ立てられない。緊急回避は `CLORD_ALLOW_MULTI_INSTANCE=1`(両ロックを無効化 — 理解した上でのみ)。

## 操作 — `scripts/staging.sh`(clone のルートで実行)

```bash
cd /home/yousan/c-lord-parallel-3

bash scripts/staging.sh status             # identity / branch / pid / instances / log
bash scripts/staging.sh stop               # この clone の bot を安全停止
bash scripts/staging.sh restart            # 現在のブランチで安全再起動
bash scripts/staging.sh restart <branch>   # branch に切り替えて再起動 (PR 検証用)
```

`restart` は: /proc/cwd で自分の bot だけを同定 → PID 直 kill → setsid + venv python で起動 →
per-run ログ → `Logged in as` を待って identity を検証(mismatch なら非0で失敗)→ 単一インスタンス確認、まで自動で行う。

**禁止事項(全て実害のあった事故パターン — #322 根因D)**:
- `pgrep -f "c_lord.main" | xargs kill` 系の**パターン kill**(本番・自分のシェルに当たる/相対パス起動を取り逃す)
- kill を**並列ツールバッチに入れる**(キャンセルしても発射済みの kill は戻らない)
- `nohup uv run ...` での起動(Bash ツール teardown で exit 144 死する)
- 本番 (`/home/yousan/c-lord`) への手動 kill+nohup(supervised — #195 の二重 bot 事故になる)

## 占有(借用)プロトコル (#328)

staging は**共有リソース**。複数セッションが同時に使うと kill / checkout の踏み合いになる(2026-05-29 実害)。
占有はリースファイル(clone 直下の `.staging-lease`、環境ごとに1枚・中央台帳なし)で機械的に管理する:

```bash
cd /home/yousan/c-lord-parallel-3
export CLORD_LEASE_OWNER="<自分のセッション識別子>"   # 例: claude-session-<thread_id>

bash scripts/staging.sh borrow --purpose "PR #NNN 検証" [--ttl-hours 2]
#   → 他セッションの有効リース中なら拒否され、誰が・何のために・いつまでが表示される
#   → 失効リースは奪取できる(旧リース内容がログに残る)

bash scripts/staging.sh restart <branch>   # ← 有効な自リースが無いと拒否される
bash scripts/staging.sh stop               # ← 他人の有効リース中は拒否される

bash scripts/staging.sh release            # 検証後の原状復帰とセットで必ず実行
```

**ルール**: `restart` は常に自リース必須(borrow → restart → … → restart main → release)。
`stop` はリースが無ければ可(掃除目的)、他人の有効リース中は不可(検証中の bot を殺さない)。
リースの確認だけなら `status`(lease 行に owner / purpose / TTL が出る)。

> 旧ドキュメントの「Lounge API (`/api/lounge`) で占有を宣言」は**使えない**:
> `CLORD_BRIDGE_MODE=jsonl` の本デプロイでは ApiServer 自体が起動しない(#322 根因B)。

## 検証レシピ(RED→GREEN on staging)

**前提**(これを満たさないと curl が静かに no-op して偽 GREEN になる — #322 根因C):
- staging の `.env` に `E2E_TEST_WEBHOOK_URL` と **`E2E_TEST_THREAD_ID`** が設定されていること
- `E2E_TEST_THREAD_ID` のスレッドが staging の `sessions.db` に**セッションレコードを持つ**こと
  (持たないスレッドへの投稿は `on_message` が無視する。チャンネル直投稿も Claude を起動しない)
- 確認/再導出: `python3 -c "import sqlite3; print(sqlite3.connect('data/sessions.db').execute('select thread_id from sessions order by last_used_at desc limit 3').fetchall())"`

```bash
cd /home/yousan/c-lord-parallel-3
set -a; . ./.env; set +a   # E2E_* を読み込む

# 1. RED — 修正前のコード (通常 main) で問題を再現
bash scripts/staging.sh restart main
curl -X POST -H "Content-Type: application/json" \
  -d '{"content":"<bug を再現する入力>"}' \
  "$E2E_TEST_WEBHOOK_URL?wait=true&thread_id=$E2E_TEST_THREAD_ID"
# → ログ (staging.sh status が示す per-run ログ) と Discord で症状を確認

# 2. GREEN — 修正ブランチで再現しないことを確認
bash scripts/staging.sh restart <fix-branch>
curl -X POST ...(同じ入力)

# 3. 原状復帰(必須)
bash scripts/staging.sh restart main && rm -f .staging-lease
```

証跡の規約(スクショ主・ログ従)は CLAUDE.md の DoD を参照。

## staging を増設するとき(チェックリスト)

環境が増えても `staging.sh` はそのまま使える(全値をディレクトリから導出)。増設手順:

1. Discord Developer Portal で新 bot application を作成 → token 取得(1 token = 1 接続なので既存と共用不可)
2. サーバに専用チャンネルを作成し、bot を招待(送信・スレッド権限)。チャンネルに Webhook を作成
3. `git clone` で新ディレクトリ(例 `/home/yousan/c-lord-parallel-4`)を作成、`uv sync --dev`
4. `.env` を**実ファイル**で作成(symlink 禁止 — #326)。必須: `DISCORD_BOT_TOKEN` / `DISCORD_CHANNEL_ID` /
   **`EXPECTED_BOT_USER_ID`(新 bot の user id)** / `CLORD_API_PORT`(未使用ポート、#258 で自動化予定) /
   `SESSION_DIR_BASE`(専用ディレクトリ) / `E2E_TEST_WEBHOOK_URL`
5. `bash scripts/staging.sh restart` → `status` で identity を確認
6. チャンネルで `/clord-init` を実行して repo を bind
7. E2E 用スレッドを 1 つ作って `E2E_TEST_THREAD_ID` を `.env` に追記
8. この表(環境レイアウト)に行を追加する

## トラブルシュート

| 症状 | 見る場所 | 典型原因 |
|---|---|---|
| `IDENTITY MISMATCH` で起動失敗 | per-run ログ | 意図と違う token(.env の token と EXPECTED_BOT_USER_ID の組を確認)。**ガードが正しく働いている** |
| webhook を投げても無反応 | `E2E_TEST_THREAD_ID` の sessions レコード有無 | スレッドにセッションが無い / thread_id 空(前提節を参照) |
| `instances: 2+` | `staging.sh status` | 二重起動 — `stop` → `restart`。手動 kill 禁止事項を守ったか確認 |
| 起動直後に死ぬ | per-run ログ末尾 | LoginFailure(token 不正)/ DB スキーマ不整合(古いブランチ — idle は main) |
| `API server not listening` | 仕様 | `CLORD_BRIDGE_MODE=jsonl` では ApiServer は起動しない(バグではない) |
