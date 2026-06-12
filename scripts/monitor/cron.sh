#!/usr/bin/env bash
# =============================================================================
# cron.sh — run the read-only traffic monitor once (Issue #404)
# =============================================================================
# Install (crontab -e), e.g. every 10 minutes (off the :00 mark):
#
#   3,13,23,33,43,53 * * * * MONITOR_LOGS='/tmp/clord-bot-c-lord*.log' \
#     MONITOR_GUILD_ID=<guild> MONITOR_CHANNELS=<ch1,ch2> MONITOR_REPORT_CHANNEL=<ch> \
#     /ABS/PATH/c-lord/scripts/monitor/cron.sh
#
# Read-only: scans bot logs (incremental, via state file) + live threads for
# anomalies and posts only NEW ones to the report channel. No lease/inject/restart,
# so it is safe to run against prod + the staging fleet at the same time.
#
# Config comes from env (above) or the harness clone's .env. Per-run logs land in
# $MONITOR_LOG_DIR (default /tmp). Tunables: MONITOR_STUCK_TIMEOUT, MONITOR_EXTRA_ARGS.
# =============================================================================
set -u

SCRIPT_DIR="$(cd "$(dirname "${BASH_SOURCE[0]}")" && pwd)"
HARNESS_DIR="$(cd "$SCRIPT_DIR/../.." && pwd)"
VENV_PY="$HARNESS_DIR/.venv/bin/python3"
export PATH="$HOME/.local/bin:/usr/local/bin:/usr/bin:/bin:$PATH"

LOG_DIR="${MONITOR_LOG_DIR:-/tmp}"
NAME="$(basename "$HARNESS_DIR")"
TS="$(date +%Y%m%d-%H%M%S)"
LOG="$LOG_DIR/clord-monitor-$NAME-$TS.log"
ln -sf "$LOG" "$LOG_DIR/clord-monitor-$NAME.log" 2>/dev/null || true

if [ ! -x "$VENV_PY" ]; then
  echo "ERROR: no venv python at $VENV_PY (run uv sync --dev in $HARNESS_DIR)" >&2
  exit 1
fi

ARGS=()
[ -n "${MONITOR_LOGS:-}" ] && ARGS+=(--logs "$MONITOR_LOGS")
[ -n "${MONITOR_CHANNELS:-}" ] && ARGS+=(--channels "$MONITOR_CHANNELS")
[ -n "${MONITOR_GUILD_ID:-}" ] && ARGS+=(--guild "$MONITOR_GUILD_ID")
[ -n "${MONITOR_REPORT_CHANNEL:-}" ] && ARGS+=(--report-channel "$MONITOR_REPORT_CHANNEL")
[ -n "${MONITOR_STUCK_TIMEOUT:-}" ] && ARGS+=(--stuck-timeout "$MONITOR_STUCK_TIMEOUT")
# shellcheck disable=SC2206
[ -n "${MONITOR_EXTRA_ARGS:-}" ] && ARGS+=(${MONITOR_EXTRA_ARGS})

cd "$HARNESS_DIR" || exit 1
echo "[cron] $(date -Is) monitor run → $LOG"
"$VENV_PY" -m scripts.monitor "${ARGS[@]}" >"$LOG" 2>&1
rc=$?
echo "[cron] $(date -Is) monitor run exited $rc (log: $LOG)"
exit "$rc"
