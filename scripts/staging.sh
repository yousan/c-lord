#!/usr/bin/env bash
# =============================================================================
# staging.sh — guarded bot lifecycle for a c-lord clone (#327, parent #322)
# =============================================================================
# 使い方 (clone のルートで実行):
#   bash scripts/staging.sh status             # identity / branch / pid / log
#   bash scripts/staging.sh stop               # この clone の bot を安全停止
#   bash scripts/staging.sh restart [<branch>] # (branch 切替+)安全再起動
#
# 環境非依存: すべての値 (bot 名・ログ名・venv・期待 identity) は「実行した
# ディレクトリ」から導出する。staging 専用にハードコードしない — 環境が
# 増えても (C-lord-4 等) このスクリプト 1 本で足りる。
#
# このスクリプトが置き換える事故パターン (#322 根因D, docs/STAGING.md 参照):
#   - `pgrep -f "c_lord.main" | xargs kill` は本番/staging/自分のシェルの
#     全部に当たる (自滅 exit 144 / 本番巻き添え / 相対パス起動の取り逃し)。
#     → プロセス同定は /proc/<pid>/cwd、kill は PID 直指定のみ。
#   - `nohup uv run ...` は Bash ツールの teardown で死ぬ (exit 144)。
#     → setsid + venv python 直叩き。
#   - ログ固定パス truncate で前回の検証証跡が消える。
#     → per-run ログ + 最新への symlink。
#   - 誤った identity (本番トークン等) のまま静かに走り続ける。
#     → 起動後に "Logged in as" を待ち、IDENTITY MISMATCH (#323) や
#       ログイン失敗を検出したら非 0 で終了する。
# =============================================================================
set -u

CLONE_DIR="$(pwd)"
NAME="$(basename "$CLONE_DIR")"
ENV_FILE="$CLONE_DIR/.env"
VENV_PY="$CLONE_DIR/.venv/bin/python3"
LOG_LINK="/tmp/clord-bot-$NAME.log"

die() {
  echo "ERROR: $*" >&2
  exit 1
}

usage() {
  echo "usage: bash scripts/staging.sh {status|stop|restart [<branch>]}" >&2
  exit 1
}

env_get() {
  # .env から key の値を取る (最後の定義が勝ち)。secrets は呼び出し側で
  # 出力しないこと。
  /usr/bin/grep "^$1=" "$ENV_FILE" 2>/dev/null | tail -1 | cut -d= -f2- || true
}

find_pids() {
  # この clone を cwd とする c_lord.main プロセスだけを列挙する。
  # cmdline パターンマッチ (pgrep -f) は使わない: 自分のシェルへの自己
  # マッチ・相対パス起動の取り逃し・他環境への誤爆があるため。
  local pid cwd
  for pid in $(ps -eo pid --no-headers); do
    cwd="$(readlink "/proc/$pid/cwd" 2>/dev/null)" || continue
    [ "$cwd" = "$CLONE_DIR" ] || continue
    if tr '\0' ' ' <"/proc/$pid/cmdline" 2>/dev/null | /usr/bin/grep -q "c_lord\.main"; then
      echo "$pid"
    fi
  done
}

cmd_status() {
  local pids count branch expected channel
  pids="$(find_pids)"
  count=$(echo "$pids" | /usr/bin/grep -c . || true)
  branch="$(git -C "$CLONE_DIR" branch --show-current 2>/dev/null || echo '(not a git repo)')"
  expected="$(env_get EXPECTED_BOT_USER_ID)"
  channel="$(env_get DISCORD_CHANNEL_ID)"
  echo "clone:     $CLONE_DIR"
  echo "branch:    $branch"
  echo "channel:   ${channel:-(unset)}"
  echo "expected:  ${expected:-(unset — #323 ガード無効。.env に EXPECTED_BOT_USER_ID を設定推奨)}"
  echo "log:       $LOG_LINK"
  echo "instances: $count"
  if [ "$count" -gt 0 ]; then
    local p
    for p in $pids; do
      echo "  pid $p (started $(ps -o lstart= -p "$p" 2>/dev/null | sed 's/^ *//'))"
    done
    if [ -e "$LOG_LINK" ]; then
      /usr/bin/grep -E "Logged in as|IDENTITY MISMATCH" "$LOG_LINK" 2>/dev/null | tail -2 | sed 's/^/  /'
    fi
  fi
  if [ "$count" -gt 1 ]; then
    echo "WARNING: 二重起動の疑い。stop してから restart してください。"
    return 2
  fi
}

cmd_stop() {
  local pids p waited
  pids="$(find_pids)"
  if [ -z "$pids" ]; then
    echo "no running instance for $CLONE_DIR"
    return 0
  fi
  # kill は PID 直指定のみ。パターン kill 禁止・並列バッチに入れるの禁止
  # (キャンセルしても発射済みの kill は戻らない — 2026-05-29 の実害)。
  for p in $pids; do
    echo "stopping pid $p"
    kill "$p" 2>/dev/null || true
  done
  waited=0
  while [ $waited -lt 15 ]; do
    sleep 1
    waited=$((waited + 1))
    pids="$(find_pids)"
    [ -z "$pids" ] && {
      echo "stopped."
      return 0
    }
  done
  die "プロセスが 15 秒で終了しない (残: $pids)。手動確認してください。"
}

