# `/clord-status` デザインコンプ（#363）

チャンネル内のセッション状態を 1 コマンドに集約する `/clord-status` の「あるべき見た目」。
散文 spec では裁定しづらいため、`scripts/discord_mockup.py` で描いた Discord 風モックで合意する（#316）。

- 仕様の本体・受け入れ条件は **Issue #363** を参照（こちらが現在の真実）。
- `mockup_spec.json` … 最新モックの spec（`uv run python scripts/discord_mockup.py docs/specs/clord-status/mockup_spec.json -o out.png` で再描画可）。
- `history/` … 設計の変遷スナップショット（経緯）。

## 変遷（なぜこの形に収束したか）

| # | 画像 | 何を変えたか / なぜ |
|---|------|----------------------|
| 01 | `history/01-embed-blocks.png` | 初版。1 セッション=1 embed ブロック（容量・attach・resume を縦に） |
| 02 | `history/02-wide-table.png` | 「縦短く横長く」要望 → 1 セッション=1 行の等幅テーブルへ |
| 03 | `history/03-dense-hash-status.png` | `#`=window 番号の数字のみ／attach をヘッダのパターンに集約（文字削減）／status 列追加 |
| 04 | `history/04-docker-ps-aps.png` | `docker ps`/`docker ps -a` モデル採用。既定=live、`all`=live+closed、deleted は行なし・footer 件数のみ |

> 注：history 内のモックは status を `idle` と表記しているが、`wait` と紛らわしいため **`closed` に改名**した（最新仕様は Issue #363 本文）。
> モックツールは `**`/バッククォート/絵文字を描画しないため、実 Discord ではそれらが正しく描画される。
