"""Every command a cog exposes goes through the allowlist — or is named as public (#781).

#405 gated ``/clear``. Scanning the other cogs the same way showed that most
commands still asked nobody: anyone who could type in a thread could run
``/workspace-delete`` (removes the working directory), ``/model set`` (changes
the model for every thread), ``/stop``, ``/compact``, ``/upgrade`` … The
"nothing configured → owner only" default of #713 never reached them either,
because a default only applies where the gate is called.

A per-command gate is a gate the next command forgets, so this file holds the
whole set to it structurally: an AST walk over ``c_lord/cogs`` finds every
``*.command(...)`` decorator and requires the command body — or a ``self._*``
helper it calls — to reach the shared rule (:class:`Authorizer` via
``_is_allowed`` / ``_authorize``, or
:func:`c_lord.command_gate.is_message_authorized`). A new ungated command fails
here, not in production.

Commands that are deliberately open are listed in :data:`PUBLIC_COMMANDS`.
They only *display* configuration that is not sensitive; anything that changes
state, touches a workspace, or shows a pane / a list of workspaces is gated.
"""

from __future__ import annotations

import ast
import re
from pathlib import Path

COGS_DIR = Path(__file__).resolve().parent.parent / "c_lord" / "cogs"

# The one rule, by the names its entry points are reached through (#405 / #507 / #713).
_GATE = re.compile(
    r"\b(?:self\._is_allowed|self\._is_message_authorized|self\._authorize|"
    r"self\._is_authorized|is_message_authorized)\("
)

#: Open to anyone who can type in the channel — read-only, nothing sensitive.
#: Adding a command here is a decision: say why in the PR.
PUBLIC_COMMANDS = frozenset(
    {
        "/version",
        "!version",
        "/model show",
        "!model-show",
        "/thread-archive show",
        "!thread-archive-show",
    }
)


def _decorator_name(node: ast.expr, groups: dict[str, str]) -> str | None:
    """``@app_commands.command(name="x")`` → ``/x``; ``@commands.command`` → ``!x``.

    Commands of an ``app_commands.Group`` class attribute are named with the
    group's own name in front (``@model_group.command(name="set")`` → ``/model set``).
    """
    if not (isinstance(node, ast.Call) and isinstance(node.func, ast.Attribute)):
        return None
    if node.func.attr != "command":
        return None
    name = next(
        (
            kw.value.value
            for kw in node.keywords
            if kw.arg == "name"
            and isinstance(kw.value, ast.Constant)
            and isinstance(kw.value.value, str)
        ),
        None,
    )
    if name is None:
        return None
    owner = node.func.value
    if isinstance(owner, ast.Name) and owner.id == "commands":
        return f"!{name}"
    if isinstance(owner, ast.Name) and owner.id == "app_commands":
        return f"/{name}"
    if isinstance(owner, ast.Name) and owner.id in groups:
        return f"/{groups[owner.id]} {name}"
    return None


def _group_names(cls: ast.ClassDef) -> dict[str, str]:
    """``model_group = app_commands.Group(name="model", ...)`` → ``{"model_group": "model"}``."""
    out: dict[str, str] = {}
    for stmt in cls.body:
        if not (isinstance(stmt, ast.Assign) and isinstance(stmt.value, ast.Call)):
            continue
        func = stmt.value.func
        if not (isinstance(func, ast.Attribute) and func.attr == "Group"):
            continue
        for kw in stmt.value.keywords:
            if kw.arg == "name" and isinstance(kw.value, ast.Constant):
                for target in stmt.targets:
                    if isinstance(target, ast.Name):
                        out[target.id] = str(kw.value.value)
    return out


def _scan() -> dict[str, list[tuple[str, bool]]]:
    """``{file: [(command, gated), ...]}`` for every command in every cog."""
    result: dict[str, list[tuple[str, bool]]] = {}
    for path in sorted(COGS_DIR.glob("*.py")):
        src = path.read_text()
        tree = ast.parse(src)
        for cls in (n for n in tree.body if isinstance(n, ast.ClassDef)):
            funcs = {
                fn.name: fn
                for fn in cls.body
                if isinstance(fn, (ast.FunctionDef, ast.AsyncFunctionDef))
            }
            groups = _group_names(cls)

            def gated(fn: ast.AST, seen: set[str]) -> bool:
                seg = ast.get_source_segment(src, fn) or ""  # noqa: B023
                if _GATE.search(seg):
                    return True
                for callee in re.findall(r"self\.(_\w+)\(", seg):
                    if callee in funcs and callee not in seen:  # noqa: B023
                        seen.add(callee)
                        if gated(funcs[callee], seen):  # noqa: B023
                            return True
                return False

            for name, fn in funcs.items():
                for dec in fn.decorator_list:
                    command = _decorator_name(dec, groups)
                    if command is not None:
                        result.setdefault(path.name, []).append((command, gated(fn, {name})))
    return result


def test_scan_finds_the_commands_it_should() -> None:
    """Guard the guard: a scanner that finds nothing would pass vacuously."""
    found = {cmd for cmds in _scan().values() for cmd, _ in cmds}
    for expected in (
        "/workspace-delete",
        "!workspace-delete",
        "/model set",
        "!model-set",
        "/stop",
        "!stop",
        "/compact",
        "/upgrade",
        "/clear",
        "/skill",
    ):
        assert expected in found, f"scanner lost {expected}"


def test_every_command_is_gated_or_declared_public() -> None:
    """#781 AC1/AC2: no command reaches its body without asking the allowlist."""
    ungated = [
        f"{file}: {cmd}"
        for file, cmds in _scan().items()
        for cmd, is_gated in cmds
        if not is_gated and cmd not in PUBLIC_COMMANDS
    ]
    assert ungated == [], (
        "These commands run without asking the allowlist (#781). Gate them the way "
        "/clear is gated (#405) — `self._authorize(user, message)` — or, if they only "
        "display something harmless, add them to PUBLIC_COMMANDS with a reason:\n"
        + "\n".join(ungated)
    )


def test_public_list_has_no_stale_entries() -> None:
    """A renamed public command must not leave a dead exemption behind."""
    found = {cmd for cmds in _scan().values() for cmd, _ in cmds}
    assert found >= PUBLIC_COMMANDS, sorted(PUBLIC_COMMANDS - found)
