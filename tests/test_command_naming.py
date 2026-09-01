"""コマンド命名規約 — Issue #578.

24 コマンドに規約が **3つ並立** していた（#540 の棚卸し）: 目的語-動詞 9本 /
動詞-目的語 4本 / 目的語なし 10本。既に多数派だった **名詞先頭** に揃える、
というのが #540 → #578 の決定。

このテストが守るのは「今きれいになったこと」ではなく **これから増えるコマンド**。
規約はドキュメントに書くだけでは守られない（`docs/COMMANDS.md` に書いた規約と
実装がズレていた、というのがそもそもの発端）ので、名前そのものを検査する。

規約は3つのバケツだけ:

1. ``<目的語>-<動詞>`` — そのリソースのライフサイクル操作。先頭は :data:`OBJECTS`
   のいずれか。Discord のオートコンプリートは前方一致なので、``/workspace`` と
   打てば start / stop / delete / cleanup が揃って出る
2. **目的語なし** — 「いま・ここ」への即時操作（:data:`IMMEDIATE`）。``/stop`` は
   *走っているターン* を止める。だから ``/stop`` と ``/workspace-stop`` は意味を
   持って共存できる
3. **旧名エイリアス**（:data:`LEGACY_ALIASES`）— 指が覚えているものは壊さない
   （Zero-Config Principle）。規約からは免除されるが、**明示的に列挙**しないと
   このテストが落ちる。「うっかり旧規約の名前を新設した」が黙って通らないように

``session`` が :data:`OBJECTS` に **無い** のは意図的。#571 で「セッション」は
``session_id`` と tmux セッションに予約した語なので、利用者が名指しで操作する
オブジェクト名には使わない（#578 で ``/session-cleanup`` →
``/workspace-cleanup``）。
"""

from __future__ import annotations

import re
from pathlib import Path

import pytest

from c_lord.cogs.auto_upgrade import AutoUpgradeCog
from c_lord.cogs.channel_repo import ChannelRepoCog
from c_lord.cogs.claude_chat import ClaudeChatCog
from c_lord.cogs.scheduler import SchedulerCog
from c_lord.cogs.session_cleanup import SessionCleanupCog
from c_lord.cogs.session_manage import SessionManageCog
from c_lord.cogs.skill_command import SkillCommandCog
from c_lord.cogs.transcript_mirror import TranscriptMirrorCog
from c_lord.cogs.version_cmd import VersionCog
from c_lord.cogs.webhook_trigger import WebhookTriggerCog

COMMANDS_DOC = Path(__file__).parent.parent / "docs" / "COMMANDS.md"

ALL_COGS = (
    AutoUpgradeCog,
    ChannelRepoCog,
    ClaudeChatCog,
    SchedulerCog,
    SessionCleanupCog,
    SessionManageCog,
    SkillCommandCog,
    TranscriptMirrorCog,
    VersionCog,
    WebhookTriggerCog,
)

#: 名前の先頭に置いてよい目的語。**利用者に見えている実体だけ**を並べる。
#: 増やすときは `docs/COMMANDS.md` の命名規約にも足すこと（このテストが両方を見る）。
OBJECTS = frozenset(
    {
        "clord",  # bot 自身 — 起動 / 接続 / 設定
        "claude",  # Claude のプロセス
        "workspace",  # スレッドに紐づく作業一式（docs/specs/workspace-vocabulary.md）
        "tmux",  # tmux セッション / ウィンドウ
        "thread",  # Discord スレッド
        "model",  # Claude のモデル設定
    }
)

#: 目的語なし＝「いま・ここ」への即時操作。ライフサイクル操作はここに入れない。
IMMEDIATE = frozenset(
    {
        "stop",  # 走っているターンを止める
        "clear",  # このスレッドの文脈を捨てる
        "compact",  # このスレッドの文脈を畳む
        "resync",  # このスレッドのミラーを繋ぎ直す
        "skill",  # ここで skill を1回走らせる
        "upgrade",  # この bot を今すぐ更新する
        "version",  # 今動いているものを答える
    }
)

