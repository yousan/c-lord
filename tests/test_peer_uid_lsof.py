"""Who connected, on macOS: ``lsof`` instead of ``/proc/net/tcp`` (#899 A).

The rule from #457 does not change: anything short of exactly one UID for the
exact connection is ``None``, and ``None`` is refused. The trap specific to
lsof is that *our own* accepted socket is listed too — its remote end is the
peer — so matching on "mentions the peer's port" would hand every caller the
bot's own UID. Only the row whose **local** end is the peer counts.
"""

from __future__ import annotations

from c_lord.ext.peer_uid import resolve_peer_uid

PEER = ("127.0.0.1", 52344)
OURS = ("127.0.0.1", 8801)


def _lsof(*rows: tuple[int, int, str]):
    """Fake ``lsof -F pun``: one (pid, uid, name) per open socket."""
    calls: list[list[str]] = []

    def run(args: list[str]) -> str | None:
        calls.append(args)
        return "".join(f"p{pid}\nu{uid}\nf3\nn{name}\n" for pid, uid, name in rows)

    run.calls = calls  # type: ignore[attr-defined]
    return run


def _resolve(lsof, peer=PEER, ours=OURS, platform="darwin"):
    return resolve_peer_uid(peer, ours, tables=(), platform=platform, lsof=lsof)


class TestDarwin:
    def test_the_client_side_row_names_the_caller(self) -> None:
        lsof = _lsof(
            (900, 502, "127.0.0.1:52344->127.0.0.1:8801"),  # the caller (another user)
            (100, 501, "127.0.0.1:8801->127.0.0.1:52344"),  # our own accepted socket
        )
        assert _resolve(lsof) == 502

    def test_our_own_accepted_socket_is_not_the_caller(self) -> None:
        """Another user's process is invisible to a non-root lsof: only our side shows."""
        lsof = _lsof((100, 501, "127.0.0.1:8801->127.0.0.1:52344"))
        assert _resolve(lsof) is None

    def test_same_user_caller_resolves_to_our_uid(self) -> None:
        lsof = _lsof(
            (900, 501, "127.0.0.1:52344->127.0.0.1:8801"),
            (100, 501, "127.0.0.1:8801->127.0.0.1:52344"),
        )
        assert _resolve(lsof) == 501

    def test_disagreeing_rows_resolve_to_none(self) -> None:
        lsof = _lsof(
            (900, 501, "127.0.0.1:52344->127.0.0.1:8801"),
            (901, 502, "127.0.0.1:52344->127.0.0.1:8801"),
        )
        assert _resolve(lsof) is None

    def test_a_similar_port_is_not_a_match(self) -> None:
        lsof = _lsof((900, 502, "127.0.0.1:523440->127.0.0.1:8801"))
        assert _resolve(lsof) is None

    def test_lsof_failure_resolves_to_none(self) -> None:
        assert _resolve(lambda args: None) is None

    def test_ipv6_rows_use_brackets(self) -> None:
        lsof = _lsof((900, 501, "[::1]:52344->[::1]:8801"))
        assert _resolve(lsof, peer=("::1", 52344, 0, 0), ours=("::1", 8801, 0, 0)) == 501

    def test_the_query_is_narrowed_to_the_peer_and_numeric(self) -> None:
        lsof = _lsof()
        _resolve(lsof)
        (args,) = lsof.calls
        assert args[0] == "lsof"
        assert "-nP" in args
        assert "-iTCP@127.0.0.1:52344" in args
        assert any(a.startswith("-F") and "u" in a and "n" in a for a in args)

    def test_a_non_ip_peer_never_runs_lsof(self) -> None:
        lsof = _lsof()
        assert _resolve(lsof, peer="/tmp/sock") is None
        assert lsof.calls == []


class TestOtherPlatforms:
    def test_linux_never_falls_back_to_lsof(self) -> None:
        lsof = _lsof((900, 501, "127.0.0.1:52344->127.0.0.1:8801"))
        assert _resolve(lsof, platform="linux") is None
        assert lsof.calls == []

    def test_unknown_platform_is_unproven(self) -> None:
        lsof = _lsof((900, 501, "127.0.0.1:52344->127.0.0.1:8801"))
        assert _resolve(lsof, platform="freebsd") is None
