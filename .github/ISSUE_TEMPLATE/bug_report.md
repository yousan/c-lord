---
name: Bug report
about: Something isn't working as expected
labels: bug
---

<!-- One Issue = one concern. If this bug bundles a second problem or an
     open-ended design task, split it into a separate Issue. -->

## Why does this matter? (何のため)

<!-- Why is this a problem from a USER's point of view? What can't they do, or
     what is confusing/broken when they use it? This is the spine: an Issue is
     read after it's closed to trace "why was this decided this way", so state
     the intent, not just the symptom. -->

## What happened? (現象)

<!-- Describe the bug in user-facing terms. Attach evidence — screenshots are
     the主役 (a text log alone hides layout/UI problems). A Discord screenshot
     (ideally tmux behaviour × Discord screen) is what the maintainer judges
     "spec vs bug" from. Paste long logs inside <details>. -->

<details>
<summary>logs / pane capture (optional)</summary>

```
<tmux pane capture or log excerpt>
```
</details>

## What did you expect? (期待)

<!-- What should have happened instead -->

## How to reproduce (再現手順)

<!-- Reproduction is required. Prefer steps that reproduce on STAGING — a fix
     is only "done" once RED→GREEN is shown on the real bot, not just in mocks. -->

1. ...
2. ...

## Acceptance Criteria (二値)

<!-- Each AC must be objectively true/false: a command to run, an observable
     output, a state to assert. No "should probably" / "consider". -->

- [ ] AC 1: …
- [ ] AC 2: …

## Out of scope / follow-ups (スコープ外)

<!-- Anything explicitly NOT handled here. Link the follow-up Issue — silence is
     not a decision. -->

## Environment

- Python version:
- Claude Code CLI version:
- discord.py version:
- OS:
