# Claude Code TUI Interactive Prompt Catalog

> Phase 1 of [#110](https://github.com/yousan/c-lord/issues/110) — 体系的な網羅カタログ。  
> 検出方式の分岐点として「JSONL に出るか否か」を各エントリに記載。

## 凡例

| 列 | 内容 |
|----|------|
| **種別** | プロンプトの名称 |
| **トリガ** | いつ出るか |
| **画面シグネチャ** | capture-pane で見える固定文字列・マーカー |
| **操作** | 矢印＋Enter / 数字 / y/n / Esc 等 |
| **JSONL** | `--output-format stream-json` で出るか（`StreamEvent` のどのフィールドか） |
| **現在の c-lord 対応** | 自動処理 / Discord UI / 未対応 |

---

## 1. 起動時プロンプト（ハーネス由来 — TUI-only）

JSONL transcript が始まる前に出るため、JSONL 経路では検出できない。`tmux capture-pane` スクレイプが唯一の検出手段。

### 1-1. Trust / Safety Prompt

| 項目 | 内容 |
|------|------|
| **トリガ** | 初回起動 or 未信頼ディレクトリで `claude` を起動 |
| **画面シグネチャ** | `Quick safety check: Is this a project you created?` / `❯ 1. Yes, I trust this folder` / `  2. No, exit` / `Enter to confirm` |
| **操作** | Enter（選択肢 1 を選択）または `2` + Enter で終了 |
| **JSONL** | ❌ TUI-only（ハーネス起動前） |
| **現在の c-lord 対応** | ✅ 自動処理（`_handle_startup_prompts` が Enter を送信） |
| **検出パターン（現行）** | `_TRUST_PROMPT_MARKERS = ("Yes, I trust this folder", "Enter to confirm")` |
| **課題** | 画面シグネチャが変わると検出漏れ。より安定なパターンは `❯ 1.` + 選択肢が並ぶ構造のみ |

### 1-2. Resume Session Picker

| 項目 | 内容 |
|------|------|
| **トリガ** | `claude --resume` をセッション ID なしで起動、かつ複数の既存セッションがある |
| **画面シグネチャ** | `Resume which session?` / 番号付きセッション一覧（日時 + 要約） / `❯` カーソル |
| **操作** | 矢印キー + Enter |
| **JSONL** | ❌ TUI-only |
| **現在の c-lord 対応** | ❌ 未対応。`--resume <session_id>` で明示 ID を渡すため通常は出ない |
| **課題** | セッション ID が DB にない場合に出る可能性。`❯` + セッション日付パターンで検出可能 |

### 1-3. ログイン方式選択（初回）

| 項目 | 内容 |
|------|------|
| **トリガ** | 未認証状態で初回起動 |
| **画面シグネチャ** | `How would you like to login?` / `❯ 1. Claude.ai account` / `  2. Anthropic API key` / `  3. Amazon Bedrock` / `  4. Google Vertex AI` |
| **操作** | 数字 + Enter |
| **JSONL** | ❌ TUI-only |
| **現在の c-lord 対応** | ❌ 未対応（認証済みの前提で運用） |

### 1-4. テーマ選択（初回）

| 項目 | 内容 |
|------|------|
| **トリガ** | 初回起動時（設定未存在） |
| **画面シグネチャ** | `Choose a theme:` / 選択肢（Light / Dark / System 等） |
| **操作** | 矢印 + Enter |
| **JSONL** | ❌ TUI-only |
| **現在の c-lord 対応** | ❌ 未対応。`--no-color` または事前設定で回避可能 |

---

## 2. セッション中プロンプト — JSONL 経由で検出可能

`stream-json` モードでは `StreamEvent` として受信できる。c-lord はすでに `TranscriptMirrorCog` + `tmux_runner.py` の JSONL パースでこれらを受信している。

### 2-1. Permission Request（ツール承認）

| 項目 | 内容 |
|------|------|
| **トリガ** | `--permission-mode default` 時に Bash / Write / Edit / WebFetch 等の実行前 |
| **画面シグネチャ** | `Do you want to proceed?` / `❯ 1. Yes` / `  2. Yes, and don't ask again for bash commands` / `  3. No (esc)` または `This command requires approval` |
| **操作** | 数字 + Enter（1=Yes / 2=Yes don't ask / 3=No）または Esc |
| **JSONL** | ✅ `StreamEvent.permission_request: PermissionRequest` に `request_id`, `tool_name`, `tool_input` が入る |
| **現在の c-lord 対応** | ⚠️ 部分対応。TUI スクレイプで Enter を自動送信（`_accept_permission_prompt`）。Discord への通知・選択は未実装 |
| **検出パターン（現行）** | `_PERMISSION_PROMPT_MARKERS = ("Do you want to proceed?", "This command requires approval")` |
| **課題** | JSONL パスを使えば文字列マッチ不要。`permission_request` フィールドがあれば確定。Discord に `AskView` でボタン表示 → `request_id` で承認応答が理想 |

### 2-2. Plan Mode Approval（ExitPlanMode）

| 項目 | 内容 |
|------|------|
| **トリガ** | Plan Mode でプランが完成したとき（`ExitPlanMode` ツール呼び出し） |
| **画面シグネチャ** | `Would you like to proceed?` / `❯ 1. Yes` / `  2. Yes, and auto-accept edits` / `  3. No, keep planning` |
| **操作** | 数字 + Enter |
| **JSONL** | ✅ `StreamEvent.is_plan_approval == True`（`ExitPlanMode` ツール呼び出しとして検出） |
| **現在の c-lord 対応** | ✅ Discord UI で承認ボタン表示（`embeds.py` の plan embed + `StopView` / `PlanView`） |

### 2-3. AskUserQuestion（Claude 生成の構造化選択）

| 項目 | 内容 |
|------|------|
| **トリガ** | Claude が `AskUserQuestion` ツールを呼んだとき |
| **画面シグネチャ** | TUI に選択メニュー。ただし cooperative 経路なので TUI と Discord が並存 |
| **操作** | 数字 + Enter（TUI）/ Discord ボタン・セレクト（Discord） |
| **JSONL** | ✅ `StreamEvent.ask_questions: list[AskQuestion]`（`AskUserQuestion` ツール呼び出し） |
| **現在の c-lord 対応** | ✅ `AskView` + `AskAnswerBus` で Discord ボタン表示・回答ルーティング済み |

### 2-4. Elicitation（MCP サーバーからの入力要求）

| 項目 | 内容 |
|------|------|
| **トリガ** | MCP サーバーが elicitation を要求したとき |
| **画面シグネチャ** | form-mode: フィールド入力画面 / url-mode: URL 表示 |
| **操作** | form-mode: 入力 + Enter / url-mode: ブラウザ操作後 Done |
| **JSONL** | ✅ `StreamEvent.elicitation: ElicitationRequest`（`request_id`, `server_name`, `mode`, `schema` 等） |
| **現在の c-lord 対応** | ✅ `ElicitationView` で Discord モーダル / URL ボタン表示済み |

### 2-5. Compaction / Context Low

| 項目 | 内容 |
|------|------|
| **トリガ** | コンテキストウィンドウが閾値（約 95%）に達したとき |
| **画面シグネチャ** | `Context window is almost full. Auto-compacting...` または対話型で `Compact now? (y/n)` |
| **操作** | y/n（対話型の場合）。自動発動時は操作不要 |
| **JSONL** | ✅ `StreamEvent.is_compact == True`（`compact_trigger`, `compact_pre_tokens` 等も取得可能） |
| **現在の c-lord 対応** | ⚠️ `is_compact` の受信はあるが Discord 通知の実装は要確認 |

---

## 3. セッション中プロンプト — TUI-only（スクレイプ必要）

JSONL に出ない。`tmux capture-pane` でしか検出できない。誤検知リスクが高い。

### 3-1. モデル選択（/model）

| 項目 | 内容 |
|------|------|
| **トリガ** | ユーザーが `/model` スラッシュコマンドを入力 |
| **画面シグネチャ** | `Select a model:` / モデル一覧（`claude-opus-4-7`, `claude-sonnet-4-6`, 等） / `❯` カーソル |
| **操作** | 矢印 + Enter |
| **JSONL** | ❌ TUI-only（slash コマンド UI） |
| **現在の c-lord 対応** | ❌ 未対応。`--model` フラグで起動時に固定するのが現行運用 |

### 3-2. 更新・再起動プロンプト

| 項目 | 内容 |
|------|------|
| **トリガ** | 新バージョンが利用可能なとき / アップデート後 |
| **画面シグネチャ** | `A new version of Claude Code is available. Update now? (y/n)` または `Claude Code has been updated. Restart now? (y/n)` |
| **操作** | y/n + Enter |
| **JSONL** | ❌ TUI-only |
| **現在の c-lord 対応** | ❌ 未対応。固まる可能性あり |
| **検出パターン（候補）** | `"Update now"`, `"Restart now"`, `"new version"` |

### 3-3. Bypass-Permissions 警告

| 項目 | 内容 |
|------|------|
| **トリガ** | `--dangerously-skip-permissions` フラグ使用時の起動直後 |
| **画面シグネチャ** | `⚠ Caution: bypass permissions mode is on. ...` / `Press Enter to continue` |
| **操作** | Enter |
| **JSONL** | ❌ TUI-only |
| **現在の c-lord 対応** | ✅ `_handle_startup_prompts` の `Enter to confirm` マーカーで捕捉（信頼プロンプトと共通パス）。ステータスバーには `bypass permissions on` が常時表示 |

### 3-4. 編集差分確認（Do you want to make this edit?）

| 項目 | 内容 |
|------|------|
| **トリガ** | `--permission-mode default` 時の Write / Edit ツール実行前（ファイル変更の差分確認） |
| **画面シグネチャ** | diff 表示 + `Do you want to make this edit?` / `❯ 1. Yes` / `  2. Yes, and don't ask again` / `  3. No` |
| **操作** | 数字 + Enter |
| **JSONL** | ✅（Permission Request と同一フォーマット。`tool_name` が `Write` / `Edit`） |
| **現在の c-lord 対応** | ⚠️ JSONL パスでは `permission_request` として受信可能。TUI スクレイプパスも同一マーカー `"Do you want to proceed?"` で拾っている可能性あり（要検証） |

### 3-5. /config・/permissions 対話画面

| 項目 | 内容 |
|------|------|
| **トリガ** | ユーザーが `/config` や `/permissions` を入力 |
| **画面シグネチャ** | 全画面 TUI の設定パネル（項目一覧 + `❯` カーソル） |
| **操作** | 矢印 + Enter / Esc で戻る |
| **JSONL** | ❌ TUI-only |
| **現在の c-lord 対応** | ❌ 未対応（Discord 経由でこれらを使うユースケースは稀） |

### 3-6. Slash コマンド補完メニュー

| 項目 | 内容 |
|------|------|
| **トリガ** | 入力欄で `/` を入力したとき |
| **画面シグネチャ** | `/help`, `/clear`, `/model`, ... のインライン補完リスト |
| **操作** | 矢印 + Enter / Tab |
| **JSONL** | ❌ TUI-only |
| **現在の c-lord 対応** | ❌ 未対応（send-keys で `/command` を直接送ればよいため実害は少ない） |

---

## 4. ステータス・通知表示（操作不要）

### 4-1. ステータスバー

| シグネチャ | 意味 |
|-----------|------|
| `-- INSERT ⏵⏵ bypass permissions on` | dangerously-skip-permissions 有効、入力モード |
| `-- NORMAL` | ノーマルモード |
| `Model: Sonnet 4.6 Style: default` | ccstatusline 行 1 |
| `Cost: $0.05 Session: 7.0%` | ccstatusline 行 2（コンテキスト使用量） |
| `⎇ main (+0,-0) cwd: /path` | ccstatusline 行 3（ブランチ） |

### 4-2. 生成中インジケーター

`_GENERATION_STATUS_RE = r"^(?!❯)[✀-➿*·] .+$"` にマッチする行。Discord に漏洩させない。

例: `✻ Forming…` / `✶ Calling plugin…` / `✹ Thinking…` / `✻ Crunched for 2s`

---

## 5. 既存コードとの対応表

| プロンプト | JSONL | 現行 c-lord | 対応ファイル |
|-----------|-------|------------|------------|
| Trust prompt | ❌ | ✅ 自動 Enter | `tmux_runner._handle_startup_prompts` |
| Permission request | ✅ | ⚠️ TUI 自動 Enter のみ | `tmux_runner._accept_permission_prompt` |
| Plan approval | ✅ | ✅ Discord UI | `discord_ui/plan_view.py`, `embeds.py` |
| AskUserQuestion | ✅ | ✅ Discord UI | `discord_ui/ask_view.py`, `ask_handler.py` |
| Elicitation | ✅ | ✅ Discord UI | `discord_ui/elicitation_view.py` |
| Compaction | ✅ | ⚠️ 受信のみ | `claude/types.py` (is_compact) |
| Resume picker | ❌ | ✅ 回避（明示 ID） | `_run_helper.py` |
| Update/restart | ❌ | ❌ 未対応 | — |
| Model selection | ❌ | ❌ 未対応（起動時 --model で回避） | — |
| Bypass-perm 警告 | ❌ | ✅ 自動 Enter（trust と共通） | `tmux_runner._handle_startup_prompts` |

---

## 6. Phase 2 への示唆

### 優先度 High（よく詰まる / JSONL 対応済み）

1. **Permission Request** — JSONL で `permission_request` を受信しているのに TUI 自動 Enter だけ。Discord に `AskView` ボタン表示 → `request_id` で承認/拒否応答を実装すれば完結。誤検知リスクほぼゼロ。

### 優先度 Medium（TUI-only だが検出は容易）

2. **Update/restart prompt** — `"Update now"` / `"Restart now"` の固定文字列で検出可。セッション固まりの主因の一つ。検出後は「y」または「n」を Discord から選択させる。

### 優先度 Low（回避策あり / 稀）

3. **Model selection** — 起動時 `--model` 指定で事実上発生しない。
4. **/config 等** — Discord ユースケースなし。

### 誤検知対策

- `tmux_runner.py` の TUI スクレイプを増やさない。JSONL パスを最大限活用する
- TUI スクレイプが必要な場合（update prompt 等）は時間ベースではなく **固定文字列マッチ**のみ
- overlay 検出（#76）と ghost text（#62）の地雷を踏まないよう、scrape 結果は「操作が確定した後」にのみ使う

---

## 7. 未検証事項（要実験）

以下は手元実験 or Claude Code 公式ソース確認が必要：

- [ ] `Do you want to make this edit?` は `permission_request` と同一フォーマットか、別フォーマットか
- [ ] Compaction 発動時の Discord 通知は現在実装済みか（`is_compact` フィールドの EventProcessor 側処理を確認）
- [ ] `--permission-mode acceptEdits` では permission_request がどの操作に出るか（Write/Bash の差異）
- [ ] 更新プロンプトの実際の文字列（バージョンにより変化する可能性）
- [ ] Resume picker が出る条件の再現（DB にセッション ID が残っている状態）
