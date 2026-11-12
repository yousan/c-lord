---
name: add-cog
description: c-lord に discord.py の Cog（スラッシュコマンド、テキストコマンド、イベントリスナー）を新しく足すときの手順。「Cog を足して」「コマンドを作って」「この機能を Cog で」と言われたときに使う。
---

# add-cog — c-lord に Cog を足す

Cog は `c_lord/cogs/` に 1 ファイル 1 クラスで置く。手本にするなら、小さいものは
`c_lord/cogs/version_cmd.py`（読み取り専用のコマンド 1 つ）、Claude を走らせるものは
`c_lord/cogs/webhook_trigger.py`。

始める前に 2 つ決める。

- **ここに置くものか。** 誰にでも役立つ機能なら c-lord に、個人のワークフローならインスタンス側の
  リポジトリに置く（CLAUDE.md「Framework vs Instance」）。サーバ固有の ID や秘密を前提にしない。
- **利用側が何も書かずに使えるか。** パッケージを更新するだけで新機能が届くこと
  （Zero-Config Principle）。利用側に配線を求める設計になったら、c-lord 側の設計を直す。

## 1. 失敗するテストから書く

`tests/test_<名前>.py` を先に作り、落ちることを確かめる（`tdd` スキル）。Cog のテストでは次を押さえる。

- コマンドが登録されている（`cog.get_app_commands()` / `cog.get_commands()` の名前）
- 入力の検証（不正な値、空、境界）と、許可されていない人が弾かれること
- 失敗したときの応答

Discord のオブジェクトは `MagicMock` / `AsyncMock` で作る。書き方は `test-guide` スキル、実例は
`tests/test_version_cog.py`。

## 2. Cog を書く

```python
"""<この Cog が利用者に何をするか、1〜2 文>。"""

from __future__ import annotations

import logging
from typing import TYPE_CHECKING

import discord
from discord import app_commands
from discord.ext import commands

from ..command_gate import NOT_AUTHORIZED, authorize_command
from ..utils.logger import log_ctx

if TYPE_CHECKING:
    from ..discord_ui.authorization import Authorizer

logger = logging.getLogger(__name__)


class ThreadNoteCog(commands.Cog):
    """<役割>。"""

    def __init__(self, bot: commands.Bot, *, authorizer: Authorizer | None = None) -> None:
        self.bot = bot
        self._authorizer = authorizer

    def _authorize(
        self, user: discord.Member | discord.User, message: discord.Message | None
    ) -> bool:
        return authorize_command(self._authorizer, user, message)

    @app_commands.command(name="thread-note", description="…")
    async def thread_note(self, interaction: discord.Interaction, text: str) -> None:
        if not self._authorize(interaction.user, None):
            await interaction.response.send_message(NOT_AUTHORIZED, ephemeral=True)
            return
        logger.info("%s note recorded", log_ctx(thread_id=interaction.channel_id))
        await interaction.response.send_message("記録しました", ephemeral=True)
```

決まりごと:

- **依存はコンストラクタで受け取る**（リポジトリ、`ClaudeConfig`、`Authorizer`）。新しい引数には
  必ず後方互換な既定値（`= None`）を付ける。既存の利用側のコードを壊さないため。
- **認可は共有の `Authorizer` に聞く。** 規則の写しを Cog の中に持たない（#713）。スラッシュコマンドは
  `self._authorize(user, None)`、メッセージ由来（テキストコマンド・webhook）は `message` を渡す
  （webhook と信頼済み bot の扱いは `command_gate` が決める）。コマンド本体か、そこから呼ぶ
  `self._*` のヘルパが `self._authorize(` / `self._is_allowed(` / `is_message_authorized(` に
  届いていないと、`tests/test_command_authorization_coverage.py` が落とす。
  誰でも使えてよい読み取り専用のコマンドだけ、そのテストの `PUBLIC_COMMANDS` に理由つきで足す。
- **コマンド名は「目的語-動詞」**（`/workspace-stop`、`/thread-rename`）。規約は `docs/COMMANDS.md`、
  検査は `tests/test_command_naming.py`（新しい Cog はそのテストの `ALL_COGS` に足す）。
