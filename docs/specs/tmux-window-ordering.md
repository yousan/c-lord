# あるべき動き: tmux window の並び順・フォーカス・サイズ

> これは「あるべき動き」。理念 [`docs/PHILOSOPHY.md`](../PHILOSOPHY.md) の下位。迷ったら理念に照らす。

## これは何か

`tmux attach -t <session>` で c-lord のセッションを見たとき、各スレッドの window は **常に番号の昇順（`w23 → w32 → … → w81`）で並びます**。新しいスレッドが立っても、見ている画面のフォーカスは奪われません。

- **並び順**: window 番号 `w{N}` の**小さい順**。番号を持たない window（初期シェル・手動作成・dir 流用で adopt された window 等）は**末尾**にまとめる（相対順は保持）。
- **フォーカス**: 新規 window は `tmux new-window -d`（detached）で作るので、attach 中の利用者の現在 window は切り替わらない。

なぜ要るか: C-lord の核は「複数のセッションを開いたまま並行して進める」こと（[理念 A](../PHILOSOPHY.md#a-切れないどこからでも複数を並行して進められるこれが核)）。何本も開いていると、window が番号順に並んでいないと目的のスレッドを探しづらい。また作業中に新スレッドが立つたび画面が飛ぶと集中が切れる。どちらも「並行して回す」体験を損なう。

## ルールと根拠

### なぜ「add 後にソート」なのか（トリガの選定）

window 名 `new-window`（`-a`/index 指定なし）は tmux の既定で **最小の空き index** に挿入される。window を kill して空いた枠を後発の番号が埋めるため、番号順が崩れる（実測: `docs/evidence/374/`）。

- **kill では番号順は壊れない** — 穴が空くだけで残りの相対順序は昇順のまま。
- **順序を壊すのは add だけ** → **window を追加した直後にだけ並べ替えれば過不足ない**。

「新規を必ず末尾に追加する」保証より「並べ替えを流す」方を選んだ理由: adoption（既存 dir の window 流用）や利用者の手動操作でも順序は乱れうるので、原因を問わず収束する**並べ替え**の方が堅牢。番号は単調増加なので、毎 add のフルソートでも「常に番号昇順」という不変条件が単純に保てる。

### 実装の要点（`c_lord/tmux.py`）

| 項目 | 実装 |
|------|------|
| トリガ | `create_session()` の**新規 window 作成パス**末尾で `_sort_windows_unlocked()` を呼ぶ（adoption / 既存再利用パスでは呼ばない） |
| 並べ替え | `#{window_id}`（不変 ID）で各 window を一旦高 index 帯（`_SORT_TMP_BASE`）へ希望順に退避 → `move-window -r` で base-index から詰め直す。**index だけ変わり、名前・`@thread_id`・ペインは保持**（bot の識別は `@thread_id` → `window_id` ベースで index 非依存。#649 以降、名前はターゲットに使わない） |
| 不要 churn 回避 | 既に整列済みなら `move-window` を発行しない |
| フォーカス | `new-window -d` |
| 直列化 | 「作成 → `@thread_id` 設定 → ソート」を 1 クリティカルセクションに。同時 add で `move-window` が交錯しない。ロックは**tmux セッション名をキーにしたプロセス内のロック**（`_session_lock()`）で、`TmuxSessionManager._lock` は毎回そこから引く。#649 以前はインスタンス変数の `threading.Lock` だったため、同じセッションを指す別インスタンス同士では直列化されず、同名 window が2枚できていた → [tmux-window-identity.md](./tmux-window-identity.md) |
| ソートキー | `_window_sort_key()` に分離（番号付き昇順 → 非番号は末尾・相対順保持） |
| 起動時 | 専用フックは無い。再起動後に既存 window が乱れていても、**次に新スレッドが立った時のフルソートで一括整列**する（per-channel の遅延生成モデルに沿う） |

## ウィンドウサイズ（端末リサイズ耐性） — #403

`tmux attach` 中に**利用者の端末（SSH クライアント）のサイズが変わっても、各 window のサイズは変わりません**（固定）。新規 window は作成時に接続中クライアントへフィットし、以後は固定されます。

なぜ要るか: tmux の既定 `window-size latest` だと、クライアントが少しでもサイズ変化するたびに**全 window が resize** され、各 window で idle 状態の Claude TUI に SIGWINCH が飛ぶ。inactive な pane の再描画はしばしば不完全で、**下部の status 行ブロックが多重ゴーストして数時間 stuck** する（実測: #403。全再描画＝手動 resize / Ctrl-L でしか直らない）。bot は多数の window を抱えるため「端末を少し触ると別 window が崩れる」が起きやすい。window サイズを端末から切り離すことで構造的に断つ。

### 実装の要点（`c_lord/tmux.py`）

| 項目 | 実装 |
|------|------|
| ポリシー | session に `set-option window-size manual`（`_ensure_window_size_manual()`、`_ensure_session()` から呼ぶ）。クライアントの resize が window に伝播しなくなる |
| 新規 window のフィット | `create_session()` で新規 window 作成直後（まだ空＝走行中 TUI を崩さない）に `_fit_window_to_client()` が接続中クライアントのサイズへ resize。未接続時は `DEFAULT_MANAGED_WINDOW_SIZE` |
| トレードオフ | window はフィット時のサイズに固定。後から端末を大きくすると余白、小さくすると下部がクリップしうる（status 行のゴースト固着よりは軽微）。主参照は Discord の `/tmux-screenshot`、生 attach はデバッグ用途 |

## 将来の拡張（今回は未実装・記録のみ）

並べ替えキーは `_window_sort_key()` に分離してあり、**差し替え可能**な設計にしてある。以下は要望が出たら実装する候補（#374 の議論で出たが今回スコープ外）:

- **他のソートキー**: アクティブ順（最近触った順）/ ログ長順 / 最終更新順 など。
  - 注意: これらは**動的**なので、window が頻繁に並び替わって逆に追いづらくなる懸念がある。既定は番号順のままにすべき。
- **手動コマンド `/sort <key>`**: 利用者がその場で並べ替え方法を選ぶ。動的キーはこのコマンド経由（＝明示操作時のみ）に限定するのが無難。

実装する場合は `_sort_windows_unlocked()` がキー関数を受け取る形に一般化し、本ドキュメントを更新すること。
