"""Which directory is *this thread's* workspace — #687.

Most c-lord threads own a directory the session-dir scheme created for them:
``{base_dir}/{thread_id}``, a fresh clone made on the thread's first turn and
refreshed on every later one (:meth:`SessionDirManager.create_session_dir`).

Scheduled runs are the exception.  A scheduled task names a **fixed checkout**
(``scheduled_tasks.working_dir`` — e.g. ``/home/yousan/c-lord-audit``) and every
run of that task works there; that is the point of the feature.  Nothing clones
it, and nothing may re-point it.

Before #687 the reply path did not know the difference: it called
``create_session_dir`` unconditionally, so answering a ``[Scheduled]`` thread
cloned a directory nobody would ever run in, overwrote ``sessions.working_dir``
with it, and — because the transcript mirror derives its project dir from that
value — sent the mirror to tail an empty directory after the next bot restart.
Claude would keep working in the real checkout while its answers went nowhere.

This module holds the one rule that tells the two apart, so the reply path and
anything else that needs to ask cannot answer it differently.
"""

from __future__ import annotations

from pathlib import Path

__all__ = ["external_workspace"]


def external_workspace(
    recorded: str | None,
    *,
    base_dir: str | None,
    thread_id: int,
) -> str | None:
    """The recorded workspace to reuse verbatim, or ``None`` to use the session dir.

    Returns *recorded* only when it is a directory that exists **and** is not the
    session dir this thread would otherwise get.  Everything else returns
    ``None``, which leaves the caller on the pre-#687 path:

    * no record yet — a thread's first turn, which is exactly when the session
      dir must be created;
    * the recorded dir *is* ``{base_dir}/{thread_id}`` — an ordinary thread, so
      ``create_session_dir`` still runs and still refreshes its co-author hook
      (#518).  This is why the change is a no-op for every normal thread;
    * the recorded dir is gone (deleted checkout, moved host).  Reusing a path
      that is not there would start Claude nowhere; falling back to the session
      dir at least leaves the thread usable, and the stale value is corrected on
      the next save.

    ``base_dir`` is ``None`` when the channel has no session-dir manager at all;
    there is then no session dir to prefer, so any existing recorded dir wins.
    """
    if not recorded:
        return None
    if base_dir is not None and Path(recorded) == Path(base_dir) / str(thread_id):
        return None
    if not Path(recorded).is_dir():
        return None
    return recorded