check_log_identity() {
  # ログの "Logged in as ... (ID: <id>)" を .env の EXPECTED_BOT_USER_ID と
  # スクリプト側で照合する。bot 側ガード (#323) に依存しない: 対象 clone が
  # 古いコード (#323/#324 以前) でも誤 identity を検出できる必要がある —
  # 2026-06-10 の検証中、この照合が無い初版スクリプトは「継承した本番 env +
  # override=False の旧コード」の組み合わせで本番 identity を再現させた
  # (即 kill、実害なし。#327 の PR の Staging Evidence 参照)。
  local log="$1" expected actual
  expected="$(env_get EXPECTED_BOT_USER_ID)"
  actual="$(/usr/bin/grep -o "Logged in as .* (ID: [0-9]*)" "$log" 2>/dev/null | tail -1 | /usr/bin/grep -o "[0-9]*" | tail -1)"
  if [ -z "$expected" ]; then
    echo "WARNING: EXPECTED_BOT_USER_ID が .env に無い — identity 照合をスキップ (設定を強く推奨)"
    return 0
  fi
  [ -n "$actual" ] || {
    echo "ERROR: ログから identity を読めない ($log)"
    return 1
  }
  if [ "$actual" != "$expected" ]; then
    echo "ERROR: IDENTITY MISMATCH (script-side): logged in as $actual, expected $expected"
    return 1
  fi
  echo "identity verified: $actual"
}

cmd_restart() {
  local branch_arg="${1:-}"
  [ -x "$VENV_PY" ] || die "no .venv in $CLONE_DIR ($VENV_PY がない)。uv sync --dev を先に実行。"

  if [ -n "$branch_arg" ]; then
    git -C "$CLONE_DIR" fetch origin "$branch_arg" -q 2>/dev/null || true
    git -C "$CLONE_DIR" checkout "$branch_arg" -q || die "branch '$branch_arg' に checkout できない"
    echo "checked out: $(git -C "$CLONE_DIR" log --oneline -1)"
  fi

  cmd_stop

  local ts log
  ts="$(date +%Y%m%d-%H%M%S)"
  log="/tmp/clord-bot-$NAME-$ts.log"
  # per-run ログ: 前回の検証証跡を truncate で消さない。最新は symlink で参照。
  # setsid + venv python 直叩き: Bash ツール teardown (exit 144) と
  # `uv run` ラッパーの cmdline 不定形を両方回避する。
  #
  # env -u による消毒は #324 (override=True) が入った現行コードでは冗長だが、
  # **対象 clone が古いコードのときの最後の砦**なので外さない (2026-06-10 の
  # 検証で実証: 消毒なし + 旧コード = 本番 identity 再現)。
  (cd "$CLONE_DIR" && setsid env \
    -u DISCORD_BOT_TOKEN -u DISCORD_CHANNEL_ID -u DISCORD_OWNER_ID \
    -u CLAUDE_COMMAND -u CLAUDE_MODEL -u CLAUDE_PERMISSION_MODE -u CLAUDE_WORKING_DIR \
    -u SESSION_DIR_BASE -u SESSION_SOURCE_REPO -u SESSION_TIMEOUT_SECONDS \
    -u MAX_CONCURRENT_SESSIONS -u CLORD_TMUX_ENABLED -u CLORD_API_PORT -u CLORD_API_URL \
    -u CLORD_API_HOST -u CLORD_API_SECRET -u CLORD_BRIDGE_MODE -u CLORD_RENDER_TABLE_IMAGES \
    -u CLORD_MIRROR_VERBOSITY -u CLORD_TRUSTED_BOT_IDS -u CLORD_ALLOWED_ROLE \
    -u COORDINATION_CHANNEL_ID -u EXPECTED_BOT_USER_ID \
    -u E2E_TEST_THREAD_ID -u E2E_TEST_WEBHOOK_URL -u VIRTUAL_ENV \
    nohup "$VENV_PY" -m c_lord.main >"$log" 2>&1 </dev/null &)
  ln -sf "$log" "$LOG_LINK"
  echo "launched -> $log"

  # 起動検証: "Logged in as" を待つ。identity mismatch / 早期死亡は失敗。
  local waited=0
  while [ $waited -lt 40 ]; do
    sleep 2
    waited=$((waited + 2))
    if /usr/bin/grep -q "IDENTITY MISMATCH" "$log" 2>/dev/null; then
      /usr/bin/grep -E "Logged in as|IDENTITY MISMATCH" "$log" | tail -2
      die "identity mismatch — 誤った bot として起動しようとした (#323 ガード作動)"
    fi
    if /usr/bin/grep -q "Logged in as" "$log" 2>/dev/null; then
      /usr/bin/grep -E "Logged in as|Watching channel" "$log" | head -2
      # スクリプト側照合: 古いコードの clone でも誤 identity を逃さない。
      if ! check_log_identity "$log"; then
        local p
        for p in $(find_pids); do
          echo "killing wrong-identity pid $p"
          kill "$p" 2>/dev/null || true
        done
        die "誤った identity で起動したため停止した (ログ: $log)"
      fi
      local count
      count="$(find_pids | /usr/bin/grep -c . || true)"
      echo "instances: $count"
      [ "$count" = "1" ] || die "単一インスタンスでない (count=$count)"
      echo "OK"
      return 0
    fi
    # プロセスが既に死んでいる (ログイン失敗等) なら早期失敗
    if [ -z "$(find_pids)" ]; then
      tail -5 "$log"
      die "bot が起動直後に終了した (ログ: $log)"
    fi
  done
  tail -5 "$log"
  die "40 秒以内に 'Logged in as' が出ない (ログ: $log)"
}

# ---- entry -----------------------------------------------------------------
[ -f "$ENV_FILE" ] || die "no .env in $CLONE_DIR — clone のルートで実行してください"

case "${1:-}" in
status) cmd_status ;;
stop) cmd_stop ;;
restart)
  shift
  cmd_restart "${1:-}"
  ;;
check-log)
  # テスト用の隠しサブコマンド: ログファイルの identity を .env と照合
  shift
  check_log_identity "${1:?usage: check-log <logfile>}"
  ;;
*) usage ;;
esac
