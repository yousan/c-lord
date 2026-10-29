# あるべき動き: ターンが終わったことを、外のプログラムへ1件知らせる

> これは「あるべき動き」。理念 [`docs/PHILOSOPHY.md`](../PHILOSOPHY.md) の下位。迷ったら理念に照らす。

## これは何か・なぜ要るか

webhook で C-lord に仕事を出す側（OpenClaw のエージェント、CI、ダッシュボード）は、これまで
「ターンが終わったか」を **Discord のスレッドを定期的に読んで推測**するしかなかった。
実測（2026-09-28〜10-14、発注 98 件）では、終わってから発注側が気づくまで **中央値 18 分・最大 62 分**。
依頼主の催促 9 件のうち 3 件は、C-lord はもう終わっていたのに伝わっていなかった（最長 約 26 時間）。

C-lord 自身はターンの終わり方を知っている（`c_lord/notify_policy.py` の `completion` / `blocked` / `failure`）。
その事実を **サーバ側の設定でだけ**外へ出す口がこの機能。特定のツール向けの作りではなく、
JSONL を `tail -F` する CI も、署名付き POST を受ける別のエージェントも、同じ口を使える。

Claude Code の Stop フックでは足りない。**最後の返事が Discord に投稿される前に鳴る**ので、
受け手がスレッドを読みに行くと「まだ」に見える。ここでは**最後の返事を投稿し終えたあと**に出す。

## 使い方（設定）

どちらも**既定は無効**。両方とも未設定なら C-lord は何も書かず、何も送らず、動きは今までと同じ。

| 環境変数 | 意味 |
|---|---|
| `CLORD_EVENT_LOG=<path>` | 1行1イベントの JSONL を追記する（ディレクトリは無ければ作る） |
| `CLORD_EVENT_WEBHOOK_URL=<url>` | イベントの JSON を POST する送り先（`http://` / `https://` のみ） |
| `CLORD_EVENT_WEBHOOK_SECRET=<secret>` | POST の署名鍵。**無いと POST しない**（署名の無い通知は偽物と見分けがつかない） |
| `CLORD_EVENT_DEFER_MAX_SECONDS=<秒>` | 背景サブエージェントを待つ `completion` の保留の上限。既定 `3600` |

宛先は**サーバの環境変数でだけ**決まる。発注文・Discord のメッセージ・REST API からは指定できない
（誰でも書き込めるところから宛先を変えられると、内部アドレスへの送信（SSRF）やイベントの横取りになる）。

## イベント

```json
{
  "v": 1,
  "event_id": "5f0c…",
  "kind": "completion",
  "guild_id": "1285582352596209694",
  "channel_id": "1505747831447883806",
  "thread_id": "1564455512194424852",
  "last_message_id": "1566000000000000000",
  "started_by": {"type": "webhook", "id": "1500000000000000000", "name": "Captain Hook"},
  "turn_started_at": "2026-10-27T00:00:00Z",
  "ended_at": "2026-10-27T00:12:34Z"
}
```

ID はすべて**文字列**（Discord の snowflake は JavaScript の数値で丸まるため）。

| フィールド | 意味 |
|---|---|
| `v` | 形のバージョン。いまは `1`。フィールドが増えることはあっても、意味が変わることはない |
| `event_id` | イベント1件ごとの一意な ID。再送で同じ本文が2回届いたときの重複排除に使う |
| `kind` | `completion`（ターンが終わった）/ `blocked`（メニューで止まっている）/ `failure`（失敗した） |
| `last_message_id` | `completion` / `failure`: そのターンで C-lord が**最後に投稿した返事**（本文）のメッセージ。無ければ `null`。`blocked`: 選択メニューのメッセージ |
| `started_by.type` | `user` / `webhook` / `scheduler` / `api` / `self`（Claude が背景の知らせで自分から始めたターン — 後述）。`webhook` のときは webhook の id と表示名、`scheduler` のときはタスクの id と名前が入る |
| `reason` | `failure` のとき。`completion` では、後述の保留が時間切れになったとき（`background_agents_unresolved`）と、Claude が自分から始めたターンの終わり（`self_resumed_turn`）と、C-lord の再起動をまたいだターンの終わり（`restart_crossed`）のときだけ。短いコード。**エラー本文は入れない**（パスやペインの内容が混ざるため） |

