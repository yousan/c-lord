"""Tests for c_lord.clord_home — the default home for c-lord's own output (#837)."""

from __future__ import annotations

from pathlib import Path

import pytest

from c_lord.clord_home import (
    LEGACY_SESSION_DIR_BASE,
    clord_home,
    default_session_dir_base,
    instance_name,
)


class TestInstanceName:
    def test_defaults_to_the_clone_directory_name(self, tmp_path: Path) -> None:
        clone = tmp_path / "c-lord-staging-1"
        clone.mkdir()
        assert instance_name(env={}, cwd=str(clone)) == "c-lord-staging-1"

    def test_clord_instance_wins_over_the_directory_name(self, tmp_path: Path) -> None:
        clone = tmp_path / "c-lord-parallel-3"
        clone.mkdir()
        # The agreed reason for the knob: renaming the clone must not move the
        # workspaces (ops#4: c-lord-parallel-3 → c-lord-staging-1).
        assert instance_name(env={"CLORD_INSTANCE": "staging-1"}, cwd=str(clone)) == "staging-1"

    def test_blank_clord_instance_falls_back_to_the_directory(self, tmp_path: Path) -> None:
        assert instance_name(env={"CLORD_INSTANCE": "  "}, cwd=str(tmp_path)) == tmp_path.name

    @pytest.mark.parametrize("bad", ["../prod", "a/b", "..", ".", "x;y", "a b"])
    def test_unsafe_clord_instance_is_ignored(self, bad: str, tmp_path: Path) -> None:
        # It becomes a path component under ~/.c-lord — it must not escape it.
        assert instance_name(env={"CLORD_INSTANCE": bad}, cwd=str(tmp_path)) == tmp_path.name

    def test_symlinked_clone_uses_the_link_name(self, tmp_path: Path) -> None:
        real = tmp_path / "real"
        real.mkdir()
        link = tmp_path / "c-lord"
        link.symlink_to(real)
        # The operator knows the clone by the name they cd'd into.
        assert instance_name(env={}, cwd=str(link)) == "c-lord"


class TestDefaultSessionDirBase:
    def test_new_install_lands_under_home_dot_c_lord(self, tmp_path: Path) -> None:
        clone = tmp_path / "c-lord-staging-2"
        clone.mkdir()
        base = default_session_dir_base(env={}, cwd=str(clone))
        assert base == str(Path.home() / ".c-lord" / "c-lord-staging-2" / "sessions")

    def test_clord_instance_names_the_directory(self, tmp_path: Path) -> None:
        base = default_session_dir_base(env={"CLORD_INSTANCE": "prod"}, cwd=str(tmp_path))
        assert base == str(Path.home() / ".c-lord" / "prod" / "sessions")

    def test_existing_legacy_data_sessions_is_kept(self, tmp_path: Path) -> None:
        # An instance that ran without SESSION_DIR_BASE has its workspaces in
        # ./data/sessions; moving them would cut every thread's --resume.
        (tmp_path / LEGACY_SESSION_DIR_BASE).mkdir(parents=True)
        assert default_session_dir_base(env={}, cwd=str(tmp_path)) is None

    def test_sessions_db_alone_is_not_legacy(self, tmp_path: Path) -> None:
        (tmp_path / "data").mkdir()
        (tmp_path / "data" / "sessions.db").write_text("")
        assert default_session_dir_base(env={}, cwd=str(tmp_path)) is not None

    def test_clord_home_is_dot_c_lord(self) -> None:
        assert clord_home() == Path.home() / ".c-lord"
