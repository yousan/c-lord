---
name: release
description: c-lord のバージョン/リリース手順。「v1.5.0としてリリースして」等と言われたときに使う。
allowed-tools: Bash, Read, Edit
---

# c-lord リリース手順

## 仕組みの要点

- **バージョンの真実源は git タグ `vX.Y.Z`**。`pyproject.toml` に version は書かない（`hatch-vcs` がタグから導出）。**手書きで version を書き換えてはいけない。**
- **patch は全自動**: main へ PR をマージするたび `.github/workflows/auto-version-bump.yml` が patch を +1 してタグ + GitHub Release を作る（`v1.4.0`→`v1.4.1`→…）。操作不要。
- **minor / major は手動**: 節目を付けたいときだけ `scripts/release.sh` でタグを打つ（このスキル）。
- 実行中ボットの版は `c-lord version` / `/version` で `v1.4.0-b<commit7>-<YYYYMMDD>` 形式（記事準拠）で確認できる。
- リリースノートは `CHANGELOG.md` の該当セクションから自動抽出される。

```
v1.4.0 ──(PR merge: 自動 patch)──> v1.4.1 ──(自動)──> v1.4.2 ──(手動 minor)──> v1.5.0
```

### 自動 patch bump が走らないケース

`auto-version-bump.yml` は head コミットの subject が次のときスキップ（release→docs-sync→release ループ防止）:

- `docs:` で始まる（翻訳・ドキュメント更新）
- `[skip-release]` または `[skip ci]` を含む

bump させたくない PR は、squash コミット subject を `docs:` にするか `[skip-release]` を付ける。

---

## 手動リリース（minor / major、「v1.5.0としてリリースして」）

### Step 1: main を最新化

```bash
cd /home/yousan/c-lord
git checkout main && git pull
```

### Step 2: CHANGELOG.md を更新（PR で）

1. `## [Unreleased]` を `## [1.5.0] - YYYY-MM-DD`（今日）に変更
2. その上に新しい空の `## [Unreleased]` を追加
3. 末尾のリンク定義（`[1.5.0]: .../compare/v1.4.0...v1.5.0` 等）も追記

```bash
git checkout -b docs/changelog-v1.5.0
# CHANGELOG.md を編集
git add CHANGELOG.md
git commit -m "docs: changelog for v1.5.0"   # docs: なので自動 patch bump は走らない
git push -u origin docs/changelog-v1.5.0
gh pr create --base main --title "docs: changelog for v1.5.0" \
  --body "Changelog for the v1.5.0 release."
```

PR をマージ（CHANGELOG をタグより先に main へ入れることで、リリースノートが正しく拾われる）。

### Step 3: タグを打つ（= リリース実行）

`scripts/release.sh` を使う。まず dry-run:

```bash
git checkout main && git pull
./scripts/release.sh --version 1.5.0          # dry-run（打たれるタグを表示）
./scripts/release.sh --version 1.5.0 --apply  # タグ作成 + push → release.yml が Release 作成
```

バージョンを明示せず、直近コミットの `[minor]`/`[major]`/`[release]` から自動算出も可能（指定なしは patch）:

```bash
./scripts/release.sh --level minor            # dry-run
./scripts/release.sh --level minor --apply
```

### Step 4: 確認

```bash
gh release view v1.5.0 --repo yousan/c-lord
```

---

## 仕組み（裏側）

```
普通の PR を main にマージ
  └── push: main → auto-version-bump.yml
        ├── subject が docs: / [skip-release] → 何もしない
        └── それ以外 → 最新タグ + patch でタグ作成 + GitHub Release

手動 minor/major
  └── scripts/release.sh --apply → vX.Y.Z タグ push
        └── push: tags v* → release.yml → GitHub Release（CHANGELOG 抽出）
```

> 注: `GITHUB_TOKEN` で打ったタグは別ワークフローを起動しない（GitHub の再帰防止）。そのため auto-version-bump.yml はタグと Release を同一ジョブで作る。release.sh が打つタグ（個人の認証）は release.yml を起動する。

---

## よくある失敗

| 状況 | 原因 | 対処 |
|------|------|------|
| Release のノートが空/簡素 | タグより先に CHANGELOG が main に入っていない | CHANGELOG を main へ入れてからタグを打ち直す |
| タグが既に存在するエラー | 同じバージョンで再実行 | `git push --delete origin v1.5.0` してから再実行 |
| docs PR なのに patch が上がった | subject が `docs:` 始まりでなかった | subject を `docs:` にするか `[skip-release]` を付ける |
| 版が `0.0.0` に見える | タグが1つも無い（ブートストラップ前） | 最初の `v1.4.0` タグを打つ（`release.sh` が seed する） |
