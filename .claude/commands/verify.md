コミット前の検査を順に流してください（`verify` スキルと同じ内容。CI は `.github/workflows/ci.yml`）。

1. `uv run ruff check c_lord/` — lint。指摘は直す
2. `uv run ruff format --check c_lord/` — 差分があれば `uv run ruff format c_lord/` で整形する
3. `uv run pyright c_lord/` — 型
4. `uv run pytest tests/ -v --cov=c_lord --cov-report=term-missing` — 全部通ること
5. セキュリティ: `shell=True`、文字列リテラルの秘密、検証していない入力が外部コマンドに渡る箇所を探す（詳しくは `security-audit` スキル）
6. 公開 API: `uv run python -c "from c_lord import setup_bridge, ClaudeConfig, ClaudeChatCog"`

段ごとに結果を報告してください。落ちた段は直してその段からやり直し、通るまで次へ進まないこと。
