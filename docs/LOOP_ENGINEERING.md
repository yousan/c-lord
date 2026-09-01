# Loop engineering — c-lord での自走ループの設計方針

> これは設計方針。理念 [`docs/PHILOSOPHY.md`](./PHILOSOPHY.md) の下位。迷ったら理念に照らす。
> 「なぜ存在するか」は理念、「各機能がどう動くべきか」は [`docs/specs/`](./specs/README.md)、
> 「どう作るか（横断的な作り方）」がここ（[`DESIGN_DECISIONS.md`](./DESIGN_DECISIONS.md) と同列）。

---

## 定義（一文）

**c-lord における loop engineering とは、「観測 → 判断 → 行動 → 再観測」を人手を介さず自走で回し続ける作り方であり、c-lord はその〈いつ・どこで・どう見えるか〉だけを持ち、ループの〈何を・いつ止めるか〉は Claude 側に置く。**

一発プロンプト（1回投げて1回返る）に対して、loop engineering は「回り続ける」ことが本体。c-lord はブリッジなので、回すエンジン（判断ロジック）ではなく、**回す土台と、回っている様子が Discord から判断できる可視化**を担う。

---

## Why（なぜ明文化するか）

c-lord には既にループ的資産が**断片的に**増えている（下の資産マップ参照）。だが「これは loop engineering の一部か」「c-lord に足すべきか、それとも Claude 側か」を判断する**共通の基準が無い**。

