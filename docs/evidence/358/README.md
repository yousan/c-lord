# #358 Staging Evidence — open-menu `send_input` fix

Real-tmux RED→GREEN for [#358](https://github.com/yousan/c-lord/issues/358) /
[PR #360](https://github.com/yousan/c-lord/pull/360). Captured against a real
`claude` TUI in an isolated throwaway tmux session (`clord-verify-358`, killed
after capture; production `clord`/`c-lord`/`games` untouched). The Claude
welcome banner (email/host/home path) is cropped out; only the menu / response
region is shown. `cwd: /tmp/v358-*` confirms the isolated dir.

| Shot | Shows |
|------|-------|
| `358-1-menu-open.png` | The `AskUserQuestion` menu open in the pane — the "選択肢" (option 1 `commit だけ` highlighted, `Esc to cancel`). |
| `358-2-RED-prefix.png` | **Pre-fix** (`send_input` straight into the open menu): the trailing Enter auto-selected `commit だけ` (`● 「commit だけ」と回答いただきました`) and the user's follow-up message was lost. |
| `358-3-GREEN-fixed.png` | **Fixed** (`run()` dismisses the menu with Esc first): the follow-up arrived as a real prompt and Claude replied to its content (`● どのファイルを添付するか…`). |

Transcript proof (inner-session jsonl):
- RED: `AskUserQuestion` tool_result = `…"新規ファイルをどう扱う?"="commit だけ"`; the
  follow-up text never appears as a user turn.
- GREEN: `AskUserQuestion` tool_result = `The user doesn't want to proceed with
  this tool use. … rejected` (Esc), and the follow-up text reached Claude as a turn.
