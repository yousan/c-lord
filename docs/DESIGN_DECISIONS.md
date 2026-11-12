# Design Decisions — 設計判断の記録

c-lord の設計で「何を決めたか」と「なぜそう決めたか」を、検討した代案と一緒に残す文書。
決定は番号で参照されるので（Issue、仕様、`CLAUDE.md` から）、**番号と見出しは変えない**。

§1〜§11 は、このプロジェクトが始まった時点（2026-02、`claude -p` を毎回起動する構成）に下した判断で、
その後の変更で実装が置き換わったものがある。それぞれに **「決めたこと」「理由」** を当時のまま残し、
実装が変わったものには **「いま」** として現在の姿と、置き換えた Issue を書き添えてある。
§12 以降は、その都度の出来事と一緒に書き足してきた記録。

いまの構成の全体像は [`ARCHITECTURE.md`](ARCHITECTURE.md)、実装に直結する決定の一覧は `CLAUDE.md` の
"Key Design Decisions"。

## 1. CLI Subprocess, Not Direct API — API を直接叩かず、Claude Code CLI を動かす

**決めたこと:** Anthropic の API を自分で呼ぶのではなく、本物の `claude` コマンドを子プロセスとして動かす。
当初の形は、メッセージごとに `claude -p --output-format stream-json` を起動し、その出力を読むものだった。

**検討した代案:**

- API を直接呼ぶ（HTTP / SDK）。リクエストを細かく制御でき、遅延も小さい
- Claude Code をライブラリとして import する。結合は密になり、メモリも共有できる

**理由:**

- **Claude Code の機能がそのまま手に入る。** CLAUDE.md、skills、tools、memory、MCP サーバ、hooks —
  本物の CLI を動かすので、何も作らなくても全部効く
- **機能追従の負担が無い。** Claude Code に新機能が入れば、c-lord のコードを変えなくても使える
- **難しい部分は CLI が既に解いている。** ツールの連携、権限の管理、エラーからの回復を API の上に
  作り直せば、壊れやすいコードが何千行も要る
- **作っているのは Claude Code の UI であって、Claude Code の代わりではない**

**受け入れたもの（当時）:** 起動のたびのオーバーヘッド（約 200ms）、セッションごとに OS プロセスが 1 つ要ること、
構造化された応答ではなく stream-json を解析すること。

**いま:** 「API ではなく CLI」は変わっていない（`CLAUDE.md` の "What Does NOT Belong Here"）。変わったのは動かし方で、
`claude -p` を毎回起動するのをやめ、**対話モードの `claude` をスレッドごとの tmux ウィンドウに常駐させて、
入力は `send-keys` で打ち込み、回答は Claude Code 自身が書く jsonl transcript から読む**（#53 → #71 → #712。
§15・§18 と `CLAUDE.md` Key Design Decisions 1）。stream-json の解析は無くなった。
代わりに受け入れているのは、スレッドごとに常駐プロセスを持つこと（放置されたものはスリープ・停止・上限で
回収する — `docs/specs/workspace-vocabulary.md`）と、CLI の TUI と transcript の形式に依存すること
（CLI 2.1.278 の変更で全スレッドが無言になった #773 がその実例）。

## 2. Thread = Session (1:1 Mapping) — スレッド 1 本がセッション 1 つ

**決めたこと:** Discord のスレッド 1 本を、Claude Code のセッションちょうど 1 つに対応させ、`--resume` で続ける。

**検討した代案:**

- 全スレッドで 1 つのセッションを共有する。単純だが、会話が分かれない
- 利用者ごとに 1 セッション。分離はできるが、同じ人が並行して別の話をできない
- チャンネルごとに 1 セッション。粗すぎて、別の話題が混ざる

**理由:**

- **利用者の感覚に合う。** Discord のスレッドは、もともと「1 つのことについての 1 つの会話」として使われている
- **履歴がそのまま見える。** スレッドをさかのぼれば会話の全体が読める。別の履歴ビューアが要らない
- **続きから再開できる。** `--resume <session_id>` は、開いていたファイルやツールの履歴といった
  Claude Code 側の状態ごと、止めたところから続ける
- **並行して作業できる。** スレッドが違えば別の案件で、別のセッションが同時に走る

**実装:** SQLite の `sessions` テーブル。`thread_id` が主キー、`session_id` が Claude Code のセッション識別子で、
保存は UPSERT。

**いま:** 対応関係はそのまま、スレッド 1 本に **tmux ウィンドウ 1 つ**と**作業ディレクトリ（git clone）1 つ**が加わった。
session id は Claude Code が決めたものを後から拾うのではなく、c-lord が `--session-id <uuid>` で名付けて
`.clord-session` に控え、再開は `--resume <控えた uuid>` で行う（#773）。保存期間を過ぎた行は消さずに
「片付け済み」の印を付けて残す（#818）。

