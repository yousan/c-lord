"""停止まわりの利用者体験 — Issue #607（yousan 指摘 2026-08-31）。

本番で自動停止を動かして、実際に使ってみて出た指摘をまとめて直す。

1. **停止したのにスレッドが開いたままだった。** アーカイブしてから通知を投稿して
   いたため、Discord の「アーカイブ済みスレッドに投稿すると自動で開く」仕様で開き
   直っていた。実測: 自動停止した6件すべて ``archived=False``。左のリストに溜まる。
2. **5件ずつ止める必要がない。** バースト抑制のつもりだったが、対象はどれも1週間
   以上放置されている。分割は「まだ終わらない」を長引かせるだけだった。
3. **ウィンドウ番号を残したい。** ``[停止]`` にすると ``W28 │`` が落ちるので、
   「W28 で作業していた」を後から辿れない。Issue 番号だけでは足りない。
4. **「会話履歴 そのまま」は将来について嘘。** 停止する7日目には確かに残っている
   が、30日目に Claude Code 側が消す。保持期間を明記する。
5. **再開すると番号が変わる。** 停止中に別スレッドが同じ番号を取り得るため、再開後
   は新しい番号になる。旧名をスレッドに書き残して辿れるようにする。
"""

from __future__ import annotations

import pytest

from c_lord.thread_name import build_name


class TestStoppedNameKeepsTheWindowNumber:
    """指摘3。

    #512 は「番号が指す tmux ウィンドウはもう無い」という理由で番号を落として
    いた。それは「いまどこを見ればいいか」の観点では正しいが、**あとから探す**
    という用途を考えていなかった。``[停止]`` が付いているので、生きているスレッドと
    見間違えることはない。
    """

    def test_window_number_survives_the_stop(self) -> None:
        name = build_name("特商法4項目", "dead", 28, issue_ref="#587", closed=True)
        assert "W28" in name
        assert name.startswith("[停止]")

    def test_issue_ref_still_survives(self) -> None:
        name = build_name("特商法4項目", "dead", 28, issue_ref="#587", closed=True)
        assert "#587" in name

    def test_no_lamp_emoji_on_a_stopped_thread(self) -> None:
        """止まっているものに 🟢 を出さない。"""
        name = build_name("特商法4項目", "dead", 28, issue_ref="#587", closed=True)
        assert not name.startswith(("🟢", "🟡", "🔴", "⚪"))

    def test_a_thread_that_never_had_a_window_is_unaffected(self) -> None:
        name = build_name("メモ", "dead", None, closed=True)
        assert name.startswith("[停止]")
        assert "W" not in name.replace("[停止]", "")


class TestNoticeIsHonestAboutTheTranscript:
    """指摘4。

    停止するのは7日目で、その時点では会話は残っている。消えるのは30日目に
    Claude Code 側の ``cleanupPeriodDays`` が働くとき。「そのまま」とだけ書くと、
    将来について約束してしまう。
    """

    def test_transcript_row_states_the_retention_period(self) -> None:
        from c_lord.workspace_notice import (
            WorkspaceAction,
            WorkspaceReason,
            workspace_notice_embed,
        )

        e = workspace_notice_embed(
            WorkspaceAction.STOP, reason=WorkspaceReason.IDLE, idle_label="7日間"
        )
        value = {f.name: f.value for f in e.fields}["会話履歴"]
        assert "30" in value

    def test_it_still_says_the_conversation_is_there_now(self) -> None:
        """いま失われてはいない。不安にさせるのが目的ではない。"""
        from c_lord.workspace_notice import (
            WorkspaceAction,
            WorkspaceReason,
            workspace_notice_embed,
        )

        e = workspace_notice_embed(
            WorkspaceAction.STOP, reason=WorkspaceReason.IDLE, idle_label="7日間"
        )
        value = {f.name: f.value for f in e.fields}["会話履歴"]
        assert "そのまま" in value


