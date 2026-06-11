#!/usr/bin/env bash
# =============================================================================
# cron_hourly.sh — run the natural-language fuzz harness once, hourly (#377)
# =============================================================================
# Install (crontab -e), top of every hour:
#
#   0 * * * * /ABS/PATH/c-lord/scripts/fuzz/cron_hourly.sh
#
# This wrapper runs the harness from the clone it lives in (which must have
# scripts/fuzz + a .venv), and points it at the STAGING clone for lease + inject
# via FUZZ_STAGING_CLONE_DIR. Config (bot token, FUZZ_* channels, API url) is
# read from the staging clone's .env by the harness. If FUZZ_STAGING_CLONE_DIR is
# unset, the harness runs against the clone it lives in (run-in-place model).
#
# The hourly run borrows the staging lease; if another session holds it, the run
# skips cleanly (exit 0). Per-run logs land in $FUZZ_LOG_DIR (default /tmp) with a
# symlink to the latest. Tunables: FUZZ_COUNT, FUZZ_MODEL, FUZZ_EXTRA_ARGS.
#
# IMPORTANT: for the default `spawn` injection, FUZZ_API_URL (or CLORD_API_URL in
# the staging .env) MUST point at the bot's ACTUAL API port — a drifted URL makes
# every scenario report SPAWN_FAILED/HEALTH_DOWN. `--restart-if-down` is therefore
# NOT passed by default (a wrong URL would needlessly restart staging every hour);
# opt in via FUZZ_EXTRA_ARGS="--restart-if-down" only when the URL is verified. For
# an API that is unreachable from this host, use FUZZ_EXTRA_ARGS="--inject webhook
# --skip-health" with FUZZ_WEBHOOK_URL + FUZZ_TEST_THREAD_ID set.
# =============================================================================
set -u

SCRIPT_DIR="$(cd "$(dirname "${BASH_SOURCE[0]}")" && pwd)"
HARNESS_DIR="$(cd "$SCRIPT_DIR/../.." && pwd)"
VENV_PY="$HARNESS_DIR/.venv/bin/python3"

# cron has a minimal PATH; make sure `claude`, `git`, `bash` resolve. Override
# the model's binary explicitly with CLAUDE_COMMAND in the staging .env if needed.
export PATH="$HOME/.local/bin:/usr/local/bin:/usr/bin:/bin:$PATH"

LOG_DIR="${FUZZ_LOG_DIR:-/tmp}"
NAME="$(basename "$HARNESS_DIR")"
TS="$(date +%Y%m%d-%H%M%S)"
LOG="$LOG_DIR/clord-fuzz-$NAME-$TS.log"
ln -sf "$LOG" "$LOG_DIR/clord-fuzz-$NAME.log" 2>/dev/null || true

if [ ! -x "$VENV_PY" ]; then
  echo "ERROR: no venv python at $VENV_PY (run uv sync --dev in $HARNESS_DIR)" >&2
  exit 1
fi

# Build args. --staging-dir defaults to the staging clone (lease lives there).
# --restart-if-down is intentionally NOT here (see header) — opt in via FUZZ_EXTRA_ARGS.
ARGS=()
if [ -n "${FUZZ_STAGING_CLONE_DIR:-}" ]; then
  ARGS+=(--staging-dir "$FUZZ_STAGING_CLONE_DIR")
fi
[ -n "${FUZZ_COUNT:-}" ] && ARGS+=(-n "$FUZZ_COUNT")
[ -n "${FUZZ_MODEL:-}" ] && ARGS+=(--model "$FUZZ_MODEL")
# shellcheck disable=SC2206
[ -n "${FUZZ_EXTRA_ARGS:-}" ] && ARGS+=(${FUZZ_EXTRA_ARGS})

cd "$HARNESS_DIR" || exit 1
echo "[cron] $(date -Is) fuzz run → $LOG"
"$VENV_PY" -m scripts.fuzz "${ARGS[@]}" >"$LOG" 2>&1
rc=$?
echo "[cron] $(date -Is) fuzz run exited $rc (log: $LOG)"
exit "$rc"
