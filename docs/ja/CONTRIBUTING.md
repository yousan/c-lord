# c-lord へのコントリビューション

[English](../../CONTRIBUTING.md) · 日本語

c-lord は公開の場で開発していて、コードの大半は、メンテナのレビューのもとで Claude Code が Issue から書いています。人からの貢献も、AI エージェントからの貢献も歓迎します。どちらも通る道は同じです。Issue、ブランチ、プルリクエスト、そして変更が実際に動くことの証明です。

> **CI が緑なのは必要条件であって、十分条件ではありません。** テストのジョブとは別に、
> `dod-gate` という必須チェックが **PR 本文**を読みます。PR テンプレートを使わない PR は、
> テストが全部通っていても落ちます。PR を開く前に、下の「PR をマージするのに必要なもの（dod-gate）」を読んでください。

## コードを書く前に

- **Issue から始めます。** `.github/ISSUE_TEMPLATE/` のテンプレートで Issue を立てるか、すでにあるものを選びます。Issue には、その変更が*なぜ*必要なのかを Discord で c-lord を使う人の目線で書き、真か偽かがはっきり決まる **Acceptance Criteria** を並べます。プルリクエストはその条件に照らして確認されるので、1 つの Issue が扱うのは 1 つのことだけです。バグ修正と、それと関係のない改善は、別々の Issue にします。
- **「これはバグ？ それとも仕様？」** まず [docs/specs/](../specs/README.md) を見てください。各ページに、1 つの機能がどう動くべきかが書いてあります。該当するページが無ければ、いまのコードから答えを読み取るのではなく、「決まりが無い」と Issue に書いてください。プロジェクトの考え方は [docs/PHILOSOPHY.md](../PHILOSOPHY.md) にあります。
- **c-lord はフレームワークであって、誰かの bot ではありません。** c-lord を動かす人なら誰にでも役立つ機能は、ここに入れます。トークン、特定のサーバーに固有のロジック、個人のワークフローは、c-lord を使う側の bot のリポジトリに置きます。
- **使う人に配線をさせません。** 新しい機能は妥当な設定で最初からオンにし、コンストラクタに足す引数には、これまでの呼び出しがそのまま動く既定値を付けます。パッケージを更新した人が、自分のコードに触らずにその機能を使えるようにします。

## Issue からマージまで

```text
Issue  →  main からブランチ  →  失敗するテスト、それからコード  →  プルリクエスト
       →  staging の bot で動くことを示す  →  セルフレビュー  →  squash マージ
```

1. **`main` からブランチを切ります。** 自分の fork で（push できる人はこのリポジトリで）作業します。`main` に直接 push する人はいません。ブランチ名は、することが分かるように付けます: `feature/…`、`fix/…`（たとえば `fix/issue-123`）、`docs/…`。
2. **先にテストを書きます。** 実行して失敗するのを確かめてから、それを通すコードを書きます。失敗を示す行は取っておいてください。プルリクエストに貼ります。
3. **push する前に、手元でチェックを流します。** 必須の `test` ジョブが実行するものと同じです。

   ```bash
   uv run ruff check c_lord/
   uv run ruff format --check c_lord/
   uv run pyright c_lord/
   uv run pytest tests/ -v --cov=c_lord
   ```

