"""Unit tests for the permission-help messages (#443)."""

from __future__ import annotations

from c_lord.discord_ui.permission_help import (
    ThreadCreateForbiddenError,
    create_thread_permission_help,
)


def test_create_thread_help_names_all_required_permissions() -> None:
    """The thread-creation help must name every permission a user might be missing.

    The #443 incident: the user granted "Manage Threads" because the old help
    only mentioned send permissions.  The real blocker was View Channel +
    Create Public Threads, so the message must name all four.
    """
    msg = create_thread_permission_help()
    for needle in (
        "チャンネルを見る",
        "公開スレッドの作成",
        "メッセージの送信",
        "スレッドでメッセージを送信",
    ):
        assert needle in msg, f"missing permission name: {needle}"


def test_thread_create_forbidden_is_exception() -> None:
    assert issubclass(ThreadCreateForbiddenError, Exception)