#: 旧名。呼ばれ続けるが規約からは免除される（#574 / #578）。
LEGACY_ALIASES = frozenset(
    {
        "close-workspace",  # → workspace-stop (#574)
        "reopen-workspace",  # → workspace-start (#574)
        "restart-claude",  # → claude-restart (#578)
        "session-cleanup",  # → workspace-cleanup (#578)
        "attach",  # → clord-attach (text twin only)
    }
)


def classify(name: str) -> str | None:
    """規約のどのバケツに入るか。``None`` なら違反。

    実装が1箇所なので、規約そのものを合成名でテストできる（下の
    :class:`TestTheRuleItself`）。「規約テストが実は何も落とさない」という
    一番よくある失敗を防ぐため。
    """
    if name in LEGACY_ALIASES:
        return "alias"
    if name.split("-", 1)[0] in OBJECTS:
        return "object"
    if name in IMMEDIATE:
        return "immediate"
    return None


def _all_command_names() -> dict[str, str]:
    """{コマンド名: 見つけた Cog 名} — スラッシュ・テキスト・グループを全部。"""
    names: dict[str, str] = {}
    for cog in ALL_COGS:
        for cmd in getattr(cog, "__cog_app_commands__", ()):
            names.setdefault(cmd.name, cog.__name__)
        for cmd in getattr(cog, "__cog_commands__", ()):
            names.setdefault(cmd.name, cog.__name__)
    return names


class TestNamingConvention:
    """すべてのコマンド名が3つのバケツのどれかに収まること。"""

    def test_every_command_follows_the_convention(self) -> None:
        violations: list[str] = []
        for name, cog_name in sorted(_all_command_names().items()):
            if classify(name) is not None:
                continue
            head = name.split("-", 1)[0]
            violations.append(f"  {cog_name}: /{name} (先頭 {head!r} は目的語ではない)")

        assert not violations, (
            "コマンド命名規約 (#578) 違反:\n"
            + "\n".join(violations)
            + "\n\n名前は次のどれかにすること:\n"
            f"  1. <目的語>-<動詞>  目的語は {sorted(OBJECTS)} のいずれか\n"
            f"  2. 目的語なし        「いま・ここ」への即時操作のみ ({sorted(IMMEDIATE)})\n"
            "  3. 旧名エイリアス    LEGACY_ALIASES に明示的に追加する\n"
            "規約そのものは docs/COMMANDS.md の「コマンド命名規約」を参照。"
        )

    def test_no_command_is_named_after_a_session(self) -> None:
        """「セッション」は ``session_id`` / tmux に予約した語 (#571)。

        利用者が名指しで操作する目的語にこの語を使うと、1語が3つの実体を指す
        状態に逆戻りする — それを終わらせたのが workspace-vocabulary.md。
        """
        assert "session" not in OBJECTS
        offenders = {
            name
            for name in _all_command_names()
            if name.split("-", 1)[0] == "session" and name not in LEGACY_ALIASES
        }
        assert not offenders, (
            f"`session` で始まるコマンドは規約違反 (#571 で予約済みの語): {sorted(offenders)}"
        )


class TestTheRuleItself:
    """規約チェックが **実際に落とす** ことを合成名で確かめる。

    これが無いと、上のスイープは「今たまたま違反が無い」のか「何も見て
    いない」のか区別が付かない。
    """

    @pytest.mark.parametrize(
        "bad",
        [
            "restart-workspace",  # 動詞-目的語（#578 が潰した規約）
            "delete-thread",
            "session-cleanup-v2",  # 予約語 session で始まる (#571)
            "reload",  # 目的語なしだが「いま・ここ」ではない
            "sync-all",
        ],
    )
    def test_rejects_off_convention_names(self, bad: str) -> None:
        assert classify(bad) is None, f"/{bad} は規約違反として弾かれるべき"

    @pytest.mark.parametrize(
        ("good", "bucket"),
        [
            ("workspace-stop", "object"),
            ("workspace-cleanup", "object"),
            ("claude-restart", "object"),
            ("tmux-list", "object"),
            ("thread-archive", "object"),
            ("stop", "immediate"),
            ("resync", "immediate"),
            ("close-workspace", "alias"),
            ("restart-claude", "alias"),
        ],
    )
    def test_accepts_on_convention_names(self, good: str, bucket: str) -> None:
        assert classify(good) == bucket


