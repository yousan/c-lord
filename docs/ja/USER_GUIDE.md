# c-lord 利用者ガイド

Discord 上で c-lord ボットを使うためのガイドです。

**[English version](../USER_GUIDE.md)**

---

## アーキテクチャ全体像

1 つの Bot が複数の Discord チャンネルを担当し、チャンネルごとに異なるリポジトリで Claude Code が作業します。

```
Bot (c-lord プロセス)                                    ← 1つ
│
├── アクセス制御: @claude-operator ロール
│   └── このロールを持つユーザーのみ Bot と対話可能
│
├── #project-a (channel)
│   │
│   ├── リポジトリ: github.com/user/project-a.git
│   │   └── /clord-init で紐づけ (DB に保存)
│   │
│   ├── tmux session: "project-a"
│   │   ├── w1 ── Thread 101 の Claude Code CLI
│   │   └── w2 ── Thread 102 の Claude Code CLI
│   │
│   └── session_dir: ~/c-lord-sessions/project-a/
│       ├── 101/ ── git clone of project-a (Thread 101 用)
│       └── 102/ ── git clone of project-a (Thread 102 用)
│
├── #project-b (channel)
│   │
│   ├── リポジトリ: github.com/user/project-b.git
│   │
│   ├── tmux session: "project-b"
│   │   └── w1 ── Thread 201 の Claude Code CLI
│   │
│   └── session_dir: ~/c-lord-sessions/project-b/
│       └── 201/ ── git clone of project-b (Thread 201 用)
│
└── #general (リポジトリ未設定)
    └── /clord → エラー「リポジトリが設定されていません」
```

### 各要素の関係

| 関係 | 対応 | 紐づけキー |
|------|------|-----------|
| Channel : リポジトリ | 1:1 | `/clord-init` で DB に保存 |
| Channel : tmux session | 1:1 | リポジトリ名から自動生成 |
| Thread : tmux window | 1:1 | `@thread_id` (tmux window option) |
| Thread : session_dir | 1:1 | `~/c-lord-sessions/{project}/{thread_id}/` |
| Thread : Claude session | 1:1 | DB (`sessions` テーブル) |

### 紐づけの流れ

```
管理者: /clord-init repo:https://github.com/user/project-a.git
  ↓
DB に保存: channel_id → repo URL, tmux session 名, session_dir パス
  ↓
利用者: /clord auth.py のバグを修正して
  ↓
Bot が自動で:
  1. Thread 作成 (Discord)
  2. git clone (session_dir)
  3. tmux window 作成 (w1, w2, ...)
  4. Claude Code CLI 起動
```

> **重要:** チャンネルは `/clord-init` でリポジトリを紐づけてから `/clord` を使用してください。未設定のチャンネルでは `/clord-init` を案内するエラーが表示されます。

---

## セッションの開始

### `/clord <プロンプト>`

Claude Code セッションを開始する基本コマンド。リポジトリが紐づけられたチャンネルで使用します。

```
/clord auth.py のログインバグを修正して
```

ボットが新しいスレッドを作成し、Claude が作業を開始します。以降そのスレッド内でのメッセージはすべて同じセッションとして継続されます。

### `/clord-attach <ウィンドウ名>` / `!attach <ウィンドウ名>`

既存の tmux ウィンドウを現在のスレッドに紐づけます。手動で作ったスレッド（例: 既に動いている Claude Code CLI セッション用）で使用します。

```
/clord-attach w1
!attach w1
```

紐づけ後、ボットはそのスレッドのメッセージに応答するようになります。

### オプトイン方式

ボットは**自分が管理するスレッドにのみ応答**します。具体的には `/clord` で作成したスレッド、`!attach` や `/clord-attach` で紐づけたスレッド、REST API (`POST /api/spawn`) で生成したスレッドが対象です。他のボットが作ったスレッドや人間同士の会話スレッドには干渉しません。

---

## スレッド内でのやりとり

### メッセージの送信