`last_message_id` は**返事の本文**であって、その後ろに付く `📊 …context` の統計行や 🟡 のメンションではない。
受け手が「ここから読む」ための印として使う。

### `kind` と出るタイミング

- **`completion`**: 普通にターンが終わった。最後の返事と統計行を投稿し終えたあとに **1 件だけ**。
- **`blocked`**: AskUserQuestion や plan 承認のメニューが Discord に出て、誰かの答えを待っている。**メニューを出した時点**で 1 件（質問が複数でも 1 件）。答えが返ってターンが終わると、あとで `completion` が出る。
  許可プロンプトはペイン内で自動承認されて Discord に出ないので、`blocked` にはならない。
- **`failure`**: 起動できなかった（`workspace_setup`）・クラッシュした（`crashed`）・タイムアウトした（`timeout`）・
  プラン上限に当たった（`usage_limit`）・ログアウトしている（`login_required`）・モデルが使えない（`model_unavailable`）・
  Claude Code が API エラーで止まった（`api_error`、#908）・
  応答が無かった（`no_response`）・ユーザーが止めた（`stopped`）・その他のエラー（`error`）。
- **出ないとき**: 次のメッセージで置き換えられたターン（`preempted`）と、キャンセルされたターンは出さない。
  そのスレッドの次のターンの終わりが出す。

### C-lord の再起動をまたいだターン（#978）

ターンの途中で C-lord が再起動すると（デプロイ・auto-upgrade）、そのターンを走らせていた run は古いプロセスと一緒に
キャンセルされ、何も出さない。新しいプロセスがそのターンの終わりを transcript で見届けた時点で **1 件だけ**出す:

- ふつうに終わった → `completion`、`"reason": "restart_crossed"`
- API エラーで止まった → `failure`、`"reason": "api_error"` / プラン上限 → `failure`、`"reason": "usage_limit"`
  （終わり方は transcript の最後の返事で決める。`restarted` という失敗理由は作らない — 再起動はターンを失敗させていない）
- `started_by` はターンのきっかけのメッセージから決め直す（人のメッセージなら `user` とその人の id、webhook なら `webhook`）。
  `/clord` などで C-lord が投稿した種のメッセージから始まったスレッドでは、元の依頼者が分からないので `api` になる
- `turn_started_at` はきっかけのメッセージが投稿された時刻
- `last_message_id` は新しいプロセスがそのスレッドに投稿した最後の返事（取りこぼしの出し直しを含む）。
  返事が再起動前に全部出ていて、新しいプロセスが何も投稿していなければ `null`（古いプロセスの記録はもう無い）
- 再起動後に同じスレッドで新しいターンが始まっていたときは出さない（新しいターンの終わりが出す）
- **途中で背景の知らせ（Monitor・背景タスクの通知）が届いても、人のターンのまま**（#1011）。再起動後のミラーは
  transcript を読み返して「人のターンが開いている」ことを引き継ぐので、その知らせを Claude が自分から始めたターン
  （`self_resumed_turn`）とは数えない。イベントは `restart_crossed` の 1 件だけで、最終回答は依頼したメッセージへの引用返信で出る
- **背景のサブエージェントが残っていれば、下の節と同じく保留する**（#1012）。再起動前に起動したエージェントも、
  ミラーが transcript を読み返して数える。全部戻ってきたターンの終わりに `restart_crossed` で 1 件出す

### 背景のサブエージェントが残っているとき

Claude のターンが、`run_in_background` で起動した**サブエージェントを待たずに終わる**ことがある。
そのあと各エージェントが戻ってくるたび、Claude Code が**自動で次のターンを始める**。
最初の終わりで `completion` を出すと、答えができる何分も前に「終わった」と伝えてしまう。

そこで、ターンが終わった時点でサブエージェントが残っていると**判別できる**ときは、
`completion` を出さずに保留し、**残りが無い状態で終わったターン**の終わりに 1 件出す（`last_message_id` はその最後の返事）。

判別できないときは**待たずに出す**。限界は次のとおり:

