---
name: python-quality
description: Python coding patterns, idioms, and quality standards for c-lord development
---

# python-quality — c-lord の Python の書き方

c-lord のコードを書く・直す・レビューするときの基準。PEP 8 と Python 3.10 以降の書き方を土台に、
このリポジトリで決めていることを足したもの。規則の出どころは CLAUDE.md の Code Conventions で、
食い違ったら CLAUDE.md が正。

機械が見る部分は `uv run ruff check c_lord/`・`uv run ruff format --check c_lord/`・`uv run pyright c_lord/`
（`verify` スキル）。以下は機械が見ない部分も含む。

## ファイルの形

どの `.py` も同じ並びで始める。

```python
"""このモジュールが何をするか、1 文。必要なら経緯と Issue 番号 (#NNN)。"""

from __future__ import annotations

import asyncio
import logging
from typing import TYPE_CHECKING

import discord

from ..utils.logger import log_ctx

if TYPE_CHECKING:
    from ..database.repository import SessionRepository

logger = logging.getLogger(__name__)
```

- `from __future__ import annotations` は**全ファイル**に書く。
- import は 標準ライブラリ → 外部 → パッケージ内（相対）の順。並びは ruff の `I` ルールが直す。
- 型注釈にしか使わない import は `if TYPE_CHECKING:` の下に置く（循環 import と起動時間を避ける）。
- 起動時に要らない重い依存や、循環になる相手は、使う関数の中で import する（`setup.py` がそうしている）。

## 型

関数とメソッドの**すべて**の引数と戻り値に型を書く。

```python
async def resolve_tmux_manager(
    self, channel_id: int, *, thread_id: int | None
) -> TmuxSessionManager | None: ...
```

- `X | None`、`list[str]`、`dict[int, str]` と書く。`Optional` / `Union` / `List` は使わない。
- 取り違えると事故になる引数はキーワード専用（`*` の後ろ）にする。上の `thread_id` は、既定値があった頃に
  渡し忘れが 2 度バグになったため、既定値なしのキーワード必須になっている（#427、#600）。
- 利用側が呼ぶ公開の関数・コンストラクタに引数を足すときは、後方互換な既定値（`= None`）を付ける
  （Zero-Config Principle）。
- 構造だけを要求したいときは継承ではなく `Protocol`（`c_lord/protocols.py` の `DrainAware`）。

## 状態と設定は dataclass

複数の値をまとめて持ち回るものは dataclass にする（`ClaudeConfig`、`RunConfig`）。
作ったあと変えないものは `@dataclass(frozen=True)`（`WebhookTrigger`）。
引数が増え続ける関数は、設定の dataclass 1 つを受け取る形に直す
（`run_claude_with_config(RunConfig)` がその例）。

## 定数と名前

| 対象 | 形 |
|------|----|
| ファイル | `snake_case.py`。パッケージ内部用は先頭に `_`（`_run_helper.py`） |
| クラス | `PascalCase`（`TmuxClaudeRunner`、`StatusManager`） |
| 関数・メソッド | `snake_case`。外から呼ばないものは先頭に `_` |
| 定数 | モジュール先頭に `UPPER_SNAKE_CASE`。変えない集合は `frozenset`、並びは `tuple` |

数値や文字列をコードの途中に裸で書かず、名前を付けて「なぜその値か」をコメントに残す。

## エラーの扱い

```python
# 飾りの Discord 呼び出し（リアクション、通知の編集）: 失敗しても本体を止めない
with contextlib.suppress(discord.HTTPException):
    await message.add_reaction(emoji)

# 業務ロジック: 握りつぶさない。文脈つきでログに出す
try:
    await repo.save(thread_id=thread.id, session_id=session_id)
except sqlite3.Error:
    logger.exception("%s could not save the session record", log_ctx(thread_id=thread.id))
```

