"""Memory readings on a host without ``/proc/meminfo`` — macOS (#899).

Reading 0 there made the resident cap fall to its floor of 2 on any Mac, so a
32 GiB laptop put its third workspace to sleep. The emergency brake must stay
off rather than fire when free memory cannot be read: a brake that reads
"0 available" would sleep every idle workspace on every tick.
"""

from __future__ import annotations

import subprocess

from c_lord import resident_cap

GIB = 1024**3


def _no_proc(monkeypatch, tmp_path) -> None:
    monkeypatch.setattr(resident_cap, "_PROC_MEMINFO", tmp_path / "no-meminfo")
    monkeypatch.setattr(resident_cap, "_CGROUP_V2_MAX", tmp_path / "none")
    monkeypatch.setattr(resident_cap, "_CGROUP_V1_MAX", tmp_path / "none")


class TestTotalWithoutProc:
    def test_physical_memory_comes_from_sysconf(self, monkeypatch, tmp_path) -> None:
        _no_proc(monkeypatch, tmp_path)
        pages = {"SC_PHYS_PAGES": 8 * 1024 * 1024, "SC_PAGE_SIZE": 4096}  # 32 GiB
        monkeypatch.setattr(resident_cap.os, "sysconf", lambda name: pages[name])

        assert resident_cap.memory_total() == 32 * GIB
        assert resident_cap.max_resident_workspaces() > resident_cap.MIN_RESIDENT_LIMIT

    def test_unknown_sysconf_reads_zero(self, monkeypatch, tmp_path) -> None:
        _no_proc(monkeypatch, tmp_path)

        def boom(name: str) -> int:
            raise ValueError(name)

        monkeypatch.setattr(resident_cap.os, "sysconf", boom)
        assert resident_cap.memory_total() == 0

    def test_linux_meminfo_still_wins(self, monkeypatch, tmp_path) -> None:
        meminfo = tmp_path / "meminfo"
        meminfo.write_text("MemTotal:       16384 kB\nMemAvailable:    8192 kB\n")
        monkeypatch.setattr(resident_cap, "_PROC_MEMINFO", meminfo)
        monkeypatch.setattr(resident_cap, "_CGROUP_V2_MAX", tmp_path / "none")
        monkeypatch.setattr(resident_cap, "_CGROUP_V1_MAX", tmp_path / "none")
        monkeypatch.setattr(resident_cap.os, "sysconf", lambda name: 1 / 0)

        assert resident_cap.memory_total() == 16384 * 1024
        assert resident_cap.memory_snapshot() == (16384 * 1024, 8192 * 1024)


class TestAvailableWithoutProc:
    def _sysctl(self, stdout: str, returncode: int = 0):
        def run(args, **kwargs):
            assert args == ["sysctl", "-n", "kern.memorystatus_level"]
            return subprocess.CompletedProcess(args, returncode, stdout=stdout, stderr="")

        return run

    def test_available_is_the_memorystatus_level_share(self, monkeypatch, tmp_path) -> None:
        _no_proc(monkeypatch, tmp_path)
        monkeypatch.setattr(resident_cap, "_physical_memory", lambda: 32 * GIB)
        monkeypatch.setattr(resident_cap.subprocess, "run", self._sysctl("25\n"))

        assert resident_cap.memory_snapshot() == (32 * GIB, 8 * GIB)

    def test_unreadable_available_switches_the_brake_off(self, monkeypatch, tmp_path) -> None:
        _no_proc(monkeypatch, tmp_path)
        monkeypatch.setattr(resident_cap, "_physical_memory", lambda: 32 * GIB)
        monkeypatch.setattr(resident_cap.subprocess, "run", self._sysctl("", returncode=1))

        # total 0 is what _observe_pressure reads as "cannot judge — do nothing".
        assert resident_cap.memory_snapshot() == (0, 0)

    def test_missing_sysctl_switches_the_brake_off(self, monkeypatch, tmp_path) -> None:
        _no_proc(monkeypatch, tmp_path)
        monkeypatch.setattr(resident_cap, "_physical_memory", lambda: 32 * GIB)

        def missing(args, **kwargs):
            raise FileNotFoundError(args[0])

        monkeypatch.setattr(resident_cap.subprocess, "run", missing)
        assert resident_cap.memory_snapshot() == (0, 0)

    def test_out_of_range_level_is_not_trusted(self, monkeypatch, tmp_path) -> None:
        _no_proc(monkeypatch, tmp_path)
        monkeypatch.setattr(resident_cap, "_physical_memory", lambda: 32 * GIB)
        monkeypatch.setattr(resident_cap.subprocess, "run", self._sysctl("250\n"))

        assert resident_cap.memory_snapshot() == (0, 0)
