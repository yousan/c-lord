"""In-memory registry of the Claude Code sessions that are running a turn.
# <<REDACTED 1 lines>>
``/workspace-cleanup`` reads it to skip workspaces that are in use.
# <<REDACTED 1 lines>>
History: this module also built a "concurrency notice" meant to be injected into
every session (#52). Since the tmux TUI (#53) there is no per-turn channel for it,
so it was built and dropped (#758); #766 removed the builder.
# <<REDACTED 8 lines>>
# Active Session Registry
# <<REDACTED 67 lines>>