スレッド内で普通にメッセージを送ります。各メッセージは Claude への新しいプロンプトとして送信されます。Claude が前のメッセージを処理中の場合、新しいメッセージで割り込み (SIGINT) が発生し、新しい指示で再開します。

### 添付ファイル

- **テキストファイル** (`.txt`, `.md`, `.csv`, `.json`, `.xml` など) — プロンプトに自動追加。最大 5 ファイル、各 50 KB、合計 100 KB まで。
- **画像** (`.png`, `.jpg` など) — ダウンロードされて `--image` で Claude に渡される。最大 4 枚、各 5 MB まで。

### ステータス表示 (絵文字リアクション)

ボットが**自分のメッセージに1つの絵文字リアクション**を付けてターンの状態を示します:

| 絵文字 | 意味 |
|--------|------|
| 🟢 | 実行中 — Claude が作業中（思考・ツール実行） |
| 🟡 | 待ち — ターンが終わり、自分の番 |
| ❌ | ターンがエラーで終了 |
| ⚠️ | しばらく無反応 — ストールの疑い（拡張思考やコンパクション） |
| 🗜️ | コンテキストのコンパクション中 |

リアクションは1往復ごとに 🟢 → 🟡 と切り替わります。リアクションとスレッド名変更は
Discord の別々のレート制限を使うため、このランプは混雑時でも即応します。**スレッド名**の
🟢/🟡 は、ゆっくり追従する結果整合のサイドバー表示です（#246）。

---

## インタラクティブ機能

### 質問 (AskUserQuestion)

Claude がユーザーの入力を必要とする場合、Discord のボタンやセレクトメニューが表示されます。選択肢を選ぶと Claude がその回答を元に続行します。ボタンはボット再起動後も有効です。

### プランモード

Claude が実装計画を提案すると、計画全文を含む embed が **承認** / **キャンセル** ボタン付きで表示されます。承認するまで Claude は実行しません。5 分でタイムアウト（自動キャンセル）。

### ツール権限リクエスト

Claude がツールの実行許可を求める場合、ツール名と入力内容を表示した embed が **許可** / **拒否** ボタン付きで表示されます。2 分でタイムアウト（自動拒否）。

### TodoWrite 進捗表示

Claude が `TodoWrite` でタスクを管理すると、1 つの embed が投稿され、更新のたびにその場で編集されます:
- ✅ 完了
- 🔄 進行中
- ⬜ 未着手

---

## スラッシュコマンド一覧

| コマンド | 説明 | 状態 |
|---------|------|------|
| `/clord <プロンプト>` | 新しい Claude Code セッションを開始 | 利用可能 |
| `/clord-attach <ウィンドウ>` | tmux ウィンドウを現在のスレッドに紐づけ | 利用可能 |
| `/clord-init <repo>` | チャンネルにリポジトリを紐づけ | 利用可能 |
| `/stop` | 現在のセッションを停止（リジューム可能な状態で保持） | 利用可能 |
| `/clear` | スレッドの Claude Code セッションをリセット | 利用可能 |
| `/skill <名前> [引数]` | Claude Code スキルを実行（オートコンプリート付き） | 利用可能 |
| `/clord-status [show_all]` | このチャンネルのセッション一覧（容量・attach・resume。`/sessions`・`/session-dirs`・`/resume-info` を統合） | 利用可能 |
| `/sync-sessions` | CLI セッションを Discord スレッドにインポート | 利用可能 |
| `/model show` | 現在のモデル表示（グローバル + スレッド別） | 利用可能 |
| `/model set <モデル>` | 新規セッションのモデルを変更（再起動不要） | 利用可能 |
| `/session-cleanup` | クリーンな孤立セッションディレクトリを削除 | 利用可能 |
| `/tmux-list` | アクティブな tmux ウィンドウ一覧 | 利用可能 |
| `/workspace-delete` | このスレッドの tmux ウィンドウとセッションディレクトリを削除 | 利用可能 |
| `/upgrade` | ボットのアップグレードをトリガー（有効時のみ） | 利用可能 |

### テキストコマンド

