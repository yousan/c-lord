#!/usr/bin/env bash
# =============================================================================
# install-systemd.sh — run c-lord as a systemd --user service (survives reboot)
# =============================================================================
# Generates ~/.config/systemd/user/c-lord.service pointing at THIS clone and your
# `uv`, enables it, and turns on linger so it starts on boot without a login.
#
# Idempotent: safe to re-run (regenerates the unit + re-enables). No root needed
# for the service itself; `loginctl enable-linger` may prompt for sudo on some
# distros. Requires a user systemd instance (standard on desktop/server Ubuntu).
#
# After a code update, restart with:  systemctl --user restart c-lord.service
# =============================================================================
set -euo pipefail

REPO_DIR="$(cd "$(dirname "${BASH_SOURCE[0]}")/.." && pwd)"
UNIT_NAME="c-lord.service"
UNIT_DIR="${XDG_CONFIG_HOME:-$HOME/.config}/systemd/user"
UNIT_PATH="$UNIT_DIR/$UNIT_NAME"

UV="$(command -v uv || true)"
if [ -z "$UV" ]; then
  echo "ERROR: 'uv' not found on PATH. Install it first: https://docs.astral.sh/uv/" >&2
  exit 1
fi
UV_DIR="$(dirname "$UV")"

# Build the service PATH from the installing shell's $PATH so the service finds
# claude / uv / tmux / git exactly like your interactive shell does (a systemd
# --user service otherwise gets a minimal PATH and can't spawn the claude CLI).
# Sanitize: keep only absolute, existing dirs, de-duplicated — drops relative
# entries (./node_modules/.bin) and stale/foreign paths that don't exist here.
SERVICE_PATH=""
add_dir() {
  case "$1" in /*) ;; *) return ;; esac          # absolute only
  [ -d "$1" ] || return                           # existing only
  case ":$SERVICE_PATH:" in *":$1:"*) return ;; esac  # de-dup
  SERVICE_PATH="${SERVICE_PATH:+$SERVICE_PATH:}$1"
}
add_dir "$UV_DIR"
add_dir "$HOME/.local/bin"
IFS=: read -ra _path_parts <<<"$PATH"
for _d in "${_path_parts[@]}"; do add_dir "$_d"; done
for _d in /usr/local/bin /usr/bin /bin; do add_dir "$_d"; done

if [ ! -f "$REPO_DIR/.env" ]; then
  echo "WARNING: $REPO_DIR/.env not found — run 'uv run c-lord setup' first," >&2
  echo "         otherwise the service will start but fail to log in to Discord." >&2
fi

mkdir -p "$UNIT_DIR"
cat > "$UNIT_PATH" <<EOF
[Unit]
Description=c-lord (Discord <-> Claude Code bridge)
Documentation=https://github.com/yousan/c-lord
After=network-online.target
Wants=network-online.target
StartLimitIntervalSec=60
StartLimitBurst=5

[Service]
Type=simple
WorkingDirectory=$REPO_DIR
ExecStart=$UV run python -m c_lord.main
Restart=always
RestartSec=5
Environment=PATH=$SERVICE_PATH

[Install]
WantedBy=default.target
EOF
echo "wrote $UNIT_PATH"
echo "  WorkingDirectory=$REPO_DIR"
echo "  ExecStart=$UV run python -m c_lord.main"

systemctl --user daemon-reload
systemctl --user enable --now "$UNIT_NAME"

# Linger: keep the user manager (and thus this service) running after logout and
# start it on boot without a login session.
if loginctl show-user "$USER" 2>/dev/null | grep -q '^Linger=yes'; then
  echo "linger already enabled for $USER"
else
  echo "enabling linger for $USER (start on boot without login)…"
  loginctl enable-linger "$USER" 2>/dev/null || sudo loginctl enable-linger "$USER"
fi

echo
systemctl --user status "$UNIT_NAME" --no-pager || true
echo
echo "Done. Manage the service with:"
echo "  systemctl --user restart c-lord.service    # after a git pull"
echo "  systemctl --user stop    c-lord.service"
echo "  systemctl --user status  c-lord.service"
echo "  journalctl --user -u c-lord.service -f     # live logs"
