#!/usr/bin/env bash
# =============================================================================
# staging.sh — guarded bot lifecycle for a c-lord clone (#327, parent #322)
# =============================================================================
# 使い方 (clone のルートで実行):
#   bash scripts/staging.sh status             # identity / branch / pid / log
#   bash scripts/staging.sh stop               # この clone の bot を安全停止
#   bash scripts/staging.sh restart [<branch>] # (branch 切替+)安全再起動
#
# 本番 (systemd の unit が WorkingDirectory にしている clone) では restart / stop は
# **bot を kill しない**。`systemctl --user restart|stop <unit>` に委ね、systemctl が
# 使えなければエラーで止まる (ops#2)。本番の起動の入口は systemd の 1 つだけ。
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
  cat >&2 <<'USAGE'
usage: bash scripts/staging.sh <command> [options]

  status                                     状態表示 (リース・identity・pid)
  borrow  --purpose "<目的>" [--ttl-hours N] リースを取得 (既定 TTL 2h)
  release                                    自分のリースを解放
  restart [<branch>]                         (branch 切替+) 安全再起動 — 要・自リース
  stop                                       安全停止 — 要・自リース (リース無しなら可)

  --owner <id> または環境変数 CLORD_LEASE_OWNER で身元を示す。
  restart/stop は他セッションの有効リース中は拒否される (#328)。
USAGE
  exit 1
}

# ---- lease (#328) -----------------------------------------------------------
# 占有リース: clone 直下の .staging-lease (環境ごとに 1 枚、中央台帳なし)。
# 1 つの staging working tree を複数セッションが取り合い、他人の検証中 bot を
# kill / checkout で踏む事故 (2026-05-29 実害) を機械的に防ぐ。
LEASE_FILE="$CLONE_DIR/.staging-lease"

lease_field() {
  # lease_field <key> — リースファイルから値を 1 つ読む (無ければ空)
  [ -f "$LEASE_FILE" ] || return 0
  python3 -c "
import json, sys
try:
    d = json.load(open('$LEASE_FILE'))
    print(d.get('$1', ''))
except Exception:
    pass"
}

lease_is_valid() {
  # 有効 (未失効) なリースが存在するか
  [ -f "$LEASE_FILE" ] || return 1
  local acquired ttl now
  acquired="$(lease_field acquired_epoch)"
  ttl="$(lease_field ttl_hours)"
  [ -n "$acquired" ] && [ -n "$ttl" ] || return 1
  now="$(date +%s)"
  python3 -c "import sys; sys.exit(0 if $now < $acquired + float($ttl)*3600 else 1)"
}

lease_remaining_min() {
  # 残り時間 (分)。失効していれば 0。
  local acquired ttl now
  acquired="$(lease_field acquired_epoch)"
  ttl="$(lease_field ttl_hours)"
  [ -n "$acquired" ] && [ -n "$ttl" ] || {
    echo 0
    return
  }
  now="$(date +%s)"
  python3 -c "print(max(0, int(($acquired + float($ttl)*3600 - $now) // 60)))"
}

lease_describe() {
  echo "owner=$(lease_field owner) purpose=\"$(lease_field purpose)\" acquired=$(lease_field acquired_at) ttl=$(lease_field ttl_hours)h remaining=$(lease_remaining_min)min"
}

lease_guard() {
  # restart / stop の前提: 有効な「自分の」リースがあること。
  # 他人の有効リース → 拒否 / リース無し or 失効 → borrow を促して拒否。
  local owner="$1" holder
  if lease_is_valid; then
    holder="$(lease_field owner)"
    if [ -z "$owner" ] || [ "$holder" != "$owner" ]; then
      echo "occupied: $(lease_describe)" >&2
      die "他セッションの有効リース中 (holder=$holder)。release を待つか TTL 失効後に borrow してください。"
    fi
    return 0
  fi
  die "有効な自リースが無い。先に borrow してください: bash scripts/staging.sh borrow --owner <id> --purpose \"...\""
}