4. **動きを変えたら、その説明も同じプルリクエストで直します。** 利用者に見えるものが変わるなら、それを説明しているページ（`docs/specs/`、`README.md`、`docs/` のガイド）も一緒に変えます。利用者に見えるものが何も変わらないなら、プルリクエストの本文に `no-user-visible-change` と書きます。
5. **PR テンプレートを使って、`main` に向けたプルリクエストを開きます。** テンプレートの節はどれも消しません。Issue からコピーした Acceptance Criteria の全項目、利用者から見た 1 行の before/after、`## Staging Evidence`、`## Definition of Done checklist` です。
6. **必須のチェックが 2 つ走ります。** `test (3.10/3.11/3.12)`（各 Python バージョンでの ruff・pyright・pytest）と、プルリクエストの本文を読む `dod-gate` です（[下の節](#pr-をマージするのに必要なものdod-gate)）。
7. **自分の差分を読み返します**（`git diff main`）。この変更に関係のないものは取り除きます。
8. **メンテナが squash マージするのは、[Definition of Done](../../CLAUDE.md#definition-of-done-dod--single-source-of-truth) の全項目にチェックが付いたときです。** CI が緑なだけでは足りません。テストはモックを相手にしているので、本物の Discord のスレッドと本物の tmux のペインで変更が動くことまでは示せないからです。

コミットメッセージは `<type>: <説明>` の形で、type は `feat`・`fix`・`refactor`・`docs`・`test`・`chore`・`security` のいずれかです。

## セットアップ

```bash
git clone https://github.com/yousan/c-lord.git
cd c-lord
uv sync --dev
make setup          # clone ごとに 1 回
```

`make setup` は、git が `.githooks/` のフックを使うようにします。以後はコミットのたびに、ステージした Python ファイルが ruff で整形され、lint のエラーが残っているとコミットが通りません。実行しなければ、CI が走るまで誰もあなたのコードを確かめません。その clone がセットアップ済みかどうかは `make check-setup` で分かります。

必要なのは、Python 3.10 以上、[uv](https://docs.astral.sh/uv/)、そして本物の tmux を動かすテストのための `tmux` です。

## テスト

```bash
uv run pytest tests/ -v --cov=c_lord
```

- **新しい機能にもバグ修正にも、コードより先に書いたテストが付きます。** テストのファイルは `tests/` に置きます。
- 動いている bot と本物の Discord のチャンネルが要るテストは `e2e` の印が付いていて、普通の実行には含まれません。`.env` に認証情報があれば、`uv run pytest -m e2e tests/e2e/ -v` で実行できます。
- **ターミナルのプロンプトを c-lord が見分ける部分**（`_has_permission_prompt`、`_is_yn_prompt`、`_has_unknown_interactive`）**を直すとき**は、まず、うまくいかなかったペインの実際のキャプチャを `tests/fixtures/panes/` に足します。修正を書くのはそのあとです。
- **テストで本物の tmux を動かすときは、専用のサーバーを使います**（`tmux -L <ソケット名>`）。1 台のホストの c-lord のセッションは、すべて既定の tmux サーバーを共有しています。それを落とすと全部が止まります（[docs/STAGING.md](../STAGING.md)）。

## コードの決まり

- 整形と lint は ruff に任せます（`uv run ruff format c_lord/` で整形されます）。1 行は 100 文字までです。
- すべての関数のシグネチャに型ヒントを付け、どのファイルも `from __future__ import annotations` で始めます。コードは Python 3.10 で動かなければなりません。
- 名前: ファイルと関数は `snake_case`、クラスは `PascalCase`、定数は `UPPER_SNAKE_CASE`、非公開のものは頭に `_`。
- スレッド・セッション・タスク・チャンネルのどれかが分かっている場面では、`log_ctx()`（`c_lord/utils/logger.py`）を通してログを書きます。`grep "thread=<id>"` で一連の流れを追えるようにするためです。c-lord が何かを無視すると決めたときは、DEBUG に隠さず、INFO に残します。
- **セキュリティは省けません。** c-lord は誰かのマシンでコマンドを実行するからです。プロセスは `asyncio.create_subprocess_exec` に引数のリストを渡して起動し、シェルは決して通しません。プロンプトの前には `--` を置き、セッション ID とスキル名はコマンドラインに渡す前に検証します。`c_lord/claude/tmux_runner.py`、`c_lord/cogs/_run_helper.py`、あるいはどれかの Cog を変えたときは、コミットの前に `.claude/skills/security-audit/SKILL.md` のチェックリストを通してください。

## どこに何があるか

- `c_lord/claude/` — tmux のペインでターンを走らせる部分、CLI の設定、イベントの型
- `c_lord/cogs/` — Discord.py の Cog（chat、skill コマンド、webhook トリガー、自動アップグレード）
- `c_lord/transcript/` — Claude Code 自身の会話記録を読み、スレッドに投稿する部分
- `c_lord/database/` — SQLite セッションおよび通知の永続化
- `c_lord/discord_ui/` — Discord に出るもの: 状態ランプ、質問のボタン、進捗の 1 行、embed
- `c_lord/ext/` — オプション拡張（REST API サーバー — aiohttp が必要）
- `c_lord/tmux.py`、`c_lord/session_dir.py` — tmux のウィンドウと、スレッドごとの git clone
- `tests/` — pytest のテスト一式。`docs/specs/` — 各機能がどう動くべきか

全体像は [README](README.md#アーキテクチャ) に、詳細は [docs/ARCHITECTURE.md](../ARCHITECTURE.md) にあります。

## PR をマージするのに必要なもの（dod-gate）

唯一の正は `CLAUDE.md` の **[Definition of Done](../../CLAUDE.md#definition-of-done-dod--single-source-of-truth)** で、PR テンプレート（`.github/pull_request_template.md`）はその写しです。この節は、そのうち機械が強制する部分の説明です。

`dod-gate` は `main` の**必須ステータスチェック**です。読むのは **PR 本文とラベルだけ**（コードは見ない）で、`dod-gate` が赤い PR は誰もマージできません（ブランチ保護は管理者にも効く）。次のときに落ちます:

| `dod-gate` が落ちる条件 | 対象 | 直し方 |
|---|---|---|
| 本文に `## Definition of Done checklist` の節が無い | **すべての PR**（ラベルに関係なく） | PR テンプレートを使い、その見出しをそのまま残す |
| そのチェックリストに未チェックの項目（`- [ ]`）がある | 免除ラベルの無い PR | その項目をやってチェックする |
| 本文に証跡が無い: 画像（`![...](URL)`）・`<img>`・Release アセット URL・GitHub 添付 URL のどれも無い | 免除ラベルの無い PR | スクショを貼る — 下の「証跡（スクショ）と staging」を参照 |
| `Closes` / `Fixes` / `Resolves #N` を使っているのに、`## Acceptance Criteria` の節が無い（`###` 見出しは数えない）か、未チェックの項目がある | **すべての PR**（ラベルに関係なく） | Issue の Acceptance Criteria を**全部**コピーしてチェックする — または `Refs #N` にして Issue を開いたままにする |

**免除ラベル** — `documentation` か `no-runtime-change` が付いていると、上の表のチェックリストと証跡の条件が免除されます（Definition of Done 上も、TDD の証跡と staging 検証が免除）。ドキュメント・CI/ツール・挙動を変えないことが明らかなリファクタに使います。`## Definition of Done checklist` の見出しと `Closes` の規律は**免除されません**。ラベルを付けるにはこのリポジトリへの書き込み権限が要るので、無い場合は PR でメンテナに頼んでください。

ほかに知っておくと良いこと:

- `dod-gate` は PR 本文を編集したり、ラベルが変わったりするたびに再実行されます — コミットし直す必要はありません。
- `Closes #N` は Issue の Acceptance Criteria を **100%** 満たすときだけ使い、**独立した行**に書きます（`- ` の箇条書きの中だと GitHub が拾わないことがある）。
- 判定ロジックは `.github/scripts/dod_gate.js`（テスト: `tests/test_dod_gate.py`）です。

### 証跡（スクショ）と staging

バグ修正と機能追加の PR は、`## Staging Evidence` に **RED**（変更前に問題を再現）と **GREEN**（変更後に消えた）を示し、**スクショを主証跡**にします — テキストログだけでは通りません。

- **メンテナの作り方**: `scripts/discord_evidence_shot.sh` で Discord の実画面を撮り、`scripts/evidence_upload.py red.png green.png --issue <N>` でアップロードして、出力された URL を貼ります。詳細は [docs/discord-evidence-capture.md](../discord-evidence-capture.md) にあります。画像をリポジトリに commit してはいけません。Discord CDN の URL は期限切れになるので、直接貼らないでください。
- **この2つのスクリプトはメンテナの環境が前提**です: `discord_evidence_shot.sh` はログイン済みのキャプチャ用アカウントがある bot ホストで動き、`evidence_upload.py` はこのリポジトリへの書き込み権限が要ります。**外部の貢献者**は、代わりにスクショを PR 本文へドラッグ&ドロップしてください — そのとき付く GitHub の添付 URL で `dod-gate` を通ります。
- **staging**（`bash scripts/staging.sh borrow` → `restart` → `release`、[docs/STAGING.md](../STAGING.md) 参照）はメンテナのホストで動くので、外部の貢献者はいまは実行できません。`## Staging Evidence` にそう書き、ローカルで何を確認したかを書いてください。Definition of Done はマージ前の staging RED→GREEN を求めるので、その部分は結果的にメンテナ側で行うことになります。外部の貢献者が staging と証跡をどう満たすかは**まだ決まっていません** — [#765](https://github.com/yousan/c-lord/issues/765) を参照。

## バージョンとリリース

バージョン番号を自分で書くことはありません。バージョンは git のタグ（`vX.Y.Z`）から決まり、`pyproject.toml` には書いてありません。

- **パッチリリースは自動です。** `main` にマージされたプルリクエストごとに、次のパッチのタグと GitHub Release が作られます。例外は、コミットの件名が `docs:` で始まるか、`[skip-release]` または `[skip ci]` を含むマージで、このときはリリースされません。
- **マイナーとメジャーのリリースは、メンテナが切ります。** プルリクエストで `CHANGELOG.md` を更新したあと、`scripts/release.sh` を使います。

## Cog を追加する

1. `c_lord/cogs/your_cog.py` を作成します
2. Claude を動かすなら、実行のロジックを複製しません。`RunConfig` を組んで `_run_helper.run_claude_with_config()` を呼びます。`run_claude_in_thread()` は同じ処理への互換の入口として残してあります
3. `c_lord/cogs/__init__.py` からエクスポートします
4. `c_lord/__init__.py` のパブリック API に追加します
5. `setup_bridge()`（`c_lord/setup.py`）に登録します。c-lord を使っている bot が、コードを変えずにその Cog を使えるようにするためです
6. テストを `tests/test_your_cog.py` に書きます

同じ手順のもっと詳しい説明が `.claude/skills/add-cog/SKILL.md` にあります。

## AI エージェントの方へ、エージェントと作業する方へ

c-lord の変更のほとんどはエージェントが行っているので、指示書はすでにエージェント向けに書いてあります。[CLAUDE.md](../../CLAUDE.md) がプロジェクトの説明書で（`AGENTS.md` は同じファイルです）、`.claude/skills/` には、テストを先に書く開発、コミット前のチェック、セキュリティ監査の手順があります。エージェントのプルリクエストにも、上とまったく同じ基準が当てはまります。テストも、証跡も、Definition of Done も、誰がコードを打ったかは問いません。
