"""「このペインは、いまターンの途中か」— 1つの判定を1箇所に置く (#742).

**再起動を生き延びる唯一の根拠**がここ。c-lord が持っている「走っている」の台帳
(:attr:`ClaudeChatCog._active_tasks`) は in-memory なので再起動で空になるが、
tmux の中の ``claude`` は再起動を生き延びる (#503)。**残っているのはペインの
ほうなので、ペインに聞く。** Claude Code 自身が描いている working spinner は、
c-lord が生きていようが死んでいようが刻み続ける。

判定は spinner の**経過タイマー** ``(<elapsed> · …)`` を見る（#190 で
:mod:`c_lord.thread_state_sync` が辿り着いた形）::

    ✢ Swirling… (2m 29s · ↓ 9.3k tokens)
    ✻ Running… (12s · esc to interrupt)

終わったターンは ``✻ Baked for 3m 9s`` のように**括弧ごと畳まれる**ので、
スクロールバックに残った完了 spinner を「走っている」と読み違えない。glyph
そのものを見ないので、Claude Code が回す文字 (``✢ ✻ ✶ ·`` …) が増えても効く。

**glyph だけを見るのは「広めで安全」ではない。** ``✻ Baked for 3m 9s`` は
ターンが**終わった**ペインの通常の姿で、入力ボックスのすぐ上に残り続ける。glyph
の有無で判定すると待機中のペインがほぼ全部「走っている」になり、常駐上限は
**1本も眠らせられなくなる** — ブレーキを無効化したのと同じことになる。だから
ここは #190 が辿り着いた「括弧のタイマー」だけを見る。

:mod:`c_lord.thread_state_sync` のランプはこの上に**glyph のフォールバックを
足している**。ランプが spinner の隙間で 🟡 に戻るのは見た目が鬱陶しいだけで
済むが、眠らせる/眠らせないの判断でそれをやると上限が死ぬ — 許容できる誤りの
向きが違うので、足し引きは呼び手側に置く。
"""

from __future__ import annotations

import re

__all__ = ["pane_shows_running"]

# The live working spinner's "(<elapsed> · …)" timer. It exists only while
# Claude is actively generating or executing; a completed turn collapses to
# "<char> <Word> for <N>s" with no parenthetical (#190).
_RUNNING_SPINNER_RE = re.compile(r"\((?:\d+h\s*)?(?:\d+m\s*)?\d+s\s*·")

# How many bottom lines to scan for that timer. The spinner renders just above
# the input box, but the box + status footer (~8 lines) and any in-progress
# tool-result preview push it 10–20 lines off the bottom — so a narrow window
# misses it. Verified against live captures (#190).
_SPINNER_PROBE_LINES = 30


def pane_shows_running(pane_text: str) -> bool:
    """True when *pane_text* shows a turn in progress **right now**.

    The shared definition of "this pane is mid-turn": the thread lamp
    (:func:`c_lord.thread_state_sync._pane_lamp_state`) and the resident cap's
    eviction guard (:func:`c_lord.tmux.running_thread_ids`) both start here, so
    the lamp a user sees and the work c-lord refuses to evict cannot disagree
    about the evidence — only about how much tolerance to add on top.
    """
    if not pane_text:
        return False
    lines = pane_text.rstrip().splitlines()
    return any(_RUNNING_SPINNER_RE.search(line) for line in lines[-_SPINNER_PROBE_LINES:])