cmd_borrow() {
  local owner="$1" purpose="$2" ttl="$3" holder branch
  [ -n "$owner" ] || die "--owner <id> (または CLORD_LEASE_OWNER) が必要"
  [ -n "$purpose" ] || die "--purpose \"<目的>\" が必要 (誰が見ても分かる形で)"
  if lease_is_valid; then
    holder="$(lease_field owner)"
    if [ "$holder" != "$owner" ]; then
      echo "occupied: $(lease_describe)" >&2
      die "borrow 拒否 — 他セッションの有効リース中"
    fi
    echo "re-borrow (同一 owner): 既存リースを更新"
  elif [ -f "$LEASE_FILE" ]; then
    # 失効リースの奪取: 旧内容をログに残す (奪取の監査痕跡)
    echo "takeover: 失効リースを奪取 — old: $(lease_describe)"
  fi
  branch="$(git -C "$CLONE_DIR" branch --show-current 2>/dev/null || echo '')"
  LEASE_PATH="$LEASE_FILE" LEASE_OWNER="$owner" LEASE_PURPOSE="$purpose" LEASE_TTL="$ttl" LEASE_BRANCH="$branch" python3 - <<'PYEOF'
import json, os, time
data = {
    "owner": os.environ["LEASE_OWNER"],
    "purpose": os.environ["LEASE_PURPOSE"],
    "branch_before": os.environ["LEASE_BRANCH"],
    "acquired_at": time.strftime("%Y-%m-%dT%H:%M:%S%z"),
    "acquired_epoch": int(time.time()),
    "ttl_hours": float(os.environ["LEASE_TTL"]),
}
with open(os.environ.get("LEASE_PATH", ".staging-lease"), "w") as f:
    json.dump(data, f, ensure_ascii=False, indent=1)
PYEOF
  echo "leased: $(lease_describe)"
}

cmd_release() {
  local owner="$1" holder
  [ -f "$LEASE_FILE" ] || {
    echo "no lease to release"
    return 0
  }
  holder="$(lease_field owner)"
  if [ -z "$owner" ] || [ "$holder" != "$owner" ]; then
    echo "lease: $(lease_describe)" >&2
    die "release 拒否 — リースの owner ($holder) ではない"
  fi
  rm -f "$LEASE_FILE"
  echo "released."
}

env_get() {
  # .env から key の値を取る (最後の定義が勝ち)。secrets は呼び出し側で
  # 出力しないこと。
  /usr/bin/grep "^$1=" "$ENV_FILE" 2>/dev/null | tail -1 | cut -d= -f2- || true
}

find_pids() {
  # この clone を cwd とする c_lord.main プロセスだけを列挙する。
  #
  # 手順: pgrep -f で「c_lord.main を含むプロセス」を候補に絞り (高速)、
  # 各候補の /proc/<pid>/cwd が $CLONE_DIR と一致するものだけ採用する。
  # pgrep を「候補抽出」だけに使い、最終判定は必ず cwd 照合で行うので、
  # かつて pgrep 直 kill を避けた理由 (自己マッチ・他 clone への誤爆) は
  # cwd 照合がそのまま吸収する。staging.sh は常に `python -m c_lord.main`
  # で起動するため -f で確実に拾える (相対パス起動の取り逃しも無い)。
  #
  # timeout ガード (#383): WSL2 等で応答しないマウント上に cwd を持つ
  # プロセスがあると readlink /proc/<pid>/cwd が uninterruptible に
  # ブロックし、これを毎反復呼ぶ restart の待機ループごとハングする
  # (2026-06-11 に約11分のハングを観測)。readlink を 2 秒で打ち切り、
  # 1つの stuck pid が関数全体を巻き込まないようにする。pgrep 前段で
  # 候補が数個に絞られるので fork 回数も少ない。
  local pid cwd
  for pid in $(pgrep -f "c_lord\.main" 2>/dev/null); do
    cwd="$(timeout 2 readlink "/proc/$pid/cwd" 2>/dev/null)" || continue
    [ "$cwd" = "$CLONE_DIR" ] && echo "$pid"
  done
}

