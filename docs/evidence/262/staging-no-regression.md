# Staging Evidence — #262 (PR #264)

staging: `c-lord-parallel-3` / E2E thread `1514085380666691664` / 2026-06-11 / branch `fix/262-mirror-dedup-answered-ask`

本不具合（回答済み AskUserQuestion を post-turn mirror が二度目に bridge）は lag/再起動 replay の
タイミングレースで決定論的な staging RED 再現が困難 → RED→GREEN は**ユニットテスト**で担保
(`test_mirror_skips_already_answered_ask`)。staging では「**通常の Ask bridge が壊れていない（回帰なし）**」を確認した。

## 手順と観測（GREEN, branch）
E2E スレッドで Claude に「AskUserQuestion で 赤/青 の2択を1問」を依頼 → tmux TUI メニューで「赤」を回答。

bot ログ:
```
15:01:38 c_lord.claude.tmux_runner: Interactive menu detected, bridging to Discord (thread=1514085380666691664)
```
→ bridge は **1 回だけ**。

Discord REST 取得（時系列）:
```
[…509902645694524] C-lord-3: ''                       components=2   ← Ask メニュー（1枚のみ）
[…510011420901606] C-lord-3: '⚠️ No activity for 30s…'  components=0
[…510050700558427] C-lord-3: '**赤** が選ばれました。'    components=0   ← 回答後の最終回答
```
→ 回答後、**components=2 の Ask メニューは再出現していない**（post-turn mirror が回答済み Ask を再 bridge しない）。

`green-thread-after-answer.png` は実クライアントのスレッド表示（スクロール位置の都合で Ask 直上に着地）。
決定的証跡は上記 bot ログ + REST メッセージ列。