## 3. Shared Run Helper (_run_helper.py) — Claude を走らせる処理は 1 か所に

**決めたこと:** Claude CLI を実行する処理を共有の関数に切り出し、`ClaudeChatCog` と `SkillCommandCog` の両方から使う。

**検討した代案:**

- それぞれの Cog に同じ処理を書く。最初は簡単だが、直すたびに 2 か所になる
- 基底クラスを継承させる。硬すぎる — 片方は `on_message`、片方はスラッシュコマンドで、入口の形が違う
- イベントの pub/sub にする。使う側が 2 つしかないのに大げさ

**理由:**

- **重複は無くすが、結合はさせない。** どちらの Cog も同じ関数を呼ぶだけで、入力の受け取り方
  （メッセージのリスナーか、スラッシュコマンドか）は互いに独立のまま
- **直す場所が 1 つ。** ステータスの絵文字、エラーの扱いなど、ターンに付随する処理がここに集まる
- **Easy to extend**: New Cogs that need Claude execution just call the same function (e.g. a consumer docs-sync Cog can implement its own streaming logic on top of the shared function for different requirements)

**いま:** 入口は `run_claude_with_config(RunConfig)`。当時の `run_claude_in_thread()` は、同じ処理を位置引数で呼ぶ
互換用の形として残っている。使う側は `WebhookTriggerCog` と `SchedulerCog` にも広がった。
受け持つのは 1 ターンの進行（ランプ、質問メニューの橋渡し、エラーの表示、セッションの保存）で、
**回答の本文は投稿しない** — それは transcript ミラーの仕事になった（#712）。返信の分割（§5）もミラー側にある。

## 4. Emoji Reactions for Status — 状態は絵文字のリアクションで示す

**決めたこと:** Claude がいま何をしているかを、利用者の元のメッセージに付けるリアクションで示す。
当初は活動の種類ごとに絵文字を出し分けた（🧠🛠️💻🌐 と、終わったときの ✅ / ❌）。

**検討した代案:**

- スレッドに「状況メッセージ」を置いて編集し続ける。目立つが、Discord のレート制限にすぐ当たる
- 入力中インジケータ。「入力中」しか出せず、何が起きているかは伝えられない
- embed を更新し続ける。情報は多いが、スレッドがうるさくなる

**理由:**

- **邪魔にならない。** 状況の更新でスレッドが埋まらない
- **ひと目で分かる。** 文章を読まなくても、絵文字 1 つで状況が伝わる
- **API の負担が軽い。** リアクションの付け外しは、メッセージの編集より安い
- **モバイルでも見える。** Discord のモバイルアプリはリアクションを目立つ位置に出す

**当時の実装:** ツールが次々切り替わってもレート制限に当たらないよう 700ms のデバウンスを入れ、
絵文字は常に 1 つだけ（前のものを外してから次を付ける）。動きが無ければ 10 秒で ⏳、30 秒で ⚠️（§11）。
終わったら ✅ / ❌ を少し見せてから外した。

**いま（#246）:** リアクションで示す方針は同じで、中身を**ターンごとのランプ 1 つ**に絞った。
🟢 実行中（考えている間もツールの間も変えない）→ 🟡 あなたの番（ターン完了）。❌ エラーと ⏳ / ⚠️ 停滞は、
その状態の間だけの一時的な上書き。1 ターンに数回しか変わらないのでデバウンスは外し、すぐ反映する。
ツールごとの絵文字は無くなり、ツールの様子は transcript ミラー由来の進捗行と `progress.txt` で見える
（ランナーはツールのイベントを出さない — #723）。
同じ 🟢 / 🟡 をスレッド名にも出す機能は、名前変更のレート制限（10 分に約 2 回）を使い切るので既定でオフ
（#329。`CLORD_THREAD_LAMP=1` で有効 — `docs/specs/thread-lamp.md`）。

## 5. Fence-Aware Message Chunking — コードフェンスの途中では切らない

**決めたこと:** Discord のメッセージを、コードフェンスの内側では決して分割しない。どうしても切るときは、
フェンスを正しく閉じ、次のメッセージで開き直す。

**検討した代案:**

- 2000 文字で機械的に切る。コードブロックが壊れ、表示が崩れる
- 段落の境界でだけ切る。長さがひどく不揃いになることがある
- 画像にして送る。やりすぎで、検索もできない

**理由:**

