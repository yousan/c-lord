# 誰が c-lord を使えるか — 既定は「アプリの所有者だけ」 (#713)

> あるべき動きの記述。実装は `c_lord/discord_ui/authorization.py`、
> テストは `tests/test_default_authorization.py`。

## Why

**c-lord に話しかけられる＝そのホストでシェルを実行できる。** だから「誰が使えるか」は
c-lord 全体のセキュリティ境界そのものになる。

以前は `DISCORD_OWNER_ID` も `CLORD_ALLOWED_ROLE` も未設定だと **allowlist 無し＝全員許可**
だった。README どおりに `.env` を書いて起動した人は、**自分のホストへの shell アクセスを
サーバ全員に開いた状態**で、そうと告げられないまま運用することになる。fail-open が
意図的に選ばれ、docstring にも「zero-config」として明記されていた。

Zero-Config 原則（利用者はパッケージを入れるだけで使える）と fail-closed は両立できる。
**Discord 自身がアプリの所有者を知っている**ので、設定を要求せずに所有者だけへ絞れる。

## 規則（1つだけ）

すべてのゲート — メッセージ / スラッシュコマンド、ボタン (View)、`/skill`、`/clord-init` —
は同じ `Authorizer` を読む。上から順に:

1. `allowed_user_ids` に一致 → **許可**
2. それ以外で `allowed_role_name` が設定済み → そのロールを持つ **Member** なら許可。
   DM（`discord.User`）とユーザー ID だけの呼び出しはロールを持てないので **不許可**
3. それ以外で `allowed_user_ids` が設定済み（一致しなかった） → **不許可**
4. **どちらも未設定** → **Discord アプリの所有者（チーム所有ならそのメンバー全員）だけ許可**

4 が #713 で変わったところ。以前はここが「allowlist が無い＝全員許可」だった。

### 明示的に全員へ開く

`CLORD_ALLOW_ANYONE=1` を設定すると 4 は「全員許可」に戻る。**fail-open を選ぶのは自由だが、
無自覚にはさせない** — 選んだ場合は起動時に WARNING が 1 行出る。

### 所有者はいつ分かるか

Discord にログインするまで分からないので、`on_ready` で 1 回だけ
`bot.application_info()` に聞き、プロセス全体で共有する（1 プロセス＝1 bot＝1 所有者。
#212 / #325 のロックがそれを保証している）。**解決できるまでは誰も通さない** — 所有者が
不明な状態でアクセスを広げてはいけないため。API が失敗した場合も同じく fail-closed で、
理由を WARNING に出す。

## 起動ログ（黙って絞らない・黙って開かない — #585）

| 状態 | ログ |
|---|---|
| 未設定 → 所有者にフォールバック | INFO: 所有者の名前と ID、そして `DISCORD_OWNER_ID` / `CLORD_ALLOWED_ROLE` / `CLORD_ALLOW_ANYONE=1` で変えられること |
| `CLORD_ALLOW_ANYONE=1` | WARNING: 全員が操作でき、それはこのホストでのシェル実行だと明示 |
| allowlist 設定済み | INFO: 設定されている user_ids / role |
| 所有者を取得できなかった | WARNING: 誰も許可されていないこと、`DISCORD_OWNER_ID` を設定して再起動すること |

## 利用者から見た before / after

- **before**: `.env` に何も書かずに起動すると、そのサーバの誰でも c-lord 経由でホストの
  シェルを叩けた
- **after**: 既定でアプリの所有者だけが使える。開放したい場合は明示的に設定する

`DISCORD_OWNER_ID` や `CLORD_ALLOWED_ROLE` を**すでに設定している環境の動作は一切変わらない**。

## 4つのゲートが1つの規則を共有する

`setup_bridge` が `Authorizer` を **1 個だけ**作り、`ClaudeChatCog` / `ChannelRepoCog` /
`SkillCommandCog` に同じインスタンスを渡す。View は `_authorizer` を受け取る。

これは #466 で始めた DRY の完了でもある。#466 はメッセージとボタンを 1 つの述語に寄せたが、
`SkillCommandCog` と `ChannelRepoCog` は自前のコピーを持ったままだった — **だから 2 箇所だけ
fail-open が残り、`/skill`（任意のスキル実行）と `/clord-init`（リポジトリ紐づけ）は誰でも
叩ける状態が続いた**。規則のコピーは、いつか片方だけ直る。

