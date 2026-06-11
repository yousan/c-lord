# #359 Staging Reproduction — bridged menu answered in TUI freezes the thread

Reproduced live on **staging** (`#c-lord-3`, real Discord client capture via the
#243 evidence tool; throwaway lease borrowed + released, staging restored to
`main`).

## What was reproduced (deterministic)

1. **Menu #1 bridges fine** — webhook triggers `AskUserQuestion テストA` → buttons
   `[選択肢A1] [選択肢A2] [選択肢A3] [✏️ Other]` appear in Discord in ~8s.
   See `359-A-menu1-buttons-OK.png`.
2. **Answer menu #1 in the TUI** (select 選択肢A2 via tmux send-keys, **no Discord
   click**) → Claude proceeds with that answer.
3. **The thread then freezes.** A second `AskUserQuestion テストB` webhook **and** a
   plain message ("ack" request) are posted — **both get no Claude response, no
   buttons.** Menu #1's buttons + the `Stop` button stay visible (the turn never
   ended). See `359-B-thread-stuck.png`.

## Root cause (confirmed by log)

Staging log shows **no events at all after `Interactive menu detected, bridging
to Discord` (11:04:45)** — menu #1's `run_claude` never logs `exit`. The
`pane_ask` bridge (`tmux_runner.run()` yields `pane_ask` → `bridge_pane_ask`
awaits the user's click for up to 24h) **suspends the `run_claude` generator**.
Answering in the TUI closes the TUI menu but the bridge never learns, so it keeps
waiting — and the suspended `run_claude` **holds the per-thread lock**, so every
later message to that thread blocks. The thread is stuck until the user clicks
the stale button or the 24h timeout fires (or the bot restarts).

## Relation to the audit (#359 comment 2026-06-11)

This is the mechanism behind the **"answered in TUI" (47/103)** audit bucket:
each TUI-answered bridged menu leaves the bridge waiting and can freeze the
thread, so later menus appear as "no buttons / no response". (The separate
intermittent "first/only menu gets no buttons ~50%" mode was **not** reproduced
in this single run — menu #1 bridged fine here — and needs a timing-specific
repro.)

No fix implemented yet (per "直す前まで").
