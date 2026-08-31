# Transcript mirror — どれを読み、何を二度流さないか (#627 / #433)

このドキュメントは、`CLORD_BRIDGE_MODE=jsonl` のときに動く JSONL transcript ミラー
(`c_lord/transcript/`、`c_lord/cogs/transcript_mirror.py`) の **「あるべき動き」**
のうち「**どの transcript を読むか**」と「過去ログを二度 Discord に流さない」保証を定める。

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
| 1. 資格 | c-lord がペインに送るプロンプトには必ず ZWSP (U+200B) が付く (`tmux.send_input` / `start_claude` #530)。それが `"content":"` の直後に生の UTF-8 で入っている transcript だけが対象。`claude -p` の transcript には決して入らない |
| 2. 前進のみ | いったん決めたら、乗り換えてよいのは**それを決めた後に現れたファイル**だけ (= `/clear` の後継)。既にあったファイルは mtime がどれだけ新しく見えても乗っ取れない。`touch` で読了済み transcript に戻って再投稿する事故を塞ぐ |
| 3. 該当なし | 資格を満たすファイルが無ければ**何も出さない**。ログに 1 度だけ警告する。他人の会話を流すくらいなら黙る。次に c-lord がターンを回した時点で自動的に解消する |

**同じ判定を #215 の救出スキャンにも使う** (`recovery.py`)。mtime 最新を読むと、
bot 再起動中に終わった `claude -p` の最終回答が「落ちた回答」として
スレッドに投稿されてしまう。

**実測 (2026-08-31, 本番ホスト)**: #627 該当ディレクトリの 182 個中、印が付いていたのは
**1 個**(そのスレッド自身のもの)。生きているセッション行 313 件全部で、
「印つきの最新」と旧ルールの選択が食い違うケースは **0 件** — つまりこの規則は
正常に動いていたスレッドの挙動を変えずに、事故だけを塞ぐ。

テスト: `tests/transcript/test_session_pinning.py`、
`tests/transcript/test_recovery.py::test_does_not_recover_a_sub_invocations_answer`。

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
