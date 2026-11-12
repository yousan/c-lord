---
name: test-guide
description: Testing patterns for c-lord — pytest, async testing, mocking Discord objects, TDD workflow
---

# Test Guide — Testing Patterns for c-lord

c-lord のテストの書き方と、`tests/` に既にある道具の案内。テストを足す・直す・落ちたテストを調べるとき、
「テストを書いて」「カバレッジを上げて」と言われたときに読む。
書く順番（先にテスト、RED を見てから実装）は `tdd` スキル。

## 実行のしくみ

設定は `pyproject.toml` の `[tool.pytest.ini_options]`。

- `asyncio_mode = "auto"` — `async def test_…` はそのまま走る。`@pytest.mark.asyncio` は書かなくてよい
  （付いている既存テストもあるが、新しく書くなら不要）。fixture も `async def` で書ける。
- `addopts = "-m 'not e2e and not live_llm'"` — 実機が要るテストは既定で外れる。
  - `e2e`: 動いている bot と webhook が要る（`tests/e2e/`、`uv run pytest -m e2e tests/e2e/ -v`）。
    `.env` に鍵が無ければ skip される
  - `live_llm`: 本物の `claude` を呼ぶ（`CLORD_TEST_LIVE_LLM=1` のときだけ）
- CI は Python 3.10 / 3.11 / 3.12 と macOS で流す。`/proc`、systemd、GNU 固有のコマンドに頼るテストは
  `skipif` で条件を付ける。

```bash
uv run pytest tests/ -v --cov=c_lord --cov-report=term-missing   # 全体（CI と同じ）
uv run pytest tests/test_tmux.py -v                              # 1 ファイル
uv run pytest tests/test_tmux.py::TestTmuxSessionManager -v      # 1 クラス（::test_名 で 1 テスト）
uv run pytest tests/ -x -q                                       # 最初の失敗で止める
```

## `tests/conftest.py` が用意しているもの

**何もしなくても全テストに効く隔離**（autouse）。新しいテストはこの前提の上で書いてよい。

| fixture | 何を隔離しているか |
|---------|-------------------|
| `_isolated_tmux_socket`（セッション単位） | `TMUX_TMPDIR` を専用ディレクトリに向け、`TMUX` / `TMUX_PANE` を外す。本番のウィンドウが載っている tmux サーバにテストが触れない（#701）。終了時はそのディレクトリ内のソケットだけを落とす |
| `_isolated_claude_home` | `Path.home()` を一時ディレクトリへ。`~/.claude/projects/…/.clord-session` の記録が開発者の実ホームに残らず、テスト同士で混ざらない（#773）。テストが自分で `$HOME` を設定した場合はそちらが勝つ |
| `_isolated_prompt_dir` | `start_claude` がプロンプトを置く一時ファイルの場所（#529） |
| `_fresh_typed_input_state` | `c_lord.tmux` がスレッドごとに覚えている「最後に打った内容」（#879） |
| `_reset_fallback_owner_ids` | プロセス全体で 1 つの認可状態（アプリ所有者・既定の `Authorizer`）（#713/#739） |
| `_fresh_login_alert` | プロセス全体で 1 つのログイン切れ通知の状態（#1309） |

プロセス全体で 1 つの状態を新しく足したら、ここに同じ形のリセットを足す。そうしないと、
あるテストの後始末漏れが別のテストの失敗として現れる。

**明示的に受け取る fixture とヘルパ**:

- `thread` — `discord.Thread` の `MagicMock`（`id = 12345`、`send` は `AsyncMock` で、返すメッセージは `edit` できる）
- `runner` — ランナーの `MagicMock`（`interrupt` が `AsyncMock`）
- `repo` — `SessionRepository` の `MagicMock`（`save` / `get` が `AsyncMock`、`get` は `None` を返す）
- `make_async_gen(events)` — 渡したイベントを順に yield する非同期ジェネレータの工場。`runner.run = make_async_gen([...])`
- `simple_events(session_id)` — SYSTEM と RESULT だけの最小のイベント列

同じ名前の fixture をテストファイルやクラスで定義すると、そちらが優先される
（`tests/test_repository.py` の `repo` は本物の SQLite を返す）。

## 素材（`tests/fixtures/`）

| ディレクトリ | 中身 |
|--------------|------|
| `panes/` | 実機で採った tmux ペインの写し。TUI の検出（メニュー、権限プロンプト、スピナー、使用量上限）のテストに使う |
| `transcripts/` | Claude Code の jsonl transcript の行。ミラーと整形のテストに使う |
| `subagents/` | サブエージェントの transcript 一式（#971） |
| `login/`、`prod_logs/` | ログイン切れの画面、本番ログの抜粋 |