| コマンド | 説明 | 状態 |
|---------|------|------|
| `!attach <ウィンドウ>` | tmux ウィンドウを紐づけ（`/clord-attach` と同じ） | 利用可能 |

---

## アクセス制御

Bot と対話できるユーザーは Discord のロールで制御します。

- サーバー管理者が `@claude-operator` ロールを作成（`CLORD_ALLOWED_ROLE` で変更可能）
- このロールを付与されたユーザーのみが `/clord`, `/clord-init` 等を実行可能
- ロールのないユーザーがスレッドに書き込んでも Bot は無視
- ロールの追加・削除は Discord のサーバー設定で行う（Bot の再起動不要）
- `DISCORD_OWNER_ID` で常に許可する特定ユーザーを設定可能

---

## セッションのライフサイクル

```
/clord "バグを修正して"
    ↓
[スレッド作成] ← Bot がここで応答
    ↓
フォローアップメッセージ → 同じセッションで Claude が継続
    ↓
/stop → セッション一時停止（メッセージ送信で再開可能）
    ↓
ボット再起動 → セッション自動再開
```

### 裏側で起きていること

```
/clord "バグを修正して"
    ↓
1. DB に session レコード作成 (thread_id → session_id)
2. session_dir 作成: ~/c-lord-sessions/project-a/{thread_id}/
   └── git clone https://github.com/user/project-a.git
3. tmux window 作成: project-a:w1
   └── @thread_id = {thread_id}
4. Claude Code CLI 起動 (tmux window 内で実行)
    ↓
スレッドにメッセージ送信
    ↓
5. DB から session_id を取得
6. tmux window に入力を送信
7. Claude Code の出力をストリーミングでスレッドに投稿
```

### タイムアウト

一定時間操作がないとセッションがタイムアウトします（デフォルト: 5 分）。経過時間とガイダンスを含む embed が表示されます。新しいメッセージを送ると同じスレッドで新しいセッションが開始されます。

### 割り込み

Claude が作業中に新しいメッセージを送ると、現在の処理が中断 (SIGINT) され、新しい指示で Claude が再開します。`/stop` は不要です。

### ボット再起動

ボットが再起動すると（アップグレード、メンテナンスなど）、アクティブなセッションは自動的にリジューム対象としてマークされます。ボットがオンラインに戻ると、セッションは中断した箇所から再開されます。

---

## 監視・可視化

セッション完了時の embed には以下が表示されます:

- **トークン使用量** — 入力・出力・キャッシュトークン数とヒット率
- **コンテキスト使用率** — コンテキストウィンドウの使用割合。83.5% を超えると ⚠️ 警告
- **コンパクト検出** — コンテキスト圧縮が発生した場合に通知
- **長時間停止警告** — 30 秒以上アクティビティがない場合にスレッド内で通知（長考やコンテキスト圧縮の可能性）

---

## tmux によるセッション管理

各 Claude Code セッションは tmux ウィンドウ内で動作します。サーバーに SSH できる管理者は tmux で直接セッションを確認・操作できます。

```bash
# プロジェクトの tmux セッション一覧
tmux ls
# project-a: 2 windows (created ...)
# project-b: 1 windows (created ...)

# project-a のセッションにアタッチ
tmux attach -t project-a

# 特定ウィンドウに切り替え (w1, w2, ...)
# Ctrl-b + n (次), Ctrl-b + p (前), Ctrl-b + 1 (番号指定)
```

各ウィンドウには Claude Code CLI のターミナルが見えるので、Claude が何をしているかリアルタイムで確認できます。

---

## Tips

1. **1 スレッド = 1 タスク** — 各スレッドは独立した Claude Code セッションです。タスクごとにスレッドを分けましょう。
2. **具体的に指示する** — `/clord` の最初のメッセージがコンテキストを決定します。明確で詳細なプロンプトがより良い結果を生みます。
3. **tmux で覗く** — サーバーにアクセスできるなら `tmux attach` で Claude の動きをリアルタイムに確認できます。
4. **チャンネル = プロジェクト** — チャンネルごとにリポジトリが紐づいています。正しいチャンネルで `/clord` してください。
