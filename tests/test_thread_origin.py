"""Was this thread ever c-lord's? — the one judgment, shared by #556 and #551.

Two features need the same answer and must not each grow their own version of
it, because a disagreement between two copies of "is this ours" is exactly what
#538 was.

* **#556** — who the 「復元できません」 notice is for. The guard shipped in #545
  asked whether the *channel* was ours, which every thread under a
  ``/clord-init`` binding satisfies, so Grafana's alert thread got the notice.
* **#551** — what ``/clord`` does inside a thread. A thread with no session row
  is refused, *except* when it is a c-lord thread that merely lost its row to
  the 30-day sweep (#554) — refusing those would strand them permanently.

The row alone cannot answer it: #554 deletes the row while leaving everything
else in place. So the evidence is what the row's deletion does *not* touch —
who created the thread, the checkout on disk, the repo binding. Any one of them
is enough; they are alternatives, not a checklist.
"""

from __future__ import annotations

from c_lord.thread_origin import Origin, inspect_origin, session_dir_exists

BOT_ID = 1475105094071750818
THREAD_ID = 1508626302813601843  # the real W3 │ Qiita thread


# ── the disk check ───────────────────────────────────────────────────────────


class TestSessionDirExists:
    def test_finds_the_checkout(self, tmp_path) -> None:
        (tmp_path / str(THREAD_ID)).mkdir()
        assert session_dir_exists(str(tmp_path), THREAD_ID) is True

    def test_absent_when_never_cloned(self, tmp_path) -> None:
        assert session_dir_exists(str(tmp_path), THREAD_ID) is False

    def test_a_file_is_not_a_session_dir(self, tmp_path) -> None:
        (tmp_path / str(THREAD_ID)).write_text("x", encoding="utf-8")
        assert session_dir_exists(str(tmp_path), THREAD_ID) is False

    def test_no_base_dir_is_not_an_error(self) -> None:
        assert session_dir_exists(None, THREAD_ID) is False

    def test_an_unusable_path_is_not_an_error(self) -> None:
        """This runs on every message into an untracked thread — it may not raise."""
        assert session_dir_exists("\0bad", THREAD_ID) is False


# ── the verdict ──────────────────────────────────────────────────────────────


class TestInspectOrigin:
    def test_a_thread_the_bot_created_is_ours(self) -> None:
        o = inspect_origin(
            thread_owner_id=BOT_ID, bot_user_id=BOT_ID, session_dir_base=None, thread_id=1
        )
        assert o.bot_created is True
        assert o.is_clords is True

    def test_a_thread_a_person_created_is_not_ours_on_that_evidence(self) -> None:
        o = inspect_origin(
            thread_owner_id=42, bot_user_id=BOT_ID, session_dir_base=None, thread_id=1
        )
        assert o.bot_created is False
        assert o.is_clords is False

    def test_an_unknown_bot_identity_does_not_claim_the_thread(self) -> None:
        """Before login ``bot.user`` is None. Unknown must not read as a match —
        that would make every thread ours during startup."""
        o = inspect_origin(
            thread_owner_id=None, bot_user_id=None, session_dir_base=None, thread_id=1
        )
        assert o.bot_created is False
        assert o.is_clords is False

    def test_a_surviving_checkout_is_enough_on_its_own(self, tmp_path) -> None:
        """The #554 case: the sweep took the row, the clone is still there.

        This is the evidence that keeps ``W3 │ Qiita`` reachable — a person made
        no thread here, the bot did, and the article is still on disk.
        """
        (tmp_path / str(THREAD_ID)).mkdir()
        o = inspect_origin(
            thread_owner_id=42,
            bot_user_id=BOT_ID,
            session_dir_base=str(tmp_path),
            thread_id=THREAD_ID,
        )
        assert o.session_dir is True
        assert o.is_clords is True

    def test_a_repo_binding_is_enough_on_its_own(self) -> None:
        o = inspect_origin(
            thread_owner_id=42,
            bot_user_id=BOT_ID,
            session_dir_base=None,
            thread_id=1,
            has_binding=True,
        )
        assert o.is_clords is True

    def test_nothing_at_all_is_a_plain_conversation_thread(self, tmp_path) -> None:
        """Grafana's alert thread: made by a person, never cloned, never bound."""
        o = inspect_origin(
            thread_owner_id=42,
            bot_user_id=BOT_ID,
            session_dir_base=str(tmp_path),
            thread_id=THREAD_ID,
            has_binding=False,
        )
        assert o == Origin(bot_created=False, session_dir=False, binding=False)
        assert o.is_clords is False

    def test_the_sources_are_alternatives_not_a_checklist(self, tmp_path) -> None:
        """Any single one suffices — #554 can remove some but never all at once."""
        base = {
            "thread_owner_id": 42,
            "bot_user_id": BOT_ID,
            "session_dir_base": str(tmp_path),
            "thread_id": THREAD_ID,
            "has_binding": False,
        }
        assert inspect_origin(**{**base, "thread_owner_id": BOT_ID}).is_clords is True
        assert inspect_origin(**{**base, "has_binding": True}).is_clords is True
        (tmp_path / str(THREAD_ID)).mkdir()
        assert inspect_origin(**base).is_clords is True  # checkout alone
        assert inspect_origin(**base).bot_created is False
