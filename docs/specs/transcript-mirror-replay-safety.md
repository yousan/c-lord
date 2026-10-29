# Transcript mirror — どれを読み、何を二度流さないか (#627 / #433)

このドキュメントは、JSONL transcript ミラー
(`c_lord/transcript/`、`c_lord/cogs/transcript_mirror.py`) の **「あるべき動き」**
のうち「**どの transcript を読むか**」「**1 つの transcript を何本のミラーが読むか**」
「過去ログを二度 Discord に流さない」保証を定める。

応答性(ミラーが Discord のメッセージ処理を邪魔しないこと)は
[transcript-mirror-liveness.md](./transcript-mirror-liveness.md)、
何を 👤 として出してよいかは [mirrored-events.md](./mirrored-events.md)。

## どの transcript を読むか (#627)

**1 つの作業ディレクトリには、たくさんのセッションがある。** `claude -p` の呼び出しも
サブエージェントも、同じ `~/.claude/projects/<slug>/` に**自分の
`<session-id>.jsonl` を書く**。#627 の実例のディレクトリには **182 個**あった。

### あるべき動き (利用者から見た期待)

- スレッドに流れるのは、**そのスレッドの Claude が言ったこと**だけ。
  同じ作業コピーで別のプロセスが動いていても、その会話は流れてこない。
- **👤 は自分が言ったことだけ。** 言っていないことを言ったことにされない。

### 実装上の不変条件

**読んでよいのは「c-lord 自身が動かしたセッションの transcript」だけ**
(`c_lord/transcript/resolver.py::ThreadSessionResolver`)。

