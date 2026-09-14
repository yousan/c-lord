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

### View に渡し忘れたとき (#739)

View は `_authorizer` を受け取るのが原則で、**全 View / 全構築経路が渡していることは
`tests/test_button_authorization.py` が固定している**。それでも渡っていない View が
作られたときは、**そのプロセスに公開されている `Authorizer`**（`setup_bridge` /
`ClaudeChatCog` が `set_process_authorizer()` で publish したもの）を見る。所有者の解決
(`_fallback_owner_ids`) と同じ理屈で、1 プロセス＝1 bot＝1 規則だから、View がどこで作られても
同じ答えに辿り着く。

これは #739 の修正。それまでは引数なしの `Authorizer()` を新しく作っていた。**新品の
`Authorizer` は allowlist を知らない**ので規則 4（未設定）に落ち、そこで見る
`_fallback_owner_ids` は「allowlist が設定済みなら解決しない」（上の「所有者はいつ分かるか」）
ため空のまま — 結果 **allowlist を正しく設定している環境で、その allowlist に載っている
本人を含む全員が拒否された**。2026-09-14 に #359 メニュー監視が出したボタンで実際に起きた。

**直し方として「渡っていなければ全員許可」に戻してはいけない** — それは #713 が塞いだ
fail-open そのもの。フォールバックは「広い方」ではなく「**本物の allowlist**」を指す。

ターン中に出るボタン（permission / plan / elicitation / ask）へは `RunConfig.authorizer`
が運ぶ。`RunConfig` を作るのに `authorizer=` を渡し忘れた経路が無いことは
`tests/test_architecture.py` が構造的に固定している（#739 時点で `/skill` ×2・scheduler・
webhook の 4 箇所が渡していなかった）。

### 拒否したときのログ (#739)

拒否は理由まで出す。「明示 allowlist に載っていない」のか「allowlist が未設定で所有者も
未解決」なのかが 1 行で分かる（#739 以前は両者が同じ 1 行で、切り分けに時間が溶けた）。

```
Rejected unauthorized button interaction from user 499163459418587176 on AskView:
  not on the configured allowlist (user_ids=[123] role=None) [wired into the view]
```

末尾の `[...]` は**どの Authorizer が答えたか** — `wired into the view` /
`the process authorizer — this view was not handed one (#739)` /
`no authorizer reached this view and none is published (#739)`。

## スコープ外

- `/clear`・`!clear` に権限チェックが無い件 → **#405**
- webhook / 信頼済み bot が human allowlist を迂回する経路 → `c_lord/command_gate.py`
  (`is_message_authorized`、#507 / #508)。webhook URL の所持そのものが認可であり、
  この規則とは別の話
