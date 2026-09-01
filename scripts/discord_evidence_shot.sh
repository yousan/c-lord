#!/usr/bin/env bash
# Dev tool (#243): capture a real Discord client screenshot of a channel/message URL.
#
# A disposable test account is logged in ONCE by a human into a dedicated Chrome
# profile (see docs/discord-evidence-capture.md). This script only renders that
# already-authenticated client in a virtual display and grabs the framebuffer.
# It never injects input into the client and never automates the account —
# navigation happens solely by passing the URL at browser launch.
#
# ⛔ DO NOT ADD INPUT INJECTION — THIS IS A RULE, NOT AN IMPLEMENTATION NOTE.
#    No key/click/scroll may ever be sent into the loaded client, not even to
#    dismiss a promo modal (yousan's 2026-08-26 ruling, #559: Discord's terms
#    are respected on purpose). A human closes the modal by hand; the profile
#    is shared, so one dismissal fixes every future capture for every thread.
#    What this script may do instead is REFUSE a frame it can see is unusable
#    (scripts/evidence_qc.py).
set -euo pipefail

usage() {
    cat <<'USAGE'
Usage:
  scripts/discord_evidence_shot.sh <discord-url|channel-id> [-o out.png]
      [--size WxH] [--wait SECONDS] [--profile DIR] [--no-qc]

  <discord-url>   https://discord.com/channels/<guild>/<channel>[/<message>]
                  (a message link makes the client jump to that message)
  <channel-id>    a bare channel/thread ID — the guild is looked up for you,
                  which is the whole point: a hand-typed guild id that belongs
                  to another server silently captures the friends screen (#559)

  -o              output PNG (default: evidence.png)
  --size WxH      virtual screen size (default: 1600x1000). A very tall window
                  is the documented mitigation while a promo modal is up
  --wait SECONDS  seconds to let the client render (default: 45)
  --profile DIR   Chrome profile holding the test account's session
  --no-qc         skip the post-capture image check (it will not be evidence)

Guild lookup and verification need a bot token: $DISCORD_BOT_TOKEN, else
DISCORD_BOT_TOKEN= in $CLORD_ENV_FILE, else the .env next to this repo.
USAGE
}

HERE="$(cd "$(dirname "${BASH_SOURCE[0]}")" && pwd)"
PROFILE="${CLORD_EVIDENCE_PROFILE:-$HOME/.clord/discord-evidence-profile}"
OUT="evidence.png"
SIZE="1600x1000"
WAIT=45
QC=1
URL=""

die() { echo "error: $*" >&2; exit 1; }

while [[ $# -gt 0 ]]; do
    case "$1" in
        -o) OUT="$2"; shift 2 ;;
        --size) SIZE="$2"; shift 2 ;;
        --wait) WAIT="$2"; shift 2 ;;
        --profile) PROFILE="$2"; shift 2 ;;
        --no-qc) QC=0; shift ;;
        -h|--help) usage; exit 0 ;;
        -*) die "unknown option: $1" ;;
        *) URL="$1"; shift ;;
    esac
done

[[ -n "$URL" ]] || { usage >&2; exit 1; }

# --- bot token (read-only lookups only) -------------------------------------
bot_token() {
    if [[ -n "${DISCORD_BOT_TOKEN:-}" ]]; then echo "$DISCORD_BOT_TOKEN"; return; fi
    local f
    for f in "${CLORD_ENV_FILE:-}" "$HERE/../.env"; do
        [[ -n "$f" && -r "$f" ]] || continue
        local v
        v="$(grep -m1 '^DISCORD_BOT_TOKEN=' "$f" | cut -d= -f2- | tr -d '"'"'"' \r')"
        if [[ -n "$v" ]]; then echo "$v"; return; fi
    done
}

# Echo the guild ID that owns a channel/thread, or nothing when it can't be read.
# Never fails: callers run under `set -e` and decide what an empty answer means.
guild_of() {
    local channel="$1" token="$2" body=""
    # The token goes through curl's config on stdin, never argv: `ps` on a
    # shared host would otherwise show it in full.
    body="$(printf 'header = "Authorization: Bot %s"\nheader = "User-Agent: DiscordBot (https://github.com/yousan/c-lord, 1.0)"\nurl = "https://discord.com/api/v10/channels/%s"\n' \
        "$token" "$channel" | curl -fsS -K - 2>/dev/null || true)"
    [[ -n "$body" ]] || return 0
    python3 -c 'import json,sys; print(json.load(sys.stdin).get("guild_id") or "")' \
        <<<"$body" 2>/dev/null || true
}

TOKEN="$(bot_token || true)"

if [[ "$URL" =~ ^[0-9]{15,25}$ ]]; then
    # Bare channel/thread ID: resolve the guild so it cannot be got wrong.
    [[ -n "$TOKEN" ]] || die "a bare channel ID needs a bot token — see --help"
    GUILD="$(guild_of "$URL" "$TOKEN")"
    [[ -n "$GUILD" ]] || die "could not resolve the guild of channel $URL (wrong ID, or the bot cannot see it)"
    URL="https://discord.com/channels/$GUILD/$URL"
    echo "resolved: $URL"
elif [[ "$URL" =~ ^https://(ptb\.|canary\.)?discord\.com/channels/([0-9]+)/([0-9]+) ]]; then
    # Full URL: check the guild really owns the channel (#559 AC10). A guild ID
    # copied from the wrong server does not error — it quietly renders the
    # friends screen, which looks exactly like "the account lacks permission".
    GIVEN_GUILD="${BASH_REMATCH[2]}"
    GIVEN_CHANNEL="${BASH_REMATCH[3]}"
    if [[ -n "$TOKEN" ]]; then
        REAL_GUILD="$(guild_of "$GIVEN_CHANNEL" "$TOKEN")"
        if [[ -n "$REAL_GUILD" && "$REAL_GUILD" != "$GIVEN_GUILD" ]]; then
            die "guild mismatch: channel $GIVEN_CHANNEL lives in guild $REAL_GUILD, not $GIVEN_GUILD
       use: https://discord.com/channels/$REAL_GUILD/$GIVEN_CHANNEL
       (or just pass the channel ID and let this script fill in the guild)"
        fi
    else
        echo "note: no bot token, so the guild in the URL was not verified" >&2
    fi
else
    die "pass a bare channel/thread ID, or an https://discord.com/channels/... link"
fi

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
    if [[ ! -e "/tmp/.X11-unix/X$n" && ! -e "/tmp/.X$n-lock" ]]; then DISP=":$n"; break; fi
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

# Never report success for a frame we can see is unusable (#559). The PNG is
# left on disk so the problem can be looked at — it just isn't "captured".
if [[ "$QC" -eq 0 ]]; then
    echo "captured (UNVERIFIED — --no-qc was given): $OUT"
    exit 0
fi

set +e
python3 "$HERE/evidence_qc.py" "$OUT"
QC_STATUS=$?
set -e

case "$QC_STATUS" in
    0)
        echo "captured: $OUT"
        ;;
    3)
        echo "captured (UNVERIFIED — Pillow missing, image check skipped): $OUT"
        ;;
    *)
        echo "not usable as evidence: $OUT (kept for inspection)" >&2
        exit "$QC_STATUS"
        ;;
esac
