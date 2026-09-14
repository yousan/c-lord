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

### 拒否したときは理由を出す

「allowlist に無い」のと「authorizer が無く判定できない」は**利用者にとっては同じ無反応でも、
運用者にとっては別物**（後者は c-lord のバグ）。拒否ログは両者を書き分ける — #739 の切り分けに
時間がかかった原因がこれだった。

## スコープ外

- `/clear`・`!clear` に権限チェックが無い件 → **#405**
- webhook / 信頼済み bot が human allowlist を迂回する経路 → `c_lord/command_gate.py`
  (`is_message_authorized`、#507 / #508)。webhook URL の所持そのものが認可であり、
  この規則とは別の話