基準が無いままループ機能を足すと、理念の「**AI のロジックを持たない**」（[PHILOSOPHY.md やらないこと](./PHILOSOPHY.md#やらないこと)、[design decision #7](./DESIGN_DECISIONS.md)）を踏み越えて、停止条件や dedup といった「ループの中身」を c-lord に埋め込む事故が起きやすい。逆に、可視化（理念 B）を怠ると「自走しているが誰も様子が分からない」ブラックボックスになる — これは**自走するほど致命的**になる（人が横で見ていない前提だから）。

だから、境界と資産マップと可視化要件を**先に文章で固定**し、後続の作業（プロダクト機能の強化 / 自身の開発・QA での活用）を理念に照らして判断できるようにする。

---

## 境界（一番大事）

loop engineering における役割分担。**この線を踏み越えたら理念に反する。**

| ループの構成要素 | 持ち主 | c-lord での具体 |
|---|---|---|
| **いつ回すか（when）** | **c-lord** | cron / interval（`SchedulerCog`）、イベント起点（`webhook_trigger`）、Claude 自走（`/loop`）の受け皿 |
| **どこで回すか（where）** | **c-lord** | fresh thread 生成（`/api/spawn`）、tmux window、thread=session の対応 |
| **どう見えるか（visibility）** | **c-lord** | 最終回答（`/api/reply`）、状態リアクション 🟢🟡❌、thread lamp、異常報告（monitor） |
| **状態の永続・継続** | **c-lord** | `scheduled_tasks`（SQLite）、`--resume`、bot 再起動をまたぐ継続 |
| **何をするか（what）** | **Claude** | プロンプト本体、ツールの選択、ドメインロジック |
| **いつ止めるか（停止条件）** | **Claude** | 「N 回連続グリーンで停止」「異常0なら黙る」などの判断 |
| **重複をどう省くか（dedup）** | **Claude** | 「既に報告済みか」の fingerprint 判断（c-lord は保存場所を貸すだけ） |

これは既存の設計判断の延長:

- **[design decision #7 — No Custom AI Logic](./DESIGN_DECISIONS.md)**: c-lord はプロンプト・ツール定義・メモリ・system prompt を持たない。
- **[design decision #9 — Claude が what、c-lord が when](./DESIGN_DECISIONS.md)**: scheduled task で確立済みのこの分担を、loop engineering 全体の原則として一般化したのが上の表。

**踏み越え禁止線の例:**

- ✕ c-lord に「3回失敗したら止める」ロジックを実装する → 停止条件は Claude 側（プロンプト / `/loop`）が持つ。c-lord が持ってよいのは「最大実行回数の**上限ガード**（暴走防止の安全弁）」まで。
- ✕ c-lord に「GitHub の CI が緑かどうか判定する」コードを入れる → ドメイン判断は Claude。c-lord は「いつ起こすか」だけ。
- ○ c-lord に「ループの現在状態（何回目・次いつ・前回結果）を Discord から見えるようにする」を足す → これは可視化＝理念 B なので c-lord の役目。

---

## ループの解剖（各フェーズを既存部品にマップ）

「観測 → 判断 → 行動 → 再観測」の1周を、c-lord のどの部品が支えるか。

```
        ┌─────────────── c-lord が持つ（土台・可視化） ───────────────┐
        │                                                              │
 [when] トリガ              [where] 実行場所           [visibility] 見える化
   cron/interval   ──▶   fresh thread (/api/spawn)  ──▶  状態リアクション 🟢🟡
   (SchedulerCog)         tmux window                    最終回答 (/api/reply)
   webhook (event)        thread = session              thread lamp / monitor 報告
   /loop (Claude自走)                                    エラー時の❌
        │                        │                             │
        └────────────┐          │          ┌──────────────────┘
                      ▼          ▼          ▼
        ┌──────────── Claude が持つ（中身） ────────────┐
        │  [what] 何を観測し、何をするか（プロンプト）      │
        │  [判断] 停止条件・次にどうするか                  │
        │  [dedup] 既にやった/報告したか                    │
        └──────────────────────────────────────────────┘
                      │
                      ▼
        [状態] c-lord が永続化: scheduled_tasks(SQLite) / --resume / 再起動継続
                      │
                      └──▶（次の周回へ：when が再び発火）
```

ポイント: **1周の中で c-lord は入口（when/where）と出口（visibility）と記憶（状態）を持ち、真ん中の判断は必ず Claude を経由する。** 真ん中を c-lord に持たせたくなったら、それは理念違反のサイン。

---

## 既存資産マップ

c-lord に既にあるループ的資産。「プロダクト側（利用者のループ）」と「開発・QA 側（c-lord 自身のループ）」に分ける。

### プロダクト側 — 利用者が回すループの土台

| 資産 | 種類 | いつ回る | 担うフェーズ | 参照 |
|---|---|---|---|---|
| `SchedulerCog` | Cog（30秒マスタループ＋SQLite動的タスク） | cron / interval | when・状態 | #90, design #7–9 |
| `webhook_trigger` | Cog | イベント（CI/CD webhook 着弾） | when（イベント起点） | design #10 |
| `/api/spawn` | REST | 呼ばれた時 | where（fresh thread 生成） | `ext/api_server.py` |
| `/api/tasks`（CRUD） | REST | Claude が登録/更新 | when の宣言 | design #7 |
| `/api/reply` | REST | 各ターン末尾 | visibility（最終回答） | #53 |
| 状態リアクション 🟢🟡❌ | discord_ui | 毎ターン | visibility | #246 |
| thread lamp / thread name | thread_state_sync | poll | visibility（一覧の俯瞰） | #95, #241 |

### 開発・QA 側 — c-lord 自身をループで守る

| 資産 | 種類 | いつ回る | 担うこと | 参照 |
|---|---|---|---|---|
| fuzz harness `scripts/fuzz/` | cron スクリプト | 毎時 | LLM 生成 adversarial 入力を staging に撃つ → `#fuzz-report` | #377, [spec](./specs/fuzz-harness.md) |
| traffic monitor `scripts/monitor/` | cron スクリプト | 例10分毎 | 実トラフィックの異常を read-only スキャン → 報告 | #404, [spec](./specs/monitor.md) |
| staging lease（borrow/release） | `scripts/staging.sh` | 検証のたび | 共有 staging の占有制御（QAループの前提） | [STAGING.md](./STAGING.md) |

### Claude 側の primitive（c-lord の外だが loop engineering の相棒）

- `/loop` — プロンプト/スラッシュコマンドを間隔 or 自ペースで繰り返す（判断・停止条件はここ）
- `/schedule` — cron のクラウドエージェント（routine）

**観察: プロダクト側は「土台はあるが、ループらしく体系化されていない」。開発・QA 側の方が（fuzz/monitor で）先に loop engineering が形になっている。**

---

## ギャップ（体系化されていない点）

現状で「基準に照らすと欠けている」もの。次の一手の入力。

1. **ループ状態が Discord から判断できない（理念 B のギャップ）** — 「このループは今何周目か／次いつ回るか／前回の結果は／なぜ止まったか」を利用者が見る手段が薄い。自走するほど命綱になる可視化がここに無い。
2. **停止条件・backoff・最大回数の“形”が無い** — 停止判断は正しく Claude 側だが、c-lord 側に「暴走防止の上限ガード」「失敗時のバックオフで再発火間隔を延ばす」という**安全弁の primitive** が無い（今は間隔固定）。
3. **dedup surface が各自実装** — fuzz と monitor がそれぞれ「報告済み fingerprint」を自前で持つ。dedup 判断は Claude の役目でよいが、**保存場所（state）の貸し出し**は共通化できる余地がある。
4. **エラー時の可視化が弱い** — ループの1周がコケた時に Discord にどう出るか（❌ は付くが「どのループが・何周目で・なぜ」が追いにくい）。
5. **占有プロトコルが QA 側だけ** — staging lease（borrow/release）はプロダクト側の「同じループを二重に回さない」には効いていない（`SchedulerCog._running` の in-memory ガードのみ、再起動をまたがない）。

---

## 次の一手（follow-up 候補 / 着手は別合意）

このノートがマージされたら、下を別 Issue に切り出す。**優先度は理念 B（判断できる可視化）を最優先に置く。**

| 優先 | 方向 | 候補 | 効く理念 | 対応ギャップ |
|---|---|---|---|---|
| P1 | プロダクト | ループ状態を Discord から見えるようにする（周回数/次回/前回結果/停止理由） | B | 1, 4 |
| P2 | プロダクト | 安全弁 primitive（最大回数の上限ガード＋失敗時 backoff）を `SchedulerCog` に追加。停止“判断”は Claude のまま | A, C | 2, 5 |
| P3 | 開発・QA | fuzz / monitor の state（fingerprint保存）を共通ヘルパーに寄せる | C | 3 |
| P3 | 開発・QA | PR babysit / staging RED→GREEN 自動検証 / bot self-heal を `/loop`+monitor で回す | B | 4 |

> これは提案であって着手指示ではない。各項目は独立 Issue（1 Issue = 1 concern）にし、Why・AC・スコープ外を明記してから始める。

---

## 理念（A/B/C）との整合チェック

loop engineering を進めるとき、各手が理念を損なわないかを都度この表に照らす。

| 理念 | loop engineering での意味 | 損なう危険 |
|---|---|---|
| **A（切れない・どこからでも・複数を文脈ごと並行）** | ループは常駐 server 上で回り、回線が切れても生き続ける。複数ループを開いたまま俯瞰できる | ループを増やして俯瞰性が落ちると A を損なう。増やす前に「一覧で見渡せるか」を確認 |
| **B（結果が判断できる形で見える）** | **loop engineering の生命線。** 自走＝人が横で見ていない → 「今どうなっているか・止まった理由」が Discord で判断できて初めて成立 | 可視化なしで自走させると B が崩壊。ギャップ1・4 を優先する理由 |
| **C（設定不要で予想どおりに動く OSS 土台）** | ループ primitive は zero-config（既定オン・後方互換の既定値）。利用者はパッケージ更新だけで恩恵を受ける | 利用者に配線を要求するループ機能は C 違反（[Zero-Config Principle](../CLAUDE.md)）。設計が間違っているサイン |

**「やらないこと」との整合:** ループの中身（プロンプト・停止条件・dedup 判断）を c-lord に持たせない。持たせたくなったら [`境界`](#境界一番大事) の表に照らして Claude 側へ戻す。

---

## 根拠

- 本ノートの起点: Issue #489（loop engineering の設計方針明文化）
- 境界の下敷き: [design decision #7（No Custom AI Logic）](./DESIGN_DECISIONS.md)、[#9（Claude=what / c-lord=when）](./DESIGN_DECISIONS.md)
- 既存資産: `SchedulerCog`(#90)、`webhook_trigger`(design #10)、fuzz harness(#377)、traffic monitor(#404)
- 上位: 理念 [`docs/PHILOSOPHY.md`](./PHILOSOPHY.md)（核・A/B/C・やらないこと）