- **Claude Code の出力はコードだらけ。** コーディングの道具の橋渡しなら、コードブロックを正しく扱えなければならない
- **壊れたフェンスは読めない。** コードブロックの後ろ半分が等幅の地の文として出て、読む人を混乱させる
- **開き直せば色も保てる。** ` ```python ` で始まったブロックは ` ``` ` で閉じ、次のメッセージを ` ```python ` で
  始め直すので、シンタックスハイライトが続く

**切る場所の優先順（当初の設計）:** (1) 段落の切れ目（空行）— 見た目が一番自然、(2) 改行 — 行を壊さない、
(3) 上限での強制分割 — 最後の手段で、このときフェンスの修復が働く。

**いま:** 実装は `c_lord/discord_ui/reply_chunker.py` の `chunk_discord_content()` で、transcript ミラーが
回答を投稿するときに使う（当初の `chunker.py` は #53 で削除）。行の境界で切り、1 行が上限を超えるときだけ
行の途中で切る。開いているフェンスは閉じて、次の断片で言語指定ごと開き直す。
利用者から見た動きは `docs/specs/long-reply-split.md`。

## 6. Installable Package, Not Monolith — 単体の bot ではなく、入れて使うパッケージ

**Decision:** Ship `c_lord` as a proper Python package installable via `uv add git+...` or `pip install git+...`.

**検討した代案:**

- 1 つにまとまった bot として配る。始めるのは楽だが、手を入れにくい
- Docker イメージ。隔離はできるが、Python のスクリプト 1 つには重い
- コピーして使うテンプレート。更新を取り込む道が無い

**理由:**

- **役割が分かれる。** Discord と CLI の橋渡しはフレームワークが持ち、案件ごとの設定・秘密・独自の Cog は
  利用側が持つ
- **Upgrade path**: `uv lock --upgrade-package c-lord && uv sync` gets you the latest framework without touching your custom code
- **ぶつからない。** 利用側の `pyproject.toml` がフレームワークの版を固定するので、複数の bot が
  それぞれ別の版を使える
- **実際に成り立っている。** 利用側の bot は c-lord の Cog を import し、自分の Cog（リマインダ、監視、
  docs-sync、自動アップグレード）を、フレームワークに手を入れずに足している

**いま:** 利用側が呼ぶのは `setup_bridge()` の 1 回で、Cog の登録と配線は c-lord が済ませる
（`CLAUDE.md` の Zero-Config Principle — パッケージを更新するだけで新機能が届くこと）。
実行の設定は `ClaudeConfig` で渡す（当時の `ClaudeRunner` は §9 のとおり無くなった）。

## 7. No Custom AI Logic — AI のロジックを持たない

**決めたこと:** c-lord は AI のロジックを一切持たない。プロンプトの作り込みも、ツールの定義も、
メモリの管理も、system prompt も置かない。

**検討した代案:**

- Discord 向けの応答になるよう system prompt を足す。やりたくなるが、筋が違う
- Discord 固有の操作のための独自ツールを定義する。守備範囲が広がり続ける
- メモリ / コンテキストの仕組みを作る。Claude Code に既にあるものの二重化になる

**理由:**

- **どれも Claude Code が既に持っている。** 振る舞いは CLAUDE.md、能力は skills、文脈は memory、行動は tools が決める
- **保守するものが減る。** AI のロジックを 1 つ足すたびに、Claude Code の進化に合わせて直し続けるものが 1 つ増える
- **挙動が読める。** ターミナルで見えるものが、そのまま Discord に出る。Claude の振る舞いを変える隠れた
  system prompt は無い
- **設定の置き場所が 1 つ。** 必要な設定は利用者自身の CLAUDE.md だけ。c-lord 用の設定ファイルも
  「Discord モード」も無い

**いま:** この決定はそのまま生きていて、理念（`docs/PHILOSOPHY.md` の「やらないこと」）と、
ループ機能の線引き（§17、`docs/LOOP_ENGINEERING.md`）の土台になっている。
その後 c-lord がセッションに持ち込むようになったものは 2 つある — 別チャンネルを読むための
`discord-read` スキル（#259）と、貼られた Discord リンクの本文の差し込み（§16）。
どこまでを許すかの判断は §16 に書いてある。

## 8. SQLite for Session Storage — セッションの保存は SQLite

**決めたこと:** スレッドとセッションの対応を SQLite（aiosqlite 経由）に保存し、接続は操作のたびに開く。

**検討した代案:**

- メモリ上の dict。単純だが、再起動で消える
- Redis。利用者 1 人の key-value には大げさ
- PostgreSQL。さらに大げさ
- JSON ファイル。同時アクセスに対する安全性が無い

**理由:**

- **再起動をまたいで残る。** 対応が残るので、bot を再起動しても会話を続けられる
- **用意するものが無い。** 外部のデータベースサーバが要らない
- **イベントループを止めない。** aiosqlite が非同期に包む
- **スキーマが単純。** テーブル 1 つ、主キー 1 つ。JOIN も、マイグレーションの枠組みも要らない
- **性能は十分。** 利用者 1 人、セッションは数十 — SQLite には何でもない量

**操作ごとに接続を開く:** 接続プールを持たず、リポジトリの各メソッドが自分で接続を開いて閉じる。
効率は少し落ちるが、接続の寿命を管理する必要が無くなり、このアプリケーションの少ない問い合わせ量には
これで足りる。

**いま:** SQLite と「操作ごとに接続」はそのまま。テーブルは 1 つではなくなった — `sessions` のほかに、
設定、チャンネル / スレッドとリポジトリの bind、回答待ちの質問、再起動後に再開するセッション、
橋渡し済みのメニュー、モデル設定、開発環境の対応、AI Lounge などがあり、定期タスクは別ファイル
（`data/tasks.db` の `scheduled_tasks`）に置く。マイグレーションの枠組みは今も入れておらず、
`database/models.py` の `init_db()` と各リポジトリの `init_db()` が、起動のたびに不足しているテーブルと列を足す。

## 9. Clone Pattern for Runner Instances — ランナーはセッションごとに別のインスタンス

**決めたこと（当時）:** `ClaudeRunner.clone()` で、設定は同じだが動いている子プロセスを持たない、
新しいインスタンスを作る。

**検討した代案:**

- 同じランナーを使い回す。子プロセスの状態をセッション間で共有することになり、危ない
- `ClaudeRunner` を毎回手で組み立てる。設定があちこちに重複する
- ファクトリにする。実質コピーコンストラクタでしかないものに対して大げさ

**理由:**

- **安全。** セッションごとに自分の子プロセスを持つ。共有する状態が無い
- **単純。** `runner.clone()` が設定を全部写す。コードは 1 行
- **追える。** `_active_runners[thread_id] = runner` でスレッドとランナーが対応するので、
  `/clear` が正しい子プロセスを止められる

**いま:** `ClaudeRunner` も `clone()` も無い。設定と実行が分かれたためで、設定は `ClaudeConfig`
（dataclass）が持ち、実行は**スレッドごとの `TmuxClaudeRunner`** がターンのたびにその設定から作られる。
「セッション間で実行状態を共有しない」「スレッド id からランナーを引ける（`_active_runners`）」という
ねらいは同じまま残っている。`/clear` は子プロセスを止めるのではなく、Claude Code 自身の `/clear` を
ペインに打つようになった（#803）。

## 10. Separate Webhook and Chat Message Paths — webhook と人の発言は別の経路

**決めたこと:** webhook のメッセージ（`message.webhook_id` がある）と、人の通常のメッセージは、
別々の Cog が扱い、重なりを持たせない。

**検討した代案:**

- 1 つの Cog で両方を扱う。コードの構造は単純だが、責任が混ざる
- ミドルウェアの形にする。discord.py にその仕組みは無く、流儀に合わない

**理由（当時）:**

- **`ClaudeChatCog`** は `message.author.bot` が偽のものだけを通す（webhook を含む bot をすべて飛ばす）
- **webhook 用の Cog**（docs-sync など）は `message.webhook_id` があるものだけを通す
- **ぶつからない。** この 2 つの条件は排他的で、1 つのメッセージが両方に当てはまることは無い
- **守り方が違う。** 人の発言は `allowed_user_ids` で確かめ、webhook は決め打ちのトリガー文字列と照合する
- **することが違う。** 人の発言は対話のセッションを始め、webhook は決められた自動処理を起動する

**いま:** 「決め打ちのトリガーで自動処理を起動する Cog（`WebhookTriggerCog`、`AutoUpgradeCog`）を、
会話の Cog と分ける」ことは変わっていない。変わったのは排他の線の引き方で、
**`ClaudeChatCog` も webhook のメッセージを受け取る**。webhook の URL を持っていること自体を認可とみなし
（#507/#508、`c_lord/command_gate.py`）、スレッドに投稿された webhook のメッセージは人の発言と同じく
会話を進め、チャンネルではテキストコマンド（`!clord` など）として働く。CI からの起動、E2E テスト、
Claude から Claude への依頼がこれで成り立っている。重ならないようにする仕組みは別に持つ:
トリガー用の Cog が自分のものと認めた webhook メッセージには `ClaudeChatCog` は手を出さず（#862）、
c-lord 自身がサブエージェントの声として使う webhook は命令として決して受け取らない（#971）。
webhook 以外の bot は、`CLORD_TRUSTED_BOT_IDS` に載せたものだけを通す。

## 11. Stall Detection in Status Manager — 動きが止まったことを示す

**決めたこと:** 活動が 10 秒検出されなければ ⏳ を、30 秒で ⚠️ を出す。

**理由:**

- **長く考えることがある。** Claude Code は動き出す前に 10〜30 秒以上考えることがある。停滞の表示が無いと、
  利用者には同じ絵文字がずっと見えるだけで、bot が固まったのかどうか分からない
- **通信の問題と見分ける。** 子プロセスやストリームが止まったとき、「考えている」と「壊れている」を
  区別する手がかりになる
- **2 段階にする。** 「時間がかかっている」と「何かおかしいかもしれない」を分けて伝える
- **本体を止めない。** 監視は asyncio のタスクとして、イベントの流れと並行に動く。I/O を塞がない

**いま:** しきい値は同じ（`discord_ui/status.py` の `STALL_SOFT_SECONDS = 10`、`STALL_HARD_SECONDS = 30`）。
「活動」の出どころが変わった。当初はランナーが出すツールのイベントで計っていたが、そのイベントが無くなって
（#53/#723）からは何もタイマーを戻さず、30 秒を超えるターンがすべて ⚠️ になっていた。いまは transcript ミラーが
jsonl に見た動き（ツールの呼び出しと結果、思考、メッセージ）を `c_lord/turn_activity.py` 経由でランプに伝え、
結果待ちのツール呼び出しがある間は「動いている」と扱う（#769/#905）。
長い沈黙には、ランプとは別に、スレッドに出る進捗行（90 秒の沈黙で `⚙️ 作業中 …`、#539）もある。

## 12. DoD & merge gate (single source of truth)

**Decision:** CLAUDE.md carries one authoritative "Definition of Done" list,
the PR template mirrors it verbatim, and a PR may close an Issue only when 100%
of that Issue's Acceptance Criteria are met. Issues must be one-concern with
binary, unambiguous ACs.

**Why (the incident that motivated this):**

While fixing `/clear`, we filed Issue #123 bundling two things: a concrete bug
(Part 1: `/clear` left the tmux window alive on idle threads, so the next
message resumed old context) and an open-ended design task (Part 2: bot-restart
loses context because `start_claude` never uses `--resume`/`--continue`, and the
stored `session_id` is a synthetic `tmux-{thread_id}` so `--resume` cannot work
anyway). PR #124 fixed Part 1 only, wrote `Closes #123`, and on merge GitHub
auto-closed the Issue as "completed". Part 2 silently vanished.

