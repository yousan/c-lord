# Fuzz harness — 自動シナリオ fuzzing（#377）

`scripts/fuzz/` は、**LLM が生成した adversarial な自然言語シナリオ**を毎時 staging bot へ
撃ち込み、想定外の挙動（no-response / TUI chrome leak / error reaction / stall / exception
leak など）を自動検出して `#fuzz-report` に報告する fuzzing/chaos テスト harness。

このドキュメントが harness の「**あるべき動き**」の一次情報源。実装を変えたらここも直す。

## Why（理念に照らす）

c-lord の CI は `ruff`+`pyright`+`pytest`（mock）しか回さず、**実機の想定外入力**で出る不具合
（chrome leak 系 #23–#50、stall #359 など）は素通りする。人手 E2E は決め打ちシナリオしか撃たない
ので、**人間が思いつかないカテゴリ**は穴になる。LLM に「c-lord を壊しそうな入力」を毎時生成させて
撃ち続ければ、未知の回帰・edge を早期に拾え、人間はレポートのアノマリ候補を triage するだけで済む。

## 一連の流れ（あるべき動き）

毎時（cron）、staging clone から:

```
borrow staging lease ──(他 owner が占有)──▶ skip / exit 0（その回は撃たない）
   │ 取得
   ▼
bot health 確認（--restart-if-down なら down 時に staging を再起動）
   ▼
claude CLI で N 件の自然言語シナリオを生成（生成 raw を artifact 保存）
   ▼
各シナリオを注入（既定 /api/spawn = シナリオごとに fresh thread）
   ├─ 返信本文（c-lord が /api/reply で出す最終回答）を polling
   └─ seed メッセージの状態リアクション（🟢🟡❌⏳⚠️）を wait 中ずっと union 収集
   ▼
oracle が各観測からアノマリ候補を検出
   ▼
docs/fuzz-runs/<ts>.json と <ts>.md を出力（再現用）
   ▼
#fuzz-report に毎時サマリを投稿（「撃ち N / 注入 x / 返信 y / 異常 M」＋該当 thread URL）
   ▼
release lease（finally で必ず）
```

利用者から見える変化: `#fuzz-report` に毎時サマリが届き、異常があれば中身（撃った入力・期待 vs 観測・
該当スレッド URL）を辿れる。`docs/fuzz-runs/<ts>.gen.txt` に「その回 LLM が何を撃ったか」が残る。

## なぜこの設計か（決定の記録）

- **撃ち先は staging のみ・本番は撃たない。** 毎時 fresh thread が生成されるため本番チャンネルを
  汚さない。staging はリース制共有資源なので、harness は毎回 `borrow → run → release` し、
  他者が借用中の回は **skip（exit 0）**。本番 (`/home/yousan/c-lord`) は触らない。
- **生成は純 LLM（claude CLI に委任）。** テンプレ網羅では「人間の想定内」しか撃てない。adversarial
  生成に任せると `tui-chrome-mimicry` / `zero-width-control-chars` / `unterminated-nested-markdown`
  のような**想定外カテゴリ**を自走で発掘する。再現性は生成 raw 保存で緩和（bit 完全再現は対象外）。
- **生成フォーマットは JSON ではなく区切りブロック**（`===FUZZ===` / `---TEXT---`、text は raw）。
  payload 自体が quote / backslash / code fence を含み、LLM の JSON は壊れる（haiku で実測）。区切り
  形式なら text を一切エスケープせず運べる。JSON はフォールバックとして残す（`scripts/fuzz/scenarios.py`）。
- **注入は `/api/spawn` 主。** c-lord はチャンネル直投稿を無視し（`claude_chat.py:372`、スレッド生成は
  slash command と spawn のみ）、新規スレッドは spawn 経由が必須。`--inject webhook` は既存スレッドへ
  多ターン投入する代替（session 継続のテスト用）。
- **オラクルの信号源は seed メッセージのリアクション。** `spawn_session` は seed に `StatusManager` の
  状態リアクションを付ける。❌/⏳/⚠️ は override で 🟡 に置換されうるので、wait 中に**union 収集**して
  transient を取りこぼさない。最終回答は `/api/reply` の plain 本文なので、tool-use embed や `-#` 始まりの
  CLI 入力行を除外して拾う。

## アノマリ・カタログ（`scripts/fuzz/oracle.py`）

検出は**確定バグではなく候補**。人間が triage する（adversarial 入力が正当に ❌ になる場合もある）。

| kind | severity | 何を意味するか |
|------|----------|--------------|
| `SPAWN_FAILED` | high | 注入自体が失敗（/api/spawn が 201 を返さない等）。後続観測は不能 |
| `HEALTH_DOWN` | critical | シナリオ後に `/api/health` が落ちている（クラッシュの疑い） |
| `NO_RESPONSE` | high | timeout 内に最終回答が来ない（lamp が 🟢 のままならターン未完了） |
| `ERROR_REACTION` | high | seed に ❌ が付いた（bot がそのターンをエラー扱い） |
| `STALL` | medium | ⏳/⚠️ が付いた（stall 検知） |
| `CHROME_LEAK` | high | 返信に TUI chrome（`Model: ` 等）や裸の `❯` が混入（#23–#50 回帰） |
| `EXCEPTION_LEAK` | high | 返信に traceback（`Traceback (most recent call last)` 等）が漏れた |
| `EMPTY_REPLY` | medium | 返信は来たが本文が空 |

`fingerprint()` は `kind` + 正規化 evidence の安定ハッシュ。`docs/fuzz-runs/seen.json` に既知の
fingerprint を貯め、レポートで **new vs seen** を出して既知異常の再掲を見分ける（run 内は全件報告）。

