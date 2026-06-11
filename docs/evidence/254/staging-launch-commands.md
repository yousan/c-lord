# Staging Evidence — PR #254 (configurable effort)

staging: `c-lord-parallel-3` / channel `1503196656265597082` / 2026-06-11
前提: staging `.env` に `CLAUDE_EFFORT=high` を一時設定。`!clord <prompt>` で新規スレッドを spawn し、
tmux window の claude 起動コマンドを `capture-pane` で観測（`--effort` フラグの有無が観測点）。

## RED — `main` (effort プラミングが存在しない)
新規 window `w13` の起動コマンド:
```
$ env -u CLAUDECODE claude --model sonnet --dangerously-skip-permissions 'PR254-RED-main …'
```
→ `CLAUDE_EFFORT=high` を設定しても `--effort` は**付かない**（main には effort を渡す経路が無い）。

## GREEN — `feat/configurable-effort-default-max`
新規 window `w14` の起動コマンド:
```
$ env -u CLAUDECODE claude --model sonnet --dangerously-skip-permissions --effort high 'PR254-GREEN-branch …'
```
→ `CLAUDE_EFFORT=high` が `--effort high` として渡る（設定可能化が機能）。

## 新デフォルト（CLAUDE_EFFORT 未設定）
`effort` 未指定なら `--effort` は付かず CLI デフォルトに従う（`ClaudeConfig().effort is None`、test_config.py）。
これは main と同じ無フラグ挙動＝ゼロコンフィグ後方互換。

注: TUI バナーの "high effort" は claude CLI 自身の env/config 由来で、c-lord の `--effort` とは独立。
本証跡は**起動コマンド文字列**を観測点としている。
