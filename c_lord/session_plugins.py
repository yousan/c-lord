"""Plugins a session's ``claude`` does not need, switched off per launch (#965).

Plugins enabled in the user's ``~/.claude/settings.json`` follow *every*
``claude`` the bot starts.  Two kinds are dead weight in a c-lord session:

- **Discord** (``discord@…``): c-lord itself carries the conversation between
  Discord and the session (transcript mirror, ask bridge), so the plugin's MCP
  server — a ``bun server.ts`` of 50–80MB per session — only costs memory.  It
  is also a trap: every call Claude made to it from a session failed with
  ``Missing Access`` (8 of 8 across 465 session transcripts, 2026-10).
- **Language servers for languages the checkout does not contain**
  (``pyright-lsp`` in a TypeScript repo, …).  The extensions a server is for
  come from the plugin's own ``lspServers.*.extensionToLanguage`` in its
  marketplace manifest; a server that declares none is left alone.

They are turned off with Claude Code's own ``--settings`` flag, which applies
to that one process only — neither ``~/.claude/settings.json`` nor the
repository's ``.claude/settings.json`` is touched.  ``enabledPlugins`` merges
key by key, so naming only these plugins leaves every other one as configured.

A directory decides for itself with the official mechanism: any plugin its
``.claude/settings.json`` or ``.claude/settings.local.json`` names in
``enabledPlugins`` is not overridden.  ``CLORD_SESSION_PLUGINS=0`` turns the
whole thing off.  See ``docs/specs/session-plugins.md``.
"""

from __future__ import annotations

import json
import logging
import os
from pathlib import Path

logger = logging.getLogger(__name__)

#: Plugin names (the part before ``@``) a c-lord session never needs.
_NEVER_IN_SESSION = frozenset({"discord"})

#: Files looked at before giving up and assuming the language is there.
_SCAN_LIMIT = 50_000

#: Directories whose files are not the repository's own code.
_SKIP_DIRS = frozenset({"node_modules", "vendor", "venv", "__pycache__", "dist", "build", "target"})


def _read_json(path: Path) -> dict:
    try:
        data = json.loads(path.read_text())
    except (OSError, ValueError):
        return {}
    return data if isinstance(data, dict) else {}


def _enabled_plugins(settings: dict) -> dict:
    plugins = settings.get("enabledPlugins")
    return plugins if isinstance(plugins, dict) else {}


def _lsp_extensions(plugin_key: str) -> frozenset[str]:
    """Extensions *plugin_key*'s language servers declare; empty if unknown."""
    name, _, marketplace = plugin_key.partition("@")
    if not marketplace:
        return frozenset()
    manifest = _read_json(
        Path.home()
        / ".claude/plugins/marketplaces"
        / marketplace
        / ".claude-plugin/marketplace.json"
    )
    plugins = manifest.get("plugins")
    for plugin in plugins if isinstance(plugins, list) else []:
        if not isinstance(plugin, dict) or plugin.get("name") != name:
            continue
        servers = plugin.get("lspServers")
        if not isinstance(servers, dict):
            return frozenset()
        return frozenset(
            ext
            for server in servers.values()
            if isinstance(server, dict) and isinstance(server.get("extensionToLanguage"), dict)
            for ext in server["extensionToLanguage"]
        )
    return frozenset()


def _has_files_with(root: Path, extensions: frozenset[str]) -> bool:
    """Whether *root* holds a file ending in one of *extensions*.

    Hidden and dependency directories are skipped.  A tree too large to scan
    counts as having them — keeping a server is the safe mistake.
    """
    seen = 0
    for _dirpath, dirnames, filenames in os.walk(root):
        dirnames[:] = [d for d in dirnames if not d.startswith(".") and d not in _SKIP_DIRS]
        for filename in filenames:
            if os.path.splitext(filename)[1] in extensions:
                return True
            seen += 1
            if seen >= _SCAN_LIMIT:
                return True
    return False


def plugin_overrides(working_dir: str | Path | None) -> dict[str, bool]:
    """``enabledPlugins`` entries to switch off for a session in *working_dir*."""
    user_enabled = [
        key
        for key, on in _enabled_plugins(_read_json(Path.home() / ".claude/settings.json")).items()
        if on is True
    ]
    if not user_enabled:
        return {}

    root = Path(working_dir) if working_dir else None
    decided: set[str] = set()
    if root is not None:
        for name in ("settings.json", "settings.local.json"):
            decided.update(_enabled_plugins(_read_json(root / ".claude" / name)))

    overrides: dict[str, bool] = {}
    for key in user_enabled:
        if key in decided:
            continue
        if key.partition("@")[0] in _NEVER_IN_SESSION:
            overrides[key] = False
            continue
        if root is None or not root.is_dir():
            continue
        extensions = _lsp_extensions(key)
        if extensions and not _has_files_with(root, extensions):
            overrides[key] = False
    return overrides


def settings_args(working_dir: str | Path | None) -> list[str]:
    """``["--settings", <json>]`` for a session in *working_dir*, or ``[]``."""
    if os.environ.get("CLORD_SESSION_PLUGINS", "").strip() == "0":
        return []
    try:
        overrides = plugin_overrides(working_dir)
    except Exception:
        # A launch must never fail over an optimisation.
        logger.exception("session_plugins: could not work out plugin overrides (#965)")
        return []
    if not overrides:
        return []
    logger.info(
        "session_plugins: disabling %s for %s (#965)",
        ", ".join(sorted(overrides)),
        working_dir,
    )
    return ["--settings", json.dumps({"enabledPlugins": overrides}, separators=(",", ":"))]
