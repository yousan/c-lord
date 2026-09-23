> **Note:** This is an auto-translated version of the original English documentation.
> If there are any discrepancies, the [English version](../../CONTRIBUTING.md) takes precedence.
> **注意:** これは英語のオリジナルドキュメントを自動翻訳したものです。
> 内容に相違がある場合は、[英語版](../../CONTRIBUTING.md)が優先されます。

# c-lord へのコントリビューション

コントリビューションに興味を持っていただきありがとうございます！このプロジェクトは Claude Code によって構築されており、人間と AI エージェント両方からのコントリビューションを歓迎します。

> **CI が緑なのは必要条件であって、十分条件ではありません。** テストのジョブとは別に、
> `dod-gate` という必須チェックが **PR 本文**を読みます。PR テンプレートを使わない PR は、
> テストが全部通っていても落ちます。PR を開く前に、下の「PR をマージするのに必要なもの（dod-gate）」を読んでください。

## ブランチワークフロー

シンプルな PR ベースのワークフローである **GitHub Flow** を使用しています:

```
main（常にリリース可能）
  ├── feature/add-xxx   → PR → CI + dod-gate 緑 → DoD 全チェック → マージ
  ├── fix/issue-123     → PR → CI + dod-gate 緑 → DoD 全チェック → マージ
  └── （main への直接プッシュは禁止）
```

### 手順

1. **Issue から始める** — `.github/ISSUE_TEMPLATE/` のテンプレートで Issue を立てる（または既存の Issue を選ぶ）。その **Acceptance Criteria** が PR の合否の基準になる
2. リポジトリを **Fork**（書き込み権限がある場合はブランチを作成）
3. `main` から**ブランチを作成**:
   ```bash
   git checkout -b feature/your-feature-name
   ```
