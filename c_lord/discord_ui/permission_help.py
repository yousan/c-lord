"""Actionable permission-error messages for Discord API failures (#443).

When a Discord operation fails with ``discord.Forbidden`` (50001 Missing Access
/ 50013 Missing Permissions), the API tells us only that access was denied — not
*which* permission is missing.  These helpers translate a known operation into
the exact permissions the bot needs, so a user can self-diagnose instead of
hitting a silent failure or a generic "interaction failed".
"""

from __future__ import annotations

import discord


class ThreadCreateForbiddenError(Exception):
    """Signals that ``channel.create_thread()`` failed with ``discord.Forbidden``.

    Raised by ``spawn_session`` so the caller (``/clord``) can surface
    :func:`create_thread_permission_help` *through the interaction* — the parent
    channel is unreachable (the bot was denied View Channel), so posting a notice
    there would fail with another Forbidden.  An interaction (ephemeral) reply
    works regardless of channel permissions.
    """


_CREATE_THREAD_PERMISSION_HELP = (
    "❌ このチャンネルでスレッドを作成できませんでした（権限不足）。\n"
    "Bot（または Bot のロール）に、このチャンネルで次を付与してください:\n"
    "・**チャンネルを見る** (View Channel)\n"
    "・**公開スレッドの作成** (Create Public Threads)\n"
    "・**メッセージの送信** (Send Messages)\n"
    "・**スレッドでメッセージを送信** (Send Messages in Threads)\n"
    "※ 非公開チャンネルでは @everyone を許可せず、Bot のロールにだけ付けてください。"
)


def create_thread_permission_help() -> str:
    """Return a message naming the permissions needed to create a thread.

    Used when ``create_thread()`` is denied (the bot lacks View Channel and/or
    Create Public Threads on the channel).
    """
    return _CREATE_THREAD_PERMISSION_HELP


# --- Generic last-resort messages (#444 safety net) ----------------------------
# Shown when an *unhandled* exception reaches a global error handler (slash/text
# command, or a View button/modal callback). They must be actionable but must
# NOT leak the raw exception text or a traceback to the user.

PERMISSION_DENIED_HELP = (
    "❌ Bot に必要な権限が無い可能性があります。対象チャンネルで Bot（またはそのロール）に "
    "「チャンネルを見る」「メッセージの送信」「公開スレッドの作成」"
    "「スレッドでメッセージを送信」などが付与されているか確認してください。"
)

UNEXPECTED_ERROR_HELP = (
    "⚠️ 処理中に予期せぬエラーが発生しました。少し待ってもう一度試してください。"
    "繰り返す場合は bot のログを確認してください。"
)


def command_error_help(error: BaseException) -> str:
    """Map a command/interaction exception to a sanitized, user-facing message.

    discord.py wraps callback exceptions in ``CommandInvokeError`` (both the
    app-command and text-command variants expose the cause as ``.original``), so
    we unwrap one level before classifying.  A ``discord.Forbidden`` becomes a
    permission hint; anything else becomes a generic "unexpected error" message
    so the user never sees a raw traceback.
    """
    original = getattr(error, "original", error)
    if isinstance(original, discord.Forbidden):
        return PERMISSION_DENIED_HELP
    return UNEXPECTED_ERROR_HELP
