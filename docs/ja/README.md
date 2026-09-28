> **Note:** This is an auto-translated version of the original English documentation.
> If there are any discrepancies, the [English version](../../README.md) takes precedence.
> **注意:** これは英語のオリジナルドキュメントを自動翻訳したものです。
> 内容に相違がある場合は、[英語版](../../README.md)が優先されます。

# c-lord

[![CI](https://github.com/yousan/c-lord/actions/workflows/ci.yml/badge.svg)](https://github.com/yousan/c-lord/actions/workflows/ci.yml)
[![Python 3.10+](https://img.shields.io/badge/python-3.10%2B-blue.svg)](https://www.python.org/downloads/)
[![License: MIT](https://img.shields.io/badge/License-MIT-yellow.svg)](https://opensource.org/licenses/MIT)

**スマホの Discord から Claude Code をガンガン使おう。複数スレッドを同時に回して、本格開発もOK。**

Discord のスレッドを開くだけで、Claude Code セッションが立ち上がります。スマートフォンから何スレッドでも並行して動かせます — あるスレッドで機能開発、別のスレッドで PR レビュー、さらに別のスレッドでバックグラウンドタスク。全部同時進行。スレッドごとにリポジトリの独立した clone で作業するので、セッション同士が同じ作業ツリーを編集することはありません。

**[English](../../README.md)** | **[简体中文](../zh-CN/README.md)** | **[한국어](../ko/README.md)** | **[Español](../es/README.md)** | **[Português](../pt-BR/README.md)** | **[Français](../fr/README.md)**

> **免責事項:** このプロジェクトは Anthropic とは無関係であり、承認や公式な関係はありません。「Claude」および「Claude Code」は Anthropic, PBC の商標です。これは Claude Code CLI と連携する独立したオープンソースツールです。

> **Claude Code によって完全構築。** このコードベース全体（アーキテクチャ、実装、テスト、ドキュメント）は Claude Code 自身によって書かれました。人間の著者は自然言語で要件と方向性を提供しましたが、ソースコードを手動で編集していません。詳細は[このプロジェクトの構築方法](#このプロジェクトの構築方法)をご覧ください。

---

## 基本的なアイデア: スレッドごとに独立した clone で並行セッション

Discord の各スレッドは、それぞれが 1 つの Claude Code セッションで、サーバー上の専用 tmux ウィンドウで動きます。複数のスレッドにタスクを送れば並行して走り、ラップトップを閉じても動き続け、スレッドに返信すれば続きから再開できます。

並行セッションが互いの作業を壊さない理由は **隔離** です。チャンネルをリポジトリに紐づけると（`/clord-init`）、各スレッドはそれぞれ独立した `git clone` の中で作業します。セッション同士が作業ツリーを共有しないので、あるスレッドの未コミットの変更が別のスレッドに現れることはありません。セッションが出会うのは git リモート（ブランチ・push・PR）だけで、人間の開発者どうしと同じです。

```
スレッド A (機能開発)    ──→  Claude Code  (専用 clone)  ─┐
スレッド B (PR レビュー)  ──→  Claude Code  (専用 clone)   ├─→  git リモート（ブランチ・PR）
スレッド C (ドキュメント) ──→  Claude Code  (専用 clone)  ─┘
```

**現在、セッション同士は互いに調整していません。** 以前のこの README は 4 つの協調の仕組みを説明していましたが、実際の状態は次のとおりです（[#758](https://github.com/yousan/c-lord/issues/758)）:

| 仕組み | 実際の状態 |
|---|---|
| **並行処理の注意書き**（並行作業について Claude に伝える指示） | 毎ターン組み立てられますが **届いていません** — セッションは tmux TUI で動いており（#53）、ターンごとにシステムプロンプトを渡す経路が無いため、その文字列は捨てられています |
| **アクティブセッションレジストリ** | メモリ上に存在し `/workspace-cleanup` が使っていますが、**セッション側からは見えません**（同じ捨てられる文字列に乗っているため） |
| **協調チャンネル** | **既定でオフ。** `COORDINATION_CHANNEL_ID` を設定すると、セッションのターンが終わったときに 1 行の通知を投稿します。開始イベントはありません |
| **AI Lounge** | REST エンドポイントは動きますが、**Claude にラウンジの存在を伝えるものが何も無い**ため、実際には誰も投稿しません — 下の「AI Lounge（セッションには届いていません）」を参照 |

つまり、作業ツリーは分かれていますが、リアルタイムの協調はありません。clone の外にあるもの — 同じリモートブランチ、共有のポートやデータベース、staging Bot など — は衝突しえます。スレッドごとに別のブランチを使ってください。

---

## できること

### インタラクティブチャット（モバイル / デスクトップ）

Discord が動く場所ならどこでも Claude Code を使用 — スマートフォン、タブレット、デスクトップ。各メッセージがスレッドを作成または継続し、永続的な Claude Code セッションと 1:1 でマッピングされます。

### 並行開発

複数のスレッドを同時に開きます。各スレッドは独自のコンテキストと、リポジトリの専用 git clone を持つ独立した Claude Code セッションです。よくある使い方:

- **機能開発 + レビューの並行**: あるスレッドで機能を開発しながら、別のスレッドで Claude が PR をレビュー。
- **複数の開発者**: チームメンバーそれぞれが自分のスレッド（と専用の clone）を持つので、セッション同士がディスク上の同じファイルを編集することはありません。
- **安全な実験**: スレッド A でアプローチを試しながら、スレッド B を安定したコードで維持。

### スケジュールタスク（SchedulerCog）

コード変更なし、再デプロイなしで、Discord の会話または REST API から定期的な Claude Code タスクを登録。タスクは SQLite に保存され、設定可能なスケジュールで実行されます。Claude はセッション中に `POST /api/tasks` で自己登録できます。

```
/skill name:goodmorning         → 即時実行
Claude が POST /api/tasks 呼び出し → 定期タスクを登録
SchedulerCog（30 秒マスターループ）  → 期限のタスクを自動実行
```

### CI/CD 自動化

GitHub Actions から Discord webhook 経由で Claude Code タスクをトリガー。Claude が自律的に動作 — コードを読み、ドキュメントを更新し、PR を作成し、自動マージを有効化します。

```
GitHub Actions → Discord Webhook → Bridge → Claude Code CLI
                                                  ↓
GitHub PR ←── git push ←── Claude Code ──────────┘
```

**実例:** main へのプッシュごとに、Claude が diff を分析し、英語・日本語ドキュメントを更新し、バイリンガル PR を作成し、自動マージを有効化します。人間の操作は不要。

### セッション同期

Claude Code CLI を直接使っている場合は `/sync-sessions` で既存のターミナルセッションを Discord スレッドに同期。最近の会話メッセージを補完するので、スマートフォンから CLI セッションの続きを開けます。

### AI Lounge（セッションには届いていません）

> **状態: 配線されていません**（[#758](https://github.com/yousan/c-lord/issues/758)）。ラウンジは、一時的なシステムコンテキストとしてすべてのセッションに注入される設計でした。しかしセッションは tmux TUI で動いており（#53）、ターンごとにそれを渡す経路が無いため、c-lord は文字列を組み立てたうえで捨てています。Claude にはラウンジの存在が伝わらず、実際には誰も投稿しません。

REST エンドポイント自体は動くので、自分で使うことはできます — たとえばリポジトリの `CLAUDE.md` にラウンジのことを書いておく方法があります:

```bash
# メモを投稿（SQLite に保存。ラウンジチャンネル — 既定は COORDINATION_CHANNEL_ID — にも転送）
curl -X POST "http://localhost:8080/api/lounge" \
  -H "Content-Type: application/json" \
  -d '{"message": "feature/oauth で auth リファクタリング開始", "label": "機能開発"}'

# 最近のメモを確認
curl "http://localhost:8080/api/lounge"
```

### プログラム的なセッション作成

スクリプト、GitHub Actions、または他の Claude セッションから Discord のメッセージ操作なしで新しい Claude Code セッションを起動できます。

```bash
# 別の Claude セッションや CI スクリプトから:
curl -X POST "$CLORD_API_URL/api/spawn" \
  -H "Content-Type: application/json" \
  -d '{"prompt": "リポジトリのセキュリティスキャンを実行", "thread_name": "Security Scan"}'
# スレッド作成後すぐに返答し、Claude はバックグラウンドで実行
```

Claude のサブプロセスには `DISCORD_THREAD_ID` 環境変数が渡されるため、実行中のセッションから子セッションを起動して作業を並列化できます。

### スタートアップリジューム

Bot の再起動中にセッションが中断された場合、Bot が再起動したときに自動的に再開されます。リジューム登録の方法は 3 つあります:

- **自動（アップグレード再起動）** — `AutoUpgradeCog` がパッケージアップグレードによる再起動直前にアクティブなセッションをすべてスナップショットし、自動でリジューム登録します。
- **自動（任意のシャットダウン）** — `ClaudeChatCog.cog_unload()` が任意のシャットダウン方法（`systemctl stop`、`bot.close()`、SIGTERM 等）でも実行中のセッションを自動登録します。
- **手動** — `POST /api/mark-resume` を直接呼び出して登録することもできます。

---

## 機能

### インタラクティブチャット

#### 🔗 セッションの基本
- **Thread = Session** — Discord スレッドと Claude Code セッションの 1:1 マッピング
- **セッション永続化** — `--resume` で複数メッセージをまたいだ会話を継続
- **並行セッション** — 設定可能な上限での複数並行セッション
- **削除せず停止** — `/stop` でセッションを保持したまま停止し、リジューム可能
- **セッション割り込み** — アクティブなスレッドに新しいメッセージを送ると実行中のセッションに SIGINT を送り、新しい指示で即座に再開。手動での `/stop` 不要

#### 📡 リアルタイムフィードバック
- **リアルタイムステータス** — 絵文字リアクション: 🧠 思考中、🛠️ ファイル読み取り、💻 編集中、🌐 Web 検索
- **ストリーミングテキスト** — Claude の作業中に中間テキストがリアルタイムで表示
- **ツール結果 embed** — ライブツール呼び出し結果（10 秒ごとに経過時間を更新）
- **拡張思考** — 推論をスポイラータグ付き embed で表示（クリックで展開）
- **スレッドダッシュボード** — アクティブ/待機スレッドを表示するライブピン embed。入力が必要なときはオーナーを @mention

#### 🤝 ヒューマンインザループ
- **インタラクティブな質問** — `AskUserQuestion` を Discord ボタンまたは Select Menu でレンダリング。回答でセッション再開。ボタンは Bot 再起動後も有効
- **Plan Mode** — Claude が `ExitPlanMode` を呼び出すと、プラン全文と Approve/Cancel ボタンを含む Discord embed を表示；承認後にのみ実行を開始；5 分でタイムアウト（自動キャンセル）
- **ツール実行許可リクエスト** — Claude がツールを実行するために許可が必要な場合、ツール名と入力内容を示した Allow/Deny ボタンを Discord に表示；2 分間応答なしで自動拒否
- **MCP Elicitation** — MCP サーバーが Discord 経由でユーザー入力を要求（form-mode: JSON スキーマから最大 5 フィールドの Modal；url-mode: URL ボタン + Done 確認）；5 分でタイムアウト
- **TodoWrite ライブ進捗** — Claude が `TodoWrite` を呼び出すと Discord embed を一度投稿し、以降の更新はその embed をインプレースで編集；✅ 完了、🔄 実行中（`activeForm` ラベル付き）、⬜ 保留中の各状態を表示

#### 📊 オブザーバビリティ
- **トークン使用量** — セッション完了 embed にキャッシュヒット率とトークン数を表示
- **コンテキスト使用率** — セッション完了 embed にコンテキストウィンドウの使用率（入力＋キャッシュトークン。出力トークンは除外）と自動コンパクトまでの残容量を表示。83.5% 以上で ⚠️ 警告
- **コンパクト検出** — コンテキスト圧縮が発生した際にスレッド内で通知（トリガー種別 + 圧縮前のトークン数）
- **長時間停止通知** — 30 秒間アクティビティがない場合にスレッドメッセージを送信（長考やコンテキスト圧縮中の可能性を通知）。Claude が再開すると自動リセット
- **ターン中の進捗表示** — スレッドが 90 秒黙ったときだけ 1 行出る（`⚙️ 作業中 5:56 · 🔧 Bash(…) · ツール 61 件`）。15 秒ごとにその場で書き換わり、実出力が戻った時点で消える。ツールも止まっていれば `⏳ 待機中`。ターン中以外は出ない。`CLORD_TURN_PROGRESS=0` で無効化（#539。あるべき動きは [docs/specs/turn-progress.md](../specs/turn-progress.md)）
- **タイムアウト通知** — 経過時間とリジューム手順付きの embed 表示。**本当に固まったときだけ**出る（ペインが所定時間まったく動かず、かつ Claude が入力プロンプトで待機していない場合）。正常に終わったターンでは出ない（#541）

#### 🔌 入力とスキル
- **添付ファイル対応** — テキストファイルをプロンプトに自動追加（最大 5 ファイル × 50 KB）；画像は `--image` フラグ経由で渡す（最大 4 枚 × 5 MB）
- **スキル実行** — `/skill` コマンド（オートコンプリート付き）、オプション引数、スレッド内リジューム
- **ホットリロード** — `~/.claude/skills/` に追加した新スキルを自動検出（60 秒更新、再起動不要）

### 並行処理と協調
- **スレッドごとに専用の git clone** — チャンネルをリポジトリに紐づけると（`/clord-init`）、各スレッドは独立した `git clone` の中で作業します。セッション同士が共有するのは git リモートだけです
- **アクティブセッションレジストリ** — 実行中のセッションのインメモリ一覧で、`/workspace-cleanup` が使います。セッション自身には互いの存在は伝わりません（#758）
- **AI Lounge エンドポイント** — `GET/POST /api/lounge` が短いメモを保存し、Discord チャンネルに転送します。**セッションには注入されていません**（#758）: Claude が使うのは、あなたがそう指示したときだけです
- **協調チャンネル** — オプション（`COORDINATION_CHANNEL_ID`、既定でオフ）: セッションのターンが終わったときに 1 行の通知を投稿します

### スケジュールタスク
- **SchedulerCog** — 30 秒マスターループを持つ SQLite バックエンドの定期タスク実行エンジン
- **自己登録** — チャットセッション中に `POST /api/tasks` でタスクを登録
- **コード変更不要** — ランタイムでタスクを追加・削除・変更
- **有効/無効切り替え** — 削除せずにタスクを一時停止（`PATCH /api/tasks/{id}`）

### CI/CD 自動化
- **Webhook トリガー** — GitHub Actions や任意の CI/CD システムから Claude Code タスクをトリガー
- **自動アップグレード** — 上流パッケージリリース時に Bot を自動更新
- **DrainAware 再起動** — 再起動前にアクティブセッションの完了を待機
- **自動リジューム登録** — アップグレード再起動（`AutoUpgradeCog`）または任意のシャットダウン（`ClaudeChatCog.cog_unload()`）でアクティブセッションを自動登録。Bot 再起動後に中断した作業を自動的に再開
- **再起動承認** — アップグレード適用前の確認ゲート（オプション）。アップグレードスレッドへの ✅ リアクション、または親チャンネルに投稿されるボタンのどちらでも承認可能
- **手動アップグレードトリガー** — `/upgrade` スラッシュコマンドで Discord から直接アップグレードパイプラインを実行（`slash_command_enabled=True` でオプトイン）

### セッション管理
- **セッション同期** — CLI セッションを Discord スレッドにインポート（`/sync-sessions`）
- **チャンネル状態** — `/clord-status` がこのチャンネルのセッションをディレクトリ容量・`tmux attach` 先・`claude --resume` コマンド付きで一覧表示。`show_all` で closed も含む（`docker ps -a` 相当）。旧 `/sessions`・`/session-dirs`・`/resume-info` を統合。
- **スタートアップリジューム** — 任意のBot 再起動後に中断セッションを自動再開。`AutoUpgradeCog`（アップグレード再起動）および `ClaudeChatCog.cog_unload()`（その他すべてのシャットダウン）が自動登録、または `POST /api/mark-resume` で手動登録
- **プログラム的スポーン** — `POST /api/spawn` でスクリプトや Claude サブプロセスから新しい Discord スレッド + Claude セッションを作成。スレッド作成後すぐに非ブロッキング 201 を返す
- **スレッド ID 注入** — すべての Claude サブプロセスに `DISCORD_THREAD_ID` 環境変数を渡し、セッションから `$CLORD_API_URL/api/spawn` で子セッションを起動可能
- **ワークスペースの回収** — `/workspace-cleanup` で、実行中のどのセッションも使っていない clone ディレクトリを回収（`dry_run` プレビューあり）。未コミットの変更があるディレクトリは削除しない
- **実行時モデル切り替え** — `/model show` で現在のグローバルモデルとスレッドごとのセッションモデルを表示、`/model set` で再起動不要のまま全新規セッションのモデルを変更

### セキュリティ
- **シェルインジェクション防止** — `asyncio.create_subprocess_exec` のみ使用、`shell=True` は一切なし
- **セッション ID 検証** — `--resume` に渡す前に厳格な正規表現で検証
- **フラグインジェクション防止** — すべてのプロンプト前に `--` セパレーター
- **シークレット分離** — Bot トークンを subprocess 環境から除去
- **ユーザー認証** — `allowed_user_ids` で Claude を呼び出せるユーザーを制限

---

## クイックスタート

### 必要条件

- Python 3.10+
- [Claude Code CLI](https://docs.anthropic.com/en/docs/claude-code) のインストールと認証
- Message Content Intent が有効な Discord Bot トークン
- [uv](https://docs.astral.sh/uv/)（推奨）または pip

### スタンドアロン

```bash
git clone https://github.com/yousan/c-lord.git
cd c-lord

cp .env.example .env
# .env を Bot トークンとチャンネル ID で編集

uv run python -m c_lord.main
```

### パッケージとしてインストール

すでに discord.py Bot を動かしている場合（Discord はトークンごとに 1 Gateway 接続のみ許可）:

```bash
uv add git+https://github.com/yousan/c-lord.git
```

```python
from discord.ext import commands
from c_lord import ClaudeRunner, setup_bridge

bot = commands.Bot(...)
runner = ClaudeRunner(command="claude", model="sonnet")

@bot.event
async def on_ready():
    await setup_bridge(
        bot,
        runner,
        claude_channel_id=YOUR_CHANNEL_ID,
        allowed_user_ids={YOUR_USER_ID},
    )
```

`setup_bridge()` はすべての Cog を自動的に配線します。c-lord に追加された新しい Cog はコード変更なしで含まれます。

最新版へのアップデート:

```bash
uv lock --upgrade-package c-lord && uv sync
```

---

## 設定

| 変数名 | 説明 | デフォルト |
|--------|------|-----------|
| `DISCORD_BOT_TOKEN` | Discord Bot トークン | （必須） |
| `DISCORD_CHANNEL_ID` | Claude チャット用チャンネル ID | （必須） |
| `CLAUDE_COMMAND` | Claude Code CLI へのパス | `claude` |
| `CLAUDE_MODEL` | 使用するモデル | `sonnet` |
| `CLAUDE_PERMISSION_MODE` | CLI のパーミッションモード | `acceptEdits` |
| `CLAUDE_WORKING_DIR` | Claude の作業ディレクトリ | カレントディレクトリ |
| `MAX_CONCURRENT_SESSIONS` | **同時に走るターン数**の上限。常駐する `claude` の本数の上限ではありません（セマフォはターンの実行だけを包み、ターンが終われば解放されます。tmux の claude はその後も生き続けます）。常駐上限は `CLORD_MAX_RESIDENT_WORKSPACES` のほう (#576) | `3` |
| `CLORD_MAX_RESIDENT_WORKSPACES` | 同時に claude を抱えていられるワークスペース数の上限。超えたら「いちばん長く使われていないもの」からスリープし、**新規の作成は待たせません**。TTL と違いこの値だけはホスト規模に依存するので、既定は固定値ではなく `MemTotal`（コンテナなら cgroup の制限を優先）からの自動算出。`0` で無効。詳細は `docs/specs/resident-cap.md` | 自動算出 |
| `SESSION_TIMEOUT_SECONDS` | セッション非アクティブタイムアウト | `300` |
| `DISCORD_OWNER_ID` | Claude が入力待ちのとき @mention する Discord ユーザー ID | （オプション） |
| `COORDINATION_CHANNEL_ID` | セッションのターンが終わったときに 1 行の通知を受け取るチャンネル。AI Lounge の既定チャンネルも兼ねる | （オプション） |

---

## Discord Bot のセットアップ

1. [Discord Developer Portal](https://discord.com/developers/applications) で新しいアプリケーションを作成
2. Bot を作成してトークンをコピー
3. Privileged Gateway Intents で **Message Content Intent** を有効化
4. 以下の権限で Bot をサーバーに招待:
   - Send Messages
   - Create Public Threads
   - Send Messages in Threads
   - Add Reactions
   - Manage Messages（リアクション削除のため）
   - Read Message History

---

## GitHub + Claude Code 自動化

### 例: 自動ドキュメント同期

main へのプッシュごとに Claude Code が:
1. 最新の変更をプルして diff を分析
2. 英語ドキュメントを更新
3. 日本語（または任意の対象言語）に翻訳
4. バイリンガルな要約付き PR を作成
5. 自動マージを有効化 — CI 通過後に PR が自動マージ

**GitHub Actions:**

```yaml
# .github/workflows/docs-sync.yml
name: Documentation Sync
on:
  push:
    branches: [main]
jobs:
  trigger:
    if: "!contains(github.event.head_commit.message, '[docs-sync]')"
    runs-on: ubuntu-latest
    steps:
      - run: |
          curl -X POST "${{ secrets.DISCORD_WEBHOOK_URL }}" \
            -H "Content-Type: application/json" \
            -d '{"content": "🔄 docs-sync"}'
```

**Bot の設定:**

```python
from c_lord import WebhookTriggerCog, WebhookTrigger, ClaudeRunner

runner = ClaudeRunner(command="claude", model="sonnet")

triggers = {
    "🔄 docs-sync": WebhookTrigger(
        prompt="変更を分析し、ドキュメントを更新し、バイリンガルな要約付き PR を作成し、自動マージを有効化。",
        working_dir="/home/user/my-project",
        timeout=600,
    ),
}

await bot.add_cog(WebhookTriggerCog(
    bot=bot,
    runner=runner,
    triggers=triggers,
    channel_ids={YOUR_CHANNEL_ID},
))
```

**セキュリティ:** プロンプトはサーバー側で定義。Webhook はどのトリガーを発火するかを選択するだけ — 任意のプロンプトインジェクションはなし。

**トリガーされたときの動き:** webhook のメッセージにスレッドが立ち、トリガーの `working_dir`（無ければ runner の）で専用の tmux ウィンドウに Claude が起動し、応答がそのスレッドに届く。ウィンドウを作れなかったとき（ホストで tmux が使えない等）は起動せず、スレッドに理由が出て、webhook のメッセージに ❌ が付き、bot のオーナーにメンションが飛ぶ (#629)。

### 例: オーナー PR の自動承認

```yaml
# .github/workflows/auto-approve.yml
name: Auto Approve Owner PRs
on:
  pull_request:
    types: [opened, synchronize, reopened]
jobs:
  auto-approve:
    if: github.event.pull_request.user.login == 'your-username'
    runs-on: ubuntu-latest
    permissions:
      pull-requests: write
      contents: write
    steps:
      - env:
          GH_TOKEN: ${{ secrets.GITHUB_TOKEN }}
          PR_NUMBER: ${{ github.event.pull_request.number }}
        run: |
          gh pr review "$PR_NUMBER" --repo "$GITHUB_REPOSITORY" --approve
          gh pr merge "$PR_NUMBER" --repo "$GITHUB_REPOSITORY" --auto --squash
```

---

## スケジュールタスク

コード変更なし、再デプロイなしで、ランタイムに定期的な Claude Code タスクを登録。

Discord セッション内から Claude がタスクを登録:

```bash
# Claude がセッション内で呼び出す:
curl -X POST "$CLORD_API_URL/api/tasks" \
  -H "Content-Type: application/json" \
  -d '{"prompt": "古い依存関係をチェックして見つかったら Issue を開く", "interval_seconds": 604800}'
```

または独自のスクリプトから登録:

```bash
curl -X POST http://localhost:8080/api/tasks \
  -H "Authorization: Bearer your-secret" \
  -H "Content-Type: application/json" \
  -d '{"prompt": "週次セキュリティスキャン", "interval_seconds": 604800}'
```

30 秒マスターループが期限のタスクを検出し、Claude Code セッションを自動起動します。

---

## 自動アップグレード

新しいリリースが公開されたときに Bot を自動アップグレード:

```python
from c_lord import AutoUpgradeCog, UpgradeConfig

config = UpgradeConfig(
    package_name="c-lord",
    trigger_prefix="🔄 bot-upgrade",
    working_dir="/home/user/my-bot",
    restart_command=["sudo", "systemctl", "restart", "my-bot.service"],
    restart_approval=True,       # スレッドの ✅ リアクション、またはチャンネルのボタンで承認
    slash_command_enabled=True,  # /upgrade スラッシュコマンドを有効化（オプトイン、デフォルト False）
)

await bot.add_cog(AutoUpgradeCog(bot, config))
```

#### `/upgrade` スラッシュコマンドによる手動トリガー

`slash_command_enabled=True` の場合、認可されたユーザーが Discord から `/upgrade` を実行することで、Webhook なしで同じアップグレードパイプラインをトリガーできます。テキストチャンネルとスレッドの両方から実行でき（スレッド内から実行した場合は親チャンネルにアップグレードスレッドを作成します）。`upgrade_approval` および `restart_approval` のゲートが適用され、進捗スレッドが作成されます。すでにアップグレードが実行中の場合はエフェメラルで通知します。

再起動前に `AutoUpgradeCog` は以下の手順を実行します:

1. **アクティブセッションのスナップショット** — `_active_runners` を持つ Cog からアクティブなスレッド ID を収集（duck-typed で自動検出）。
2. **ドレイン** — アクティブセッションが自然に完了するまで待機。
3. **リジューム登録** — アクティブなスレッド ID を保留リジュームテーブルに保存。次回起動時に「Bot が再起動しました。前の作業の続きを確認してください」プロンプトで自動再開。
4. **再起動** — 設定した再起動コマンドを実行。

`active_count` プロパティを持つ Cog は自動検出されてドレインされます:

```python
class MyCog(commands.Cog):
    @property
    def active_count(self) -> int:
        return len(self._running_tasks)
```

セッションのリジューム登録は完全にオプトイン方式です。`setup_bridge()` でセッション DB を初期化している場合（デフォルト）に有効になります。有効な場合、セッションは `--resume` の継続性を保ちながら再開されるため、Claude Code は中断した会話を正確に再開できます。

> **カバレッジ:** `AutoUpgradeCog` はアップグレードによる再起動を担当します。`systemctl stop`、`bot.close()`、SIGTERM など*その他すべて*のシャットダウンに対しては、`ClaudeChatCog.cog_unload()` が第二の自動セーフネットとして機能します。

---

## REST API

通知とタスク管理のためのオプション REST API。aiohttp が必要:

```bash
uv add "c-lord[api]"
```

**bot を動かしている Unix ユーザーだけに応答する** (#457)。`POST /api/spawn` は Claude Code
セッションを起動するので、このポートに届くことは**そのユーザーのシェルを取ること**と同じ。
`127.0.0.1` バインドは*ネットワーク*境界であって *UID* 境界ではない（同一ホストの別アカウントは
普通に到達できる）ため、接続元の UID を `/proc/net/tcp` から引いて bot 自身の UID と突き合わせ、
一致しなければ `403`（`/api/health` も同様）。**設定は不要** — 自分のユーザーからの呼び出しは
そのまま通る。別ユーザー・別ホストを通したいときは `CLORD_API_SECRET` を設定し、
`Authorization: Bearer …` を送らせる。詳細は [docs/SECURITY.md](../SECURITY.md#rest-api-exposure-712-457)。

### エンドポイント

| メソッド | パス | 説明 |
|----------|------|------|
| GET | `/api/health` | ヘルスチェック |
| POST | `/api/notify` | 即時通知の送信 |
| POST | `/api/schedule` | 通知のスケジュール |
| GET | `/api/scheduled` | 保留中の通知一覧 |
| DELETE | `/api/scheduled/{id}` | スケジュール済み通知のキャンセル |
| POST | `/api/tasks` | 定期的な Claude Code タスクを登録 |
| GET | `/api/tasks` | 登録済みタスクの一覧 |
| DELETE | `/api/tasks/{id}` | タスクの削除 |
| PATCH | `/api/tasks/{id}` | タスクの更新（有効/無効、スケジュール変更） |
| POST | `/api/spawn` | 新しい Discord スレッドを作成し Claude Code セッションを起動（非ブロッキング） |
| POST | `/api/mark-resume` | 次回 Bot 起動時のスレッド自動リジュームを登録 |
| GET | `/api/lounge` | AI Lounge の最近のメッセージを取得（セッションにはラウンジの存在が伝わらないため、自分で仕組みを用意しない限り誰も投稿しません — #758） |
| POST | `/api/lounge` | AI Lounge にメッセージを投稿（`label` オプション）— c-lord のセッションが自発的に呼ぶことはありません（#758） |

```bash
# 通知の送信
curl -X POST http://localhost:8080/api/notify \
  -H "Authorization: Bearer your-secret" \
  -H "Content-Type: application/json" \
  -d '{"message": "ビルド成功！", "title": "CI/CD"}'

# 定期タスクの登録
curl -X POST http://localhost:8080/api/tasks \
  -H "Authorization: Bearer your-secret" \
  -H "Content-Type: application/json" \
  -d '{"prompt": "デイリースタンドアップ要約", "interval_seconds": 86400}'
```

---

## アーキテクチャ

```
c_lord/
  main.py                  # スタンドアロンエントリーポイント
  setup.py                 # setup_bridge() — 1 行で Cog を配線
  bot.py                   # Discord Bot クラス
  concurrency.py           # アクティブセッションレジストリ（並行処理の注意書きは届いていない — #758）
  cogs/
    claude_chat.py         # インタラクティブチャット（スレッド作成、メッセージ処理）
    skill_command.py       # /skill スラッシュコマンド（オートコンプリート付き）
    session_manage.py      # /clord-status, /sync-sessions
    scheduler.py           # 定期 Claude Code タスク実行エンジン
    webhook_trigger.py     # Webhook → Claude Code タスク実行（CI/CD）
    auto_upgrade.py        # Webhook → パッケージアップグレード + DrainAware 再起動
    event_processor.py     # EventProcessor — stream-json イベントのステートマシン
    run_config.py          # RunConfig データクラス — CLI 実行パラメーターをまとめる
    _run_helper.py         # 薄いオーケストレーション層（run_claude_with_config + shim）
  claude/
    runner.py              # Claude CLI subprocess マネージャー
    parser.py              # stream-json イベントパーサー
    types.py               # SDK メッセージの型定義
  coordination/
    service.py             # 共有チャンネルへのセッション終了通知（オプション）
  database/
    models.py              # SQLite スキーマ
    repository.py          # セッション CRUD
    task_repo.py           # スケジュールタスク CRUD
    ask_repo.py            # 保留中 AskUserQuestion CRUD
    notification_repo.py   # スケジュール通知 CRUD
    resume_repo.py         # スタートアップリジューム CRUD（Bot 再起動をまたいだ保留リジューム）
    settings_repo.py       # ギルドごとの設定
  discord_ui/
    status.py              # 絵文字リアクションステータスマネージャー（デバウンス付き）
    chunker.py             # フェンス・テーブル対応メッセージ分割
    embeds.py              # Discord embed ビルダー
    ask_view.py            # AskUserQuestion 用 Discord ボタン / Select Menu
    ask_handler.py         # collect_ask_answers() — AskUserQuestion UI + DB ライフサイクル
    streaming_manager.py   # StreamingMessageManager — デバウンス付きインプレース編集
    tool_timer.py          # LiveToolTimer — 長時間ツール実行の経過時間カウンター
    thread_dashboard.py    # スレッドのセッション状態を表示する live ピン embed
    plan_view.py           # Plan Mode 承認ボタン（Approve/Cancel）
    permission_view.py     # ツール実行許可ボタン（Allow/Deny）
    elicitation_view.py    # MCP Elicitation 用 Discord UI（Modal フォームまたは URL ボタン）
  session_sync.py          # CLI セッションの検出とインポート
  session_dir.py           # スレッドごとに 1 つの git clone（未コミットの変更があるディレクトリは削除しない）
  ext/
    api_server.py          # REST API サーバー（オプション、aiohttp が必要）
  utils/
    logger.py              # ロギング設定
```

### 設計思想

- **CLI スポーン、API ではない** — `claude -p --output-format stream-json` を呼び出し、Claude Code の全機能（CLAUDE.md、スキル、ツール、メモリ）を利用
- **並行処理ファースト** — 複数の同時セッションが例外ではなく期待値。各スレッドに専用の git clone を与えるので、隔離は Claude への指示ではなくファイルシステムによって実現
- **Discord を接着剤として** — Discord が UI、スレッディング、リアクション、Webhook、永続的な通知を提供。カスタムフロントエンド不要
- **フレームワーク、アプリケーションではない** — パッケージとしてインストールし、既存の Bot に Cog を追加し、コードで設定
- **ゼロコード拡張性** — ソースを変更せずにスケジュールタスクと Webhook トリガーを追加
- **シンプルさによるセキュリティ** — 約 3000 行の監査可能な Python。subprocess exec のみ、シェル展開なし

---

## テスト

```bash
uv run pytest tests/ -v --cov=c_lord
```

700 件以上のテストがパーサー、チャンカー、リポジトリ、ランナー、ストリーミング、Webhook トリガー、自動アップグレード（`/upgrade` スラッシュコマンド、スレッド内実行、承認ボタン含む）、REST API、AskUserQuestion UI、スレッドダッシュボード、スケジュールタスク、セッション同期、AI Lounge、スタートアップリジューム、モデル切り替え、コンパクト検出、TodoWrite 進捗 embed、許可／Elicitation／Plan Mode イベントパースをカバーしています。

---

## このプロジェクトの構築方法

**このコードベース全体は [Claude Code](https://docs.anthropic.com/en/docs/claude-code)** — Anthropic の AI コーディングエージェント — によって書かれました。人間の著者（[@ebibibi](https://github.com/ebibibi)）は自然言語で要件と方向性を提供しましたが、ソースコードを手動で読んだり編集したりしていません。

つまり:

- **すべてのコードは AI 生成** — アーキテクチャ、実装、テスト、ドキュメント
- **人間の著者はコードレベルでの正確性を保証できません** — 確信が必要な場合はソースを確認してください
- **バグレポートと PR を歓迎します** — Claude Code を使って対応することになるでしょう
- **これは AI が著したオープンソースソフトウェアの実例です**

このプロジェクトは 2026-02-18 に開始され、Claude Code との反復的な会話を通じて進化し続けています。

---

## 実例

**[EbiBot](https://github.com/ebibibi/discord-bot)** — このフレームワーク上に構築された個人 Discord Bot。自動ドキュメント同期（英語 + 日本語）、プッシュ通知、Todoist ウォッチドッグ、スケジュールヘルスチェック、GitHub Actions CI/CD を含みます。自分の Bot を構築する際のリファレンスとしてご利用ください。

---

## インスパイアされたプロジェクト

- [OpenClaw](https://github.com/openclaw/openclaw) — 絵文字ステータスリアクション、メッセージデバウンシング、フェンス対応チャンキング
- [claude-code-discord-bot](https://github.com/timoconnellaus/claude-code-discord-bot) — CLI スポーン + stream-json アプローチ
- [claude-code-discord](https://github.com/zebbern/claude-code-discord) — パーミッション制御パターン
- [claude-sandbox-bot](https://github.com/RhysSullivan/claude-sandbox-bot) — スレッドごとの会話モデル

---

## ライセンス

MIT
