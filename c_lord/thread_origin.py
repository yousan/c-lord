"""Was this Discord thread ever c-lord's? — #556, #551.

The obvious test — "does it have a ``sessions`` row?" — cannot answer this, and
the reason is #554: c-lord deletes every row untouched for 30 days, on every
startup. A month-old c-lord thread and a thread c-lord never touched look
*identical* by that test, and treating them the same breaks in both directions:

* Answer "ours" to both and you get #556 — Grafana's server-alert thread, made
  by hand under a ``/clord-init`` channel, told during incidents that its alerts
  could not be restored.
* Answer "not ours" to both and you strand every #554 victim: ``W3 │ Qiita``
  still has its checkout, half-written article and images included, and would
  become permanently unreachable.

So the evidence used here is deliberately the things the row's deletion does not
touch:

* **who created the thread** — ``/clord`` threads are created by the bot, and
  Discord keeps ``owner_id`` forever. The widest signal by far: bindings number
  21 against 243 session rows, so binding alone would miss most threads.
* **the checkout on disk** — ``<base>/<channel_id>/<thread_id>/``. The most
  direct proof that c-lord worked here; 159 of 564 session dirs on yousan's
  instance outlived their row.
* **a repo binding** — a ``/clord-thread-init`` row.

Any one of them is enough. They are alternatives, not a checklist: #554 can take
the row and Claude Code can take the transcript, but nothing removes all three at
once, and requiring agreement would fail exactly the threads this is meant to
rescue.

One module, two callers, on purpose. #556 uses it to decide who the 「復元でき
ません」 notice is for; #551 uses it as the middle branch of what ``/clord`` does
in a thread. Letting those grow separate spellings of "is this ours" is the shape
of bug #538 — a promise and a check that were never the same test.
"""

from __future__ import annotations

import logging
from dataclasses import dataclass
from pathlib import Path

logger = logging.getLogger(__name__)

__all__ = ["Origin", "inspect_origin", "session_dir_exists"]


@dataclass(frozen=True)
class Origin:
    """Which traces of c-lord this thread still carries."""

    #: Discord says the bot created the thread (``thread.owner_id``).
    bot_created: bool = False
    #: A session checkout for this thread is still on disk.
    session_dir: bool = False
    #: A ``thread_repo_bindings`` row names this thread.
    binding: bool = False

    @property
    def is_clords(self) -> bool:
        """True when *any* trace was found — see the module docstring on why."""
        return self.bot_created or self.session_dir or self.binding


def session_dir_exists(base_dir: str | None, thread_id: int) -> bool:
    """Whether ``<base_dir>/<thread_id>`` is a directory.

    ``base_dir`` is a :class:`~c_lord.session_dir.SessionDirManager`'s own base,
    which already includes the channel id. ``None`` (no binding resolves for the
    channel) simply yields False — the other two signals still apply.

    Never raises. This is on the path of every message that lands in a thread
    with no session row, including a webhook mid-incident; an unreadable or
    malformed path must read as "no evidence", never as an error.
    """
    if not base_dir:
        return False
    try:
        return (Path(base_dir) / str(thread_id)).is_dir()
    except (OSError, ValueError):
        logger.debug("session_dir_exists: unusable base_dir %r", base_dir)
        return False


def inspect_origin(
    *,
    thread_owner_id: int | None,
    bot_user_id: int | None,
    session_dir_base: str | None,
    thread_id: int,
    has_binding: bool = False,
) -> Origin:
    """Gather the evidence that ``thread_id`` was once a c-lord thread.

    ``bot_user_id`` is ``None`` before login, and an unknown identity must not
    match an unknown ``thread_owner_id`` — "both None" would otherwise claim
    every thread in the guild during startup.
    """
    bot_created = (
        bot_user_id is not None and thread_owner_id is not None and thread_owner_id == bot_user_id
    )
    return Origin(
        bot_created=bot_created,
        session_dir=session_dir_exists(session_dir_base, thread_id),
        binding=has_binding,
    )