instance_leaders() {
  # find_pids のうち「親が同じ clone の c_lord.main プロセスでない」pid =
  # 論理インスタンスの代表 (プロセスツリーの根) だけを返す (#437)。
  #
  # なぜ必要か: `uv run python -m c_lord.main` は uv ラッパ(親)+python(子) の
  # 2 プロセスになり、どちらの cmdline にも c_lord.main が入るので pgrep は
  # 両方に当たる。これは「正常な 1 インスタンス」であって二重起動ではない
  # (prod は systemd → uv run … でこの形。docs/STAGING.md
  # 参照)。子 (親が同 clone の matched pid である pid) を除けば、本当に独立
  # した起動だけが代表として残り、parent+child は 1 と数えられる。
  local pids pid ppid
  pids="$(find_pids)"
  [ -z "$pids" ] && return 0
  for pid in $pids; do
    ppid="$(ps -o ppid= -p "$pid" 2>/dev/null | tr -d ' ')"
    # 親が matched 集合に居れば、この pid は子 → 代表ではない (出力しない)。
    printf '%s\n' "$pids" | /usr/bin/grep -qx "$ppid" || echo "$pid"
  done
}

count_instances() {
  # 論理インスタンス数 (parent+child を 1 と数える)。0 なら 0 を返す。
  instance_leaders | /usr/bin/grep -c . || true
}

# ---- systemd 管理下の clone (本番) — ops#2 ------------------------------------
# 本番を起動する入口は systemd の unit 1 つだけにする。以前は staging.sh restart が
# 本番の bot を kill → setsid で自前起動していたため、systemd が「落ちた」と判断して
# 立て直しを試み、単一インスタンスロック (#212/#325) に弾かれて 6 回失敗 →
# `Start request repeated too quickly` で諦め、本番が監視外で動き続けた (4 か月で
# 少なくとも 3 回)。
#
# 「本番か」は unit ファイルの WorkingDirectory= がこの clone と一致するかで決める。
# ファイルを読むだけなので、systemctl --user が bus に繋がらないときでも判定できる
# (その場合は kill に落ちずエラーで止まる — それが肝)。
SYSTEMD_UNIT_DIR="${XDG_CONFIG_HOME:-$HOME/.config}/systemd/user"
CLONE_DIR_REAL="$(pwd -P)"
SYSTEMD_WAIT="${CLORD_SYSTEMD_WAIT_SECONDS:-90}"
case "$SYSTEMD_WAIT" in '' | *[!0-9]*) SYSTEMD_WAIT=90 ;; esac

unit_working_dir() {
  # unit_working_dir <unit file> — drop-in (<unit>.d/*.conf) を含めた最後の定義
  local f="$1"
  cat "$f" "$f.d"/*.conf 2>/dev/null |
    sed -n 's/^[[:space:]]*WorkingDirectory=[[:space:]]*//p' | tail -1 |
    sed -e 's/^-//' -e "s|%h|$HOME|g" -e 's/[[:space:]]*$//'
}

