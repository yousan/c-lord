# Contributing to c-lord

c-lord is developed in the open, and most of its code is written by Claude Code working from Issues under a maintainer's review. Contributions from people and from AI agents are both welcome, and both go through the same door: an Issue, a branch, a pull request, and proof that the change works.

> **Green CI is necessary but not sufficient.** Besides the test job, a required check called
> `dod-gate` reads your **PR body**, and a PR that skips the PR template fails it even when every
> test passes. Read [What a PR needs to merge](#what-a-pr-needs-to-merge-dod-gate) before you open one.

## Before you write code

- **Start from an Issue.** Open one with the templates in `.github/ISSUE_TEMPLATE/`, or pick one that exists. An Issue says *why* the change is wanted, from the point of view of someone using c-lord in Discord, and lists **Acceptance Criteria** that are each plainly true or false. Your pull request is checked against those criteria, so an Issue covers one concern — a fix and an unrelated improvement are two Issues.
- **"Is this a bug, or is it meant to work this way?"** Look in [docs/specs/](docs/specs/README.md) first: each page there states how one feature should behave. If no page covers it, say so in the Issue rather than reading the answer off the current code. The reasoning behind the project is in [docs/PHILOSOPHY.md](docs/PHILOSOPHY.md).
- **c-lord is a framework, not somebody's bot.** A feature that is useful to anyone running c-lord belongs here. Tokens, server-specific logic and personal workflows belong in the repository of the bot that uses c-lord.
- **Nothing for users to wire up.** A new feature is on by default with a sensible setting, and a new constructor parameter has a default that keeps old callers working. Someone who updates the package should get the feature without touching their code.

## From Issue to merge

```text
Issue  →  branch from main  →  failing test, then code  →  pull request
       →  shown working on a staging bot  →  self-review  →  squash merge
```

1. **Branch from `main`** in your fork (or in this repository if you can push to it). Nobody pushes to `main` directly. Name the branch for what it does: `feature/…`, `fix/…` (for example `fix/issue-123`) or `docs/…`.
2. **Write the test first.** Run it and watch it fail, then write the code that makes it pass. Keep the line that shows the failure — the pull request asks for it.
3. **Run the checks locally** before pushing. They are the same ones the required `test` job runs:

   ```bash
   uv run ruff check c_lord/
   uv run ruff format --check c_lord/
   uv run pyright c_lord/
   uv run pytest tests/ -v --cov=c_lord
   ```

4. **Update the description of the behaviour in the same pull request.** If what a user sees changes, the page that describes it (`docs/specs/`, `README.md`, the guides under `docs/`) changes with it. If nothing a user sees changes, write `no-user-visible-change` in the pull request body.
5. **Open the pull request against `main` using the PR template**, and keep every section of it: every Acceptance Criterion copied from the Issue, the one-line before/after as a user would see it, `## Staging Evidence`, and `## Definition of Done checklist`.
6. **Two required checks run**: `test (3.10/3.11/3.12)` — ruff, pyright and pytest on each Python version — and `dod-gate`, which reads the pull request body ([below](#what-a-pr-needs-to-merge-dod-gate)).
7. **Read your own diff** (`git diff main`) and take out anything that is not part of this change.
8. **A maintainer squash-merges when every box of the [Definition of Done](CLAUDE.md#definition-of-done-dod--single-source-of-truth) is checked.** Green CI alone is not enough: the tests run against mocks, so they cannot show that a change works in a real Discord thread and a real tmux pane.

Commit messages read `<type>: <description>`, where the type is one of `feat`, `fix`, `refactor`, `docs`, `test`, `chore` or `security`.

## Setting up

```bash
git clone https://github.com/yousan/c-lord.git
cd c-lord
uv sync --dev
make setup          # once per clone
```

`make setup` points git at the hooks in `.githooks/`. From then on, every commit formats the staged Python files with ruff and refuses to go through if lint errors remain. Skip it and nothing checks your code until CI does. `make check-setup` tells you whether a clone has been set up.

You need Python 3.10 or newer, [uv](https://docs.astral.sh/uv/), and `tmux` for the tests that drive a real tmux.

## Tests

```bash
uv run pytest tests/ -v --cov=c_lord
```

- **Every new feature and every bug fix comes with a test, written before the code.** Test files live in `tests/`.
- Tests that need a live bot and a real Discord channel are marked `e2e` and left out of a normal run; `uv run pytest -m e2e tests/e2e/ -v` runs them when the credentials are in `.env`.
- **A fix to how c-lord recognises a prompt in the terminal** (`_has_permission_prompt`, `_is_yn_prompt`, `_has_unknown_interactive`) starts with a real capture of the pane that went wrong, added to `tests/fixtures/panes/` before the fix is written.
- **When a test drives real tmux, give it its own server**: `tmux -L <socket-name>`. Every c-lord session on a host shares the default tmux server, and killing it kills them all ([docs/STAGING.md](docs/STAGING.md)).

## Code conventions

- Formatting and linting are ruff's job (`uv run ruff format c_lord/` fixes formatting). Lines are at most 100 characters.
- Every function signature has type hints, and every file begins with `from __future__ import annotations`. The code must run on Python 3.10.
- Names: `snake_case` for files and functions, `PascalCase` for classes, `UPPER_SNAKE_CASE` for constants, a leading `_` for anything private.
- Log through `log_ctx()` (`c_lord/utils/logger.py`) whenever a thread, session, task or channel is in scope, so that `grep "thread=<id>"` finds the whole story. When c-lord decides to ignore something, that decision is logged at INFO, not hidden at DEBUG.
- **Security is not optional**, because c-lord runs commands on someone's machine. Start processes with `asyncio.create_subprocess_exec` and an argument list, never through a shell; put `--` before a prompt; validate session IDs and skill names before they reach the command line. If you change `c_lord/claude/tmux_runner.py`, `c_lord/cogs/_run_helper.py` or any cog, go through the checklist in `.claude/skills/security-audit/SKILL.md` before you commit.

## Where things are

- `c_lord/claude/` — Running a turn in the tmux pane, CLI settings, event types
- `c_lord/cogs/` — Discord.py Cogs (chat, skill command, webhook trigger, auto-upgrade)
- `c_lord/transcript/` — Reading Claude Code's own transcript and posting it to the thread
- `c_lord/database/` — SQLite session and notification persistence
- `c_lord/discord_ui/` — What appears in Discord: status lamp, question buttons, progress line, embeds
- `c_lord/ext/` — Optional extensions (REST API server — requires aiohttp)
- `c_lord/tmux.py`, `c_lord/session_dir.py` — tmux windows and the per-thread git clones
- `tests/` — the pytest suite; `docs/specs/` — how each feature should behave

[README.md](README.md#architecture) has the overview and [docs/ARCHITECTURE.md](docs/ARCHITECTURE.md) the detail.

## What a PR needs to merge (`dod-gate`)

The single source of truth is the **[Definition of Done](CLAUDE.md#definition-of-done-dod--single-source-of-truth)** in `CLAUDE.md`; the PR template (`.github/pull_request_template.md`) mirrors it. This section explains the part a machine enforces.

`dod-gate` is a **required status check** on `main`. It reads only your **PR body and labels** — not your code — and nobody can merge past a red `dod-gate` (branch protection applies to admins too). It fails when:

| `dod-gate` fails when… | Applies to | Fix |
|---|---|---|
| The body has no `## Definition of Done checklist` section | **every PR**, labels or not | Use the PR template and keep that heading as-is |
| A box in that checklist is unchecked (`- [ ]`) | PRs without an exempt label | Do the item and check it |
| The body has no evidence: no image (`![...](URL)`), `<img>`, Release-asset URL or GitHub attachment URL | PRs without an exempt label | Attach a screenshot — see [Evidence and staging](#evidence-screenshots-and-staging) |
| `Closes` / `Fixes` / `Resolves #N` is used, but there is no `## Acceptance Criteria` section (a `###` heading does not count) or one of its boxes is unchecked | **every PR**, labels or not | Copy **every** Acceptance Criterion from the Issue and check it — or write `Refs #N` instead and leave the Issue open |

**Exempt labels** — `documentation` or `no-runtime-change` waive the checklist and evidence rows above (and, per the Definition of Done, TDD evidence and staging verification). Use them for docs, CI/tooling, or provably behavior-free refactors. They never waive the `## Definition of Done checklist` heading or the `Closes` rule. Adding a label needs write access to this repo — if you don't have it, ask a maintainer in the PR.

Also good to know:

- `dod-gate` re-runs whenever you edit the PR body or the labels change — no new commit needed.
- Use `Closes #N` only when the PR meets **100%** of the Issue's Acceptance Criteria, and write it on its own line — not inside a `- ` bullet, where GitHub may not pick it up.
- The gate's logic is `.github/scripts/dod_gate.js` (tests: `tests/test_dod_gate.py`).

### Evidence (screenshots) and staging

For bug fixes and features, the PR shows **RED** (the problem reproduced before the change) and **GREEN** (gone after it) under `## Staging Evidence`, with a **screenshot as the main proof** — text logs alone don't pass.

- **How maintainers make it**: capture the real Discord screen with `scripts/discord_evidence_shot.sh`, upload it with `scripts/evidence_upload.py red.png green.png --issue <N>`, and paste the printed URLs. Details: [docs/discord-evidence-capture.md](docs/discord-evidence-capture.md). Never commit images to the repo, and never hot-link Discord CDN URLs (they expire).
- **Those two scripts need the maintainers' environment**: `discord_evidence_shot.sh` runs on the bot host with a logged-in capture account, and `evidence_upload.py` needs write access to this repo. **Outside contributors**: drag and drop your screenshot into the PR body instead — the GitHub attachment URL it produces satisfies `dod-gate`.
- **Staging** (`bash scripts/staging.sh borrow` → `restart` → `release`, see [docs/STAGING.md](docs/STAGING.md)) runs on the maintainers' host, so outside contributors can't run it today. Say so under `## Staging Evidence` and describe what you verified locally. The Definition of Done still requires the staging RED→GREEN before merge, so that part ends up with a maintainer. How outside contributors should cover staging and evidence is **not decided yet** — see [#765](https://github.com/yousan/c-lord/issues/765).

## Versions and releases

You never set a version number. The version comes from git tags (`vX.Y.Z`); nothing in `pyproject.toml` holds it.

- **Patch releases are automatic.** Each pull request merged into `main` gets the next patch tag and a GitHub Release. The exception is a merge whose commit subject starts with `docs:` or contains `[skip-release]` or `[skip ci]` — those do not release.
- **Minor and major releases are cut by a maintainer** with `scripts/release.sh`, after `CHANGELOG.md` has been updated in a pull request.

## Adding a cog

1. Create `c_lord/cogs/your_cog.py`
2. If it runs Claude, do not copy the run logic: build a `RunConfig` and call `_run_helper.run_claude_with_config()`. `run_claude_in_thread()` is the older entry point to the same code, kept for compatibility
3. Export from `c_lord/cogs/__init__.py`
4. Add to `c_lord/__init__.py` public API
5. Register it in `setup_bridge()` (`c_lord/setup.py`) so that bots built on c-lord get it without changing their code
6. Put its tests in `tests/test_your_cog.py`

`.claude/skills/add-cog/SKILL.md` walks through the same steps in more detail.

## If you are an AI agent, or work with one

Most changes to c-lord are made by an agent, so the instructions are already written for one: [CLAUDE.md](CLAUDE.md) is the project's brief (`AGENTS.md` is the same file), and `.claude/skills/` holds the step-by-step guides for test-first development, the pre-commit checks and the security audit. An agent's pull request is held to exactly the standard above — the tests, the evidence and the Definition of Done do not care who typed the code.