4. **変更を加える** — 先に失敗するテストを書き、それからコードを書く（TDD）
5. ブランチを **Push** して、**PR テンプレートを使って** `main` に対して **PR を開く** — `## Definition of Done checklist` を含め、どの節も消さない
6. **2つの必須チェックが自動実行される**: `test (3.10/3.11/3.12)`（ruff・pyright・pytest）と `dod-gate`（PR 本文を見る — 下の節を参照）
7. メンテナがマージするのは、**[Definition of Done](../../CLAUDE.md#definition-of-done-dod--single-source-of-truth) の全項目がチェックされたときだけ**。CI が緑というだけではマージしない — CI はモックのテストなので、実際の Discord / tmux で動くことまでは示せない

### ブランチ命名

- `feature/description` — 新機能
- `fix/description` または `fix/issue-123` — バグ修正
- `docs/description` — ドキュメントのみ
- `refactor/description` — 動作変更なしのコード整理

## 開発環境のセットアップ

```bash
git clone https://github.com/yousan/c-lord.git
cd c-lord
uv sync --dev
make setup   # git hooks を登録（クローン後に一度だけ実行）
```

> **`make setup` は必須です** — 新しくクローンするたびに実行してください。`.githooks/` の pre-commit hook を有効化し、ステージされた Python ファイルの自動フォーマットと lint を行います。
> 実行しないと hook が動作せず、不正なコードがローカルで通過してしまいます（CI では検出されますが、予期せぬビルド失敗に驚くことになります）。
>
> `make check-setup` をいつでも実行して、環境が正常かどうか確認できます。

## テストの実行

```bash
uv run pytest tests/ -v --cov=c_lord
```

PR を提出する前にすべてのテストが通過している必要があります。

## コードスタイル

- **フォーマッター**: `ruff format`
- **リンター**: `ruff check`
- **型ヒント**: すべての関数シグネチャに必須
- **Python**: 3.10+（モダンな構文のために `from __future__ import annotations` を使用）

```bash
uv run ruff check c_lord/
uv run ruff format c_lord/
```

## プロジェクト構造

- `c_lord/claude/` — Claude Code CLI との連携（runner、parser、types）
- `c_lord/cogs/` — Discord.py の Cog（chat、skill コマンド、webhook トリガー、自動アップグレード）
- `c_lord/database/` — SQLite セッションおよび通知の永続化
- `c_lord/discord_ui/` — Discord UI コンポーネント（status、chunker、embeds）
- `c_lord/ext/` — オプション拡張（REST API サーバー — aiohttp が必要）
- `tests/` — pytest テストスイート

## 変更の提出

1. Issue から始め、リポジトリを Fork してフィーチャーブランチを作成
2. 先にテストを書き（変更前は失敗すること）、それからコードを書く
3. プッシュ前にローカルで実行 — 必須の `test` ジョブと同じチェック:
   ```bash
   uv run ruff check c_lord/
   uv run ruff format --check c_lord/
   uv run pyright c_lord/
   uv run pytest tests/ -v
   ```
4. **PR テンプレートを使って** PR を開き、埋める: Issue の Acceptance Criteria を全部コピー、1行の before/after、`## Staging Evidence`、`## Definition of Done checklist`
5. メンテナがマージするのは、2つの必須チェック（`test (3.10/3.11/3.12)` と `dod-gate`）が緑で、Definition of Done の全項目がチェックされてから

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

- **メンテナの作り方**: `scripts/discord_evidence_shot.sh` で Discord の実画面を撮り、`scripts/evidence_upload.py red.png green.png --issue <N>` でアップロードして、出力された URL を貼る。詳細は [docs/discord-evidence-capture.md](../discord-evidence-capture.md)。画像をリポジトリに commit しない。Discord CDN の URL は期限切れになるので直貼りしない。
- **この2つのスクリプトはメンテナの環境が前提**です: `discord_evidence_shot.sh` はログイン済みのキャプチャ用アカウントがある bot ホストで動き、`evidence_upload.py` はこのリポジトリへの書き込み権限が要ります。**外部の貢献者**は、代わりにスクショを PR 本文へドラッグ&ドロップしてください — そのとき付く GitHub の添付 URL で `dod-gate` を通ります。
- **staging**（`bash scripts/staging.sh borrow` → `restart` → `release`、[docs/STAGING.md](../STAGING.md) 参照）はメンテナのホストで動くので、外部の貢献者はいまは実行できません。`## Staging Evidence` にそう書き、ローカルで何を確認したかを書いてください。Definition of Done はマージ前の staging RED→GREEN を求めるので、その部分は結果的にメンテナ側で行うことになります。外部の貢献者が staging と証跡をどう満たすかは**まだ決まっていません** — [#765](https://github.com/yousan/c-lord/issues/765) を参照。

## バージョニング

このプロジェクトは自動バージョニングを採用しているため、**通常のコントリビューションではバージョンを手動で変更する必要はありません。**

- **自動パッチバンプ**: `main` にマージされた PR ごとにパッチバージョンが自動的にインクリメントされ（例: `1.3.0` → `1.3.1`）、GitHub Release が作成されます。
- **手動マイナー/メジャーリリース**: `1.4.0` などのマイナー/メジャーリリースを切る場合は、`pyproject.toml` と `CHANGELOG.md` を手動で更新し、PR タイトルに `[release]` を含めます。これによりパッチバンプなしで現在のバージョンがそのままタグ付けされます。

## 新しい Cog の追加

1. `c_lord/cogs/your_cog.py` を作成
2. Claude CLI 実行には `_run_helper.run_claude_with_config(RunConfig(...))` を使用
   （旧 `run_claude_in_thread()` shim も引き続き使えるが、新規コードは `run_claude_with_config` を優先）
3. `c_lord/cogs/__init__.py` からエクスポート
4. `c_lord/__init__.py` のパブリック API に追加
5. `tests/test_your_cog.py` にテストを書く

## AI 生成コードについて

このプロジェクトは Claude Code によって書かれました。コントリビューションに Claude Code や他の AI ツールを使うのは全く問題ありません — コードが動作し、テストされており、意味を成すことを確認してください。
