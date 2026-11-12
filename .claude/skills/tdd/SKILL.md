---
name: tdd
description: 機能追加とバグ修正を「テストを先に書く → 落ちるのを見る → 実装する」の順で進める。c-lord ではこの順が必須。新機能、バグ修正、リファクタ、Cog・DB 操作・表示の追加に着手するときに使う。
---

# tdd — テストを先に書く（c-lord では必須）

When adding features or fixing bugs in c-lord, you MUST write tests before writing implementation code. No exceptions.

理由は 2 つある。1 つは、後から書いたテストは「いまの実装が通るテスト」になりやすく、直したはずのバグを
捕まえないこと。もう 1 つは、DoD（CLAUDE.md）が **RED の失敗行を PR に貼ること**を求めていて、
先に落としていなければ貼るものが無いこと。

## 進め方

### 1. RED — 落ちるテストを書いて、落ちるのを見る

`tests/test_<対象>.py` に、**利用者や呼び出し側から見える動き**を 1 つ書く。

```bash
uv run pytest tests/test_<対象>.py -v
```

落ちることを自分の目で確かめる。落ち方も見る — `ImportError` や typo ではなく、
**期待した動きが無いこと**で落ちているか。その失敗行（`FAILED tests/… - AssertionError: …`）は
PR の TDD 証跡に使うので控えておく。

最初から通ってしまったら、そのテストは何も守っていない。期待を見直す。

### 2. GREEN — 通す最小の実装

テストが通るところまでだけ書く。ついでの整理や、頼まれていない機能はここで足さない。

```bash
uv run pytest tests/test_<対象>.py -v
```

### 3. REFACTOR — 緑のまま整える

名前、重複、分割を直す。1 手ごとに全体を流して、緑のままであることを確かめる。

```bash
uv run pytest tests/ -v --cov=c_lord --cov-report=term-missing
```

### 4. VERIFY — CI と同じものを通す

```bash
uv run ruff check c_lord/
uv run ruff format c_lord/
uv run pytest tests/ -v --cov=c_lord
```

`pyright` まで含めた全段は `verify` スキル。

## 何から書くか（変更の種類ごと）

テストの書き方そのもの（モックの作り方、共有 fixture）は `test-guide` スキルにある。
ここは「最初の 1 本を何にするか」。

### バグ修正

**直す前のコードで落ちる**再現テストが最初の 1 本。Issue の再現条件をそのままテストにする。
テスト名か docstring に Issue 番号を入れる（後から「なぜこのテストがあるか」を辿れる）。

```python
def test_reply_longer_than_the_limit_is_not_cut_off() -> None:
    """#NNN: 2000 文字を超える返信の後半が届かなかった。"""
    text = "x" * (DISCORD_MAX + 1)
    assert "".join(chunk_discord_content(text)) == text
```

### TUI の検出（`claude/tmux_runner.py` のプロンプト判定）

`_has_permission_prompt` / `_is_yn_prompt` / `_has_unknown_interactive` などを直すときは、
**修正より先に、実機で採ったペインの写しを `tests/fixtures/panes/` に置く**（DoD の
"Detection bug fixture rule"）。その写しに対して、壊れた判定が間違った値を返すことをテストで示してから直す。
手で組んだ文字列ではなく実物を使うのは、実物にしか無い空白・罫線・折り返しが原因であることが多いから。

### 純粋なロジック（`discord_ui/reply_chunker.py`、`transcript/formatter.py`、`thread_name.py`、`version.py` など）

入力と出力だけで書ける。正常系 1 本のあと、境界を先に並べる: 空文字列、ちょうど上限、上限 +1、
コードフェンスの途中、絵文字や全角。

### データベース（`database/*_repo.py`）

モックではなく、`tmp_path` に作った本物の SQLite に対して書く（`tests/test_repository.py` の `repo` fixture）。
保存して読み戻す、無い行を読む、同じキーで 2 回保存する、の順。

### Cog のコマンド

Discord を `MagicMock` / `AsyncMock` で置き換え、(1) コマンドが登録されている、(2) 不正な入力と
許可されていない人が弾かれる、(3) 正常系で期待した応答が送られる、を書く。
コマンドの中身を `_xxx_impl(respond=…)` に寄せておくと、`respond` を差し替えるだけで試せる
（`tests/test_version_cog.py`）。

### 文書の約束

「この文書はこう書いてある」こと自体を守りたいとき（消した機能の説明が残っていない、など）は、
文言ではなく**コードを根拠に**検査するテストを書く（`tests/test_docs_status_lamp.py`、
`tests/test_docs_contributing.py`）。

## カバレッジの目安

| 対象 | 目安 |
|------|------|
| 純粋なロジック（パーサ、分割、型、整形） | 90% 以上 |
| Discord に依存するコード（Cog、`StatusManager`） | モックを使って 30% 以上 |

数字は結果であって目的ではない。新しく足した分岐にテストが当たっているかを
`--cov-report=term-missing` の `Missing` で見る。

## やってはいけない形

- **実装を先に書いて、あとからテストを足す。** RED を見ていないテストは、壊れても落ちない可能性がある。
- **RED を飛ばす。** 「落ちるはず」で済ませない。実行して落ちた行を見る。
- **内部の形を検査する。** private な属性の中身や、呼ばれた回数だけを assert するテストは、
  動きが同じままのリファクタで落ち、動きが壊れても通る。外から見える結果
  （戻り値、送られたメッセージ、DB の行、ログの 1 行）を assert する。
- **何もかもモックにする。** モックにするのは外との境界（Discord、tmux、`claude` の起動、ネットワーク）だけ。
  自分たちのロジックまでモックにすると、テストはモックの設定を検査しているだけになる。
- **テストを通すためにテストを緩める。** 落ちたら直すのはコードの側。期待が間違っていたなら、
  なぜ間違っていたかを PR に書いてから直す。
- **実機の tmux を既定ソケットで触る。** `tests/conftest.py` がテスト全体を専用ソケットに隔離しているので
  普通に書けば安全だが、テストの外で手動確認するときは必ず `tmux -L <名前>`（CLAUDE.md、#701）。

## よく使うコマンド

```bash
uv run pytest tests/test_reply_chunker.py -v                 # 1 ファイル
uv run pytest tests/test_reply_chunker.py -k limit -v        # 名前で絞る
uv run pytest tests/ -x -q                                   # 最初の失敗で止める
uv run pytest tests/ -q --lf                                 # 前回落ちたものだけ
uv run pytest tests/test_x.py -v -l                          # 失敗時にローカル変数を出す
uv run pytest tests/ -v --cov=c_lord --cov-report=term-missing
```

テストが緑になっても、それは「モックの上で通った」まで。Discord と tmux を通した確認は
CLAUDE.md の「動作確認スキーム」で別に行う。
