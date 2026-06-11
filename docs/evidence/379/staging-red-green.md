# #379 Staging Evidence — close-workspace mirror un-archive

検証日: 2026-06-11 / staging bot `C-lord-3#1206` (`/home/yousan/c-lord-parallel-3`, `CLORD_BRIDGE_MODE=jsonl`)
E2E thread: `1514085380666691664` (staging guild `1503196656265597082`)

## 再現の仕掛け（決定論的）

実バグは「close-workspace の kill が生む `<task-notification>` を mirror が 👤 echo →
archive 後に着弾 → un-archive」。staging では background task の kill を毎回同条件で起こせないため、
**「close-workspace で archive した後に transcript の user 行が増えると mirror がそれを 👤 で post する」**
という同一機構を、当該 thread の Claude transcript jsonl に合成 user 行を 1 行 append して再現した
（`{"type":"user","message":{"role":"user","content":"...probe..."}}` → formatter が `user_input`(👤) として描画）。
観測量は Discord REST の `thread_metadata.archived`（＝利用者が見る「閉じた/開いた」の真実）。

## RED（main, 修正前）

```
[RED main] reset → unarchive:           archived=False
[RED main] webhook !close-workspace:     archived=True    (close がアーカイブ)
[RED main] append synthetic user 行:     archived=False   ← 👤 echo が着弾し thread が再オープン（バグ）
[RED main] stop ログ:                    （なし — main は mirror を stop しない）
```

mirror が実際に 👤 を post している（thread 内メッセージ, 08:35:00）:

```
08:34:56 C-lord-3:  [EMBED 🧹 Workspace Closed]
08:35:00 C-lord-3:  👤 clord #379 staging RED probe (synthetic transcript event)   ← archive 後に着弾
```

## GREEN（fix/379-close-workspace-stop-mirror, 修正後）

```
[GREEN fix] reset → unarchive:           archived=False
[GREEN fix] webhook !close-workspace:     archived=True    (close がアーカイブ)
[GREEN fix] stop ログ:
  17:39:26 [INFO] c_lord.cogs.session_manage: [thread=1514085380666691664] stopping transcript mirror (workspace teardown)
  17:39:35 [INFO] c_lord.transcript.mirror:   TranscriptMirror stopped: thread=1514085380666691664
[GREEN fix] append synthetic user 行:     archived=True    ← mirror 停止済みで echo されず、archive 維持（修正）
```

GREEN では `👤 ... GREEN probe` は thread に **出ない**（mirror が close で停止済み）。

## 利用者から見た差分

- before（main）: `/close-workspace` 後、transcript が動くと 👤 echo でスレッドが即開き直る（閉じない）
- after（fix）: `/close-workspace` 後、スレッドはアーカイブされたまま畳まれる（次のメッセージで resume は従来どおり）

## 実画面スクショについて

- **RED の実 Discord 画面**は本番で実際に発生したもの（利用者 yousan 提供）を `red-prod-real-screen.png` に同梱。
  `🧹 Workspace Closed` の直後に `👤 <task-notification ... status killed ...>` が並んでおり、これが本バグの実体。
- staging guild (`1503196656265597082`) は証跡用テストアカウントが**未参加**のため、`scripts/discord_evidence_shot.sh`
  での AI 自動キャプチャは「テキストチャンネルがありません」となり描画不可（`docs/discord-evidence-capture.md` の既知制約）。
  そのため staging 側の RED→GREEN は上記 REST 観測（archived 状態遷移）＋ bot ログを主証跡とした。

## 注記（環境ノイズ）

staging チャンネルには複数 bot が在席し `!close-workspace` に重複応答するが、当該 thread の transcript を
mirror するのは staging session dir を持つ `C-lord-3` のみ（prod bot は `c-lord-sessions/` を見るため当 thread を
mirror しない）。よって archived 状態の観測は本検証で一意。