View に `_authorizer` を渡し忘れた場合は、**プロセスが実際に使っている `Authorizer`**
（`set_default_authorizer()` で公開されるもの）を参照する。どちらも無ければ拒否する。

> **#739 の教訓**: ここは当初「渡し忘れたら空の `Authorizer()` を作る」だった。空の
> `Authorizer()` は allowlist を持たないので上の 4 に落ちるが、**allowlist が設定されている
> 環境では所有者フォールバックが意図的に未解決のまま**（3 の手前で return する）なので、
> 結果として**全員拒否**になった。`DISCORD_OWNER_ID` を設定している本番で、**allowlist に
> 載っている所有者自身がボタンを押せなくなった**。deny 既定は正しいが、**間違った allowlist
> から計算した deny は正しくない**。
>
> 配線漏れそのものは #713 以前から存在していた（`_authorizer is None` が「全員許可」だった
> ため見えていなかっただけ）。実際に漏れていたのは `bridge_pane_ask` を呼ぶ 2 箇所
> （`cogs/transcript_mirror.py` — jsonl 経路なので**本番の主経路**、`thread_state_sync.py` —
> watchdog 経路）。

全 View が authorizer を渡されていることは `tests/test_button_authorization.py` が、
`AskView` の全構築経路が `authorizer=` を持つことは `tests/test_view_authorizer_wiring.py`
が（AST で）固定している。

### `/clear`・`!clear` も同じ規則を通る (#405)

`/clear` は走っているターンを止めて Claude Code に `/clear` を打ち込む（#803。以前は runner と tmux window を kill してセッション行をリセットしていた）
— **会話が消える、チャットの中で一番破壊的なコマンド**。それなのに隣の `/clord-attach` は
ゲートされていて、`/clear` だけ権限チェックが無かった（スレッドに書き込める人なら誰でも
他人の会話を消せた）。

いまは `ClaudeChatCog._clear_impl` の先頭で、何かを壊す**前に**判定する。規則は新しく
作らず、上の `Authorizer` をそのまま使う:

- `/clear`（slash）→ human allowlist（`Authorizer.is_allowed`）
- `!clear`（text）→ `is_message_authorized`。**webhook は通す**ので、`DISCORD_OWNER_ID` を
  設定したままでも E2E（`tests/e2e/test_text_command_twins.py`）は壊れない

拒否したら `You are not authorized to use this command.` を返し（slash は本人にだけ見える）、
`/clear rejected` を INFO で残す。テストは `tests/test_clear_authorization.py`。

### すべてのコマンドが同じ規則を通る (#781)

#405 のあと同じ観点で全 Cog を走査すると、**`Authorizer` を一度も通らないコマンドが 37 個**
残っていた。`/workspace-delete`（作業ディレクトリ削除）・`/model set`（全スレッド共通の
モデル変更）・`/stop`・`/compact`（要約は戻せない）・`/upgrade`（パッケージ更新＋再起動）など。
#713 で既定を「所有者だけ」に絞っても、**ゲートを呼ばないコマンドには届かない**（#405 と同じ構図）。

いまは次の**公開コマンド以外すべて**が、本体に入る前に同じ規則で判定する:

| 公開（誰でも叩ける） | 理由 |
|---|---|
| `/version` `!version` | 走っているビルドの版を表示するだけ |
| `/model show` `!model-show` | 現在のモデル名を表示するだけ |
| `/thread-archive show` `!thread-archive-show` | 自動アーカイブ期間を表示するだけ |

表示だけでも `/tmux-screenshot`（ペインの中身）・`/clord-status`・`/tmux-list`（ワークスペースの
一覧とパス）は**ゲート対象**。見せてよいものは「設定値として公開して困らないもの」に限る。

- slash → human allowlist（`Authorizer.is_allowed`）
- text → `is_message_authorized`（webhook・信頼 bot は通る — E2E の text twin はそのまま動く）
- `SessionManageCog` は `setup_bridge` から同じ `Authorizer` を受け取る。受け取っていなければ
  bot / プロセスに公開された `Authorizer` を使い、それも無ければ**拒否**（空の `Authorizer()` は
  使わない — #739）。判定は `c_lord.command_gate.authorize_command` に 1 つだけある