- **テキストコマンドの双子**（`!version` のような、webhook から打てる版）を付けるなら、
  スラッシュ版と中身を共有する（`_xxx_impl(respond=…)` の形 — `version_cmd.py`、`session_manage.py`）。
  3 秒以上かかるスラッシュコマンドの応答は `c_lord/discord_ui/slash_io.py` を使う。
- **ボタンを出すなら** View に `AuthorizedViewMixin` を混ぜる（#466）。
- ログは `logging.getLogger(__name__)`。スレッド・チャンネル・セッション・タスクのどれかが文脈にあれば
  `log_ctx()` を付ける。`print()` は使わない。
- 飾りの Discord 呼び出し（リアクション、通知の編集）が失敗しても本体を落とさない:
  `contextlib.suppress(discord.HTTPException)`、ネットワーク断まで含めるなら
  `c_lord.discord_failures.DISCORD_CALL_FAILURES`。業務ロジックの失敗は握りつぶさずログに出す。

## 3. Claude を走らせる Cog の場合

1 ターンの実行は `c_lord/cogs/_run_helper.py` の `run_claude_with_config(RunConfig(...))` に任せる。
ステータスのランプ、質問メニューの橋渡し、エラー表示、セッションの保存はそこが持っているので、
Cog の中に同じものを書かない。`run_claude_in_thread()` は古い呼び出し方のために残してある互換用で、
新しいコードでは使わない。

呼ぶ前に Cog の側で用意するもの（`webhook_trigger.py` の `on_message` 以降がそのままの例）:

1. **tmux の管理者をスレッドで引く。**
   `ChannelRepoCog.resolve_tmux_manager(channel_id, thread_id=thread.id)`。
   `thread_id` はキーワード必須で、スレッドが文脈にあるなら必ず渡す。渡し忘れると親チャンネルの
   tmux セッションを引いてしまい、スレッドが別リポジトリに bind されているときに
   作業ディレクトリと tmux セッションが食い違う（#427、#600）。スレッドに属さない処理だけ
   `thread_id=None` と明示して書く。
2. **ウィンドウを作る。** `tmux.create_session(thread.id, working_dir)` のあと
   `tmux.session_exists(thread.id)` で実在を確かめる（どちらもブロッキングなので
   `asyncio.to_thread`）。これを飛ばすと、Claude のいないスレッドに ❌ だけが残る（#629）。
3. **transcript ミラーを始める。** `bot.transcript_mirror_cog.start_for(thread.id, working_dir)`。
   Claude の回答が Discord に届く経路はこれだけなので（#712）、忘れると Claude は走るのに
   スレッドには何も出ない。
4. **ランナーを作って渡す。** `TmuxClaudeRunner(tmux_manager=…, thread_id=…, model=…, …)` を
   `RunConfig(thread=…, runner=…, prompt=…, session_id=…, authorizer=…)` に入れる。

利用者の文字列をコマンドに混ぜる処理を足したら、`security-audit` スキルを通す。

## 4. 登録と export

- **`c_lord/setup.py` の `setup_bridge()`** で `await bot.add_cog(...)` し、
  `logger.info("Registered <Cog 名>")` を出す。これが「パッケージを更新するだけで届く」の実体。
  設定が要る機能でも既定で動くようにし、止めるスイッチ（環境変数）を用意する側に倒す。
  ほかの Cog から参照されるなら `bot.<属性>` にも置く（既存の `bot.transcript_mirror_cog` と同じ）。
- **`c_lord/cogs/__init__.py`**: import を足し、`__all__` に名前を入れる。
- **`c_lord/__init__.py`**: 公開 API として import と `__all__` に足す。

## 5. 文書

利用者に見えるコマンドや動きを足したなら、同じ PR で `docs/COMMANDS.md`（と `docs/ja/COMMANDS.md`）、
必要なら `docs/specs/` に「あるべき動き」を書く。環境変数を足したら `.env.example` にも。
CLAUDE.md の Project Structure にも 1 行足す。

## 6. 通す

```bash
uv run ruff check c_lord/
uv run ruff format --check c_lord/
uv run pyright c_lord/
uv run pytest tests/ -v --cov=c_lord
```

（`verify` スキルと同じ。）Cog は実機の Discord と tmux があって初めて動くものなので、テストが緑でも
完了ではない。staging での確認と証跡は CLAUDE.md の「動作確認スキーム」と DoD に従う。
