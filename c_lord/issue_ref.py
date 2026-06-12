"""Issue/PR number detection for Discord thread names (#414).

Pure, side-effect-free extractors used by the thread-naming flow to derive the
``#<number>`` token shown in a thread title (``W3 │ #404 認証リファクタ``).

Two sources, used in the naming flow's priority order:

1. :func:`extract_from_branch` — the git branch of the session clone
   (e.g. ``fix/404-foo`` → ``"404"``). The authoritative "what am I working on
   now" signal; re-read on every rename so a branch switch is followed.
2. :func:`extract_from_text` — a ``#NNN`` mention or a GitHub issue/PR URL in a
   message body. Used only as a fallback when the branch carries no number, and
   only from the thread's first message (a casual mid-conversation ``#123``
   must not hijack the title).

Both return a bare digit string (``"404"``) or ``None``. The leading ``#`` is
added by the name builder, not stored here. The matchers are deliberately
conservative: a number is returned only when it is clearly an issue/PR
reference, never an incidental digit (the ``2`` in ``feature/v2-api`` etc.).
"""

from __future__ import annotations

import re

# Branch names that carry no issue association — never derive a number from them.
_BASE_BRANCHES = frozenset({"main", "master", "develop", "dev", "trunk", "release"})

# Conservative branch matcher. A number qualifies only when it is:
#   (a) at the very start of the branch        — ``404-foo`` / ``#404``
#   (b) at the start of a path segment         — ``fix/404-foo``
#   (c) right after an issue/PR keyword         — ``issue-404`` / ``gh-404`` / ``pr_7``
# and is followed by end-of-string or a separator. This excludes glued digits
# (``v2``, ``oauth2``) and mid-segment counters (``step-2``).
_BRANCH_RE = re.compile(
    r"""
    (?:
        ^\#?(?P<a>\d{1,7})
      | /\#?(?P<b>\d{1,7})
      | (?:issue|issues|gh|pr|bug|fix|hotfix|feat|feature)[-_/]?\#?(?P<c>\d{1,7})
    )
    (?=$|[/_-])
    """,
    re.IGNORECASE | re.VERBOSE,
)

# A GitHub issue/pull URL — the most reliable in-text signal.
_TEXT_URL_RE = re.compile(
    r"github\.com/[^/\s]+/[^/\s]+/(?:issues|pull)/(\d{1,7})",
    re.IGNORECASE,
)
# A bare ``#NNN`` reference, not glued to a preceding word (avoids hex like
# ``#fff`` because the body must be all digits, and avoids ``abc#1``).
_TEXT_HASH_RE = re.compile(r"(?:^|[^\w#])#(\d{1,7})(?=$|\D)")


def extract_from_branch(branch: str | None) -> str | None:
    """Return the issue/PR number encoded in a git ``branch`` name, or ``None``.

    Conservative by design (see module docstring): returns a number only for
    clear issue/PR conventions and never for base branches or incidental digits.
    """
    if not branch:
        return None
    name = branch.strip()
    if name.lower() in _BASE_BRANCHES:
        return None
    match = _BRANCH_RE.search(name)
    if match is None:
        return None
    return match.group("a") or match.group("b") or match.group("c")


def extract_from_text(text: str | None) -> str | None:
    """Return the first issue/PR number referenced in ``text``, or ``None``.

    Recognises a GitHub ``issues``/``pull`` URL and a bare ``#NNN`` mention.
    A bare number with no ``#`` is *not* treated as a reference.
    """
    if not text:
        return None
    url = _TEXT_URL_RE.search(text)
    if url is not None:
        return url.group(1)
    hash_ref = _TEXT_HASH_RE.search(text)
    if hash_ref is not None:
        return hash_ref.group(1)
    return None
