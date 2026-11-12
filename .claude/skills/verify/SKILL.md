---
name: verify
description: コミットや push の前に、CI と同じ検査（ruff の lint と format、pyright、pytest）とセキュリティ確認をひととおり流す。「verify して」「チェックして」「通るか確かめて」と言われたとき、まとまった変更を入れ終えたときに使う。
---

# verify — コミット前に CI と同じものを手元で通す

`main` の必須チェック `test (3.10/3.11/3.12)` は `.github/workflows/ci.yml` の 4 コマンドでできている。
ここではそれを同じ順に手元で流し、そのあと CI が見ていない 2 点（セキュリティと公開 API）を確かめる。
**どこかで落ちたら先へ進まず、直してその段からやり直す。**

純粋なドキュメントだけの変更なら 1〜4 は省いてよい（docs の中身を検査するテストがあるので、
`uv run pytest tests/ -q -k docs` だけは流す）。

## 1. lint

```bash
uv run ruff check c_lord/
```

自動で直せる指摘は `uv run ruff check --fix c_lord/` で直し、もう一度素の `ruff check` を流す。

## 2. format

```bash
uv run ruff format --check c_lord/
```

差分が出たら `uv run ruff format c_lord/` で整形する。CI は `--check` なので、整形し忘れは CI で落ちる。

## 3. 型

```bash
uv run pyright c_lord/
```

設定は `pyproject.toml` の `[tool.pyright]`（`basic`、Python 3.10 として検査）。Pillow / emoji に依存する
描画モジュールは対象外になっている。

## 4. テスト

```bash
uv run pytest tests/ -v --cov=c_lord --cov-report=term-missing
```

`e2e` と `live_llm` のマーカーが付いたテストは既定で外れる（`pyproject.toml` の `addopts`）。
全部通ること。新しく書いたコードの行が `Missing` に並んでいないかも見る。
落ちたときに直すのは原則としてコードの側。テストの期待が間違っていると判断したときだけテストを直し、
その理由を PR に書く。

## 5. セキュリティ

`c_lord/tmux.py`、`c_lord/claude/tmux_runner.py`、`c_lord/cogs/` のどれかを触ったなら、
`security-audit` スキルのチェックリストを通す（CLAUDE.md の Security 節で必須とされている）。
それ以外の変更でも、次だけは毎回見る。

```bash
grep -rn "shell=True\|create_subprocess_shell\|os\.system(" c_lord/   # 説明文以外に 0 件のはず
uv run ruff check c_lord/ --select S                                 # bandit 相当のルールで流し読み
```

`--select S` は既存のコードにも数十件の指摘を出す（引数リストでの `subprocess` 利用など、確認済みのもの）。
0 件にする検査ではない — 自分の差分で増えた行だけを読む。

- 新しい秘密の環境変数を足したなら、`c_lord/tmux.py` の `SENSITIVE_ENV_KEYS` に入っているか
- トークンやパスワードを文字列リテラルで書いていないか、ログに出していないか

ここで問題が見つかったら**コミットしない**。先に直す。

## 6. 公開 API が import できる

```bash
uv run python -c "from c_lord import setup_bridge, ClaudeConfig, ClaudeChatCog, TmuxClaudeRunner, SessionRepository"
```

落ちたら `c_lord/__init__.py` の import と `__all__` が、実際のモジュール構成と合っているかを見る。
c-lord はパッケージとして他のボットから import されるので、ここが壊れると利用側が起動しなくなる。

## 報告

段ごとに「通った / 落ちて直した / 落ちたまま」を、実際の出力の末尾と一緒に書く。
流していない段を通ったことにしない。実機（Discord・tmux）での確認はこの検査に含まれない —
それは CLAUDE.md の「動作確認スキーム」の仕事で、ここが緑でも「動く」とは言えない。
