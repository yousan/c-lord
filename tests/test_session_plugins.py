"""Tests for c_lord.session_plugins — plugins a session's claude does not need (#965)."""

from __future__ import annotations

import json
from pathlib import Path

import pytest

from c_lord import session_plugins
from c_lord.session_plugins import plugin_overrides, settings_args

DISCORD = "discord@claude-plugins-official"
PYRIGHT = "pyright-lsp@claude-plugins-official"
TS = "typescript-lsp@claude-plugins-official"
OTHER = "claude-mem@thedotmack"


def _write_json(path: Path, data: object) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text(json.dumps(data))


@pytest.fixture
def home() -> Path:
    """``Path.home()`` is already a tmp dir (conftest ``_isolated_claude_home``)."""
    home = Path.home()
    _write_json(
        home / ".claude/plugins/marketplaces/claude-plugins-official/.claude-plugin"
        "/marketplace.json",
        {
            "plugins": [
                {
                    "name": "pyright-lsp",
                    "lspServers": {
                        "pyright": {"extensionToLanguage": {".py": "python", ".pyi": "python"}}
                    },
                },
                {
                    "name": "typescript-lsp",
                    "lspServers": {"typescript": {"extensionToLanguage": {".ts": "typescript"}}},
                },
                {"name": "discord"},
            ]
        },
    )
    return home


def _enable(home: Path, *names: str, disabled: tuple[str, ...] = ()) -> None:
    plugins = {n: True for n in names} | {n: False for n in disabled}
    _write_json(home / ".claude/settings.json", {"enabledPlugins": plugins})


@pytest.fixture
def py_repo(tmp_path: Path) -> Path:
    repo = tmp_path / "py"
    (repo / "src/pkg").mkdir(parents=True)
    (repo / "src/pkg/main.py").write_text("x = 1\n")
    return repo


@pytest.fixture
def docs_repo(tmp_path: Path) -> Path:
    repo = tmp_path / "docs"
    repo.mkdir()
    (repo / "README.md").write_text("# hi\n")
    # Python only inside places that are not the repository's own code.
    (repo / ".venv/lib").mkdir(parents=True)
    (repo / ".venv/lib/site.py").write_text("")
    (repo / "node_modules/x").mkdir(parents=True)
    (repo / "node_modules/x/a.py").write_text("")
    return repo


class TestDiscord:
    def test_discord_disabled_when_user_enabled_it(self, home: Path, py_repo: Path) -> None:
        _enable(home, DISCORD)
        assert plugin_overrides(py_repo) == {DISCORD: False}

    def test_discord_disabled_without_a_working_dir(self, home: Path) -> None:
        _enable(home, DISCORD)
        assert plugin_overrides(None) == {DISCORD: False}

    def test_nothing_when_user_did_not_enable_it(self, home: Path, py_repo: Path) -> None:
        _enable(home, OTHER, disabled=(DISCORD,))
        assert plugin_overrides(py_repo) == {}

    def test_other_plugins_are_left_alone(self, home: Path, py_repo: Path) -> None:
        _enable(home, DISCORD, OTHER)
        assert OTHER not in plugin_overrides(py_repo)


class TestLanguageServers:
    def test_pyright_disabled_in_a_repo_without_python(self, home: Path, docs_repo: Path) -> None:
        _enable(home, PYRIGHT)
        assert plugin_overrides(docs_repo) == {PYRIGHT: False}

    def test_pyright_kept_in_a_python_repo(self, home: Path, py_repo: Path) -> None:
        _enable(home, PYRIGHT)
        assert plugin_overrides(py_repo) == {}

    def test_each_language_server_judged_on_its_own(self, home: Path, py_repo: Path) -> None:
        _enable(home, PYRIGHT, TS)
        assert plugin_overrides(py_repo) == {TS: False}

    def test_unknown_language_server_left_alone(self, home: Path, docs_repo: Path) -> None:
        """No declared extensions → we cannot tell, so we do not guess."""
        mystery = "mystery-lsp@somewhere"
        _enable(home, mystery)
        assert plugin_overrides(docs_repo) == {}

    def test_no_working_dir_keeps_language_servers(self, home: Path) -> None:
        _enable(home, PYRIGHT)
        assert plugin_overrides(None) == {}

    def test_huge_tree_counts_as_present(
        self, home: Path, docs_repo: Path, monkeypatch: pytest.MonkeyPatch
    ) -> None:
        """Stopping the scan early must err on the side of keeping the server."""
        monkeypatch.setattr(session_plugins, "_SCAN_LIMIT", 1)
        (docs_repo / "a.md").write_text("")
        _enable(home, PYRIGHT)
        assert plugin_overrides(docs_repo) == {}


class TestDirectorySettingsWin:
    @pytest.mark.parametrize("name", ["settings.json", "settings.local.json"])
    def test_key_decided_by_the_directory_is_not_overridden(
        self, home: Path, docs_repo: Path, name: str
    ) -> None:
        _enable(home, DISCORD, PYRIGHT)
        _write_json(docs_repo / ".claude" / name, {"enabledPlugins": {DISCORD: True}})
        assert plugin_overrides(docs_repo) == {PYRIGHT: False}

    def test_broken_directory_settings_are_ignored(self, home: Path, py_repo: Path) -> None:
        _enable(home, DISCORD)
        (py_repo / ".claude").mkdir()
        (py_repo / ".claude/settings.local.json").write_text("{not json")
        assert plugin_overrides(py_repo) == {DISCORD: False}


class TestRobustness:
    def test_no_user_settings(self, home: Path, py_repo: Path) -> None:
        assert plugin_overrides(py_repo) == {}

    def test_broken_user_settings(self, home: Path, py_repo: Path) -> None:
        (home / ".claude").mkdir(exist_ok=True)
        (home / ".claude/settings.json").write_text("[1,")
        assert plugin_overrides(py_repo) == {}

    def test_missing_dir(self, home: Path, tmp_path: Path) -> None:
        _enable(home, DISCORD, PYRIGHT)
        assert plugin_overrides(tmp_path / "gone") == {DISCORD: False}


class TestSettingsArgs:
    def test_flag_carries_the_overrides(self, home: Path, docs_repo: Path) -> None:
        _enable(home, DISCORD, PYRIGHT)
        flag, value = settings_args(docs_repo)
        assert flag == "--settings"
        assert json.loads(value) == {"enabledPlugins": {DISCORD: False, PYRIGHT: False}}

    def test_no_flag_when_nothing_to_override(self, home: Path, py_repo: Path) -> None:
        _enable(home, PYRIGHT)
        assert settings_args(py_repo) == []

    def test_opt_out(self, home: Path, docs_repo: Path, monkeypatch: pytest.MonkeyPatch) -> None:
        _enable(home, DISCORD)
        monkeypatch.setenv("CLORD_SESSION_PLUGINS", "0")
        assert settings_args(docs_repo) == []