Root cause was **not** lax rules in prose — CLAUDE.md already said staging
verification was mandatory. The cause was a gap between the *enforced* gate and
the *aspirational* prose:

- The only hard gate (CI) runs `ruff`/`pyright`/`pytest`, all of which pass with
  **mocked** tests. Real tmux/Discord behavior is never exercised. Green CI ≠ works.
- Staging verification was honor-system, and the **PR template didn't even list
  it** — so the artifact the author actually fills out never surfaced it.
- The Definition of Done was scattered across the TDD section, the flow section,
  the staging section, and the PR template, and they disagreed. The weakest one won.
- A single multi-concern Issue + `Closes #N` auto-close let a partial PR mark the
  whole Issue done with no check that all ACs were satisfied.

**Fix:** Move the strictness from prose into the artifact the author touches.
One DoD list; the PR template is a verbatim copy; ACs are copied into the PR and
all must be checked; `Closes` is gated on 100% AC completion (else `Refs`);
Issues are kept one-concern with binary ACs so they cannot be half-satisfied.

## 13. Restarting the bot: filter by venv path, kill by PID, launch detached

**Decision:** Process control for the prod/staging bots follows three rules:

1. **Target by venv path, never the bare module name.** Stop prod with
   `pgrep -f "/home/yousan/c-lord/.venv/bin/python3 -m c_lord.main"` (staging:
   `c-lord-parallel-3/.venv/...`). The unfiltered `pgrep -f c_lord.main` matches
   *every* c-lord on the host.
