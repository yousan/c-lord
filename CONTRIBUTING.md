# Contributing to c-lord

Thanks for your interest in contributing! This project was built by Claude Code and welcomes contributions from both humans and AI agents.

> **Green CI is necessary but not sufficient.** Besides the test job, a required check called
> `dod-gate` reads your **PR body**, and a PR that skips the PR template fails it even when every
> test passes. Read [What a PR needs to merge](#what-a-pr-needs-to-merge-dod-gate) before you open one.

## Branch Workflow

We use **GitHub Flow** — a simple, PR-based workflow:

```
main (always releasable)
  ├── feature/add-xxx   → PR → CI + dod-gate green → DoD checked → merge
  ├── fix/issue-123     → PR → CI + dod-gate green → DoD checked → merge
  └── (direct push to main is not allowed)
```

### Steps

1. **Start from an Issue** — open one with the templates in `.github/ISSUE_TEMPLATE/` (or pick an existing one). Its **Acceptance Criteria** are what your PR is checked against
2. **Fork** the repo (or create a branch if you have write access)
3. **Create a branch** from `main`:
   ```bash
   git checkout -b feature/your-feature-name
   ```
4. **Make your changes** — write a failing test first, then the code (TDD)
5. **Push** your branch and **open a PR** against `main` **using the PR template** — keep every section, including `## Definition of Done checklist`
6. **Two required checks run automatically**: `test (3.10/3.11/3.12)` (ruff, pyright, pytest) and `dod-gate` (your PR body — see [below](#what-a-pr-needs-to-merge-dod-gate))
7. A maintainer merges **only when every box of the [Definition of Done](CLAUDE.md#definition-of-done-dod--single-source-of-truth) is checked** — not just because CI is green. CI runs mocked tests, so it cannot show that a change works in a real Discord / tmux session

### Branch Naming

- `feature/description` — New functionality
- `fix/description` or `fix/issue-123` — Bug fixes
- `docs/description` — Documentation only
- `refactor/description` — Code restructuring without behavior change

## Development Setup

```bash
git clone https://github.com/yousan/c-lord.git
cd c-lord
uv sync --dev
make setup   # register git hooks (one-time per clone)
```

> **`make setup` is required** after every fresh clone. It configures git to use the
> pre-commit hook in `.githooks/`, which auto-formats and lints staged Python files.
> Without it, the hook never runs and bad code can slip through locally (CI will still
> catch it, but you'll get a surprise red build).
>
> Run `make check-setup` at any time to verify your environment is ready.

## Running Tests

```bash
uv run pytest tests/ -v --cov=c_lord
```

All tests must pass before submitting a PR.

## Code Style

- **Formatter**: `ruff format`
- **Linter**: `ruff check`
- **Type hints**: Required on all function signatures
- **Python**: 3.10+ (use `from __future__ import annotations` for modern syntax)

```bash
uv run ruff check c_lord/
uv run ruff format c_lord/
```

## Project Structure

- `c_lord/claude/` — Claude Code CLI interaction (runner, parser, types)
- `c_lord/cogs/` — Discord.py Cogs (chat, skill command, webhook trigger, auto-upgrade)
- `c_lord/database/` — SQLite session and notification persistence
- `c_lord/discord_ui/` — Discord UI components (status, chunker, embeds)
- `c_lord/ext/` — Optional extensions (REST API server — requires aiohttp)
- `tests/` — pytest test suite

## Submitting Changes

1. Start from an Issue, fork the repo and create a feature branch
2. Write tests first (they must fail before your change), then the code
3. Run locally before pushing — the same checks as the required `test` job:
   ```bash
   uv run ruff check c_lord/
   uv run ruff format --check c_lord/
   uv run pyright c_lord/
   uv run pytest tests/ -v
   ```
4. Open the PR **with the PR template** and fill it in: copy every Acceptance Criterion from the Issue, the one-line before/after, `## Staging Evidence`, and `## Definition of Done checklist`
5. Both required checks — `test (3.10/3.11/3.12)` and `dod-gate` — must be green, and every Definition of Done box checked, before a maintainer merges

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

## Versioning

This project uses automatic versioning — **you never need to manually bump the version** for regular contributions.

- **Automatic patch bump**: Every PR merged to `main` triggers an automatic patch version increment (e.g., `1.3.0` → `1.3.1`) and creates a GitHub Release.
- **Manual minor/major release**: To cut a minor or major release (e.g., `1.4.0`), update `pyproject.toml` and `CHANGELOG.md` manually, then include `[release]` in your PR title. This tags the current version as-is without bumping the patch.

## Adding a New Cog

1. Create `c_lord/cogs/your_cog.py`
2. Use `_run_helper.run_claude_with_config(RunConfig(...))` for Claude CLI execution
   (The legacy `run_claude_in_thread()` shim is still available but prefer `run_claude_with_config`)
3. Export from `c_lord/cogs/__init__.py`
4. Add to `c_lord/__init__.py` public API
5. Write tests in `tests/test_your_cog.py`

## A Note on AI-Generated Code

This project was written by Claude Code. If you use Claude Code or other AI tools to contribute, that's perfectly fine — just make sure the code works, is tested, and makes sense.