| ルール | 中身 |
|---|---|
| 0. 名前 (#773) | **c-lord は自分が起動するセッションに自分で名前を付ける**。`start_claude` が `--session-id <uuid>` を渡し、その uuid を transcript の隣 (`<project_dir>/.clord-session`) に記録する (`c_lord/transcript/claim.py`)。`<uuid>.jsonl` が存在すれば**それがこのスレッドの transcript**。中身を読まないので、CLI の入力整形に左右されない |
| 0'. `/clear` の後継 (#803) | `/clear` は Claude Code に**新しい `<uuid>.jsonl`** を作らせる（先頭が `<command-name>/clear</command-name>`）。名前が指すファイルに加えて、**その後継**も自分のものとして扱い、記録 (`.clord-session`) をそちらへ書き換える (`claim.clear_successor`)。後継とみなすのは「`/clear` で始まり、その `/clear` が**記録中のセッションの開始より後**で、かつ**記録中の transcript の最後の書き込み（±5 秒）以降**」のファイルだけ — 生きているセッションは書き続けるので、他人の／過去の `/clear` はこの条件を満たせない。c-lord の `/clear` でも、**人がペインに直接打った `/clear`** でも同じく追従する（2026-09-28 本番: ペインで `/clear` → ミラーが旧 jsonl に張り付き最終回答が届かなかった）。記録を書き換えるので、後で起こし直しても `--resume` は**クリア後の会話**を開く |
| 1. 資格（後方互換） | 名前が無いセッション（#773 以前に起動して走り続けているもの）だけがここに来る。c-lord がペインに送るプロンプトには以前 ZWSP (U+200B) が付いていた (`tmux.send_input` / `start_claude` #530) ので、それが `"content":"` の直後に生の UTF-8 で入っている transcript は対象。**付いていても偽陽性にはならない**ので残してあるが、**CLI 2.1.278 以降は transcript に残らず、#861 以降は c-lord も付けない**ので、名前の無い古い transcript を拾うためだけの規則 |
| 2. 前進のみ | 名前で決まらなかったとき、いったん決めたら乗り換えてよいのは**それを決めた後に現れたファイル**だけ (= `/clear` の後継)。既にあったファイルは mtime がどれだけ新しく見えても乗っ取れない。`touch` で読了済み transcript に戻って再投稿する事故を塞ぐ |
| 3. 該当なし | どれにも当たらなければ**何も出さない**。他人の会話を流すくらいなら黙る。**ただし黙っていることは隠さない** — 次節 |

**resume も名前で戻る (#773)**: ペインが落ちたスレッドを起こし直すとき、`--continue`
（= その作業ディレクトリで**最後に書いた**会話を開く）ではなく **`--resume <記録した uuid>`**
を使う。`--continue` が開く「最後に書いた会話」は `claude -p` のサブ呼び出しかもしれず、
それは #627 と同じ取り違えを一段下で再現する。`--resume` は同じ jsonl に追記されるので、
ミラーは追従し直す必要すらない。

**名前が無いセッションは resume では救えない（意図した限界）**: 記録が無い古いスレッドは
従来どおり `--continue` で開き直す。ここで `--session-id` を足すことは**できない** —
CLI は `--continue` との併用を `--fork-session` 無しでは拒否し、fork は**会話履歴を丸ごと
新しい transcript に複製する**ので、ミラーがそれを新規ファイルとして 0 バイト目から読み、
**過去の会話を全部 Discord に流し直す**（#433 の重複防止は、元ファイルを読んでいた場合しか
効かない — つまり壊れている場合にこそ効かない）。だから名前の無いセッションの復旧は
**`/clear`（新しいセッション = 新しい名前）**であり、上の告知もそう案内する。

### 見つからないことを黙らない (#773 / #585)

ルール 3 の沈黙は**投稿の方針としては正しいが、利用者への説明としては間違っている**。
配信経路は jsonl ミラー一本 (#712) なので、**ミラーが黙る = そのスレッドには何も届かない**。

- **ターンが走っているスレッドにだけ**、「transcript が見つからないので転送できていない」と
  **1 ターンに 1 回**投稿する (`TranscriptMirror._on_unresolved`)。
  **復旧手段は場合分けして書く** (`_unresolved_notice`)。間違った手順を案内すると時間だけ溶ける:
  - **名前があるのに見つからない** → `/claude-restart`（会話の文脈は残る）
  - **名前が無い**（#773 以前に起動したセッション）→ **安い順に 2 段**で出す。
    ① `/claude-restart`（会話の文脈は残る。マーカーを書く CLI なら旧ルールで復活し、
    そもそも復元できる会話が無ければ c-lord は名前付きの新規起動にフォールバックするので、
    これで直ることがある）→ ② それで直らなければ `/clear`（**新しいセッション＝新しい名前**なので
    確実。作業ディレクトリはそのまま、会話の文脈だけ失う）。
    ①が確実でないのは、**マーカーを消す CLI で resume が成功した場合**だけ — 名前の無い
    transcript をそのまま開き直し、`--continue` には `--session-id` を足せない
- **アイドルのスレッドには出さない。** bot 起動時 (`on_ready`) に復元されるミラーは
  `expect_turn=False` で立つ。Claude を一度も動かしていないワークスペースに transcript が
  無いのは正常で、このホストにはそれが数百ある
- ログは**ターン中なら `ERROR`**（本当に返事が届いていない）。**アイドルなら `WARNING` を
  スレッドごとに 1 時間に 1 行**だけ出し、抑えた回数を `(+N suppressed in the last 3600s)` で添える（#810）。
  以前はアイドルでも 2 分おきに `ERROR` を出していたため、本番ログの `ERROR` の 99.99% がこの 1 行になり、
  本当のエラーが埋もれていた。アイドルでも無言にはしない（#678 — `grep thread=<id>` で状態が分かるように）。
  `候補 jsonl の本数` と `記録した session id` を含めるので、
  「1本も書かれていない」と「書かれているが自分のものと確認できない」を切り分けられる
- **名前を付けた直後の「まだ書かれていない」は警告しない**（#926）。CLI は確認ダイアログ
  （フォルダの信頼）を通るまで `<session-id>.jsonl` を作らないので、新しいワークスペースの最初の
  ターンでは、名前を付けてから数秒は「転送先が無い」状態が必ず起きる。名前（`.clord-session`）を
  書いてから **120 秒**までは `INFO` を 1 行（`claimed transcript … not written yet`）出して待ち、
  それを過ぎてもファイルが無いときだけ従来どおり resolver の `WARNING`
  （`no c-lord-driven transcript …`）を出す。以前は新しいワークスペースの**毎回**この WARNING が出ており
  （1 週間で 18 本中 18 本）、本当に転送できていないときの WARNING が埋もれていた。
  ミラーは `start_claude` より先に始まるので、**名前がまだ置かれていない**瞬間もある（`write_claim` が
  フォルダを作ってから名前を置くまでの間）。jsonl が 1 本も無く、フォルダができてから 120 秒以内なら、
  名前が無くても同じように黙って待つ（#926 の再オープン — 2026-10-29 の本番で、名前が置かれたのと
  同じ秒にこの WARNING が出ていた）。他人の jsonl があるフォルダや、120 秒を過ぎても空のフォルダは、
  名前が無ければ従来どおりすぐ WARNING を出す

### 名前が指すファイルが現れず、c-lord の別の会話が書かれ続けているとき (#994)

名前（`.clord-session`）は **CLI を起動する前に**書く。だから、名前を付けた起動が実際には走らず、
**c-lord が動かしている別の会話**が書かれ続ける、ということが起きうる。2026-10-29 の本番
（スレ 1564444583767703562）では、スリープからの復元で `--resume a07fc39e…` を打ち、3 秒後の判定の時点で
ペインがまだ zsh だったため「失敗」と読み、新しい名前 `93b54394…` を書いて新規起動を打った。
実際には resume の claude がその 1 秒後に起動して `a07fc39e….jsonl` に書き続け、`93b54394….jsonl` は
作られなかった。次の bot 再起動から、そのスレッドには何も届かなくなった。

**起きないようにする（runner 側）:**

- `--continue` / `--resume` の成否は、**claude がペインに一度でも現れてから**決める。まだ現れていない
  ペインは「失敗」ではなく「未定」。失敗とみなすのは、現れた claude が居なくなったときと、CLI が
  `No conversation found` を出したときだけ。上限（15 秒）まで一度も現れなければ失敗とみなす
- 新規起動に切り替える**直前にもう一度** claude が動いているかを見る。動いていれば resume が生きているので、
  新規起動を打たない（打てば、コマンド行がその claude への発言になるか、起動中に飲まれて名前だけが動く）

**起きてしまったら直す（ミラー側、`ThreadSessionResolver._adopt_orphaned_claim`）:**

名前を書いてから **120 秒**（#926 の猶予）たっても名前のファイルが無いとき、次の 2 つを満たす transcript が
**ちょうど 1 本**あれば、それを読み、**名前をそちらへ書き換える**（WARNING を 1 行。`(#994)`）:

1. 名前を書いた**あとに**書き込まれている（書かれ続けている）
2. **c-lord のものだと証明できる** — 次のどちらか
   - その id を c-lord がこのフォルダで**以前に名前として書いた**（`.clord-session-history`。名前を書くたびに追記、最大 32 件）
   - このスレッドの作業ディレクトリで**動いている対話の claude** が、その id で起動されている
     （`/proc/<pid>/cmdline` の `--resume <id>` / `--session-id <id>` と `/proc/<pid>/cwd`。`claude -p` は除く）

「いま書かれているもの」というだけでは追わない — 同じ作業コピーの `claude -p` もそうであり、それを流すのが
#627 の事故だから。証明できる候補が 2 本以上なら選ばずに黙る（WARNING）。調べるのは 30 秒に 1 回。

**追従したときに何を流すか**: 二度流さない規則（下の「二度流さない」）はそのまま効く。ミラーが始まった時点で
既にあったファイルは**そのときの大きさから先**を読むので、bot 再起動の直後に追従した場合（本番のこのスレが
直ったコードで再起動したとき）は、**それまでの途中経過は流さず、それ以降の発言から届く**。ただし
**最後に終わったターンの最終回答 1 通だけ**は、下の #215 の救出が届ける（staging 実測 2026-10-29: 名前を
存在しない id に差し替えて黙らせたスレで、修正版に再起動した瞬間に追従し、取り残されていた最終回答 1 通が
届いた）。ミラーが動いている最中に追従した場合は、ミラーが始まってから追従までに書かれた分が**まとめて届く**
（重複はしない）。

テスト: `tests/transcript/test_claim_orphaned.py`、`tests/test_continue_late_start.py`。

**なぜ必要か**: #773 では CLI 2.1.278 が ZWSP を削るようになり、**全スレッドが同時に**
ルール 3 に落ちた。2026-09-20 から 09-23 まで誰も気づかず、9/23 には一斉発注した
作業スレッド 17 本が丸一日、開始通知のまま止まって見えた（中身は進んでいて PR も 15 本
上がっていた）。痕跡はミラー 1 本につき WARNING 1 行だけだった。

**同じ判定を #215 の救出スキャンにも使う** (`recovery.py`)。mtime 最新を読むと、
bot 再起動中に終わった `claude -p` の最終回答が「落ちた回答」として
スレッドに投稿されてしまう。

**実測 (2026-08-31, 本番ホスト)**: #627 該当ディレクトリの 182 個中、印が付いていたのは
**1 個**(そのスレッド自身のもの)。生きているセッション行 313 件全部で、
「印つきの最新」と旧ルールの選択が食い違うケースは **0 件** — つまりこの規則は
正常に動いていたスレッドの挙動を変えずに、事故だけを塞ぐ。

**実測 (2026-09-23, CLI 2.1.280, 隔離 tmux)**: `--session-id <uuid>` を付けて対話モードで
起動すると、transcript は `<渡した uuid>.jsonl` として作られ、**中の ZWSP は 0 バイト**
（TUI 自身が `Removed 1 invisible character from the launch prompt before sending it` と
表示する）。つまり**ルール 1 だけでは 0 件、ルール 0 なら確実に当たる**。

テスト: `tests/transcript/test_session_claim.py`、`tests/transcript/test_session_pinning.py`、
`tests/transcript/test_unresolved_notice.py`、`tests/test_start_claude_session_id.py`、
`tests/transcript/test_recovery.py::test_does_not_recover_a_sub_invocations_answer`。

## 1 つの transcript を読むミラーは 1 本だけ (#719)

前節が「**1 本のミラーがどのファイルを読むか**」を決めるのに対し、ここは
「**1 つのファイルを何本のミラーが読んでよいか**」を決める。答えは **1 本**。

### なぜ 2 本になりうるのか

普通のスレッドは `c-lord-sessions/<channel>/<thread>/` に自分だけの作業コピーを持つので、
「スレッド 1 本 : ミラー 1 本」と「transcript 1 つ : ミラー 1 本」は同じことを言っている。
**`working_dir` を*名指し*した瞬間にこの 2 つはずれる**:

- **スケジュール実行**は `working_dir` を固定したまま、**実行のたびに新しいスレッド**を立てる
  ([scheduled-tasks.md](./scheduled-tasks.md))。N 週目には同じ `working_dir` を持つスレッドが N 本。
- **`/clord-thread-init`** で 2 つのスレッドを同じパスに向けても同じことが起きる。

`working_dir` が同じ ⇒ `~/.claude/projects/<slug>` が同じ ⇒ **同じ jsonl**。
ミラーが 2 本張られると、Claude が 1 回書いた文章が 2 つのスレッドに投稿される。
重複排除 (`mirror_replied_uuid`) は**スレッドごと**に持っているので効かない。

### あるべき動き (利用者から見た期待)

- **とっくに終わったスレッドが、ある日いきなり別のスレッドの作業で埋まらない。**
  自分は何も送っていないのに知らない作業の途中経過が流れ続ける、という状態にならない。
- **作業は、それを始めたスレッドにだけ届く。** 週次タスクが 10 週走っても、
  投稿先は常にその回のスレッド 1 本。
- **bot を再起動しても同じ。** 再起動で古い組み合わせが復活しない。

### 実装上の不変条件

**1 つの project dir を映してよいのは 1 スレッドだけ**
(`cogs/transcript_mirror.py` の `_owners`)。誰がその 1 本かは、経路ごとに決まる:

| 経路 | 誰が持つか | なぜ |
|---|---|---|
| `start_for()` — ターンが**今から**始まるスレッド | **新しく名乗り出た方**。前の持ち主のミラーは止める (WARNING 1 行) | これから transcript に書くのはそのセッション。ここで新参を拒むと、生きているスレッドが配信経路を 1 つも持たなくなる (#712 で jsonl ミラーが唯一の経路になった) |
| `on_ready()` — 再起動後の復元 | **`last_used_at` が新しい行**。同じ project dir を指す後続の行は飛ばす (WARNING 1 行) | `list_all` は `last_used_at DESC`。最後にその作業コピーを使ったスレッドが、その transcript の持ち主 |
| `stop_for()` / `cog_unload()` | 誰も持たない | 止めたミラーが project dir を握ったままだと、次の名乗りが不要な takeover になる |

**#215 の救出スキャンも同じ規則に従う** — `on_ready` で飛ばした行は救出も走らせない。
落ちた最終回答は transcript の持ち主のものなので、古い方に再投稿すればそれこそが症状になる。

### なぜ「覚えておく」方式をやめたか

#621 では `SchedulerCog` 側に `task_id → 前回のスレッド` を覚えさせ、次の実行で
そのミラーを止めていた。コメントはこう言っていた: 「In-memory only: a restart kills
the mirrors too」。**これが事実と違った。** `on_ready` は `closed_at` が空の行すべてに
ミラーを張り直すので、**再起動はガードの記憶だけを消して、止めたい相手を復活させる**。
2026-09-04 のスレッドに 2026-09-11 の週次実行が丸ごと流れ込んだのはこれ (#719)。

だから規則は「誰かが覚えている」ところではなく、**ミラーを配る当人** (`TranscriptMirrorCog`)
が持つ。記憶は要らない — 張ろうとした瞬間に project dir の持ち主を見るだけで済む。

テスト: `tests/test_transcript_mirror_cog.py`
(`test_on_ready_starts_one_mirror_per_project_dir` /
`test_on_ready_does_not_recover_into_the_stale_duplicate` /
`test_a_live_claim_takes_the_mirror_over`)、
`tests/test_scheduler.py::TestScheduledRunMirrorOwnership`。

## 二度流さない (#433)

### あるべき動き (利用者から見た期待)

- bot / ホストが再起動しても、**古いスレッドに過去の会話履歴が再投稿されない**。
- 再起動後に古いスレッドへ話しかけると、**届くのはその新しいターンの応答だけ**。
  6/1〜6/17 にすでに送られた応答が、もう一度スレッドに流れてくることはない。
- これは `/clear`（新しいセッション jsonl への切り替え）や、クラッシュ後の
  `--resume`（Claude Code がアクティブ jsonl を**履歴を保ったまま書き換える**）を
  またいでも成り立つ。

### なぜこの保証が要るか (#433 の実害)

ミラーは Claude Code のセッション jsonl を tail し、`assistant` の最終応答などを
Discord へ転送する。`c_lord/transcript/tail.py` は通常 **EOF から** 追従するので
起動時に過去分は流さない。しかし、

- truncation（`size < offset`）
- 同一サイズの in-place rewrite（`size == offset && mtime 更新` **かつ読了位置の直前 4 KB が変わった**）
- 新しい active jsonl への切り替え（`/clear`）

を検知すると **読み取り offset を 0 にリセットして先頭から読み直す**。

クラッシュで tmux 内の Claude が死んだ状態で古いスレッドに話しかけると、
`--resume` が走り Claude Code がアクティブ jsonl を**履歴ごと書き換える**。これが
上記リセットを誘発し、ミラーが**全 `assistant` 履歴を Discord に再投稿**してしまう
（2026-06-18: 約44秒で1スレッドの98発話が再送される実害）。

### 実装上の不変条件 (regression を防ぐ)

`tail_events` は **1 回の追従セッション中、同じイベントを二度 yield しない**。

- dedup キーは各イベントの安定した `uuid`。投稿対象（`assistant` / `user` /
  `system`）は必ず `uuid` を持つ。`uuid` を持たないメタデータ（`mode` /
  `permission-mode` / `file-history-snapshot` 等）は `render_event` が `None` を
  返す＝そもそも投稿されないため dedup 対象外（同一内容メタの誤った取りこぼしを
  避ける）。
- 起動時（`from_start=False`）は、ファイルに既に存在する `uuid` を baseline として
  seen に投入する。これにより、起動直後に `--resume` で offset が 0 にリセットされ
  ても、**起動前から在った履歴は「配信済み」として二度と流れない**。
- **mtime だけが進んだ（中身もサイズも同じ）ときは読み直さない**（#906）。Claude Code は
  アイドルの transcript をおよそ 1 時間ごとに触る。これを同一サイズの書き換えとみなすと、
  `uuid` を持たない `api-request-blob`（リクエストの写し。中に過去の `tool_use` を持つ）が
  再び流れ、終わったスレッドに進捗行「⏳ 待機中」が立っていた。判定は読了位置の直前 4 KB を
  覚えておき、同サイズで mtime が進んだときにだけ比べる（100 MB の transcript を読み直さない）。
- `from_start=True`（明示的な全リプレイ）では baseline seeding を行わず、各イベントは
  一度だけ流れる（リセットが起きても重複しない）。
- **読み位置はファイルごとに覚える**（#627）。乗り換えて戻ってきても、読了済みの
  ところから再開する＝二度流さない。
- **一時的に読めなかったときも読み位置を手放さない**（#987）。ファイルが一瞬消えた・
  開けなかった（EMFILE）ときは、そこまでの読み位置を覚えたまま次の poll で選び直し、
  同じファイルなら続きから読む。手放すと tail 開始時の位置から読み直し、`uuid` を持たない
  `api-request-blob` が二度流れていた（#906 と同じ「⏳ 待機中」の誤表示の種）。
  覚える位置は**書きかけで持っていた行の頭**（#1004）。読んだ所の末尾を覚えると、
  選び直したときに行の後半だけを読んで捨て、その1行（返事本文のこともある）が届かなかった。
- **tail の開始時に既にあったファイル**を後から読み始めるとき（そのスレッドの
  transcript が、c-lord が次のターンを回して初めて資格を得る場合）は、
  **開始時点のサイズから**読む。開始前からディスクにあった履歴は、そもそも
  こちらが配信すべきものではなかった。
- **「開始時点のサイズ」は、最後に書き終わった行の終わりに揃える**（#988）。開始の瞬間に
  Claude Code が1行を書いている途中だと、生のサイズはその行の途中を指す。そこから読むと
  行の後半だけを読んで JSON として捨て、その1件（返信本文のこともある）が届かなかった。
  書きかけの行は、書き終わってから丸ごと1回届く。

テスト: `tests/transcript/test_tail.py`
(`test_tail_does_not_replay_history_on_resume_rewrite`,
`test_tail_does_not_re_yield_already_followed_event_on_rewrite`,
`test_tail_does_not_re_read_when_only_mtime_moves`,
`test_tail_recovers_from_a_same_size_rewrite`) と
`tests/transcript/test_mirror.py`
(`test_mirror_does_not_repost_history_on_resume_rewrite`)。

## 再起動時の「落ちた最終回答」救出 (#215) と、その誤検知 (#553)

上の不変条件は tail の話。もう一つ、**再起動をまたいだ再投稿**の経路がある。

`TranscriptMirrorCog.on_ready` は、mirror が止まっている間に書かれた最終回答を
救出する (#215)。基準は `sessions.mirror_replied_uuid` (以下「カーソル」)。

### あるべき動き

- **カーソルは「最終回答として実際に配信した uuid」だけを指す。**
  中間メッセージ (silent flush で投稿した assistant_text) では**進めない**。
- 救出の判定は、カーソルとの**等価比較ではなく順序**で行う:
  カーソルが最後の完了ターンの最終回答**以降**にあれば配信済み → 何もしない。
  カーソルがそれより**前**にあれば、そのターンは mirror が止まっている間に
  完了した → 1 度だけ再配信する。
- カーソルの uuid が**現在の transcript に存在しない**ときは**救出する**。
  mirror は「配信したその場でカーソルを進める」ので、このファイルから 1 度でも
  配信していればカーソルの uuid は**このファイルの中にある**はず。無いということは
  このファイルの間ずっと mirror が止まっていた (例: 間に `/clear` が入って
  新しい jsonl になった) ということ＝ #215 が救うべきケースそのもの。
  #553 が潰したいのは「カーソルが**同じファイルの中で**最終回答より後ろにいる」
  ケースなので、ここを救出にしても再発しない。

### なぜこの形か (#553 の実害)

`_last_text_uuid` は assistant_text を受けるたびに更新され、silent flush では
消えなかった。したがって**ターン実行中に停止**すると、「最終回答として配信した
わけではない中間メッセージ」の uuid がカーソルに書かれる。一方
`last_completed_final_answer` は最後の turn-end までしか見ない＝**より古い**
uuid を返す。判定が等価比較だったので「不一致 → 落ちた」と読み違え、
**すでに読んだ長文がもう一度投稿された**。

再起動通知が出る条件がまさに「ターン実行中」なので、実質**再起動のたびに**
起きていた (2026-08-25 の本番デプロイでは通知が出た 3 スレッド全部で発生)。

### 実装

| 役割 | 場所 |
|---|---|
| カーソルを進めるのは最終回答の配信時だけ | `transcript/mirror.py` の `_delivered_uuid` / `_flush_pending_as_reply` |
| 中間メッセージではカーソルを進めない | `transcript/mirror.py::_flush_pending_silently` |
| 順序による救出判定 | `transcript/recovery.py::final_answer_needs_recovery` |
| 呼び出し側 | `cogs/transcript_mirror.py::_recover_final_answer` |

テスト: `tests/transcript/test_mirror.py::test_cursor_never_records_a_silently_flushed_intermediate`、
`tests/transcript/test_recovery.py` (`test_no_recovery_when_the_cursor_is_past_the_final_answer` ほか)、
`tests/test_transcript_mirror_cog.py`
(`test_no_recovery_after_a_restart_mid_turn` / `test_still_recovers_a_genuinely_dropped_answer`)。