- ネットワーク断（DNS、接続拒否、タイムアウト）まで「飾りの失敗」として扱う箇所は、
  `discord.HTTPException` だけでなく `c_lord.discord_failures.DISCORD_CALL_FAILURES` を捕まえる。
  ただし**その呼び出し自体が仕事**の箇所（回答の投稿、スレッドの作成）では使わない — そこは失敗を表に出す。
- `except Exception: pass` は書かない。握りつぶすなら、何を・なぜ握りつぶすかをコメントに書き、ログに出す。
- CLI や子プロセスの失敗は例外で投げ上げず、`error` を持つ `StreamEvent` として yield する。
  ターンの終わり方（Discord に何を出すか）を 1 か所で決めるため。

## ログ

- `print()` は使わない。`logger = logging.getLogger(__name__)`。
- 引数は `%s` で渡す（f-string で先に組み立てない）。
- スレッド・セッション・タスク・チャンネルのどれかが文脈にあるなら、先頭に `log_ctx(...)` を付ける。
  `grep "thread=<id>"` で 1 本のスレッドの経過を追えるようにするため。
- **「何もしなかった」ことも INFO に残す。** DEBUG に落とすと、あとで「落ちたのか対象外だったのか」を
  切り分けられない（#678）。量が心配なら `c_lord/log_sampler.py` の `LogSampler` で間引く。
- token、secret、サインイン用のコードはログに出さない。

## 非同期

- 外部コマンドは `asyncio.create_subprocess_exec(*argv)`、または引数リストを渡す `subprocess.run([...])`。
  **シェルは通さない**（`shell=True` 禁止）。利用者由来の位置引数の前には `--`。
- ブロッキングな処理（`subprocess.run`、ファイル I/O、`tmux` の呼び出し）をイベントループの上で直接呼ばない。
  `await asyncio.to_thread(...)` に包む。
- 待つものには上限を付ける（`asyncio.wait_for(..., timeout=…)`、`subprocess.run(..., timeout=…)`）。
  返ってこない呼び出しが 1 つあると、終了処理ごと止まる（#699）。
- 取ったものは `try` / `finally` で返す（プロセス、一時ファイル、登録、セマフォ）。
  `finally` に置いたログは「必ず出る」ので、対になる開始ログとあわせて経過を追える
  （`run_claude: enter` / `exit`）。
- `asyncio.CancelledError` を握りつぶさない。後始末をしたら再送出する。

## コメントと docstring

コードが「何をしているか」ではなく「**なぜそうしているか**」を書く。このリポジトリでは、
事故や決定のあった箇所に Issue 番号（`#NNN`）と、何が起きたかを 1〜数行で残している。
同じ箇所を次に触る人が、その守りを知らずに外さないようにするため。
逆に、Issue 番号だけ書いて理由を書かないコメントは役に立たない。

## 置き場所

- Cog は 1 ファイル 1 クラス。複数の Cog が共有する処理は `cogs/_run_helper.py` のような内部モジュールへ出す。
- Discord に依存しない判断（文字列の整形、状態の判定、しきい値の計算）は、Cog から切り離して
  単独のモジュールにする。Discord のモック無しでテストできる（`status_view.py`、`thread_name.py`、`usage_limit.py`）。
- `c_lord/__init__.py` と `c_lord/cogs/__init__.py` は公開 API の窓口。足したら `__all__` にも入れる。
- import するだけで秘密や外部サービスを要求するコードを置かない。

## ruff / pyright の設定（`pyproject.toml`）

- 行長 100、`target-version = "py311"`
- lint: `E` `F` `W`（pycodestyle / pyflakes）、`I`（import 順）、`N`（命名）、`UP`（新しい構文へ）、
  `B`（bugbear）、`A`（組み込み名の上書き）、`SIM`（単純化）
- ビルド時に生成される `c_lord/_version.py` と `c_lord/_build_info.py` は対象外
- pyright は `basic`、Python 3.10 として検査。Pillow / emoji に依存する描画モジュールは対象外

指摘を `# noqa` / `# type: ignore` で消すときは、ルール名と理由を同じ行に書く
（`# noqa: S608 - fixed column names`）。