class TestStopAllAtOnce:
    """指摘2。分割する理由がなかった。"""

    def test_default_cap_no_longer_limits_a_realistic_backlog(self) -> None:
        from c_lord.idle_stop import MAX_STOPS_PER_TICK

        assert MAX_STOPS_PER_TICK >= 100

    @pytest.mark.asyncio
    async def test_a_whole_backlog_is_stopped_in_one_tick(self) -> None:
        import datetime
        from dataclasses import replace
        from unittest.mock import AsyncMock, MagicMock

        from c_lord.database.repository import SessionRecord
        from c_lord.idle_stop import IdleStopLoop

        now = datetime.datetime(2026, 8, 31, 12, 0, 0)
        base = SessionRecord(
            thread_id=0, session_id="a" * 32, working_dir="/w", model="opus",
            origin="discord", summary=None,
            created_at="2026-01-01 00:00:00", last_used_at="2026-01-01 00:00:00",
        )
        records = [replace(base, thread_id=i) for i in range(1, 94)]

        cog = MagicMock()
        cog._close_workspace_impl = AsyncMock()
        repo = MagicMock()
        repo.list_all = AsyncMock(return_value=records)
        bot = MagicMock()
        bot.get_cog = MagicMock(return_value=cog)
        bot.get_channel = MagicMock(side_effect=lambda t: MagicMock(id=t))

        await IdleStopLoop(bot, repo, threshold_days=7, now_fn=lambda: now).tick()

        assert cog._close_workspace_impl.await_count == 93


class TestThreadActuallyEndsArchived:
    """指摘1。**本番で実測した不具合**。

    自動停止した6件すべてが ``archived=False`` だった。原因は順番で、リネーム＋
    アーカイブを先に済ませてから通知を投稿していた。Discord はアーカイブ済み
    スレッドに投稿されると自動で開くので、自分で閉じたものを自分で開けていた。

    ついでにリネームも効いていなかった（``W28 │ #587`` のまま残っていた）:
    Discord はアーカイブ済みスレッドの rename を拒否する（code 50083）ので、
    先にアーカイブすると名前の変更まで巻き添えで落ちる。

    順番を入れ替えれば両方直る。
    """

    @pytest.mark.asyncio
    async def test_the_notice_is_posted_before_the_archive(self) -> None:
        import inspect

        from c_lord.cogs.session_manage import SessionManageCog

        src = inspect.getsource(SessionManageCog._close_workspace_impl)
        post_at = src.index("await respond(embed=")
        archive_at = src.index("apply_closed_name(")
        assert post_at < archive_at, (
            "通知はアーカイブより前に投稿すること — 逆だとDiscordが自動で開き直す"
        )


class TestReopenRecordsTheOldName:
    """指摘5。

    停止中は ``[停止] W28 │ …`` だが、再開すると番号は付け直される（停止中に別の
    スレッドが 28 を取っている可能性があるので、古い番号を再利用してはいけない）。
    つまり再開した瞬間に W28 という手掛かりが名前から消える。

    消える前にスレッド本文へ書き残せば、あとから遡れる。
    """

    def test_notice_names_both_the_old_and_the_new_name(self) -> None:
        from c_lord.session_close import reopen_rename_notice

        text = reopen_rename_notice("[停止] W28 │ #587 特商法4項目", "#587 特商法4項目")

        assert "W28" in text
        assert "#587 特商法4項目" in text

    def test_it_explains_that_a_new_number_will_be_assigned(self) -> None:
        """再開直後はまだ番号が無い（窓は次のターンで作られる）ので、
        「消えた」ではなく「これから付く」と伝える。"""
        from c_lord.session_close import reopen_rename_notice

        text = reopen_rename_notice("[停止] W28 │ #587 特商法4項目", "#587 特商法4項目")

        assert "新しい" in text or "付き" in text

    def test_no_notice_when_the_name_did_not_change(self) -> None:
        """変わっていないものを「変わりました」と言わない。"""
        from c_lord.session_close import reopen_rename_notice

        assert reopen_rename_notice("#587 特商法4項目", "#587 特商法4項目") == ""

    def test_no_notice_when_the_rename_failed(self) -> None:
        from c_lord.session_close import reopen_rename_notice

        assert reopen_rename_notice("[停止] W28 │ #587", "") == ""