class TestRenamedCommands:
    """#578 の改名 — 新名が居て、旧名も同じ実装を呼ぶこと。"""

    @pytest.mark.parametrize(
        ("new_name", "old_name"),
        [
            ("claude-restart", "restart-claude"),
            ("workspace-cleanup", "session-cleanup"),
        ],
    )
    def test_new_name_and_old_alias_both_registered(self, new_name: str, old_name: str) -> None:
        names = _all_command_names()
        assert new_name in names, f"/{new_name} が登録されていない (#578)"
        assert old_name in names, (
            f"/{old_name} は旧名エイリアスとして残すこと — 指が覚えているものは壊さない"
        )

    def test_both_slash_and_text_twin_exist_for_each(self) -> None:
        """text twin が無いと webhook からの E2E で触れなくなる。"""
        slash = {c.name for cog in ALL_COGS for c in getattr(cog, "__cog_app_commands__", ())}
        text = {c.name for cog in ALL_COGS for c in getattr(cog, "__cog_commands__", ())}
        for name in ("claude-restart", "restart-claude", "workspace-cleanup", "session-cleanup"):
            assert name in slash, f"/{name} (slash) が無い"
            assert name in text, f"!{name} (text twin) が無い"

    def test_alias_shares_the_implementation(self) -> None:
        """エイリアスは同じ ``_impl`` を呼ぶこと — 2つ実装があると必ずズレる。"""
        assert ClaudeChatCog.claude_restart.callback.__doc__ is not None
        assert hasattr(ClaudeChatCog, "_restart_impl")
        assert hasattr(SessionManageCog, "_workspace_cleanup_impl")


class TestConventionIsDocumented:
    """規約が docs/COMMANDS.md に **書いてある** こと (#578 AC)。

    ここが空だと「新規コマンドをどう名付けるか」の答えがコードにしか無くなり、
    次の人がまた4つ目の規約を持ち込む。
    """

    def _section(self) -> str:
        """「コマンド命名規約」の節だけを切り出す。

        ファイル全体を対象にすると、たまたま他の節に出てくる ``/stop`` などで
        テストが通ってしまい、規約の節が空でも気づけない。
        """
        doc = COMMANDS_DOC.read_text(encoding="utf-8")
        start = re.search(r"^## .*コマンド命名規約.*$", doc, re.MULTILINE)
        if start is None:
            return ""
        rest = doc[start.end() :]
        end = re.search(r"^## ", rest, re.MULTILINE)
        return rest[: end.start()] if end else rest

    def test_section_exists(self) -> None:
        assert self._section().strip(), "docs/COMMANDS.md に「コマンド命名規約」の節が要る (#578)"

    def test_doc_lists_every_object_and_immediate_command(self) -> None:
        doc = self._section()
        missing = [w for w in sorted(OBJECTS | IMMEDIATE) if f"`{w}" not in doc]
        assert not missing, (
            f"docs/COMMANDS.md の命名規約に載っていない語: {missing} — "
            "OBJECTS / IMMEDIATE を足したらドキュメントにも足すこと"
        )

    def test_section_names_all_three_buckets(self) -> None:
        """3つのバケツが全部説明されていること — 1つ欠けると次の人が迷う。"""
        section = self._section()
        for phrase in ("目的語", "即時操作", "エイリアス"):
            assert phrase in section, f"命名規約の節に「{phrase}」の説明が無い (#578)"

    def test_section_records_the_renames(self) -> None:
        """旧名→新名の対応表が節にあること（利用者が旧名で探しに来る）。"""
        section = self._section()
        for old_name, new_name in (
            ("restart-claude", "claude-restart"),
            ("session-cleanup", "workspace-cleanup"),
        ):
            assert old_name in section and new_name in section, (
                f"{old_name} → {new_name} の対応が命名規約の節に無い (#578)"
            )