2. **Count with `ps`, not `pgrep -fc`.** Use
   `ps -eo pid,lstart,args | grep "<venv-path>/bin/python3 -m c_lord.main" | grep -v grep`.
   A healthy env has exactly one python (with one `uv run` parent).
3. **Launch detached with `setsid -f`, never a session-tracked background runner.**
   `setsid -f bash -c 'cd <dir> && exec uv run python -m c_lord.main >> <log> 2>&1' < /dev/null`
   reparents the daemon to `systemd --user` so it outlives the launching session.

**Why (the incident that motivated this):**

During the #62 / #182 prod redeploy, following the documented
`pgrep -f c_lord.main | xargs kill` plus a session-tracked background launch
produced **2–3 duplicate prod bot instances** that fought over the same Discord
token. Each new same-token gateway login forces Discord to close the previous
one, which the bot handles as a graceful `bot_shutdown` — so the instances
churned, dropped sessions, and double-posted (Discord rate-limited the
duplicated thread renames).

Three compounding root causes, none of them "the bot is buggy":

- **`pgrep -f c_lord.main` matched the operator's own shell.** When the kill
  command's own command line contains the string `c_lord.main`, `pkill`/`pgrep`
  match the shell running it. `pkill` then killed its own shell mid-command
  (observed as exit code 144), leaving the kill half-done — so the next launch
  stacked an instance on top of survivors.
