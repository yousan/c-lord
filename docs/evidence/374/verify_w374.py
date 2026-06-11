"""Real-tmux verification for #374 (sort-on-add + -d detached).

Isolated throwaway sessions only — never touches the live clord / c-lord
sessions. Exercises the REAL TmuxSessionManager + real tmux (unit tests mock
the subprocess layer).
"""
from __future__ import annotations

import subprocess

from c_lord.tmux import TmuxSessionManager, parse_work_number

GREEN = "clord-verify-374-green"
RED = "clord-verify-374-red"


def wins(sess: str) -> list[str]:
    r = subprocess.run(
        ["tmux", "list-windows", "-t", sess, "-F", "#{window_index}:#{window_name}"],
        capture_output=True, text=True,
    )
    return [w for w in r.stdout.split() if w]


def active(sess: str) -> str:
    r = subprocess.run(
        ["tmux", "list-windows", "-t", sess, "-F", "#{window_active}:#{window_name}"],
        capture_output=True, text=True,
    )
    for line in r.stdout.splitlines():
        if line.startswith("1:"):
            return line.split(":", 1)[1]
    return ""


def numbered_order(sess: str) -> list[int]:
    nums = []
    for entry in wins(sess):
        name = entry.split(":", 1)[1]
        n = parse_work_number(name)
        if n is not None:
            nums.append(n)
    return nums


def red_demo() -> None:
    """Show the PROBLEM with raw tmux (no -d, no sort): a window created into a
    gap lands out of order AND steals the active window."""
    print("### RED — raw `tmux new-window` (no -d, no sort)")
    subprocess.run(["tmux", "kill-session", "-t", RED], capture_output=True)
    subprocess.run(["tmux", "new-session", "-d", "-s", RED, "-n", "base"], capture_output=True)
    subprocess.run(["tmux", "set-option", "-t", RED, "base-index", "1"], capture_output=True)
    for n in ("w1", "w2", "w3", "w4"):
        subprocess.run(["tmux", "new-window", "-t", RED, "-n", n], capture_output=True)
    subprocess.run(["tmux", "kill-window", "-t", f"{RED}:3"], capture_output=True)  # kill w2
    subprocess.run(["tmux", "select-window", "-t", f"{RED}:1"], capture_output=True)  # user on base
    subprocess.run(["tmux", "new-window", "-t", RED, "-n", "w5"], capture_output=True)  # no -d
    print(f"  windows : {wins(RED)}")
    print(f"  active  : {active(RED)}  (raw new-window stole focus → expected w5)")
    print(f"  numbered: {numbered_order(RED)}  (w5 landed in the gap, NOT ascending)")
    subprocess.run(["tmux", "kill-session", "-t", RED], capture_output=True)


def green_demo() -> None:
    """Show the FIX with the real TmuxSessionManager (-d + sort-on-add)."""
    print("### GREEN — real TmuxSessionManager (#374: -d + sort-on-add)")
    subprocess.run(["tmux", "kill-session", "-t", GREEN], capture_output=True)
    dirs = [f"/tmp/v374-{i}" for i in range(6)]
    for d in dirs:
        subprocess.run(["mkdir", "-p", d], capture_output=True)
    try:
        mgr = TmuxSessionManager(session_name=GREEN, mapping_path="")
        for i in range(1, 5):
            mgr.create_session(1_000_000_000_000 + i, dirs[i])
        print(f"  after 4 adds      : {wins(GREEN)}")
        # close one in the middle (free a low index → next add fills the gap)
        subprocess.run(["tmux", "kill-window", "-t", f"{GREEN}:w2"], capture_output=True)
        print(f"  after killing w2  : {wins(GREEN)}")
        # user navigates to the first window, then a NEW thread spawns
        subprocess.run(["tmux", "select-window", "-t", f"{GREEN}:w1"], capture_output=True)
        before_active = active(GREEN)
        new_name = mgr.create_session(1_000_000_000_005, dirs[5])
        print(f"  after new thread  : {wins(GREEN)}")
        nums = numbered_order(GREEN)
        print(f"  numbered order    : {nums}  (expect ascending [1,3,4,5])")
        print(f"  new window name   : {new_name}")
        print(f"  active before/after new thread: {before_active!r} -> {active(GREEN)!r} "
              f"(focus NOT stolen by -d)")
        assert nums == sorted(nums), f"not ascending: {nums}"
        assert nums == [1, 3, 4, 5], nums
        assert active(GREEN) == before_active, "focus was stolen (expected -d to prevent it)"
        print("  RESULT: PASS — sorted ascending after add, and focus kept.")
    finally:
        subprocess.run(["tmux", "kill-session", "-t", GREEN], capture_output=True)


if __name__ == "__main__":
    red_demo()
    print()
    green_demo()