**取りこぼしは構造テストで止める**: `tests/test_command_authorization_coverage.py` が
`c_lord/cogs/*.py` の `*.command(...)` を AST で全部拾い、本体（か本体が呼ぶ `self._*`）が
ゲートを通るか、上の公開リストに載っているかを確かめる。ゲート無しのコマンドを足すと CI が落ちる。
公開リストに足すのは設計判断なので、PR に理由を書くこと。

### 拒否したときは理由を出す

「allowlist に無い」のと「authorizer が無く判定できない」は**利用者にとっては同じ無反応でも、
運用者にとっては別物**（後者は c-lord のバグ）。拒否ログは両者を書き分ける — #739 の切り分けに
時間がかかった原因がこれだった。

## 許可されていない人の発言 — 本人には何も見せず、オーナーへ DM (#346)

> 実装は `c_lord/denied_notice.py`、テストは `tests/test_denied_notice.py`。

**以前は黙って捨てていた。** `on_message` の先頭で許可されていない人の発言を `return` するだけで、
返信もリアクションも**ログも**無かった。2026-09-26、guild のスレッドでゲストの依頼が 2 回とも
黙って捨てられ、本人もオーナーも気づけなかった（招待し忘れ・許可の書き漏れかもしれないし、
不審なアクセスかもしれない — どちらにせよオーナーは知るべき）。

いまの動き:

| 発言したのは | スレッド／チャンネル | オーナーへの DM | ログ |
|---|---|---|---|
| 許可されていない**人** | **何も出ない**（返信・リアクション・スレッド作成なし） | 届く（同じ人は 24 時間に 1 回まで） | 毎回 INFO |
| 許可されていない **bot** | 何も出ない | 送らない | INFO（スレッドごとに 10 分に 1 回、抑えた件数つき — #678） |
| webhook | — | — | — （webhook URL の所持が認可なので、そもそも弾かれない — #507） |

- **本人には何も見せない**: 「bot が動いていて発言を読んでいる」ことすら伝えない（yousan 判断 2026-10-02）
- **DM の宛先**: Discord アプリの所有者（チーム所有ならメンバー全員）。上の規則 4 と同じ判定
  （`read_application_owners`）で、**`DISCORD_OWNER_ID` を設定していても宛先はアプリの所有者**。設定は要らない
- **DM の中身**: 発言者（メンション・表示名・ID）、場所（サーバ › チャンネル › スレッド）、先頭 3 行
  （300 字まで、引用として）、メッセージへのリンク、許可の仕方（`CLORD_ALLOWED_ROLE` のロールを付ける）。
  **誰が許可されているか（allowlist の中身・ロール名）は書かない**。メンションは飛ばさず、リンクのプレビューも出さない
- **間引き**: 同じ人については 24 時間に 1 回。記録は**プロセスのメモリだけ**（DB には持たない。再起動で数え直し）
- **対象になる場所**: この bot が受け持つスレッド（`owns_channel` — #596）の通常メッセージだけ。
  チャンネルへの直接投稿は**誰の発言でも**ターンを起こさないので「弾いた」ことにならず、DM もログも出ない。
  受け持っていない場所の雑談も同じ（同じ guild に c-lord が何台いても DM は 1 通）。`!` で始まる
  テキストコマンドは各コマンドのゲートに任せる（#781）
- **オフにする**: `CLORD_NOTIFY_DENIED=0`。DM だけが止まり、ログは残る
- **DM を受け付けないオーナー**: 送れなければ WARNING を 1 行残して続行する（bot は止まらない）

ログの例（`grep thread=<id>` で拾える）:

```
[thread=1514546023282769920 channel=1503245597841559623] ignored message 1555… from user 99 (babeln): not authorized — owner DM sent to 4242 (#346)
```

## スコープ外

- webhook / 信頼済み bot が human allowlist を迂回する経路 → `c_lord/command_gate.py`
  (`is_message_authorized`、#507 / #508)。webhook URL の所持そのものが認可であり、
  この規則とは別の話
