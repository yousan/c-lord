"""Workspace lifecycle notices — Issue #571.

#540 settled that the notice is not a label but an *inventory*: it always says
what stopped **and what is still there**. That second half is what stops an
automatic sleep/stop from reading as "my work was deleted" — the user did not
ask for it, so the message has to answer the question they will actually have.

It also settled that "ワークスペース" is a word c-lord had never taught anyone
(2 occurrences in Japanese UI strings, both unrelated), so every notice names it
through a word people do know: 「このスレッドのワークスペース」.
"""

from __future__ import annotations

import pytest

from c_lord.devenv import DevContainer
from c_lord.workspace_notice import (
    WorkspaceAction,
    WorkspaceReason,
    workspace_notice_embed,
)


def _c(name: str, ports: tuple[int, ...] = (), status: str = "running") -> DevContainer:
    return DevContainer(
        container_id=f"id-{name}", name=name, status=status, ports=ports,
        project=None, source="mount",
    )


def _text(embed) -> str:
    parts = [embed.title or "", embed.description or ""]
    for f in embed.fields:
        parts += [f.name, f.value]
    return "\n".join(parts)


class TestAlwaysNamesTheThreadFirst:
    @pytest.mark.parametrize("action", list(WorkspaceAction))
    def test_title_bridges_from_thread_to_workspace(self, action: WorkspaceAction) -> None:
        """Never "ワークスペースを停止しました" on its own — the reader has not
        been taught that word."""
        e = workspace_notice_embed(action, reason=WorkspaceReason.MANUAL)
        assert "このスレッドのワークスペース" in (e.title or "")


class TestAlwaysListsWhatSurvives:
    @pytest.mark.parametrize("action", list(WorkspaceAction))
    def test_conversation_history_is_always_reported_as_kept(
        self, action: WorkspaceAction
    ) -> None:
        """True for every action, delete included — and the whole reason the
        inventory exists."""
        e = workspace_notice_embed(action, reason=WorkspaceReason.IDLE)
        assert "会話履歴" in _text(e)

    def test_delete_says_the_working_copy_is_gone_but_history_is_not(self) -> None:
        e = workspace_notice_embed(WorkspaceAction.DELETE, reason=WorkspaceReason.IDLE)
        fields = {f.name: f.value for f in e.fields}
        assert "削除" in fields["作業フォルダ"]
        assert "そのまま" in fields["会話履歴"]

    def test_volume_is_reported_as_kept_even_on_delete(self) -> None:
        """#540: DB data is never destroyed by any lifecycle action."""
        e = workspace_notice_embed(WorkspaceAction.DELETE, reason=WorkspaceReason.IDLE)
        fields = {f.name: f.value for f in e.fields}
        assert "そのまま" in fields["DBのデータ (volume)"]


class TestSleepKeepsDockerRunning:
    def test_sleep_reports_docker_as_still_running(self) -> None:
        e = workspace_notice_embed(
            WorkspaceAction.SLEEP,
            reason=WorkspaceReason.IDLE,
            containers=[_c("supabase_db", (55322,))],
        )
        fields = {f.name: f.value for f in e.fields}
        assert "動いたまま" in fields["開発環境 (docker)"]

    def test_sleep_warns_about_held_ports(self) -> None:
        """The one piece of information the user cannot do without: a port still
        held will collide with the next environment they start."""
        e = workspace_notice_embed(
            WorkspaceAction.SLEEP,
            reason=WorkspaceReason.IDLE,
            containers=[_c("supabase_db", (55322,)), _c("supabase_studio", (55323,))],
        )
        text = _text(e)
        assert "55322" in text and "55323" in text

    def test_sleep_without_containers_has_no_port_warning(self) -> None:
        e = workspace_notice_embed(WorkspaceAction.SLEEP, reason=WorkspaceReason.IDLE)
        assert "ポート" not in _text(e)

    def test_stopped_containers_are_not_warned_about(self) -> None:
        """An exited container holds no port."""
        e = workspace_notice_embed(
            WorkspaceAction.SLEEP,
            reason=WorkspaceReason.IDLE,
            containers=[_c("old", (55399,), status="exited")],
        )
        assert "55399" not in _text(e)


class TestStopReleasesPorts:
    def test_stop_reports_released_ports(self) -> None:
        e = workspace_notice_embed(
            WorkspaceAction.STOP,
            reason=WorkspaceReason.MANUAL,
            containers=[_c("supabase_db", (55322,))],
        )
        fields = {f.name: f.value for f in e.fields}
        assert "停止" in fields["開発環境 (docker)"]
        assert "55322" in fields["開発環境 (docker)"]


class TestAutomaticDiffersByExactlyOneLine:
    """#540: automatic and manual must share one code path, so the only thing
    ``reason`` may change is the explanatory line."""

    def _fields(self, reason: WorkspaceReason) -> dict[str, str]:
        e = workspace_notice_embed(
            WorkspaceAction.STOP, reason=reason, idle_label="7日間",
            containers=[_c("db", (5432,))],
        )
        return {f.name: f.value for f in e.fields}

    def test_fields_are_identical_between_manual_and_idle(self) -> None:
        assert self._fields(WorkspaceReason.MANUAL) == self._fields(WorkspaceReason.IDLE)

    def test_only_the_idle_variant_explains_why_it_happened(self) -> None:
        manual = workspace_notice_embed(WorkspaceAction.STOP, reason=WorkspaceReason.MANUAL)
        idle = workspace_notice_embed(
            WorkspaceAction.STOP, reason=WorkspaceReason.IDLE, idle_label="7日間"
        )
        assert "7日間" not in (manual.description or "")
        assert "7日間" in (idle.description or "")

    def test_titles_match(self) -> None:
        manual = workspace_notice_embed(WorkspaceAction.STOP, reason=WorkspaceReason.MANUAL)
        idle = workspace_notice_embed(
            WorkspaceAction.STOP, reason=WorkspaceReason.IDLE, idle_label="7日間"
        )
        assert manual.title == idle.title


class TestVocabulary:
    @pytest.mark.parametrize("action", list(WorkspaceAction))
    @pytest.mark.parametrize("reason", list(WorkspaceReason))
    def test_never_says_sagyou_session(
        self, action: WorkspaceAction, reason: WorkspaceReason
    ) -> None:
        """#540 retired 「作業セッション」: the same state was being described
        with two different nouns in adjacent messages."""
        e = workspace_notice_embed(action, reason=reason, idle_label="7日間")
        assert "作業セッション" not in _text(e)
