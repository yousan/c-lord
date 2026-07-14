# コマンドリファレンス

Discord ユーザーおよび API 利用者が使えるすべてのコマンド一覧です。

> アーキテクチャ図、セッションのライフサイクル、Tips 等の包括的なガイドは **[利用者ガイド](USER_GUIDE.md)** を参照してください。

## アーキテクチャ概要

```
Bot → Channel (= 1 リポジトリ + 1 tmux session) → Thread (= 1 tmux window)
```

- **Channel ↔ リポジトリ**: 各 Discord チャンネルは `/clord-init` で git リポジトリに紐づけます。紐づけはデータベースに保存されます。
- **Thread ↔ セッション**: 各 Discord スレッドは Claude Code セッションと 1:1 で対応します。スレッド内の返信は `--resume` で同じセッションを継続します。
- **未設定チャンネル**: `/clord-init` で紐づけのないチャンネルで `/clord` を実行するとエラーが返され、先に紐づけを設定するよう案内されます。
- **実行モード**: Claude Code は tmux TUI モードのみで動作します。レガシーの subprocess モードは v1.x で廃止されました。

## スラッシュコマンド

### チャット & セッション

| コマンド | 説明 | 使用場所 |
|---------|------|---------|
| `/clord <prompt>` | 新しい Claude Code セッションを開始 | チャンネル・スレッド |
| `/stop` | 実行中のセッションを停止（セッションは保持される） | スレッドのみ |
| `/clear` | セッションをリセット — 次のメッセージで新規セッション開始 | スレッドのみ |
| `/clord-attach <window>` | このスレッドを既存の tmux ウィンドウに接続 | スレッドのみ |

**`/clord`** は新しいスレッドを作成し、プロンプトを Claude Code に送信します。既存スレッド内で使うと、同じセッションを継続します。

**`/stop`** はプロセスを安全に中断します。セッションは保存されるので、スレッドにメッセージを送ればいつでも再開できます。

**`/clord-attach`** はスレッドを tmux ウィンドウにリンクし、Discord とターミナルの両方から同じ Claude Code セッションを操作できるようにします。

### スキル

| コマンド | 説明 | 使用場所 |
|---------|------|---------|
| `/skill <name> [args]` | Claude Code スキルを実行 | チャンネル・スレッド |

スキルは `~/.claude/skills/` に保存された定義済みプロンプトです。`name` パラメータはオートコンプリートに対応しています。

### チャンネル設定

| コマンド | 説明 | 使用場所 |
|---------|------|---------|
| `/clord-init` | チャンネルとリポジトリの紐づけを一覧表示 | 任意のチャンネル |
| `/clord-init repo:<url>` | このチャンネルを git リポジトリに紐づけ | 任意のチャンネル |
| `/clord-init remove:True` | このチャンネルの紐づけを解除 | 任意のチャンネル |
| `/clord-thread-init` | このスレッドのスレッドレベル紐づけを表示 | スレッドのみ |
| `/clord-thread-init repo:<url>` | このスレッドを git リポジトリに紐づけ（チャンネル設定を上書き） | スレッドのみ |
| `/clord-thread-init remove:True` | このスレッドのスレッドレベル紐づけを解除 | スレッドのみ |

**サーバー管理**権限が必要です。チャンネルをリポジトリに紐づけると、そのチャンネルで開始されたセッションは自動的にそのリポジトリを作業ディレクトリとして使用します。`/clord-thread-init` で設定したスレッドレベルの紐づけはチャンネル設定より優先されます。

### モデル管理

| コマンド | 説明 | 使用場所 |
|---------|------|---------|
| `/model show` | 現在の Claude モデルを表示 | どこでも |
| `/model set <model>` | 新規セッション用のグローバルモデルを変更。tier エイリアス（`sonnet`/`opus`/`haiku`、各 tier の最新に解決）を選ぶか、任意のモデルID（例 `claude-fable-5`）を直接入力できる（可否は CLI が判定） | どこでも |

選択可能なモデル: `haiku`（高速）、`sonnet`（バランス型、デフォルト）、`opus`（高性能）。

### セッション管理

| コマンド | 説明 | 使用場所 |
|---------|------|---------|
| `/clord-status` | **このチャンネル**の稼働中セッション一覧（容量・attach・resume） | どこでも |
| `/clord-status show_all:true` | closed なセッションも含める（`docker ps -a` 相当） | どこでも |

**`/clord-status`** はチャンネル単位のセッション状態を 1 コマンドにまとめたものです。各行に window 番号 `#`（昇順）・`status`・スレッドの `topic`・`size`・`used`（最終活動からの経過）を表示。表の上にコピペ可能な `tmux attach -t <session>:work<#>` を出します（`#` を置換）。Claude Code セッション ID（`cc-session`、ターミナルで `claude --resume <id>` する用）は **`all` の時だけ右端**に出ます。既定は **live** のみ（`docker ps` 相当）、`show_all` で **closed**（`/close-workspace` 済み — tmux は閉じたが dir は残り容量を食う）も表示。`/workspace-delete` 済み（作業 dir 削除）のものは footer に件数のみ。**削除された `/sessions`・`/session-dirs`・`/resume-info` を統合**したものです（#363）。