- **The bot was launched via a session-tracked background runner.** Because the
  daemon never exits, the task never "completes"; it lingers as a child of the
  launching session and can be retried/duplicated, and it dies if that session ends.
- **Repeated launches while kills were incomplete** stacked up. The unfiltered
  pattern also meant a "prod restart" could take down the staging clone, and
  vice-versa.

**Fix:** The prod-restart command in CLAUDE.md is scoped to the prod venv path
(so it can't hit the staging clone), the E2E-restart snippet carries the same
caution, and the operational rules above (kill by explicit PID, count with
`ps`, launch with `setsid -f`) are the standard procedure. Net: one bot per
env, no token churn, no cross-clone collateral.

## 14. Working agreements (how Issues / PRs / evidence are run)

**Decision:** The maintainer's working conduct is codified in three layers, kept
separate so commands stay terse and rationale stays traceable:

1. **CLAUDE.md = commands only** — the "開発の行動規範 (Working Conduct)" section
   plus the Issue/PR/DoD rules. Imperative, short.
2. **`.github` templates = enforcement by form** — the Issue and PR templates make
   each rule a field the author must fill (Why, binary ACs, scope-out, Staging
   Evidence with screenshots, `<details>` for long logs).
3. **This file = the why** — rationale and the conversation that produced each rule.

The agreed rules:

- **Answer the question first; don't silently turn a question into work.** An
  opening question ("why is the menu not showing?", "is this spec or a bug?") is a
  request for a *diagnosis*, not a work order. Diagnose (spec vs bug) from real
  behaviour + the Why, then act once it's agreed.
- **Autonomy applies to agreed work only.** Once "we're fixing X" is settled, the
  agent may self-run through PR → manual QA → merge → prod redeploy → prod
  measurement. The opening question is *not yet* agreed work.
- **Soft requests get "answer → then action", in that order.** Heavy/irreversible
  calls (design changes, destructive ops) get a one-line confirm instead of a guess.
- **No pandering, no taking the user's hypothesis as fact** — verify against real
  data and flag mismatches.
- **Why is the spine of an Issue.** It's read after close to trace intent.
- **Evidence is real, not asserted.** Green tests / "I implemented it" is a
  necessary-but-insufficient condition; "done" requires RED→GREEN reproduced **on
  staging**. Discord-side proof is normally a **user-provided screenshot** (the
  server-side agent has no GUI), supplemented by tmux captures + REST-fetched text.

**Why (what motivated this):**

A week-long review of the maintainer's own Discord messages (~1900, excluding
AI-authored Issue/commit text) surfaced a gap between *what was said* and *what
got recorded*. The records (Issues/PRs/commits) tended to close on "tests green /
done"; the maintainer repeatedly pushed the opposite: mocks-green-but-broke-on-real-hardware
had happened, so "done" must mean reproduced-and-fixed on the real bot, with a
screenshot of the actual Discord behaviour. A second, subtler gap: when the
maintainer asked "why is X happening?", the agent would read code and jump
straight to a fix/PR, skipping the diagnosis the maintainer actually wanted
("is this the intended spec, or a bug?"). A third: some transcript "user"
messages were polished instruction text the maintainer had an AI draft and
pasted — not the maintainer's own intent — so they over-weighted a "stop after
every PR / merge is the user's gate" reading that the maintainer later corrected
(self-run through merge/deploy is fine for now).

**Fix:** Codify the conduct in CLAUDE.md, enforce the artifact fields via the
`.github` templates, and record the rationale here. See CLAUDE.md → "開発の行動規範
(Working Conduct)" and "Definition of Done (DoD)".

## 15. 経路B移行で撤去するのは reply 経路だけ — API サーバ本体と control plane は残す

**決定:** 「経路A→経路B」への移行（#71）で取り除くのは、Claude が Discord に
最終回答を出すための **reply 経路だけ** です。具体的には:

- `POST /api/reply` というエンドポイント
- 各セッションに入れている `discord-reply` skill（`.claude/skills/discord-reply/SKILL.md` の注入）

これだけを撤去します。**API サーバ本体（`c_lord/ext/api_server.py`）と、それ以外の
control plane エンドポイントは残します。** 残すものの例:

