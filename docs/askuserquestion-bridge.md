# AskUserQuestion → Discord Bridge

How c-lord turns Claude Code's **AskUserQuestion** TUI menu into clickable
Discord buttons in `CLORD_BRIDGE_MODE=jsonl` / tmux mode (production's mode).

Related issues: #166 (bridge), #169 (descriptions), #171 (keystroke timing),
#172 (free-text — open). See also [`tui-prompts.md`](./tui-prompts.md) §2-3.

## Why this exists

In jsonl/tmux mode Claude runs inside a tmux pane and `AskUserQuestion` renders
a **terminal menu** there; Claude then *blocks* waiting for a keyboard
selection. Nothing reached Discord, so a user watching Discord saw the session
stall with no way to answer. This bridge parses that menu, shows Discord
buttons, and delivers the choice back to the still-open menu as keystrokes.

> Note: the JSONL transcript carries the AskUserQuestion tool call, but the
> tmux runner does **not** populate `StreamEvent.ask_questions` — that field is
> only filled by the (currently unused) SDK-streaming runner. The live bridge
> works by **parsing the pane** (`_parse_ask_from_pane`), not the JSONL.

## Side-by-side

### What Claude Code shows in the tmux pane (raw)

```
 ☐ Approach

実装方針は?

❯ 1. A案
     その場で書き換える
  2. B案
     新規ファイルを作る
  3. C案
     いったん保留
  4. Type something.
────────────────────────────────────
  5. Chat about this

Enter to select · ↑/↓ to navigate · Esc to cancel
```

(The real capture also carries per-token ANSI colour codes that split `❯` from
`1.`; c-lord normalises them away before parsing — see #166.)

### What c-lord shows in Discord

![AskUserQuestion rendered as Discord buttons with a description legend](./assets/askuserquestion-discord.png)

```
❓ Approach
実装方針は?

A案 — その場で書き換える
B案 — 新規ファイルを作る
C案 — いったん保留

[A案] [B案] [C案]   [✏️ Other]
```

## Transformation rules

| Claude Code (tmux pane) | Discord | Handling |
|---|---|---|
| `☐ <header>` | embed title `❓ <header>` | header extracted |
| question line | embed body (first line) | question extracted |
| `❯ N. <label>` + indented description | **button `<label>`** + body legend `<label> — <description>` | label → button; description → legend (#169) |
| `Type something.` (meta) | **`✏️ Other` button** (free-text modal) | mapped to free text — **broken, see Limitations (#172)** |
| `Chat about this` (meta) | *(dropped)* | TUI-only affordance |
| `Enter to select · ↑/↓ · Esc` footer | *(dropped)* | replaced by clicking |
| ANSI colour codes | *(stripped)* | `_normalize_capture` before detection (#166) |
| keyboard select (↑/↓ + Enter) | **button click** | click → keystrokes sent to pane |

## Buttons vs. select menu

- **≤ 4 options, single-select** → buttons, **plus** a `label — description`
  legend in the embed body (Discord buttons cannot show descriptions) (#169).
- **≥ 5 options, or multi-select** → a Discord **select menu**, which shows
  each option's description in its dropdown; no body legend is added.

## How a click is answered

The menu is still open in the pane and the cursor starts on the first option.
On a button click c-lord computes the chosen option's 0-based index and calls
`TmuxClaudeRunner.answer_menu(index)`, which sends:

```
Down  (×index)   then   Enter
```

**Keystrokes are sent one at a time with a short delay** (`_MENU_NAV_DELAY`,
0.25 s) between them. Batching them into a single `tmux send-keys Down Down
Enter` is too fast — the TUI drops the `Down` navigations and Enter selects the
wrong (first) option. This was a real regression (#171); the answer path is now
verified with the bot's actual `answer_menu` selecting the intended option.

While the bridge waits for the click, the runner is suspended at its `yield`,
so the pane is not re-polled and the menu is not re-detected (natural dedup).

## Limitations

- **Free text (`✏️ Other`) does not work yet (#172).** Selecting
  `Type something.` via navigate-then-Enter registers as *"User declined to
  answer questions"* and the typed text arrives as a separate follow-up
  message rather than the AskUserQuestion answer. The correct TUI interaction
  for free-text input is still being determined; until then, prefer providing
  explicit options.
- **Single question at a time.** A multi-question AskUserQuestion is handled one
  menu at a time as each renders in the pane.

## Source map

| Concern | Location |
|---|---|
| Parse menu from pane | `c_lord/claude/tmux_runner.py::_parse_ask_from_pane` |
| Detect & yield `pane_ask` event | `tmux_runner.py::run` (poll loop) |
| Show buttons / route answer | `c_lord/discord_ui/ask_handler.py::bridge_pane_ask` |
| Buttons & legend | `c_lord/discord_ui/ask_view.py`, `embeds.py::ask_embed` |
| Send selection keystrokes | `tmux_runner.py::answer_menu` / `answer_menu_text` |
| Regression fixtures | `tests/fixtures/panes/ask_user_question_*.txt` |
