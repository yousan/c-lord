#!/usr/bin/env bash
# Dev tool (#243): capture a real Discord client screenshot of a channel/message URL.
#
# A disposable test account is logged in ONCE by a human into a dedicated Chrome
# profile (see docs/discord-evidence-capture.md). This script only renders that
# already-authenticated client in a virtual display and grabs the framebuffer.
# It never injects input into the client and never automates the account —
# navigation happens solely by passing the URL at browser launch.
#
# Usage:
#   scripts/discord_evidence_shot.sh <discord-url> [-o out.png] [--size WxH] \
#       [--wait SECONDS] [--profile DIR]
#
#   <discord-url>  https://discord.com/channels/<guild>/<channel>[/<message>]
#                  (a message link makes the client jump to that message)
set -euo pipefail

PROFILE="${CLORD_EVIDENCE_PROFILE:-$HOME/.clord/discord-evidence-profile}"
OUT="evidence.png"
SIZE="1600x1000"
WAIT=20
URL=""

usage() { sed -n '2,15p' "$0" | sed 's/^# \{0,1\}//'; }
die() { echo "error: $*" >&2; exit 1; }

while [[ $# -gt 0 ]]; do
    case "$1" in
        -o) OUT="$2"; shift 2 ;;
        --size) SIZE="$2"; shift 2 ;;
        --wait) WAIT="$2"; shift 2 ;;
        --profile) PROFILE="$2"; shift 2 ;;
        -h|--help) usage; exit 0 ;;
        -*) die "unknown option: $1" ;;
        *) URL="$1"; shift ;;
    esac
done

[[ -n "$URL" ]] || { usage >&2; exit 1; }
# Restrict to Discord: the profile holds the test account's session token, so
# this script must not be usable as a generic logged-in browser.
[[ "$URL" =~ ^https://(ptb\.|canary\.)?discord\.com/channels/ ]] \
    || die "URL must be an https://discord.com/channels/... link"
[[ "$SIZE" =~ ^[0-9]+x[0-9]+$ ]] || die "--size must look like 1600x1000"
[[ "$WAIT" =~ ^[0-9]+$ ]] || die "--wait must be an integer"
[[ -d "$PROFILE" ]] \
    || die "profile not found: $PROFILE — do the one-time human login first (docs/discord-evidence-capture.md)"

# One Chrome per profile: captures are serialized, never shared.
if pgrep -f "user-data-dir=$PROFILE" >/dev/null 2>&1; then
    die "profile is in use by another Chrome — wait for it to finish or close it"
fi

# Find a free X display (other Xvfb instances may occupy low numbers).
DISP=""
for n in $(seq 90 120); do
    if [[ ! -e "/tmp/.X11-unix/X$n" ]]; then DISP=":$n"; break; fi
done
[[ -n "$DISP" ]] || die "no free X display found in :90..:120"

CHROME_PID=""
XVFB_PID=""
cleanup() {
    [[ -n "$CHROME_PID" ]] && kill "$CHROME_PID" 2>/dev/null || true
    [[ -n "$XVFB_PID" ]] && kill "$XVFB_PID" 2>/dev/null || true
}
trap cleanup EXIT

Xvfb "$DISP" -screen 0 "${SIZE}x24" >/dev/null 2>&1 &
XVFB_PID=$!
sleep 2

DISPLAY="$DISP" google-chrome \
    --user-data-dir="$PROFILE" \
    --no-first-run --disable-gpu --disable-dev-shm-usage \
    --window-size="${SIZE/x/,}" --kiosk \
    "$URL" >/dev/null 2>&1 &
CHROME_PID=$!

sleep "$WAIT"
ffmpeg -hide_banner -loglevel error -y \
    -f x11grab -video_size "$SIZE" -i "$DISP" -frames:v 1 "$OUT"

echo "captured: $OUT"
echo "note: if the image shows the Discord login page, the session expired —"
echo "      ask a human to re-run the one-time login (docs/discord-evidence-capture.md)"
