# デザインカンプ: Discord 出力の「あるべき姿」をモック画像で合意する (#316)

> これは **c-lord 自身の開発・保守のための内製ツール**。利用者に配る機能ではない。
> 対になる概念: 「実際どうだったか」の証跡（real-client の見た目）は別物 → #243。

## 何のため（Why）

c-lord 開発で「この機能は Discord 上で**こう見えるべき**」を**実装前に**合意したい。だが spec は散文だと、
yousan（コードを読まない立場）が「ゴール像」を掴めず、仕様かバグかを裁定できない（[#287](https://github.com/yousan/c-lord/issues/287) P1）。

このツールは、**手で組んだ会話データ**から Discord 風の PNG を描く。だから「**まだ存在しない、意図した状態**」を
絵にできる（実スクショは未来の状態を撮れない）。Issue に「**この機能のゴールはこれ**」という**デザインカンプ／
ワイヤーフレーム**を絵で添付し、実装前に合意する。

## 使い方

```bash
# 依存（Pillow）を入れる
uv sync --extra table

# サンプルを描く
uv run python scripts/discord_mockup.py scripts/examples/mockup_spec.json -o mockup.png --title "#new-feature"
# → wrote mockup.png (... bytes, 3 message(s))
```

できた PNG を Issue にドラッグ&ドロップで添付し、「あるべき動き」の合意材料にする。

## spec の書き方

JSON のメッセージ配列（または `{"messages": [...]}`）。1 メッセージ:

| キー | 例 | 説明 |
|------|----|------|
| `author` | `"C-lord"` | 発言者名（必須） |
| `content` | `"了解です\n\`\`\`py\n...\n\`\`\`"` | 本文。` ``` ` のコードフェンスは等幅ブロックで描画 |
| `is_bot` | `true` | bot なら `BOT` バッジを付ける |
| `timestamp` | `"2026-06-04 12:00"` | 時刻表示（任意） |
| `color` | `"#5865F2"` / `0x5865F2` | 名前の色（任意） |
| `embeds` | 下記 | ツール使用 embed 等 |
| `reactions` | `["🧠", {"emoji":"✅","count":2}]` | リアクション pill。文字列か `{emoji,count}` |
| `attachments` | `["diff.patch", {"filename":"log.txt"}]` | 添付チップ。文字列か `{filename,content_type}` |

embed: `{ "title": "🛠️ Bash(ls)", "description": "...", "color": "#FEE75C", "fields": [{ "name": "dir", "value": "/tmp", "inline": true }] }`

最小例:

```json
[
  { "author": "yousan", "content": "ゴールはこう 🙏", "reactions": ["✅"] },
  { "author": "C-lord", "is_bot": true, "color": "#5865F2",
    "embeds": [{ "title": "✅ Done", "color": "#57F287" }] }
]
```

サンプル全体は [`scripts/examples/mockup_spec.json`](../scripts/examples/mockup_spec.json)。

## 制約（正直に）

- これは **Discord 風の再現画（デザインカンプ）**であって、**実クライアントの実際の見た目ではない**。
  本物のレンダリングのバグ（ボタンが実際に出るか・崩れ等）を確かめたいなら、実際の証跡が要る → **#243**。
- 依存は既存の `c-lord[table]`（Pillow）のみ。ヘッドレスブラウザ等は使わない。
- コードフェンス（` ``` `）内は等幅フォントで描くため **CJK はうまく出ない**（豆腐になる）。コード例は ASCII 推奨。
- レンダラ本体は `c_lord/discord_ui/conversation_renderer.py`（`render_conversation_png` / `conversation_from_spec`）。
  CLI は `scripts/discord_mockup.py`（shipped パッケージの一部ではない dev ツール）。

## 関連

- #316（本ツール）/ #287 P1（あるべき挙動の正典）/ #243（real-client 証跡。別目的・別手段）
