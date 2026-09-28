# スラッシュコマンドの返事は誰に見えるか (#748)

> あるべき動きの記述。実装は `c_lord/discord_ui/slash_io.py`、
> テストは `tests/test_slash_ephemeral_after_defer.py`。

## Why

c-lord はフレームワークで、**知らない人がいるサーバでも動く**。操作した本人向けの返事
（「このチャンネルにはリポジトリが紐づけられていません」「`pip install c-lord[table]` を
入れてください」など）がスレッド全員に見える形で残ると、**無関係な利用者の会話に混ざる**。
利用者は自分が何か壊したのかと思う。

2026-09-18、webio サーバの利用者のスレッドの最後のメッセージが、別の人が `/tmux-screenshot`
を失敗させたときの Pillow のエラーになっていた。コードは `ephemeral=True`（本人だけ）の
つもりだったのに、Discord 上は `flags: 0`（全員に見える）で残っていた。

## 規則

| 返事の種類 | 見える人 |
|---|---|
| コード上 `ephemeral=True` の返事（案内・エラー・権限不足など） | **実行した本人だけ**。スレッドには何も残らない |
| コマンドの成果物（`/tmux-screenshot` の PNG、`Session started → …` など） | スレッドの全員（今までどおり） |
| 最初から本人向けに受け付けたコマンド（`/tmux-list` など、`ack(ephemeral=True)`） | 本人だけ（今までどおり） |
| コマンドが途中で例外を出したときのエラー通知（`on_app_command_error`） | 本人だけ |

## なぜ壊れていたか（Discord 側の決まり）

3 秒以内に答えられないコマンドは、まず `defer()`（「考え中…」の仮メッセージ）で受け付け、
あとから followup で答える。Discord は **仮メッセージが残っている間の最初の followup を
新しいメッセージにせず、仮メッセージの書き換えとして扱う**。そのとき followup の
`ephemeral` 指定は**無視され、`defer()` 時の見え方がそのまま残る**（Discord API docs,
"Create Followup Message"）。

c-lord の多くのコマンドは `defer()` を公開で行う（成功時の結果は全員に見せたいから）。
だから「失敗したので本人にだけ伝える」返事が、公開の仮メッセージに化けていた。

## いまの動き

公開で受け付けた後に本人向けの返事をするときは、**先に公開の「考え中…」を消してから**
本人向けの返事を送る。周りの人には「考え中…」が一瞬見えて消えるだけで、何も残らない。

- この処理は `slash_io()` 1か所にある。スラッシュコマンドの返事はここを通る
  （以前は `SessionManageCog` / `/clord` / `/skill` がそれぞれ同じコードを持っていて、3つとも同じ穴があった）
- `ephemeral=True` の followup を `slash_io.py` 以外で送ると
  `tests/test_slash_ephemeral_after_defer.py` の静的チェックが落ちる

## スコープ外

- `!tmux-screenshot` などテキスト版は Discord の仕組み上 ephemeral が無い（常に公開）
- Pillow（`c-lord[table]`）が入っていないこと自体
