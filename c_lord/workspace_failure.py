"""User-facing text for a workspace that could not be set up (#477).

Before a turn can run, c-lord clones the thread's checkout and opens its tmux
window. When either fails the user used to see nothing — a 🟢 that never
changed, then the stall lamp — while the cause (git's own stderr, usually) sat
in a log on a host they typically cannot read. This module turns that cause
into a message the thread can show: what failed, git's own words, and what to
check next.

Pure: no Discord, no I/O — the caller posts the text.
"""

from __future__ import annotations

import re

from .session_dir import GitCloneError

#: How much of git's stderr to show. The *last* lines are kept: git prints
#: progress first and the reason (``fatal: …``) last.
_STDERR_MAX_LINES = 6
_STDERR_MAX_CHARS = 600

#: What git says when the problem is access, not the URL's shape. GitHub answers
#: a private repo you cannot read with "Repository not found" — deliberately
#: indistinguishable from a repo that does not exist — so that counts too.
_AUTH_SIGNATURES = (
    "authentication failed",
    "could not read username",
    "could not read password",
    "terminal prompts disabled",
    "permission denied (publickey",
    "repository not found",
    "http basic: access denied",
    "the requested url returned error: 401",
    "the requested url returned error: 403",
)

#: ``scheme://user:token@host`` — a binding URL may carry a token, and git
#: echoes the URL back in its errors. Never repeat it into a Discord thread.
_URL_CREDENTIALS = re.compile(r"(?P<scheme>[a-zA-Z][\w+.-]*://)[^/@\s]+@")


def redact_credentials(text: str) -> str:
    """Drop any ``user:token@`` part from URLs in *text*."""
    return _URL_CREDENTIALS.sub(r"\g<scheme>", text)


def _stderr_summary(stderr: str) -> str:
    # ``Cloning into '<path>'...`` is progress, not the reason — and the path
    # is the bot host's, which the thread has no use for.
    lines = [
        line.rstrip()
        for line in stderr.strip().splitlines()
        if line.strip() and not line.startswith("Cloning into")
    ]
    tail = "\n".join(lines[-_STDERR_MAX_LINES:])
    if len(tail) > _STDERR_MAX_CHARS:
        tail = "…" + tail[-_STDERR_MAX_CHARS:]
    # A fence inside the fence would let the stderr escape it (and its
    # contents render as markdown — or as a mention).
    return redact_credentials(tail).replace("```", "'''")


def _fenced(text: str) -> str:
    return f"```\n{text}\n```" if text else "(git は理由を出力しませんでした)"


def _is_auth_failure(stderr: str) -> bool:
    lowered = stderr.lower()
    return any(sig in lowered for sig in _AUTH_SIGNATURES)


def _clone_failure(exc: GitCloneError) -> str:
    repo = redact_credentials(exc.repo)
    detail = _fenced(_stderr_summary(exc.stderr))
    if _is_auth_failure(exc.stderr):
        return (
            f"リポジトリ `{repo}` をクローンできませんでした — **アクセスに認証が必要です**"
            "（または URL が間違っています）。\n"
            f"{detail}\n"
            "private リポジトリなら、bot を動かしているホストの git に読み取り権限のある"
            "認証情報（`gh auth login` / SSH 鍵 / credential helper）が必要です。"
            "GitHub は権限の無い private リポジトリを「存在しない」と返すので、"
            "URL の打ち間違いも同じ表示になります。"
        )
    return (
        f"リポジトリ `{repo}` のクローンに失敗しました。\n"
        f"{detail}\n"
        "`/clord-init`（またはスレッドの repo 指定）の URL とブランチ名が正しいか、"
        "bot のホストからそのリポジトリに届くかを確認してください。"
    )


def _is_missing_tmux(exc: BaseException) -> bool:
    return isinstance(exc, FileNotFoundError) and (exc.filename == "tmux" or "tmux" in str(exc))


def describe_workspace_failure(exc: BaseException) -> str:
    """What to tell the thread when its workspace could not be set up.

    Always names the failure in the tool's own words; adds the next step when
    the cause is one we recognise.
    """
    if isinstance(exc, GitCloneError):
        return _clone_failure(exc)
    if _is_missing_tmux(exc):
        return (
            "作業場所（tmux ウィンドウ）を用意できませんでした — "
            "bot のホストで `tmux` コマンドが見つかりません。"
            "tmux がインストールされていて、bot の実行ユーザーの PATH に入っているかを"
            "確認してください。"
        )
    reason = redact_credentials(f"{type(exc).__name__}: {exc}")[:_STDERR_MAX_CHARS]
    return (
        "作業場所（リポジトリのチェックアウトと tmux ウィンドウ）を用意できませんでした。\n"
        f"{_fenced(reason.replace('```', chr(39) * 3))}\n"
        "ホストで tmux が使えるか、ディスクの空きやファイルの権限に問題が無いかを"
        "確認してください。"
    )