| エンドポイント | 役割 |
|---|---|
| `/api/lounge` (GET/POST) | AI Lounge。CLAUDE.md の「Staging bot の占有・解放プロトコル」が依存している |
| `/api/tasks` (POST/GET/DELETE/PATCH) | SchedulerCog へのタスク登録（設計判断 #8 / #9） |
| `/api/spawn` | 外部トリガからスレッドを作って Claude を起動する |
| `/api/mark-resume` | bot 再起動後にセッションを復帰させる |
| `/api/notify` `/api/schedule` `/api/scheduled` | 即時通知・予約通知 |

**この決定は GO 済み（#71、2026-06-04 に経路A→Bへ進めると決定）です。** ただし
「GO」が指すのは reply 経路の置き換えだけで、API サーバ撤去は含みません。

**なぜ切り分けるか:**

- 「経路を A→B にすれば API サーバはいらなくなる」という見立ては、**reply の部分に
  限れば正しい**。でもそこから「API サーバ全体がいらない」へは広げられません。
- reply 経路（A も B も）が運ぶのは **Claude→Discord の最終回答** だけです。
  一方 API サーバは、それとは別に **Claude→c-lord の操作面（control plane）**
  をまるごと持っています（設計判断 #7 / #8 / #9）。スケジュール登録、外部トリガ
  からのスレッド生成、再起動後の復帰、通知、AI Lounge など、reply とは独立した
  機能です。
- 経路B（JSONL transcript の透明ミラー）が置き換えるのは **回答の出し方** であって、
  操作面の代わりにはなりません。だから reply 経路を消しても control plane は残す
  必要があります。
- もし control plane を撤去すると巻き添えになるものが出ます。たとえば `/api/lounge`
  を消すと、CLAUDE.md に書いてある staging bot の占有・解放プロトコル（複数
  セッションの協調）が壊れます。

**スコープの固定:**

- #71 の実装で「既存の skill push を撤去する」ステップは、**撤去対象を
  `/api/reply` 経路（エンドポイント＋skill 注入）だけに限定**します。他の
  エンドポイントには触りません。
- 将来どこかの control plane エンドポイントを撤去・移設したくなった場合は、
  **エンドポイントごとに別の Issue を立てて判断**します。本決定の既定は「残す」です。

参照: #71（経路B 本体）/ #238（このスコープ明確化）/ 設計判断 #7（REST API を
control plane にする）・#8 / #9（SQLite スケジューラ）。

## 16. 読み取りは「push（入力エンリッチ）」で行う — tool 注入はしない

**決定 (2026-06-05, yousan):** 人間が Discord のメッセージリンクを貼った（または
Discord の返信機能で別メッセージに返信した）とき、c-lord は **そのリンク先の本文を
サーバ側（bot token）で取得し、Claude に渡す prompt に verbatim で inline する**
（"push"）。読み取り用の skill / tool / MCP を Claude Code に **注入しない**（"pull"）。
実装は `c_lord/discord_ref.py::enrich_discord_references`、配線は
`claude_chat._handle_new_conversation` / `_handle_thread_reply`。`CLORD_RESOLVE_DISCORD_LINKS`
（既定 on）でゲート。

**背景（解決する問題）:** Claude が別チャンネルを読むとき、c-lord 外の MCP
(`plugin:discord:discord`) を選んで `Missing Access` で諦める事例があった。読み取りを
2つに分けると:
- **reactive**（人間がリンクを貼る）= 本決定の push で解く。
- **proactive**（Claude が自発的に読む）= 別途（読み取り API + CLAUDE.md 誘導）。本決定の対象外。

**なぜ push（tool でなく）か — #71 原則との関係:**
- **tool（注入）= 原則②「c-lord は Claude Code に何も注入しない」へのハード違反**。
  skill/MCP を Claude Code の "中" に置くことになり、利用者の他 skill/plugin と衝突する
  （#259 を止めた理由）。
- **push（入力エンリッチ）= 原則①「透明な I/O ミラー（解釈・編集しない）」へのソフト抵触**。
  人間が打っていない中身を足すため。ただし:
  1. 既存の **ファイル添付inline**（`claude_chat._build_prompt_and_images`）が全く同じ
     「人間のターンを広げる」操作を既に行い、許容されている **前例** がある。
  2. inline した内容は **ペインにも verbatim で出る** ので、原則③「ターミナル=Discord
     等価性」は保たれる。
  3. c-lord は Claude Code の **外側のまま**（入力テキストを太らせるだけ）なので原則②は
     クリア。
  4. reactive を **決定論的** に解決でき、「Claude が経路を選び損ねて諦める」再発余地が
     消える（tool では選択ミスが残る）。

