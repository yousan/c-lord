# あるべき動き: ミラーは Discord の応答性を絶対に奪わない

> これは「あるべき動き」。理念 [`docs/PHILOSOPHY.md`](../PHILOSOPHY.md) の下位。迷ったら理念に照らす。

## これは何か

既定の jsonl モードでは、`sessions` の**行の数だけ** `TranscriptMirror` が動きます
(本番実測 252〜255 本)。各ミラーは Claude Code の transcript jsonl を
0.5 秒ごとに polling します。

この spec は「**そのポーリングが、利用者のメッセージ処理を邪魔しない**」ことを定めます。
「何を流すか」は [mirrored-events.md](./mirrored-events.md)、「二度流さないか」は
[transcript-mirror-replay-safety.md](./transcript-mirror-replay-safety.md) にあります。

## 大原則

**利用者がスレッドに投げたメッセージは、必ず・すぐに bot に届く。**

bot が何本のミラーを抱えていようと、transcript が何 MB あろうと、それは
利用者から見えてはいけません。ミラーは**裏方**です。

## あるべき動き

- スレッドにメッセージを送ると、**1 秒以内**に 🟢 ランプ（トリガーメッセージへの
  リアクション）が付く。ミラーが 250 本動いていても同じ。
- bot の起動中・再起動直後に送ったメッセージも取りこぼされない。
- `journalctl` / bot ログに `discord.gateway: ... heartbeat blocked` が出ない。

### なぜ「heartbeat blocked」がそのまま実害なのか

discord.py はゲートウェイの heartbeat をイベントループ上のコールバックとして
送ります。ループが詰まって heartbeat が落ちると、**Discord 側が shard を切断**し、
discord.py は再 IDENTIFY で再接続します。**切断中に送られたメッセージは
再配信されません**。つまり利用者からは:

> 送ったのに、返事も ❌ も出ない。跡形もなく消えた。

に見えます。2026-08-29 の本番では 26 連発で後退し、2 通が消えて
**約 29 時間**無応答になりました (#537)。

## 実装上の不変条件 (regression を防ぐ)

**`c_lord/transcript/` の中で、ファイルシステムに触る処理と JSON パースは
イベントループ上で実行しない。**

| 経路 | どこで実行するか | 根拠 |
|---|---|---|
| 起動時の #215 救出スキャン (`last_completed_final_answer`) | `asyncio.to_thread` | #537 (1回目) |
| 起動時の #433 uuid seeding (`_seed_seen_uuids`) | tail 専用プール | #537 (1回目) |
| **follow ループ1周分** (`_poll_once`: 対象ファイルの解決 / `stat` / `read` / `json.loads`) | **tail 専用プール** | **#537 (再オープン)** |

- **1 周を 1 回のスレッドホップにまとめる。** 解決とリードを別々に投げると、
  その合間にイベントループへ戻ってきてしまい、syscall の途中でループが走る形が残る。
- **プールは tail 専用。** `asyncio.to_thread` は c-lord の他の処理
  (git checkout・救出スキャン・`_transcript_has_ask_result`) と 1 つの
  bounded pool を共有する。250 本 × 毎秒 2 回 = 毎秒 500 タスクをそこへ流すと、
  それらが**キュー待ち**になる。ワーカ数は `CLORD_TAIL_WORKERS` (既定 8)。
- **1 周で読むバイト数に上限を置く** (8 MB)。ファイル切り替えは offset を 0 に
  戻すので、上限が無いと 109 MB を一度に decode + `split("\n")` してメモリを
  跳ね上げる (実測: 本番相当の負荷で RSS 2.1 GB)。打ち切った周はスリープせず
  即座に次周へ回るので、追いつきは遅くならない。

### 起動時のミラー本数

`closed_at` の入った行 (`!close-workspace` 済み) はミラーを起動しない。
誰も待っていないスレッドの、たいていディスク上で最大の transcript を
毎回読む理由がない (#537 AC3)。

## バグの例

- スレッドに投げたメッセージに 🟢 が付くまで 10 秒以上かかる → **バグ**
- 投げたメッセージが処理されず、返事も ❌ も出ない → **バグ**（#537 の実害そのもの）
- ログに `heartbeat blocked` が出る → **バグ**
- `transcript/` の中で `glob` / `stat` / `open().read()` / `json.loads` を
  `await` を挟まずイベントループ上で呼ぶコードが増える → **バグ**（再発の入口）

## 計測 (2026-08-31, このホスト)

| 対象 | コスト |
|---|---|
| `latest_session_jsonl` (182 ファイルの dir) | 2.9 ms |
| `latest_session_jsonl` (2481 ファイルの dir) | 28.9 ms → ミラー 1 本で毎秒 57.8 ms |
| 109 MB transcript の read + parse | 2.07 秒 |

ミラー 252 本 × 毎秒 2 周なので、1 周 2 ms でも**毎秒 1 秒分**のループ時間になる。
これが「ループが飽和する」の中身。

## 根拠

- **#537** — 本 spec の発端。1回目は起動経路だけを直し、follow ループが残って
  3 日後に再発した。テスト: `tests/transcript/test_startup_nonblocking.py`（起動経路）と
  `tests/transcript/test_tail_nonblocking.py`（follow ループ）
- **#627** — 対象 jsonl の選び方。`glob` を毎周やめる分、本 spec の負荷も下がる
- **#492 / #216** — jsonl mirror が既定の配信経路であること