- 数えているのは Claude Code の記録（jsonl）に出る**サブエージェント**だけ。背景で動かしたシェルコマンドは数えない（その後のターンの終わりは次の節のとおり別に出る）。
- 数は C-lord のメモリ上にある。C-lord を再起動したら、ミラーが transcript の末尾（最大 16 MiB）を読み返して数え直す（#1012）。それより前に起動したエージェントは数えない。
- ターン終わりの印（`turn_duration`）を記録に書かない古い Claude Code では、残りを読めないので待たずに出す。
- 戻ってこないエージェントのせいで永遠に出ないことを避けるため、保留は `CLORD_EVENT_DEFER_MAX_SECONDS`（既定 1 時間）で解け、
  `"reason": "background_agents_unresolved"` を付けて出す。
- 保留中に同じスレッドへ新しいメッセージが来て新しいターンが始まったら、保留していた `completion` は捨てる。
  その新しいターンの終わりが、スレッドの終わりとして出る（`started_by` は新しいターンのもの）。

### Claude が自分から始めたターン（背景のシェルコマンドなど、#961）

背景で動かした**シェルコマンド**（CI 待ち・長い pytest など）が終わると、Claude Code は `<task-notification>` を受けて
**誰のメッセージも無いまま次のターンを始める**。最初のターンが「裏で CI を待っています」で終わり、
本当の最終報告はこの後のターンに出る、という形がよくある。上のサブエージェントと違い、こちらは保留しない
（最初のターンの `completion` はそのまま出る）。代わりに、**知らせで始まったターンが終わるたびに `completion` を 1 件**出す:

```json
{"kind": "completion", "reason": "self_resumed_turn",
 "started_by": {"type": "self", "id": null, "name": null}, "...": "..."}
```

- `last_message_id` はそのターンの返事。ツールを使わない短い返事（「まだ実行中です」など）は通知なしで投稿されるので、`null` のことがある。
- `guild_id` / `channel_id` はそのスレッドの直前のターンから引き継ぐ。C-lord の再起動後で直前のターンを知らないときは `null`。
- `turn_started_at` は知らせを読んだ時刻。
- **出さないとき**:
  - 知らせと同じターンに人のメッセージが入ったとき（そのターンは人のもので、終わりは普通の `completion` が出す — 二重にならない）。
  - サブエージェントの保留を解くターンのとき（その終わりは保留していた `completion` として出る — 重ならない）。
  - C-lord の再起動前に始まったターン（始まりを知らないので出さない）。
- 知らせで始まったターンがさらにサブエージェントを残して終わったら、上と同じく保留し、解けたときに `self_resumed_turn` で出す。

## 受け手の例

### JSONL を `tail -F` する

```bash
tail -n0 -F /home/yousan/c-lord/data/turn-events.jsonl | while read -r line; do
  kind=$(jq -r .kind <<<"$line"); thread=$(jq -r .thread_id <<<"$line")
  [ "$kind" = completion ] && echo "thread $thread finished; read from $(jq -r .last_message_id <<<"$line")"
done
```

発注した `thread_id` の行だけを見る。同じファイルに全スレッドのイベントが流れる。

### 署名を検証して POST を受ける

署名は**本文のバイト列そのもの**の HMAC-SHA256（16進）を `X-Clord-Signature: sha256=<hex>` に入れる。
JSON にパースし直してから検証しない（キーの順や空白が変わると合わなくなる）。

```python
import hashlib, hmac, json

def verify(secret: bytes, body: bytes, header: str) -> dict | None:
    expected = "sha256=" + hmac.new(secret, body, hashlib.sha256).hexdigest()
    if not hmac.compare_digest(expected, header):
        return None  # 偽物 — 捨てる
    return json.loads(body)
```

送信は 2xx が返れば成功。5xx・408・429・接続失敗は 1 秒・5 秒・30 秒あけて最大 3 回再送する
（同じ `event_id` で届く）。それ以外の 4xx は再送しない。リダイレクトは辿らない。
どの失敗もログに残るだけで、**ターンの後処理と Discord への投稿は止まらない**。

## 変えていないこと

- 両方の環境変数が未設定のとき、C-lord の動きは変わらない。
- Discord に出るメッセージ・リアクション・メンションは変わらない（`notify_policy` の `CLORD_OWNER_FALLBACK` とは独立）。