## 安全機構

- **本番を撃たない / kill しない。** 撃ち先・リース対象は staging clone のみ。
- **リース。** 他者占有中は skip。`finally` で必ず release。死んでも TTL（1h）で自然解放。
- **実行時間 budget。** `--budget`（既定 1200s）超過で残シナリオを skip（次の hour に食い込まない）。
- **長さガード。** Discord 2000 字超の payload は seed 送信が失敗するため truncate（非 Nitro の現実的入力に合わせる）。
- **subprocess は exec + `--` separator**（`shell=True` 禁止）。生成プロンプトはフラグ注入されない。

## 一度きりのセットアップ（人手）

harness は「チャンネルが用意され `/clord-init` 済み」を前提に動く。初回だけ以下を行う:

1. Discord に **`#fuzz-staging`**（注入先）と **`#fuzz-report`**（報告先）を作る。
2. `#fuzz-staging` を sandbox リポジトリに紐づける: チャンネルで `/clord-init repo:<URL> branch:<branch>`
   （spawn したセッションが実際に動くために必要。本番リポを避け、捨てて良い repo を推奨）。
3. staging clone（例 `c-lord-parallel-3`）の `.env` に `FUZZ_*` を設定（`.env.example` の Fuzz harness 節参照）:
   - `FUZZ_CHANNEL_ID` = `#fuzz-staging` の channel id
   - `FUZZ_REPORT_CHANNEL_ID` = `#fuzz-report` の channel id
   - `FUZZ_GUILD_ID` = サーバ id（レポートの thread URL をクリッカブルにする）
   - **`FUZZ_API_URL` = staging bot の API の実ポート**（既定 `CLORD_API_URL`）。
     ⚠️ **これがズレていると spawn 注入が毎回 `SPAWN_FAILED`/`HEALTH_DOWN` になる**（#377 の検証中、
     `.env` の `CLORD_API_URL=:8089` が実際の bind とドリフトしていて実害が出た）。設定後に
     `curl -m3 $FUZZ_API_URL/api/health` が 200 を返すことを必ず確認する。API がこのホストから
     到達不能なら `--inject webhook --skip-health`（`FUZZ_WEBHOOK_URL`＋`FUZZ_TEST_THREAD_ID` を設定）で運用する。
4. cron を入れる（毎時 0 分）。**フリート（#395）**は `FUZZ_STAGING_CLONES` に複数 clone を渡す:

   ```cron
   0 * * * * FUZZ_STAGING_CLONES=/home/you/c-lord-staging-1,/home/you/c-lord-staging-2,/home/you/c-lord-staging-3,/home/you/c-lord-staging-4 \
     /home/you/c-lord/scripts/fuzz/cron_hourly.sh
   ```

   `cron_hourly.sh` は自分の置かれた clone（harness コードと `.venv` を持つ安定 clone）から実行し、
   `--staging-clones` でフリートを指す。config は **各 staging clone 自身の `.env`** から読む。
   ログは `$FUZZ_LOG_DIR`（既定 `/tmp`）に per-run + 最新 symlink で残る。単一台なら
   `FUZZ_STAGING_CLONES` の代わりに `FUZZ_STAGING_CLONE_DIR` でも可。

### フリート rotation と注入モード自動判定（#395）

- **rotation**: `--staging-clones`（または `FUZZ_STAGING_CLONES`）に複数 clone を渡すと、harness は
  **lease を borrow できた最初の台で実走**する。全台が占有中/不在なら静かに skip（exit 0）。1台が
  検証で塞がっていても、別の空き台で撃てる（staging フリート #381 の活用）。
- **jsonl-bridge 自動判定**: 対象 clone の `.env` が `CLORD_BRIDGE_MODE=jsonl` の場合、その bot は
  **REST API（ApiServer）を bind しない**（`docs/STAGING.md`「staging フリート」節）。harness は
  `--inject` 未指定ならこの台を自動的に **webhook + skip-health** で撃つ（spawn の偽陽性 SPAWN_FAILED/
  HEALTH_DOWN を出さない）。明示 `--inject spawn` は尊重する。
- **堅牢化**: `--staging-dir`/clone が**存在しない**場合は「その台を skip」として扱い、`FileNotFoundError`
  で落ちない（フリート改称で対象 dir が消えた事故を踏まえた #395）。

## 手で動かす

```bash
# 生成だけ（注入・リース・報告なし）。claude CLI さえあれば動く
python -m scripts.fuzz --dry-run -n 5 --model haiku

# staging へ 1 回（リースは使わずローカル検証）
python -m scripts.fuzz --no-lease -n 3 --channel <fuzz-staging-id> \
  --report-channel <fuzz-report-id> --staging-dir /home/you/c-lord-parallel-3

# 毎時 cron が打つのと同じ実体
python -m scripts.fuzz --restart-if-down --staging-dir /home/you/c-lord-parallel-3
```

主なフラグ: `-n/--count`・`--inject {spawn,webhook}`・`--focus <theme>`・`--model`・`--timeout`
（1 シナリオの返信待ち）・`--budget`（全体）・`--no-lease`・`--no-report`・`--dry-run`・`--restart-if-down`。

## 既知の限界 / 将来

- LLM 生成は非決定的。`*.gen.txt` で「何を撃ったか」は残るが bit 完全再現はしない。
- cross-run の dedup は `seen.json` による単純な fingerprint 抑制まで（学習的な抑制は将来 Issue）。
- 注入は逐次。並行注入での負荷テストは対象外（`MAX_CONCURRENT_SESSIONS` と相談）。