**判定:** 原則①への抵触は **添付inline と同列の許容範囲** と判断し、push を採用する。
ただし境界を守る:
- **verbatim のみ**（要約・解釈をしない）、件数・文字数の **ハード上限**、重複は dedupe。
- inline するのは **人間が参照したもの**だけ。取得失敗は `[c-lord could not fetch …]` の
  明示ノートにする（黙って欠落させない）。
- token は prompt にもファイルにも一切出さない（discord.py が内部で auth を運ぶ）。

参照: #318（実装）/ #259（読み取り経路の設計）/ #71（経路B の3原則）/ #234（アクセス方針）。

## 17. Loop engineering — c-lord は「いつ/どこで/どう見えるか」を持ち、ループの中身は Claude が持つ

**決定 (2026-07-14, #489):** c-lord における loop engineering（観測→判断→行動→再観測を
自走で回し続ける作り方）の役割分担を、単一の設計方針として固定する。**c-lord が持つのは
ループの〈いつ (when) / どこで (where) / どう見えるか (visibility) / 状態の永続〉だけ**で、
**〈何を (what) / いつ止めるか / dedup 判断〉は Claude 側**（プロンプト・`/loop`）に置く。

これは design decision #7（No Custom AI Logic）と #9（Claude=what / c-lord=when）を
loop engineering 全体に一般化したもの。真ん中の判断を c-lord に持たせたくなったら理念違反の
サイン。可視化（理念 B）は自走するほど命綱になるため最優先。

**全文・境界の表・既存資産マップ（scheduler / webhook / spawn / reply / fuzz / monitor /
`/loop`）・ギャップ・次の一手・理念(A/B/C)整合チェックは
[`docs/LOOP_ENGINEERING.md`](./LOOP_ENGINEERING.md) を参照。**

参照: #489（本ノート）/ #7・#9（下敷きの設計判断）/ #90（SchedulerCog）/ #377（fuzz）/ #404（monitor）。

## 18. 配信経路は1つだけ — 選べる状態そのものをやめる（#712）

**決定 (2026-09-08, yousan):** 「どちらかを選べる」をやめる。JSONL transcript ミラー
（経路B, #71/#216）が**唯一の配信経路**で、skill push（経路A, #53）と、それを選ぶための
`CLORD_BRIDGE_MODE` / `USE_SKILL_REPLY` は**削除**した。

**なぜ「既定を jsonl にする（#492）」で足りなかったか:**

- 経路A の失敗モードは「**Claude が投稿を忘れるとターンが丸ごと届かない**」(#491)。
  それを消すために移行したのに、legacy opt-in が残っている限り env 1行で復活できる。
  踏める地雷を置いておく理由がない。
- 実測（2026-09-08 の棚卸し）で **全6環境（本番・staging 4台・babeln）が jsonl**。
  選択肢はコードにあるだけで、選んでいる環境はゼロだった。
- そのゲート（`skills_enabled()`）が **REST API サーバの起動条件を兼ねていた**ため、
  既定構成では `/api/spawn` `/api/tasks` が丸ごと使えなかった (#543)。分岐を消すことが
  そのまま #543 の修正になる。

**この決定に含まれるもの / 含まれないもの:**

- 撤去した: `discord-reply` / `discord-prompt-choice` の **SKILL.md 注入**と、その
  テンプレート、bridge mode 分岐、#67 の「skill を呼ばなかった」フォールバック通知。
- **残した**: `POST /api/reply` と `POST /api/prompt-choice` の**エンドポイント**。
  §15 は #71 のスコープとしてエンドポイントごと消すと書いていたが、これらは外部から
  スレッドへ投稿する control plane としても使える。**注入をやめれば失敗モードは消える**
  ので、エンドポイント撤去は別の判断として切り出す（既定は §15 どおり「残す」）。
- **残した**: `discord-read` skill (#259)。Discord の API を直接叩く読み取り専用の道具で、
  配信とは無関係（`injector.py` に同じ注意書きがある）。
- **古い session dir の掃除**: 経路A 時代に注入された SKILL.md がディスクに残っていると、
  API サーバが常時起動した今、Claude がそれに従って**回答を二重に届けてしまう**。
  そこで毎ターン `remove_legacy_skills()` で無条件に掃除する。
- **黙って挙動を変えない**: `.env` に旧 env を残したまま更新した利用者には、起動時に
  警告を出して jsonl で動く（`c_lord/legacy_env.py`、Zero-Config Principle）。

**スコープ外（この決定では直さない）:** 経路B に移した結果の等価性の穴 — 添付ファイル
(#233) と reply 層の装飾（quote-reply / cli-prefix / prompt-choice, #237）。逃げ道を
消したぶん優先度は上がるが、独立した bug として別に追う。

参照: #712（本決定）/ #543（API 起動条件）/ #53・#71・#216・#492（経緯）/ #491（経路A の失敗モード）/ §15。
