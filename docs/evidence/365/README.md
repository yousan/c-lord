# Evidence — #365 「Claude has finished」メンションが実応答より前に飛ぶ

## RED（修正前・本番で観測された現象）

本番スレッド `#W71 │ 選択肢が無視されている`（thread=1514163047453687828）, 2026-06-11 10:57。

- `red-mention-before-response-1.png` / `red-mention-before-response-2.png`
  - ①ユーザー発言「そうですね、おすすめされた通り…」
  - ②C-lord: `@yousan Claude has finished — your reply is needed here.` ← **早すぎる**
  - ③C-lord: 「詳細調査が役に立って…①done… Now ② staging reproduction…」← 実応答（メンションの**後**）

### 本番ログ（RED in the wild）

`/tmp/clord-bot-c-lord.log`:

```
10:57:47 [INFO] _run_helper: run_claude: enter (prompt=422 chars)
10:57:55 [INFO] _run_helper: run_claude: exit          ← ここで WAITING_INPUT → メンション
```

メンションのタイムスタンプ `01:57:55 UTC`（= 10:57:55 JST）が `run_claude: exit` と完全一致。
422 文字の入力に対する複数段落の応答が **8 秒**で「完了」扱い。
同スレッドの本物のターンは 77 秒・196 秒。継続ターンの早期 exit が連発（10:28:46 / 10:33:36 / 10:39:29 / 10:57:55）。

## 根因と修正

`tmux_runner.py` の poll ループが、継続スレッド（`claude_running=True`）で、前ターンの残像（stable・非空・プロンプト可視）を新ターンの「完了」と誤検出していた。
修正：新ターンが実際に始まったこと（生成スピナーを1回以上観測 or 応答がベースラインから変化）を確認してから完了を受理する（`saw_generation` / `baseline_response` ゲート）。

## 決定的な RED→GREEN（ユニットテスト）

`tests/test_tmux_runner.py::TestPrematureCompletionOnStaleResponse::test_does_not_finalize_on_stale_previous_response`

- RED（修正前）: `assert 6 > 12` で失敗 — stale phase 内の 6 polls で早期完了。
- GREEN（修正後）: stale phase を越えて新ターンの生成を観測してから完了。PASS。
