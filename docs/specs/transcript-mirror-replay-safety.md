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
| 1. 資格（後方互換） | 名前が無いセッション（#773 以前に起動して走り続けているもの）だけがここに来る。c-lord がペインに送るプロンプトには ZWSP (U+200B) が付く (`tmux.send_input` / `start_claude` #530) ので、それが `"content":"` の直後に生の UTF-8 で入っている transcript は対象。**付いていても偽陽性にはならない**ので残してあるが、**CLI 2.1.278 以降は付かない**ので、これ単独では成立しない |
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
- ログには（ターンの有無に関わらず）`ERROR` で 1 行出す。
  `候補 jsonl の本数` と `記録した session id` を含めるので、
  「1本も書かれていない」と「書かれているが自分のものと確認できない」を切り分けられる

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
- 同一サイズの in-place rewrite（`size == offset && mtime 更新`）
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
- `from_start=True`（明示的な全リプレイ）では baseline seeding を行わず、各イベントは
  一度だけ流れる（リセットが起きても重複しない）。
- **読み位置はファイルごとに覚える**（#627）。乗り換えて戻ってきても、読了済みの
  ところから再開する＝二度流さない。
- **tail の開始時に既にあったファイル**を後から読み始めるとき（そのスレッドの
  transcript が、c-lord が次のターンを回して初めて資格を得る場合）は、
  **開始時点のサイズから**読む。開始前からディスクにあった履歴は、そもそも
  こちらが配信すべきものではなかった。

テスト: `tests/transcript/test_tail.py`
(`test_tail_does_not_replay_history_on_resume_rewrite`,
`test_tail_does_not_re_yield_already_followed_event_on_rewrite`) と
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