**status の値**（DB ではなく呼び出し時のライブ判定）：

| status | 意味 |
|--------|------|
| `run` | tmux window あり・Claude 実行中（🟢） |
| `wait` | tmux window あり・ターン完了で入力待ち（🟡） |
| `err` | tmux window あり・ペインにエラー（🔴） |
| `closed` | tmux window 無しだが session dir は残存（`/close-workspace` 済み — 容量を食う／メッセージ送信で再開）（⚪） |

### ワークスペース管理

| コマンド | 説明 | 使用場所 |
|---------|------|---------|
| `/session-cleanup [dry_run]` | 孤立したセッションディレクトリを削除 | どこでも |
| `/tmux-list` | アクティブな tmux ウィンドウを一覧表示 | どこでも |
| `/workspace-delete` | このスレッドの tmux ウィンドウとセッションディレクトリを削除 | スレッドのみ |

### アップグレード

| コマンド | 説明 | 使用場所 |
|---------|------|---------|
| `/upgrade` | パッケージの手動アップグレード | どこでも |

Bot 運用者がアップグレードコマンドを有効にしている場合のみ利用可能です。

---

## テキストコマンド

| コマンド | 説明 | 例 |
|---------|------|---|
| `!attach <window>` | このスレッドを tmux ウィンドウに接続 | `!attach w13` |

テキストコマンドは `!` プレフィックスを使います。`!attach` は `/clord-attach` と同じ機能です。

---

## アクセス制御

Bot は 2 つの認可方式に対応しています（OR 条件 — どちらか一方で許可）：

1. **ユーザー ID** — `.env` で `DISCORD_OWNER_ID` を設定すると、特定ユーザーのみに制限できます。
2. **Discord ロール** — `.env` で `CLORD_ALLOWED_ROLE` にロール名（例: `claude-operator`）を設定すると、そのロールを持つメンバーが Bot を使えます。

どちらも未設定の場合、全ユーザーが Bot を利用できます。

---

## REST API

オプションの REST API サーバーにより、外部ツール（Claude Code CLI、CI/CD、スクリプト等）から Bot をプログラム的に操作できます。

**ベース URL:** `http://127.0.0.1:8080`（変更可能）
**認証:** `Authorization: Bearer <CLORD_API_SECRET>` ヘッダー（`/api/health` 以外はオプション）

### エンドポイント一覧

| メソッド | パス | 説明 |
|---------|------|------|
| `GET` | `/api/health` | ヘルスチェック |
| `POST` | `/api/notify` | Discord へ即時通知を送信 |
| `POST` | `/api/schedule` | 通知を予約 |
| `GET` | `/api/scheduled` | 予約済み通知の一覧 |
| `DELETE` | `/api/scheduled/{id}` | 予約済み通知をキャンセル |
| `POST` | `/api/tasks` | スケジュールタスクを登録 |
| `GET` | `/api/tasks` | スケジュールタスクの一覧 |
| `PATCH` | `/api/tasks/{id}` | タスクを更新（有効/無効、プロンプト、間隔） |
| `DELETE` | `/api/tasks/{id}` | タスクを削除 |
| `POST` | `/api/spawn` | 新しいスレッドを作成して Claude Code を開始 |
| `POST` | `/api/threads/{thread_id}/messages` | Discord スレッドにメッセージを投稿 |
| `POST` | `/api/mark-resume` | 再起動後にスレッドを再開するようマーク |
| `GET` | `/api/lounge` | AI Lounge の最新メッセージ一覧 |
| `POST` | `/api/lounge` | AI Lounge にメッセージを投稿 |

### 使用例

通知を送信:

```bash
curl -X POST http://127.0.0.1:8080/api/notify \
  -H "Content-Type: application/json" \
  -H "Authorization: Bearer $CLORD_API_SECRET" \
  -d '{"message": "ビルド完了!", "title": "CI"}'
```

新しいセッションを開始:

```bash
curl -X POST http://127.0.0.1:8080/api/spawn \
  -H "Content-Type: application/json" \
  -H "Authorization: Bearer $CLORD_API_SECRET" \
  -d '{"prompt": "最新の PR をレビューして変更点をまとめてください"}'
```

CLI の入力をスレッドに転送:

```bash
curl -X POST http://127.0.0.1:8080/api/threads/123456789/messages \
  -H "Content-Type: application/json" \
  -H "Authorization: Bearer $CLORD_API_SECRET" \
  -d '{"content": "テストカバレッジも確認してください", "source": "cli"}'
```

スケジュールタスクを登録:

```bash
curl -X POST http://127.0.0.1:8080/api/tasks \
  -H "Content-Type: application/json" \
  -H "Authorization: Bearer $CLORD_API_SECRET" \
  -d '{
    "name": "daily-review",
    "prompt": "オープン中の PR を確認してサマリーを投稿してください",
    "interval_seconds": 86400,
    "channel_id": 123456789
  }'
```
