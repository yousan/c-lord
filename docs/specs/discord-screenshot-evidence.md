# あるべき動き: Discord 画面の自動証跡スクリーンショット 📸 (#243)

> これは「あるべき動き」。理念 [`docs/PHILOSOPHY.md`](../PHILOSOPHY.md) の下位。迷ったら理念に照らす。
> 対になる tmux 側（「原因」の証跡）は #285/#286 の `/tmux-screenshot`。本書は Discord 側（「結果」の証跡）。

## これは何か

PR/Issue の証跡として「**Discord 画面そのもの**」が欲しい場面がある。テキストログでは表に出ない種類の問題
（レイアウト・ボタンの出る/出ない・絵文字ランプの状態・添付の見え方）を、メンテナはその画像を見て
spec/bug を判断している。これまで Discord 画面の証跡は **人間が手でスクショを撮って貼る**しかなく、
AI が自走で完了まで持っていく際のボトルネックだった（[#287](https://github.com/yousan/c-lord/issues/287) P4）。

C-lord は **Claude のターンが完了するたびに、現在スレッドを自動で PNG にレンダリングして証跡化する**。
サーバ上の AI は GUI を持たず Discord クライアントの画面を撮れないので、**Discord クライアントを撮るのではなく、
c-lord が既にプロセス内に持っているデータ（`channel.history`）から会話を自前で描画する**。これは tmux 側の
`/tmux-screenshot`（#285）が ANSI ペインを自前で PNG 化したのと同じ思想。

## あるべき動き（利用者から見て）

### 自動証跡（既定の挙動）

- **Claude が 1 ターンを返し終えると、そのスレッドの会話を写した PNG が自動で残る。**
  - スレッドにも画像が**投稿される**（その場で結果の見た目を確認できる）。
  - 同じ画像が `<session_dir>/.evidence/discord-<日時>.png` に**保存される**（AI が PR の `## Staging Evidence` に
    添付・参照するため）。`.evidence/` は git に出ない（汚さない）。
- 画像には、利用者が実際に Discord で見ているものが写る: 発言者・アバター・時刻・本文（コードブロック含む）・
  **ツール使用 embed**（`🛠️ Bash(...)` / `✅ Done`）・**状態ランプのリアクション**（🧠/🛠️/✅）・添付チップ。
- **連投防止（debounce）**: 短時間に何度も完了しても、1 スレッドにつき既定 12 秒に 1 回までに間引かれる。

### 手動でも撮れる

- スレッドで **`/discord-screenshot`**（スラッシュ）または **`!discord-screenshot`**（テキスト twin）を実行すると、
  その時点のスレッドを撮って投稿する。`engine` を選べる（下記）。テキスト twin は webhook/E2E 用。

### 描画エンジンは 2 つ（見た目の忠実度 vs 依存の重さ）

- **`pillow`（既定）** — 追加依存ゼロ（既存の `c-lord[table]` extra の Pillow のみ）。どの環境でも動き、完全オフライン。
- **`html`** — HTML/CSS をヘッドレスブラウザ（Chrome/Chromium）で描画。Discord の実レイアウトに最も忠実だが、
  **ブラウザが要る**。ブラウザが無ければ静かに失敗する（`pillow` を選び直せばよい）。
- **`auto`** — ブラウザがあれば `html`、無ければ `pillow` に自動フォールバック。

### 境界

- **スレッド外**で手動コマンドを使うと、「Claude チャットスレッド専用」と ephemeral で返す。
- **メッセージが無い**スレッドでは撮らない。
- **依存が無い**（Pillow も無く `engine` 用ブラウザも無い）ときは、画像を出さず「`pip install c-lord[table]` を」と
  ephemeral で案内する。**証跡が撮れなくても Claude のターンは絶対に壊さない**（自動証跡の失敗は握り潰す）。

## 設定（環境変数 / zero-config）

利用者はパッケージ更新だけで自動で有効になる。嫌なら **コードを書き換えずに** env で切れる:

| 環境変数 | 既定 | 効果 |
|----------|------|------|
| `CLORD_AUTO_SCREENSHOT` | on | `0` で自動証跡を**完全に無効化** |
| `CLORD_AUTO_SCREENSHOT_POST` | on | `0` で**保存はするが投稿しない**（スレッドを汚したくない運用向け） |
| `CLORD_AUTO_SCREENSHOT_ENGINE` | `pillow` | `pillow` / `html` / `auto` |
| `CLORD_AUTO_SCREENSHOT_LIMIT` | `25` | レンダリングする直近メッセージ数 |
| `CLORD_AUTO_SCREENSHOT_DEBOUNCE` | `12` | 同一スレッドの最小撮影間隔（秒） |
| `CLORD_HEADLESS_BROWSER` | 自動検出 | `html` 用ブラウザのパスを明示指定（省略時は PATH から探す） |

---

## なぜ「自前描画」なのか — Discord 規約の調査結果 (AC2)

当初案は「**ヘッドレスブラウザで本物の Discord クライアント画面を撮る**（検証専用アカウント）」だった。
調査の結論は **不採用**。理由は規約・技術の両面で決定的：

1. **bot トークンはクライアント UI にログインできない。** bot アカウントは REST API と Real Time Gateway 専用
   （`Authorization: Bot <token>`）。Discord web/desktop クライアントの画面を開けるのは**実ユーザーアカウントだけ**。
   → 「ヘッドレスブラウザで本物の画面」は構造上、ユーザーアカウントの自動ログインを要求する。
2. **ユーザーアカウントの自動操作＝self-bot で規約違反（アカウント停止リスク）。**
   - Help Center「Automated User Accounts (Self-Bots)」: *"Automating normal user accounts (generally called 'self-bots')
     outside of the OAuth2/bot API is forbidden, and can result in an account termination if found."*
     <https://support.discord.com/hc/en-us/articles/115002192352-Automated-User-Accounts-Self-Bots>
   - Community Guidelines: *"Do not use self-bots or user-bots. Each account must be associated with a human, not a bot."*
     <https://discord.com/guidelines>
   - Platform Manipulation Policy: *"Modifying a user account to perform automated actions — regardless of the type of action"* を禁止。
     <https://discord.com/safety/platform-manipulation-policy-explainer>
   - Terms of Service §9: スクレイピング（robot/crawler 等での自動取得）を禁止。<https://discord.com/terms>
3. **専用・使い捨て・読み取り専用・非公開サーバ、いずれも例外にならない。** 禁止対象は「ユーザーアカウントを自動操作する
   行為そのもの」で、可視性・意図・範囲・場所に依らない。throwaway/private にしても**検知されにくくなるだけで許可はされない**。

→ よって route-1（実クライアント自動操作）は **GO/NO 以前に不採用**。Discord 公式が self-bot 記事で案内している
正規ルート＝「**bot アカウントで API からデータを取り、自前で描画する**」を採る（= route-2）。本機能はこれ。

> 規約は安定しているが、運用前に最新を確認すること（上記リンクは 2026-06 時点）。

---

## アーキテクチャ (AC3)

```
Claude turn 完了 (RESULT)                         手動: /discord-screenshot, !discord-screenshot
        │                                                        │
   EventProcessor._on_complete                          SessionManageCog._discord_screenshot_impl
        │ env gate + debounce                                    │
        ▼                                                        ▼
   evidence.capture_evidence(thread, working_dir) ──────► conversation_renderer
        │                                                  ├─ fetch_conversation(channel)  … channel.history → ConvMessage[]
        ├─ .evidence/<ts>.png に保存                         ├─ render_conversation(msgs, engine=...)
        └─ スレッドに投稿（既定）                              │    ├─ render_conversation_png        (Pillow)
                                                            │    └─ render_conversation_html_png   (HTML + headless Chrome)
                                                            └─ → bytes | None  (依存無し時は None で graceful degrade)
```

| ファイル | 役割 |
|---------|------|
| `c_lord/discord_ui/conversation_renderer.py` | データモデル（`ConvMessage` 等）＋ `fetch_conversation`（`channel.history`）＋ 2 エンジン＋ dispatcher。`bytes \| None` 契約で依存欠如時は `None`。 |
| `c_lord/discord_ui/evidence.py` | 自動証跡オーケストレータ。env ゲート・debounce・`.evidence/` 保存・スレッド投稿・`.git/info/exclude` への登録。**例外は握り潰す**。 |
| `c_lord/cogs/event_processor.py` | `_on_complete` で `_maybe_auto_evidence()` を呼ぶ完了フック。 |
| `c_lord/cogs/session_manage.py` | 手動 `/discord-screenshot` slash ＋ `!discord-screenshot` text twin（`_discord_screenshot_impl`）。 |

**設計上の選択（fonts/optional-dep は #285 を踏襲）**:

- **対象限定**: スレッド単位（= Claude セッション単位、設計判断 #2）。bind 済みチャンネル配下のスレッドで動く。
- **認証**: 追加認証なし。bot 自身の既存トークンで `channel.history`（プロセス内 discord.py のみ。ブラウザも第2トークンも不要）。
- **依存**: Pillow は既存の `c-lord[table]` extra。`html` エンジンのブラウザはランタイムに存在すれば使い、無ければ degrade。
  新しい必須依存は増やさない（CLAUDE.md「重い依存は持ち込まない」）。
- **zero-config**: 既定 on・auto-discovery。新しい必須セットアップ無し。env で opt-out。

## スコープ外 / フォローアップ

- **REST API エンドポイント**（`POST /api/threads/{id}/screenshot`）からの発火 — 必要になれば別 Issue。
- **実アバター画像の取り込み** — 現状は頭文字＋色ディスク（Discord のデフォルトアバター風）。実画像 DL は follow-up。
- **インライン Markdown**（太字/斜体/インラインコード）の厳密な描画 — 現状はコードフェンス（```）のみ特別扱い。
- route-1（実クライアント自動操作）は上記の通り**恒久的に不採用**。

## 関連

- [#243](https://github.com/yousan/c-lord/issues/243)（本機能）/ [#287](https://github.com/yousan/c-lord/issues/287) P4（位置づけ）
- 対の tmux 側: #285 / #286（`/tmux-screenshot`、`docs` の対応記述）
- 設計判断 #2（Thread=Session）・#7（REST 制御面）・#10（per-channel）
