# Transcript mirror — replay safety across restarts/rewrites (#433)

このドキュメントは、`CLORD_BRIDGE_MODE=jsonl` のときに動く JSONL transcript ミラー
(`c_lord/transcript/`、`c_lord/cogs/transcript_mirror.py`) の **「あるべき動き」**
のうち「過去ログを二度 Discord に流さない」保証を定める。

## あるべき動き (利用者から見た期待)

- bot / ホストが再起動しても、**古いスレッドに過去の会話履歴が再投稿されない**。
- 再起動後に古いスレッドへ話しかけると、**届くのはその新しいターンの応答だけ**。
  6/1〜6/17 にすでに送られた応答が、もう一度スレッドに流れてくることはない。
- これは `/clear`（新しいセッション jsonl への切り替え）や、クラッシュ後の
  `--resume`（Claude Code がアクティブ jsonl を**履歴を保ったまま書き換える**）を
  またいでも成り立つ。

## なぜこの保証が要るか (#433 の実害)

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

## 実装上の不変条件 (regression を防ぐ)

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

テスト: `tests/transcript/test_tail.py`
(`test_tail_does_not_replay_history_on_resume_rewrite`,
`test_tail_does_not_re_yield_already_followed_event_on_rewrite`) と
`tests/transcript/test_mirror.py`
(`test_mirror_does_not_repost_history_on_resume_rewrite`)。
