#!/usr/bin/env bash
# =============================================================================
# cron_hourly.sh — run the natural-language fuzz harness once, hourly (#377)
# =============================================================================
# Install (crontab -e), top of every hour:
#
#   0 * * * * /ABS/PATH/c-lord/scripts/fuzz/cron_hourly.sh
#
# This wrapper runs the harness from the clone it lives in (which must have
# scripts/fuzz + a .venv). Point it at the staging FLEET with FUZZ_STAGING_CLONES
# (comma-separated clone dirs) — the harness runs on the first one it can lease,
# so a busy clone no longer wastes the whole hour. FUZZ_STAGING_CLONE_DIR (single)
# still works; with neither set, it runs in-place.
#
#   export FUZZ_STAGING_CLONES=/home/you/c-lord-staging-1,/home/you/c-lord-staging-2,\
#     /home/you/c-lord-staging-3,/home/you/c-lord-staging-4
#
# Per-clone config (bot token, channels, webhook) is read from each clone's own
# .env. Injection mode is auto-resolved per clone: a CLORD_BRIDGE_MODE=jsonl clone
# does not bind its REST API, so the harness uses webhook+skip-health there
# automatically (no FUZZ_EXTRA_ARGS needed); otherwise spawn. Override with
# FUZZ_EXTRA_ARGS="--inject spawn" / "--restart-if-down" only when you've verified
# FUZZ_API_URL points at a reachable API.
#
# The run borrows a lease; if all clones are busy/absent it skips cleanly (exit 0).
# Per-run logs land in $FUZZ_LOG_DIR (default /tmp). Tunables: FUZZ_COUNT,
# FUZZ_MODEL, FUZZ_EXTRA_ARGS.
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

# Build args. Fleet (FUZZ_STAGING_CLONES) wins; else single FUZZ_STAGING_CLONE_DIR.
# --restart-if-down / --inject are intentionally NOT here (auto-resolved per clone;
# see header) — opt in via FUZZ_EXTRA_ARGS.
ARGS=()
if [ -n "${FUZZ_STAGING_CLONES:-}" ]; then
  ARGS+=(--staging-clones "$FUZZ_STAGING_CLONES")
elif [ -n "${FUZZ_STAGING_CLONE_DIR:-}" ]; then
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
