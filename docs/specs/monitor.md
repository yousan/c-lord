# Traffic monitor — 実トラフィック常時監視（#404）

`scripts/monitor/` は、**実際のトラフィック**（bot ログ＋稼働中スレッド）を定期スキャンして異常を拾い、
報告する **read-only** ツール。合成入力を作らず、ユーザが既に生成している実シナリオを観測対象にする。

このドキュメントが monitor の「**あるべき動き**」の一次情報源。

## Why（理念・経緯）

自然言語ファザー(#377)は staging で多数回走って **c-lord 本体バグ検出0**。原因は「一番薄くて堅い層
（単発テキスト→返信）」を叩き、オラクルが構造的破綻しか見ないため（診断は memory `fuzz-tests-wrong-layer`
／Issue #404）。実バグは **対話UI/状態ライフサイクル/描画/タイミング** に集中し、それは**実利用で踏まれて
ログ・リアクション・ペインに痕跡を残す**。だから「合成入力を強くする」より「**実トラフィックを監視する**」方が
当たる。実際この monitor は初回スキャンで prod bot の未処理 `KeyError`（tmux capture 経路）等を即検出した
（`docs/evidence/404/`）。

## 一連の流れ（あるべき動き）

定期（cron, 例 10 分毎）、1 回の実行で:

```
state を読む（前回までのログ offset と報告済み fingerprint）
   ▼
(1) ログ増分スキャン: 各ログファイルを前回 offset から末尾まで読み、
    traceback / [ERROR] / IDENTITY MISMATCH を拾う。[thread=/session=] 文脈を抽出
   ▼
(2) スレッド健全性スキャン（任意 / 要 bot token + guild または --threads）:
    稼働スレッドの trigger が 🟢 のまま 🟡 が付かず timeout 超過(=hang/no-response)、
    ❌、最新返信に chrome/traceback、を拾う
   ▼
fingerprint で dedup（既報告は除外）→ NEW 異常のみ
   ▼
docs/monitor-runs/<ts>.{json,md} を出力、state（offset + seen）を更新
   ▼
NEW があれば報告チャンネルへ投稿
```

利用者から見た変化: 監視チャンネルに「**新規異常 N 件**＋種別＋該当スレッド/ログ」が届く。traceback は
本文（exception 行）と発生スレッドまで辿れる。何も新規が無ければ静かに（投稿は NEW がある時だけ）。

## なぜこの設計か

- **external（bot 内 Cog ではない）**: hang/no-response は「bot が応えない」状態 → 内部監視では報告不能。
  外から（ログ＋REST）見る必要がある。prod + staging フリート全台を1本で監視できる。
- **read-only**: lease 取得なし・注入なし・restart なし。prod に当てても安全（観測専用）。
- **ログ走査が主軸**: traceback / `[ERROR]` は「バグを踏んだ瞬間」の最も曖昧さの無い信号。構造化ログ
  `[thread=/session=]`（`log_ctx`）で発生スレッドまで辿れる。**正規化**（timestamp/id/context を除去）で
  同一エラーの再掲を fingerprint dedup。
- **増分**: ログは offset 管理で前回の続きから。`…-latest.log` symlink が再起動で別ファイルを指しても、
  実パス解決＋truncation 検出で取りこぼさない（新ファイルは頭から走査）。
- fuzz の primitive（`Anomaly`/`fingerprint`/oracle の chrome・exception・emoji 判定）を再利用。

## 検出カタログ

| kind | severity | 源 | 意味 |
|------|----------|----|------|
| `LOG_TRACEBACK` | high | log | bot ログに traceback（未処理例外） |
| `LOG_ERROR` | medium | log | `[ERROR]` レベルのログ行 |
| `IDENTITY_MISMATCH` | critical | log | 誤った identity でログイン |
| `THREAD_STUCK` | high | thread | trigger が 🟢 のまま 🟡 にならず timeout 超過（hang/no-response） |
| `ERROR_REACTION` | high | thread | trigger に ❌ |
| `STALL` | medium | thread | trigger に ⏳/⚠️ |
| `CHROME_LEAK` / `EXCEPTION_LEAK` | high | thread | 最新返信に TUI chrome / traceback |

検出は**候補**。人間が triage する。

## 動かす

```bash
# ログだけ dry-run（投稿/ state 書き込みなし）。token 不要
python -m scripts.monitor --dry-run --logs '/tmp/clord-bot-c-lord*.log'

# 本番運用相当（ログ + スレッド + 報告）
python -m scripts.monitor \
  --logs '/tmp/clord-bot-c-lord*.log' \
  --guild <guild_id> --channels <ch1,ch2> --report-channel <report_ch>

# cron（10 分毎・:00 を避ける）
3,13,23,33,43,53 * * * * MONITOR_LOGS='/tmp/clord-bot-c-lord*.log' \
  MONITOR_GUILD_ID=<g> MONITOR_CHANNELS=<chs> MONITOR_REPORT_CHANNEL=<ch> \
  /home/you/c-lord/scripts/monitor/cron.sh
```

主なフラグ: `--logs`(glob可) / `--channels` / `--threads`(guild の代わりに直接指定) / `--guild` /
`--report-channel` / `--stuck-timeout`(既定600s) / `--state-file` / `--dry-run` / `--no-report`。
設定キーは `.env.example` の Monitor 節参照。

## 既知の限界 / 将来（別 Issue）

- ログの `[ERROR]` には良性のものも混じる（dedup で再掲は抑えるが初回は出る）。重要度の学習的フィルタは将来。
- スレッド走査は active threads（guild）か明示 thread 一覧。アーカイブ済みは未対象。
- **B**: 対話フロー能動駆動 + tmuxペインvs Discord の視覚差分 + LLM/vision 判定（別 Issue, #404 out-of-scope）。
