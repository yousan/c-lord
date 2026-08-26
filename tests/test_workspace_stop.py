"""停止 — the docker-stopping half of the lifecycle. Issue #574.

#540 renamed this operation from 終了 to 停止 and gave it a second job.

The word had to change because it is about to fire **automatically** after 7
days. 「終了」 says *it is over*, but nothing is: the working directory, the
conversation and the DB volume all survive and ``/workspace-start`` brings it
straight back. A notice the user did not ask for, telling them their work
"ended", is a false alarm — 「停止」 is simply true.

The second job is stopping the dev environment. Until now ``/close-workspace``
left containers running, which is how 12 supabase containers ended up on the
production host holding ports 55321-55327 with no owner.
"""

from __future__ import annotations

import pytest

from c_lord.devenv import DevContainer
from c_lord.thread_name import CLOSED_MARK, LEGACY_CLOSED_MARKS, parse_topic_from_name


def _c(name: str, ports: tuple[int, ...] = (), status: str = "running") -> DevContainer:
    return DevContainer(
        container_id=f"id-{name}", name=name, status=status, ports=ports,
        project=None, source="mount",
    )


class TestMarkerWording:
    def test_marker_is_teishi_not_shuuryou(self) -> None:
        assert CLOSED_MARK == "[停止]"

    def test_legacy_marker_is_still_recognised(self) -> None:
        """Threads closed before the rename still carry 「[終了]」 in their name.

        They must keep parsing, or every one of them silently gains 「[終了]」 as
        part of its topic on the next rename.
        """
        assert "[終了]" in LEGACY_CLOSED_MARKS

    @pytest.mark.parametrize("mark", ["[停止]", "[終了]"])
    def test_both_marks_are_stripped_from_the_topic(self, mark: str) -> None:
        assert parse_topic_from_name(f"{mark} W3 │ メモリ設計") == "メモリ設計"

    def test_a_topic_that_merely_mentions_the_word_is_untouched(self) -> None:
        """Only the leading marker is a marker."""
        assert parse_topic_from_name("W3 │ 停止条件の設計") == "停止条件の設計"


class TestStoppingTheDevEnvironment:
    @pytest.mark.asyncio
    async def test_running_containers_are_stopped(self, monkeypatch) -> None:
        from c_lord import devenv

        stopped: list[str] = []

        async def fake_docker(argv: list[str]) -> tuple[int, str]:
            if "stop" in argv:
                stopped.extend(a for a in argv[2:] if not a.startswith("-"))
            return 0, ""

        monkeypatch.setattr(devenv, "_docker", fake_docker)
        result = await devenv.stop_containers([_c("db", (5432,)), _c("web", (8080,))])

        assert sorted(stopped) == ["db", "web"]
        assert sorted(result) == ["db", "web"]

    @pytest.mark.asyncio
    async def test_already_stopped_containers_are_not_touched(self, monkeypatch) -> None:
        """Nothing to do, and issuing a stop would only invite an error."""
        from c_lord import devenv

        calls: list[list[str]] = []

        async def fake_docker(argv: list[str]) -> tuple[int, str]:
            calls.append(argv)
            return 0, ""

        monkeypatch.setattr(devenv, "_docker", fake_docker)
        result = await devenv.stop_containers([_c("old", (5432,), status="exited")])

        assert calls == []
        assert result == []

    @pytest.mark.asyncio
    async def test_a_docker_failure_is_reported_not_raised(self, monkeypatch) -> None:
        """A stop that fails must not break the close — the tmux window is
        already gone and the user is owed a notice either way."""
        from c_lord import devenv

        async def fake_docker(argv: list[str]) -> tuple[int, str]:
            return 1, ""

        monkeypatch.setattr(devenv, "_docker", fake_docker)

        assert await devenv.stop_containers([_c("db", (5432,))]) == []

    @pytest.mark.asyncio
    async def test_no_containers_makes_no_docker_call(self, monkeypatch) -> None:
        from c_lord import devenv

        calls: list[list[str]] = []

        async def fake_docker(argv: list[str]) -> tuple[int, str]:
            calls.append(argv)
            return 0, ""

        monkeypatch.setattr(devenv, "_docker", fake_docker)

        assert await devenv.stop_containers([]) == []
        assert calls == []