find_managed_unit() {
  # この clone を WorkingDirectory にしている user unit の名前 (無ければ空)
  local f wd
  for f in "$SYSTEMD_UNIT_DIR"/*.service; do
    [ -f "$f" ] || continue
    case "$(basename "$f")" in *@.service) continue ;; esac # テンプレートは対象外
    wd="$(unit_working_dir "$f")"
    [ -n "$wd" ] || continue
    wd="$(cd "$wd" 2>/dev/null && pwd -P)" || continue
    if [ "$wd" = "$CLONE_DIR_REAL" ]; then
      basename "$f"
      return 0
    fi
  done
}

unit_prop() {
  # unit_prop <PROP> — 失敗 (bus 不通) は空文字 + 非 0
  systemctl --user show -p "$1" --value "$UNIT" 2>/dev/null
}

systemctl_usable() {
  systemctl --user show -p ActiveState --value "$UNIT" >/dev/null 2>&1
}

pid_cgroup() {
  sed -n 's/^0:://p' "/proc/$1/cgroup" 2>/dev/null
}

is_supervised_pid() {
  # is_supervised_pid <pid> <unit の ControlGroup> — pid がその cgroup の中か。
  # 「親が systemd --user か」では判定しない: setsid で立った孤児も親は
  # systemd --user に付け替わるので、監視外でも親だけ見ると監視下に見える。
  local cg="$2" pcg
  [ -n "$cg" ] || return 1
  pcg="$(pid_cgroup "$1")"
  case "$pcg" in "$cg" | "$cg"/*) return 0 ;; esac
  return 1
}

orphan_pids() {
  # この clone の bot のうち unit の cgroup の外に居るもの (= 監視外)
  local cg p
  cg="$(unit_prop ControlGroup)"
  for p in $(find_pids); do
    is_supervised_pid "$p" "$cg" || echo "$p"
  done
}

managed_status() {
  # status の supervisor 部。異常 (unit が active でない / 監視外の bot) なら 2 を返す
  local rc=0 active sub mainpid nrestarts cg frag p pids
  if ! systemctl_usable; then
    echo "supervisor: systemd $UNIT — systemctl --user に繋がらない (状態を確認できない)"
    return 2
  fi
  active="$(unit_prop ActiveState)"
  sub="$(unit_prop SubState)"
  mainpid="$(unit_prop MainPID)"
  nrestarts="$(unit_prop NRestarts)"
  cg="$(unit_prop ControlGroup)"
  echo "supervisor: systemd $UNIT — $active ($sub) MainPID=$mainpid NRestarts=$nrestarts"
  echo "log:        journalctl --user -u $UNIT"
  pids="$(find_pids)"
  for p in $pids; do
    if is_supervised_pid "$p" "$cg"; then
      echo "  pid $p: 監視下 ($UNIT)"
    else
      echo "  pid $p: 監視外 (cgroup=$(pid_cgroup "$p")) — systemd は落ちても立て直さない"
      rc=2
    fi
  done
  if [ "$active" != "active" ]; then
    echo "WARNING: $UNIT が $active。bash scripts/staging.sh restart で systemd の下に戻す。"
    rc=2
  fi
  [ "$rc" = 2 ] && [ -n "$pids" ] &&
    echo "WARNING: 監視外の bot がいる。bash scripts/staging.sh restart で止めて systemd に渡す。"
  # unit が repo の deploy/ と一致しているか (本番の構成を repo の外に置かない)
  frag="$(unit_prop FragmentPath)"
  if [ -f "$CLONE_DIR/deploy/c-lord.service" ] && [ -n "$frag" ] &&
    ! cmp -s "$frag" "$CLONE_DIR/deploy/c-lord.service"; then
    echo "WARNING: $frag が repo の deploy/c-lord.service と違う。bash scripts/install-systemd.sh で揃える。"
  fi
  return "$rc"
}

managed_stop() {
  systemctl_usable ||
    die "systemctl --user が使えない — 本番 ($UNIT) の bot は kill していない。bus を直してから再実行してください (docs/STAGING.md)。"
  systemctl --user stop "$UNIT" || die "systemctl --user stop $UNIT に失敗"
  if [ -n "$(orphan_pids)" ]; then
    echo "WARNING: 監視外の bot が残っている — PID 直指定で止める"
    cmd_stop orphan_pids
  fi
  echo "stopped (systemd: $UNIT)。再開は bash scripts/staging.sh restart"
}

managed_restart() {
  # kill → 自前起動はしない。systemd に restart させ、監視下で上がったことを確かめる。
  systemctl_usable ||
    die "systemctl --user が使えない — 本番 ($UNIT) の bot は kill していない。bus を直してから再実行してください (docs/STAGING.md)。"
  if [ -n "$(orphan_pids)" ]; then
    # 放っておくと systemd の起動が単一インスタンスロックに弾かれて failed に戻る
    echo "WARNING: 監視外の bot がいる (pid $(orphan_pids | tr '\n' ' ')) — 止めてから systemd に渡す"
    cmd_stop orphan_pids
  fi
  local since log waited=0 r0 active mainpid
  since="$(date +%s)"
  # 立て直しに諦めた後 (start-limit-hit) でも起動できるように、失敗カウンタを戻す
  systemctl --user reset-failed "$UNIT" 2>/dev/null || true
  echo "systemctl --user restart $UNIT"
  systemctl --user restart "$UNIT" || die "systemctl --user restart $UNIT に失敗 (journalctl --user -u $UNIT)"
  r0="$(unit_prop NRestarts)"
  log="$(mktemp)"
  while [ $waited -lt "$SYSTEMD_WAIT" ]; do
    journalctl --user -u "$UNIT" --since "@$since" -o cat --no-pager >"$log" 2>/dev/null || true
    if /usr/bin/grep -q "IDENTITY MISMATCH" "$log"; then
      /usr/bin/grep -E "Logged in as|IDENTITY MISMATCH" "$log" | tail -2
      rm -f "$log"
      die "identity mismatch — 誤った bot として起動しようとした (#323 ガード作動)"
    fi
    active="$(unit_prop ActiveState)"
    if [ "$active" = "failed" ]; then
      tail -5 "$log"
      rm -f "$log"
      die "$UNIT が failed になった (journalctl --user -u $UNIT)"
    fi
    if /usr/bin/grep -q "Logged in as" "$log"; then
      /usr/bin/grep -E "Logged in as" "$log" | tail -1
      if ! check_log_identity "$log"; then
        rm -f "$log"
        systemctl --user stop "$UNIT" || true
        die "誤った identity で起動したため $UNIT を停止した"
      fi
      rm -f "$log"
      mainpid="$(unit_prop MainPID)"
      [ "$active" = "active" ] && [ "${mainpid:-0}" != "0" ] ||
        die "$UNIT が active でない (ActiveState=$active MainPID=$mainpid)"
      [ "$(unit_prop NRestarts)" = "$r0" ] ||
        die "$UNIT が起動中に落ちて立て直された (NRestarts $r0 -> $(unit_prop NRestarts))"
      [ -z "$(orphan_pids)" ] ||
        die "監視外の bot が残っている (pid $(orphan_pids | tr '\n' ' '))"
      echo "supervisor: $UNIT active MainPID=$mainpid"
      echo "OK"
      return 0
    fi
    sleep 2
    waited=$((waited + 2))
  done
  tail -5 "$log"
  rm -f "$log"
  die "${SYSTEMD_WAIT} 秒以内に 'Logged in as' が出ない (journalctl --user -u $UNIT)"
}

cmd_status() {
  local pids count branch expected channel
  pids="$(find_pids)"
  count="$(count_instances)" # 論理インスタンス数 (uv ラッパ+子 = 1, #437)
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
  [ -n "$UNIT" ] && {
    managed_status
    return $?
  }
  return 0
}

cmd_stop() {
  # cmd_stop [<pid lister>] — 既定は find_pids (この clone の bot 全部)。
  # 本番の監視外 bot だけを止めるときは orphan_pids を渡す。
  local lister="${1:-find_pids}" pids p waited
  pids="$($lister)"
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
  local grace="${CLORD_STOP_GRACE_SECONDS:-15}"
  case "$grace" in '' | *[!0-9]*) grace=15 ;; esac
  waited=0
  while [ $waited -lt "$grace" ]; do
    sleep 1
    waited=$((waited + 1))
    pids="$($lister)"
    [ -z "$pids" ] && {
      echo "stopped."
      return 0
    }
  done

  # SIGTERM で止まりきらない (#699)。以前はここで die していたため restart が
  # 起動まで到達せず、本番が「止まったまま」人手待ちになった。bot 側にも
  # shutdown watchdog (c_lord/shutdown_watchdog.py, 既定 10 秒) があるので
  # ここに来るのは古いコード or watchdog 自体が詰まったとき。
  # kill は引き続き PID 直指定のみ。find_pids を取り直す = /proc/<pid>/cwd の
  # 照合を SIGKILL の直前にもう一度通す (その間に pid が再利用されていても
  # 別プロセスを撃たない)。エスカレーションした事実は bot のログにも残す。
  local msg
  pids="$($lister)"
  for p in $pids; do
    msg="staging.sh: pid $p が SIGTERM から ${grace} 秒で終了しないため SIGKILL します (#699)"
    echo "WARNING: $msg" >&2
    [ -e "$LOG_LINK" ] && echo "$(date '+%Y-%m-%d %H:%M:%S') [WARNING] $msg" >>"$LOG_LINK"
    kill -KILL "$p" 2>/dev/null || true
  done
  waited=0
  while [ $waited -lt 5 ]; do
    pids="$($lister)"
    [ -z "$pids" ] && {
      echo "stopped (SIGKILL)."
      return 0
    }
    sleep 1
    waited=$((waited + 1))
  done
  die "SIGKILL 後もプロセスが残っている (残: $pids)。手動確認してください。"
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

  # ブランチ同期は venv チェックより前に行う (#436)。理由 2 つ:
  #  1) 単なる `git checkout <branch>` はローカルブランチを古い HEAD のまま
  #     切り替えるだけで、fetch 済みでも origin に追従しない。検証者は
  #     「最新の fix を回したつもりで古いコード」を起動し偽 RED/GREEN を得る
  #     (#399 検証中に実害: d09fd57 を push・fetch 済みなのに 1 つ前の
  #     47c3f02 を起動していた)。fetch → checkout → origin/<branch> へ
  #     fast-forward まで行って初めて「最新を回している」と言える。
  #  2) launch 前に確実に同期させ、回しているコミットを起動ログに出すため。
  # ff 不能 (ローカルが分岐) なら黙って古いコードを起動せず明示エラーで止める。
  if [ -n "$branch_arg" ]; then
    git -C "$CLONE_DIR" fetch origin "$branch_arg" -q \
      || die "git fetch origin '$branch_arg' に失敗 (ブランチ名 / ネットワークを確認)。restart は origin に push 済みのブランチを対象にする。"
    git -C "$CLONE_DIR" checkout "$branch_arg" -q \
      || die "branch '$branch_arg' に checkout できない"
    if ! git -C "$CLONE_DIR" merge --ff-only "origin/$branch_arg" -q; then
      die "branch '$branch_arg' を origin/$branch_arg に fast-forward できない (local=$(git -C "$CLONE_DIR" rev-parse --short HEAD) origin=$(git -C "$CLONE_DIR" rev-parse --short "origin/$branch_arg" 2>/dev/null))。ローカルに origin へ無いコミットがある — 'git -C $CLONE_DIR reset --hard origin/$branch_arg' で破棄するか push してから再実行。"
    fi
  fi
  # 起動するコミットを必ず明示する (#436 AC: 検証者が回している HEAD を一目で)。
  local head_branch head_sha
  head_branch="$(git -C "$CLONE_DIR" rev-parse --abbrev-ref HEAD 2>/dev/null || true)"
  head_sha="$(git -C "$CLONE_DIR" rev-parse --short HEAD 2>/dev/null || true)"
  [ -n "$head_sha" ] && echo "checked out $head_branch @ $head_sha"

  if [ -n "$UNIT" ]; then
    managed_restart
    return $?
  fi

  [ -x "$VENV_PY" ] || die "no .venv in $CLONE_DIR ($VENV_PY がない)。uv sync --dev を先に実行。"

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
  #
  # `exec` とサブシェル全体へのリダイレクトは外さない (#401)。旧形
  # `(cd … && setsid … nohup py >log 2>&1 &)` では `&` が作るサブシェル
  # (cmdline は "bash scripts/staging.sh restart" のまま) が bot の親として
  # bot の寿命いっぱい wait し、リダイレクトが nohup にしか掛からないため
  # **呼び出し元の stdout/stderr を握り続けた**。本体は OK まで出して終わる
  # のに、`$(…)` やパイプで待つ呼び出し元には EOF が来ない = restart が
  # return しない。exec でサブシェル自体を bot に置き換え、fd も丸ごと
  # ログへ向けることで、呼び出し元の fd を持つプロセスが残らない。
  (cd "$CLONE_DIR" && exec setsid env \
    -u DISCORD_BOT_TOKEN -u DISCORD_CHANNEL_ID -u DISCORD_OWNER_ID \
    -u CLAUDE_COMMAND -u CLAUDE_MODEL -u CLAUDE_PERMISSION_MODE -u CLAUDE_WORKING_DIR \
    -u SESSION_DIR_BASE -u SESSION_SOURCE_REPO -u SESSION_TIMEOUT_SECONDS \
    -u MAX_CONCURRENT_SESSIONS -u CLORD_TMUX_ENABLED -u CLORD_API_PORT -u CLORD_API_URL \
    -u CLORD_API_HOST -u CLORD_API_SECRET -u CLORD_BRIDGE_MODE -u CLORD_RENDER_TABLE_IMAGES \
    -u CLORD_MIRROR_VERBOSITY -u CLORD_TRUSTED_BOT_IDS -u CLORD_ALLOWED_ROLE \
    -u COORDINATION_CHANNEL_ID -u EXPECTED_BOT_USER_ID \
    -u E2E_TEST_THREAD_ID -u E2E_TEST_WEBHOOK_URL -u VIRTUAL_ENV \
    nohup "$VENV_PY" -m c_lord.main) >"$log" 2>&1 </dev/null &
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
      # 論理インスタンスが 1 に収束するのを待つ (#437)。parent+child(uv ラッパ
      # +python) は 1 と数える。staging は venv python 直起動で即 1 になるが、
      # 万一 churn しても収束を待ってから単一を保証する。
      local inst tries=0
      while :; do
        inst="$(count_instances)"
        [ "$inst" = "1" ] && break
        tries=$((tries + 1))
        if [ "$tries" -ge 5 ]; then
          echo "instances: $inst"
          die "単一インスタンスに収束しない (instances=$inst)"
        fi
        sleep 1
      done
      echo "instances: $inst"
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

COMMAND="${1:-}"
[ $# -gt 0 ] && shift

UNIT="$(find_managed_unit)" # 空 = staging (従来どおり) / 非空 = 本番 (systemd に委ねる)

# 共通フラグ解析: --owner / --purpose / --ttl-hours、残り 1 つは branch
OWNER="${CLORD_LEASE_OWNER:-}"
PURPOSE=""
TTL_HOURS="2"
BRANCH=""
POSITIONAL=""
while [ $# -gt 0 ]; do
  case "$1" in
  --owner)
    OWNER="${2:?--owner needs a value}"
    shift 2
    ;;
  --purpose)
    PURPOSE="${2:?--purpose needs a value}"
    shift 2
    ;;
  --ttl-hours)
    TTL_HOURS="${2:?--ttl-hours needs a value}"
    shift 2
    ;;
  *)
    POSITIONAL="$1"
    shift
    ;;
  esac
done
BRANCH="$POSITIONAL"

case "$COMMAND" in
status)
  # cmd_status の終了コード (二重起動疑い = 2) を後続の lease 出力で潰さず
  # スクリプトの終了コードに伝播させる (#437): 機械からは exit code で単一性を
  # 判定できる必要がある。
  cmd_status
  status_rc=$?
  if [ -f "$LEASE_FILE" ]; then
    if lease_is_valid; then
      echo "lease:     $(lease_describe)"
    else
      echo "lease:     EXPIRED — $(lease_describe)"
    fi
  else
    echo "lease:     (none)"
  fi
  exit "$status_rc"
  ;;
borrow) cmd_borrow "$OWNER" "$PURPOSE" "$TTL_HOURS" ;;
release) cmd_release "$OWNER" ;;
stop)
  # 本番はリースの対象外 (借りるものではない)。systemd に委ねる。
  if [ -n "$UNIT" ]; then
    managed_stop
    exit $?
  fi
  # リース無しの stop は許可 (掃除目的)。他人の有効リース中のみ拒否。
  if lease_is_valid; then
    holder="$(lease_field owner)"
    if [ -z "$OWNER" ] || [ "$holder" != "$OWNER" ]; then
      echo "occupied: $(lease_describe)" >&2
      die "stop 拒否 — 他セッション ($holder) の有効リース中 (検証中の bot を殺さない)"
    fi
  fi
  cmd_stop
  ;;
restart)
  [ -n "$UNIT" ] || lease_guard "$OWNER" # staging は有効な自リース必須 (#328)。本番は対象外
  cmd_restart "$BRANCH"
  ;;
check-log)
  # テスト用の隠しサブコマンド: ログファイルの identity を .env と照合
  check_log_identity "${BRANCH:?usage: check-log <logfile>}"
  ;;
*) usage ;;
esac
