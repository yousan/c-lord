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

## 利用者から見た before/after（1行・必須）

<!-- 利用者（Discord を使う人）から見て「この PR で操作したとき、Discord 上の見え方が
     どう変わるか」を1行で書く。コード語（関数名・`pytest 37/37`・`dod-gate green`・行番号）
     ではなく、利用者の言葉で。
     例: 「いまは長い返信が途中で切れる → この PR 後は最後まで複数メッセージで届く」
     利用者の見え方が変わらない変更（内部リファクタ・ドキュメントなど）なら
     `no-user-visible-change` と書く（空欄は不可）。 -->

- before（この PR 前）→ after（この PR 後）:

## Acceptance Criteria (copied from the issue)

<!-- Paste EVERY AC from the linked issue here and check them. Not "most" — every one. -->

- [ ] AC 1: …
- [ ] AC 2: …

## Staging Evidence (証跡)

> 挙動/UI を変える報告は**スクショを主証跡**にする（テキストログは補助）。Discord 実画面は AI が `scripts/discord_evidence_shot.sh`（#243）で撮り、PNG は `docs/evidence/<issue番号>/` に commit して参照する（Discord CDN の添付 URL は期限付きなので直貼り禁止）。原因側の tmux ペイン PNG（#286）とペアで貼る。

<!-- REQUIRED for behavior changes (bug fixes / features).
     Skip ONLY for pure-docs or provably no-behavior-change refactors —
     and if you skip, write the reason here.

     Show RED→GREEN reproduced ON STAGING (not just mocks):
       RED  = problem reproduced before the fix
       GREEN = problem gone after the fix
     Include timestamp / thread / branch hash, and clickable URLs.

     Discord-side proof: the agent captures the real Discord client itself via
     scripts/discord_evidence_shot.sh (#243, runs on the bot host). Commit
     evidence PNGs to docs/evidence/<issue>/ and reference them (Discord CDN
     attachment URLs expire — never hot-link them). Supplement with tmux pane
     captures (#286) and REST-fetched message text; human-provided screenshots
     cover what the test account cannot see. Fold long excerpts into <details>. -->

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

> **Label exemptions** (`no-runtime-change` or `documentation` label): the DoD-completion checklist below is waived (notably **TDD evidence** and **Staging Evidence**).
> **`Closes` discipline is always enforced**, regardless of label: if you use `Closes`/`Resolves #N`, the Acceptance Criteria above must still be fully checked (else use `Refs #N`).

- [ ] Every Acceptance Criterion above is checked
- [ ] TDD: test failed before (RED) and passes after (GREEN) — RED line pasted
- [ ] `uv run pytest tests/ -v` passes
- [ ] `uv run ruff check c_lord/` + `ruff format --check` + `pyright` pass
- [ ] Staging Evidence above is filled in (or a skip reason is written)
- [ ] No unrelated changes in the diff; self-review done
- [ ] 動きを変えたら「あるべき動き」のドキュメント（仕様 / README / docs）も更新した。変えないなら本文に `no-user-visible-change` と明記した
- [ ] `Closes`/`Resolves #N` used only if 100% of the issue's ACs are met (else `Refs #N`), and written on its own line
