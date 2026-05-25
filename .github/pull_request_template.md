## What does this PR do?

<!-- Brief description of the change -->

## Why?

<!-- What problem does this solve? -->
<!-- Link the issue. Use "Closes #N" ONLY if this PR satisfies 100% of that
     issue's Acceptance Criteria. Otherwise use "Refs #N" and keep it open. -->

## Acceptance Criteria (copied from the issue)

<!-- Paste EVERY AC from the linked issue here and check them. Not "most" — every one. -->

- [ ] AC 1: …
- [ ] AC 2: …

## Staging Evidence

<!-- REQUIRED for behavior changes (bug fixes / features).
     Skip ONLY for pure-docs or provably no-behavior-change refactors —
     and if you skip, write the reason here.
     Paste log excerpts: RED = problem reproduced before the fix,
     GREEN = problem gone after the fix, on the staging bot. -->

<!-- RED (before):
```
<log excerpt showing the problem on staging>
```
GREEN (after):
```
<log excerpt showing it fixed on staging>
```
-->

## Definition of Done checklist

See CLAUDE.md → "Definition of Done (DoD)". Merge only when ALL are checked:

- [ ] Every Acceptance Criterion above is checked
- [ ] TDD: test failed before (RED) and passes after (GREEN) — RED line pasted
- [ ] `uv run pytest tests/ -v` passes
- [ ] `uv run ruff check c_lord/` + `ruff format --check` + `pyright` pass
- [ ] Staging Evidence above is filled in (or a skip reason is written)
- [ ] No unrelated changes in the diff; self-review done
- [ ] `Closes #N` used only if 100% of the issue's ACs are met (else `Refs #N`)
