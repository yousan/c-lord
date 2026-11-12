"""The in-memory ledger of sessions that are running a turn (``c_lord.concurrency``).

``/workspace-cleanup`` asks it which workspaces are in use, so what matters is
that a thread appears exactly while it is registered, and never twice.
"""

from __future__ import annotations

import threading

import pytest

from c_lord.concurrency import ActiveSession, SessionRegistry


@pytest.fixture
def registry() -> SessionRegistry:
    return SessionRegistry()


def _ids(sessions: list[ActiveSession]) -> list[int]:
    return [session.thread_id for session in sessions]


class TestActiveSession:
    def test_carries_thread_description_and_directory(self) -> None:
        """An entry is plain data: what was put in is what is read back."""
        session = ActiveSession(thread_id=7, description="fix the build", working_dir="/srv/app")

        assert (session.thread_id, session.description, session.working_dir) == (
            7,
            "fix the build",
            "/srv/app",
        )

    def test_working_dir_is_optional(self) -> None:
        """A session that was not given a directory has none — not an empty string."""
        assert ActiveSession(thread_id=7, description="chat").working_dir is None

    def test_is_mutable(self) -> None:
        """``SessionRegistry.update`` rewrites entries in place, so they cannot be frozen."""
        session = ActiveSession(thread_id=7, description="before")
        session.description = "after"
        assert session.description == "after"


class TestRegistering:
    def test_a_new_registry_is_empty(self, registry: SessionRegistry) -> None:
        assert registry.list_active() == []

    def test_a_registered_session_is_listed(self, registry: SessionRegistry) -> None:
        registry.register(1, "write docs", "/work/docs")

        assert registry.list_active() == [ActiveSession(1, "write docs", "/work/docs")]

    def test_working_dir_defaults_to_none(self, registry: SessionRegistry) -> None:
        registry.register(1, "write docs")

        assert registry.list_active()[0].working_dir is None

    def test_sessions_are_listed_in_the_order_they_were_registered(
        self, registry: SessionRegistry
    ) -> None:
        for thread_id in (30, 10, 20):
            registry.register(thread_id, f"task {thread_id}")

        assert _ids(registry.list_active()) == [30, 10, 20]

    def test_registering_a_thread_again_replaces_its_entry(self, registry: SessionRegistry) -> None:
        """One thread, one entry: the second registration wins and nothing is duplicated."""
        registry.register(1, "first", "/a")
        registry.register(1, "second", "/b")

        assert registry.list_active() == [ActiveSession(1, "second", "/b")]

    def test_the_list_is_the_callers_own_copy(self, registry: SessionRegistry) -> None:
        """Emptying a returned list must not empty the registry."""
        registry.register(1, "task")
        registry.list_active().clear()

        assert _ids(registry.list_active()) == [1]


class TestUnregistering:
    def test_an_unregistered_session_is_no_longer_listed(self, registry: SessionRegistry) -> None:
        registry.register(1, "one")
        registry.register(2, "two")

        registry.unregister(1)

        assert _ids(registry.list_active()) == [2]

    def test_unregistering_an_unknown_thread_is_harmless(self, registry: SessionRegistry) -> None:
        """A turn's cleanup may run for a thread that never got registered."""
        registry.register(1, "one")

        registry.unregister(999)

        assert _ids(registry.list_active()) == [1]


class TestListOthers:
    def test_leaves_out_the_asking_thread(self, registry: SessionRegistry) -> None:
        for thread_id in (1, 2, 3):
            registry.register(thread_id, f"task {thread_id}")

        assert _ids(registry.list_others(2)) == [1, 3]

    def test_is_empty_when_the_asking_thread_is_the_only_one(
        self, registry: SessionRegistry
    ) -> None:
        registry.register(1, "alone")

        assert registry.list_others(1) == []

    def test_is_everything_when_the_asking_thread_is_not_registered(
        self, registry: SessionRegistry
    ) -> None:
        registry.register(1, "one")

        assert _ids(registry.list_others(999)) == [1]


class TestUpdating:
    def test_description_alone_can_be_changed(self, registry: SessionRegistry) -> None:
        registry.register(1, "before", "/work")

        registry.update(1, description="after")

        assert registry.list_active() == [ActiveSession(1, "after", "/work")]

    def test_working_dir_alone_can_be_changed(self, registry: SessionRegistry) -> None:
        registry.register(1, "task")

        registry.update(1, working_dir="/work/new")

        assert registry.list_active() == [ActiveSession(1, "task", "/work/new")]

    def test_both_fields_can_be_changed_at_once(self, registry: SessionRegistry) -> None:
        registry.register(1, "before", "/old")

        registry.update(1, description="after", working_dir="/new")

        assert registry.list_active() == [ActiveSession(1, "after", "/new")]

    def test_an_update_without_fields_changes_nothing(self, registry: SessionRegistry) -> None:
        """``None`` means "leave as is" — it does not clear a field."""
        registry.register(1, "task", "/work")

        registry.update(1)

        assert registry.list_active() == [ActiveSession(1, "task", "/work")]

    def test_updating_an_unknown_thread_does_not_create_it(self, registry: SessionRegistry) -> None:
        registry.update(999, description="ghost")

        assert registry.list_active() == []

    def test_updates_are_keyword_only(self, registry: SessionRegistry) -> None:
        """A positional second argument would be ambiguous between the two fields."""
        registry.register(1, "task")

        with pytest.raises(TypeError):
            registry.update(1, "after")  # type: ignore[misc]


def test_registrations_from_several_os_threads_are_all_kept(registry: SessionRegistry) -> None:
    """The registry is shared with worker threads; none of their entries may be lost."""
    per_worker = 200

    def work(offset: int) -> None:
        for n in range(per_worker):
            registry.register(offset + n, "task")

    workers = [threading.Thread(target=work, args=(k * per_worker,)) for k in range(4)]
    for worker in workers:
        worker.start()
    for worker in workers:
        worker.join()

    assert sorted(_ids(registry.list_active())) == list(range(4 * per_worker))
