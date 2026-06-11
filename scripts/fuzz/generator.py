"""Scenario generator — drives the ``claude`` CLI headlessly (Issue #377).

Security: the CLI is invoked with :func:`subprocess.run` and an argument *list*
(never ``shell=True``), and the generation prompt is passed after a ``--``
separator so it can never be interpreted as a flag — same discipline c-lord uses
for spawning Claude sessions (see ``.claude/skills/security-audit``).
"""

from __future__ import annotations

import subprocess

from .scenarios import Scenario, build_generation_prompt, parse_scenarios


class GenerationError(RuntimeError):
    """Raised when the ``claude`` CLI fails to produce any usable scenario."""


def generate_scenarios(
    n: int,
    *,
    focus: str | None = None,
    claude_cmd: str = "claude",
    model: str = "haiku",
    timeout: float = 180.0,
) -> tuple[list[Scenario], str]:
    """Generate up to *n* fuzz scenarios via the ``claude`` CLI.

    Returns ``(scenarios, raw_stdout)``. ``raw_stdout`` is always returned (even
    on a partial/garbled generation that parses to zero scenarios) so the caller
    can persist exactly what the LLM produced — that is the "save the generation
    log" reproducibility lever, and it makes a bad generation debuggable.

    Raises:
        GenerationError: only when the CLI itself fails (missing binary, non-zero
            exit, or timeout). A successful CLI run that parses to zero scenarios
            returns ``([], raw)`` — the caller decides what to do.
    """
    prompt = build_generation_prompt(n, focus=focus)
    argv = [claude_cmd, "--print", "--model", model, "--", prompt]
    try:
        proc = subprocess.run(
            argv,
            capture_output=True,
            text=True,
            timeout=timeout,
            check=False,
        )
    except FileNotFoundError as exc:
        raise GenerationError(f"claude CLI not found: {claude_cmd}") from exc
    except subprocess.TimeoutExpired as exc:
        raise GenerationError(f"claude generation timed out after {timeout}s") from exc

    raw = proc.stdout or ""
    if proc.returncode != 0:
        raise GenerationError(
            f"claude exited {proc.returncode}: {(proc.stderr or '').strip()[:300]}"
        )

    scenarios = parse_scenarios(raw, limit=n)
    return scenarios, raw