読み込みは `Path(__file__).parent / "fixtures" / "panes" / "<名前>.txt"`。
検出のバグを直すときは、**直す前に**再現する実物をここへ足す（DoD の "Detection bug fixture rule"）。
ファイル名に Issue 番号や CLI の版を入れておく（`i925_…_v2_1_312.txt`）。

## 対象ごとの書き方

### 純粋なロジック

Discord も tmux も要らないもの: `discord_ui/reply_chunker.py`、`transcript/formatter.py`、
`thread_name.py`、`version.py`、`usage_limit.py` など。入力と出力をそのまま assert する。
境界を厚く — 空、上限ちょうど、上限 +1、コードフェンスの途中、全角と絵文字、改行の有無。

```python
def test_content_exactly_at_limit_is_single_chunk() -> None:
    text = "x" * DISCORD_MAX
    assert chunk_discord_content(text) == [text]
```

### リポジトリ（SQLite）

モックにしない。`tmp_path` に本物の DB を作る。

```python
@pytest.fixture
async def repo(tmp_path):
    db_path = str(tmp_path / "test.db")
    await init_db(db_path)
    return SessionRepository(db_path)


async def test_save_and_get(repo) -> None:
    await repo.save(thread_id=12345, session_id="session-abc")
    fetched = await repo.get(12345)
    assert fetched is not None and fetched.session_id == "session-abc"
```

### tmux とランナー

`TmuxSessionManager` を `MagicMock` にして、ペインの内容を `capture_pane.return_value` で与える
（`tests/test_tmux_runner.py` の `tmux_manager` fixture が既定値の一式を持っている）。
`TmuxClaudeRunner` には本物を使い、yield されたイベントを集めて assert する。

`c_lord/tmux.py` 自体のテストは、外部コマンドを実行する `c_lord.tmux._run` を
`unittest.mock.patch("c_lord.tmux._run", …)` で差し替え、
**組み立てられた引数リスト**を検査する（`--` があるか、秘密を `env -u` しているか、対象の指定が
`=名前` の完全一致か）。本物の tmux を動かすテストは少数で、`shutil.which("tmux")` が無ければ skip する。

### Cog

Discord のオブジェクトを `MagicMock`、await されるメソッドを `AsyncMock` にする。

```python
interaction = MagicMock()
interaction.response.send_message = AsyncMock()
interaction.response.defer = AsyncMock()
interaction.response.is_done = MagicMock(return_value=False)
```

確かめるのは、コマンドの登録（`cog.get_app_commands()` / `cog.get_commands()`）、入力の検証、
認可で弾かれること、送られた応答（`send_message.assert_called_once()` と引数）。
コマンドの中身が `_xxx_impl(respond=…)` に分かれていれば、`respond` に記録用の関数を渡すのが一番軽い。

### ログ

「黙って捨てた」を INFO に残す規約（CLAUDE.md、#678）や `log_ctx()` の前置きを守るコードでは、
`caplog` でログの 1 行を assert する。`thread=<id>` で grep できることまで含めて検査する。

### 環境変数

`monkeypatch.setenv` / `delenv` を使う。`os.environ` を直接書き換えない（次のテストに漏れる）。

### 構造を守るテスト

個別の動きではなく「全部がこうなっていること」を守るテストがある。新しいコマンドや文書を足すと
ここで落ちることがあるので、落ちたら緩めるのではなく、求められている側を直す。

- `tests/test_command_authorization_coverage.py` — すべてのコマンドが認可を通る（AST で検査）
- `tests/test_command_naming.py` — コマンド名の規約と `docs/COMMANDS.md` の一致
- `tests/test_docs_*.py` — 文書が実装と食い違っていない

## カバレッジの目安

- 純粋なロジック（パーサ、分割、型、整形）: 90% 以上
- リポジトリ: 本物の SQLite で書けるので、同じく高く保つ
- Discord に依存するコード（Cog、`StatusManager`）: モックを使って 30% 以上

## 書かないもの

- discord.py、SQLite、asyncio そのものの動作確認
- 値を返すだけのプロパティ
- モックの設定をそのまま assert し直すだけのテスト

## テストで確かめられないこと

ここまでのテストはすべてモックの上で走る。tmux の実際の描画、Discord の表示、再起動をまたいだ継続は
CI では試されない。だから DoD は staging での確認と証跡を別に求めている（CLAUDE.md
「Definition of Done」「動作確認スキーム」）。テストが緑であることを「動いた」と報告しない。
