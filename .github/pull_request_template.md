<!-- 30-second summary first. A reviewer should be able to judge this PR from the
     top: What / Why / Proof / Tests / Scope. Be comprehensive; fold long logs or
     code into <details><summary>…</summary> so the body stays readable. -->

## What does this PR do?

<!-- Brief description of the change -->

## Why?

<!-- What problem does this solve, from a user's point of view? -->

<!-- Link the issue on ITS OWN LINE (not inside a bullet list — GitHub won't
     parse the keyword reliably inside a `- ` item). Use "Closes #N" or
     "Resolves #N" ONLY if this PR satisfies 100% of that issue's Acceptance
     Criteria. Otherwise use "Refs #N" and keep the issue open. -->

Refs #

## Acceptance Criteria (copied from the issue)

<!-- Paste EVERY AC from the linked issue here and check them. Not "most" — every one. -->

- [ ] AC 1: …
- [ ] AC 2: …

## Staging Evidence (証跡)

<!-- REQUIRED for behavior changes (bug fixes / features).
     Skip ONLY for pure-docs or provably no-behavior-change refactors —
     and if you skip, write the reason here.

     Show RED→GREEN reproduced ON STAGING (not just mocks):
       RED  = problem reproduced before the fix
       GREEN = problem gone after the fix
     Include timestamp / thread / branch hash, and clickable URLs.

     Discord-side proof: a Discord screenshot is the主役 and is normally
     USER-PROVIDED (the server-side agent has no GUI and cannot screenshot the
     Discord client). The agent supplements with tmux pane captures and
     REST-fetched message text. Fold long excerpts into <details>. -->

<details>
<summary>RED (before) — reproduced on staging</summary>

```
<log excerpt / pane capture showing the problem>
```
</details>

<details>
<summary>GREEN (after) — fixed on staging</summary>

```
<log excerpt / pane capture showing it fixed>
```
</details>

Discord screenshot (user-provided): <attach / link>

## Definition of Done checklist

See CLAUDE.md → "Definition of Done (DoD)". Merge only when ALL are checked.

> **Label exemptions** (`no-runtime-change` or `documentation` label): items 1–2 and 5 below are waived.
> `Closes` discipline (item 7) is **always enforced**, regardless of label.

- [ ] Every Acceptance Criterion above is checked
- [ ] TDD: test failed before (RED) and passes after (GREEN) — RED line pasted
- [ ] `uv run pytest tests/ -v` passes
- [ ] `uv run ruff check c_lord/` + `ruff format --check` + `pyright` pass
- [ ] Staging Evidence above is filled in (or a skip reason is written)
- [ ] No unrelated changes in the diff; self-review done
- [ ] `Closes`/`Resolves #N` used only if 100% of the issue's ACs are met (else `Refs #N`), and written on its own line
