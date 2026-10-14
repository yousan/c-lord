# Running c-lord on macOS

> **Status: not yet confirmed on a real Mac.** The code paths below are covered by
> the `test-macos` CI job (#899), but nobody has run a bot end to end on macOS yet.
> Until that happens this page is not linked from the README.

c-lord was written on Linux. On macOS it works the same from Discord's side; a few
things underneath are done differently because macOS has no `/proc` and no systemd.

## Prerequisites

```bash
brew install tmux git uv
# The Claude Code CLI — see https://docs.claude.com/en/docs/claude-code
claude --version
```

## Setup and a first run (foreground)

Same as on Linux — the setup wizard writes `.env`:

```bash
git clone https://github.com/yousan/c-lord.git
cd c-lord
uv sync
uv run c-lord setup        # token, channel, working directory
uv run c-lord start        # Ctrl-C to stop
```

Post a message in the configured channel; Claude replies in a new thread.

## Keep it running (launchd)

systemd does not exist on macOS; the equivalent is a launchd **LaunchAgent**. c-lord
does not install one for you yet — copy the example below.

Save as `~/Library/LaunchAgents/local.c-lord.plist`, replacing every
`/Users/YOU/c-lord` with the absolute path of your clone:

```xml
<?xml version="1.0" encoding="UTF-8"?>
<!DOCTYPE plist PUBLIC "-//Apple//DTD PLIST 1.0//EN" "http://www.apple.com/DTDs/PropertyList-1.0.dtd">
<plist version="1.0">
<dict>
  <key>Label</key>
  <string>local.c-lord</string>

  <!-- The venv's python directly, not `uv run`: one process, so a stop signal
       reaches the bot exactly once (the double-SIGTERM of #877 cannot happen). -->
  <key>ProgramArguments</key>
  <array>
    <string>/Users/YOU/c-lord/.venv/bin/python</string>
    <string>-m</string>
    <string>c_lord.main</string>
  </array>
  <key>WorkingDirectory</key>
  <string>/Users/YOU/c-lord</string>

  <!-- launchd starts jobs with a bare environment. PATH must reach tmux, git
       and claude (`command -v tmux claude git`); LANG keeps tmux in UTF-8 so
       non-ASCII prompts are typed correctly. -->
  <key>EnvironmentVariables</key>
  <dict>
    <key>PATH</key>
    <string>/opt/homebrew/bin:/usr/local/bin:/Users/YOU/.local/bin:/usr/bin:/bin</string>
    <key>LANG</key>
    <string>en_US.UTF-8</string>
  </dict>

  <key>RunAtLoad</key>
  <true/>
  <key>KeepAlive</key>
  <true/>
  <key>ThrottleInterval</key>
  <integer>5</integer>

  <key>StandardOutPath</key>
  <string>/Users/YOU/Library/Logs/c-lord.log</string>
  <key>StandardErrorPath</key>
  <string>/Users/YOU/Library/Logs/c-lord.log</string>
</dict>
</plist>
```

Manage it with `launchctl`:

```bash
launchctl bootstrap gui/$(id -u) ~/Library/LaunchAgents/local.c-lord.plist   # load + start
launchctl print gui/$(id -u)/local.c-lord | grep -E 'state|pid'             # status
launchctl kickstart -k gui/$(id -u)/local.c-lord                            # restart
launchctl bootout gui/$(id -u)/local.c-lord                                 # stop + unload
tail -f ~/Library/Logs/c-lord.log                                           # logs
```

As with systemd on Linux: once the agent is loaded, **launchd is the only thing that
starts the bot**. Don't also run `c-lord start` by hand — that is a second bot on the
same token.

A LaunchAgent runs while you are logged in. If the Mac sleeps, the bot goes offline
until it wakes; keep it awake (System Settings → Energy) if it must stay reachable.

## What is different from Linux

| | Linux | macOS |
|---|---|---|
| Who called the REST API (`CLORD_API_URL`) | `/proc/net/tcp` | `lsof`. If the caller cannot be proven — no `lsof`, timeout, no matching socket, or more than one owner — the request is **refused** (403). A non-root `lsof` cannot see other users' processes, so callers from other accounts are refused |
| Resident workspace cap / memory brake | `/proc/meminfo` (or the cgroup limit) | installed RAM from `sysconf`, free memory from `sysctl kern.memorystatus_level`. If free memory cannot be read the emergency brake stays off |
| ⏹ Stop finding Claude's background commands | `/proc` | `ps` |
| tmux server after a bot restart | started in its own systemd unit so a restart cannot kill it (#503) | no systemd; tmux daemonizes on its own and outlives the bot |
| Monospace font for pane / table images | DejaVu Sans Mono etc. | Menlo / Monaco. Japanese uses Hiragino. Color emoji are not rendered |
| `scripts/staging.sh`, `scripts/install-systemd.sh` | maintainer / service tools | Linux-only — not needed on a Mac |
