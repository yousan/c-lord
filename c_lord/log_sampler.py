"""一定時間に 1 回だけ INFO に出す — 黙って捨てたことを、うるさくせず残す (#678).

捨てる判断そのものは正しいことがある。#556 の webhook がそれで、`sessions` 行の無い
スレッドに監視 webhook が流し込むたびに ⚠️ と案内文を返したら、インシデント中に唯一
読めるはずのスレッドが埋まる。だから Discord には何も返さない。

**問題は、その事実がログにも残らなかったこと。** chatty な webhook でログが溢れるのを
避けて DEBUG にしたため、通常運用の INFO には 1 行も出ず、2026-09-02 には
`grep <thread_id> <bot ログ>` が 1 行も返さない状態から「bot が落ちたのか / webhook が
壊れたのか / スレッドが対象外なのか」を判別できなかった (#678、#585 の具体例)。

ここが両方を成立させる:

* **1 回きりの切り分けは必ず INFO に出る** — 最初の 1 通は常に emit される
* **連続で届いてもログは溢れない** — 同じキーについては `window` に 1 回だけ
* **黙らせた分は次の 1 行が数で語る** — `suppressed` / :attr:`Sample.suffix`

キーは「何を 1 本と数えるか」（c-lord では thread_id）。スレッド単位なので、1 本の
chatty なスレッドが他のスレッドの切り分けを潰すことはない。
"""

from __future__ import annotations

import time
from collections.abc import Callable, Hashable
from dataclasses import dataclass

#: 既定の窓 — 10 分。1 回きりの probe を救うには十分短く、アラートの連投を
#: INFO に通さないには十分長い。
DEFAULT_WINDOW = 600.0

#: 追跡するキーの上限。アラートごとにスレッドが増える運用でも辞書が育たないように。
DEFAULT_MAX_KEYS = 512


@dataclass(frozen=True)
class Sample:
    """1 件の発生に対する判定。"""

    #: True なら INFO で出す。False なら（従来どおり）DEBUG に落とす。
    emit: bool
    #: この 1 行が代表している、黙らせた件数（前回 emit 以降）。
    suppressed: int = 0
    #: 黙らせていた窓の長さ（秒）。ログ文言に添えるためだけに持つ。
    window: float = 0.0

    @property
    def suffix(self) -> str:
        """ログ行の末尾に足す `` (+N suppressed in the last Ms)``。

        黙らせた件数が 0 のときは空文字 — 静かなスレッドの 1 行に `` (+0 ...)`` が
        付くと、読み手が「何かを取りこぼした」と誤読する。
        """
        if not self.suppressed:
            return ""
        return f" (+{self.suppressed} suppressed in the last {int(self.window)}s)"


class LogSampler:
    """キーごとに「`window` に 1 回だけ emit」を判定する。

    プロセス内の状態のみ。再起動でリセットされるが、それは望ましい方向のリセット
    （再起動後の 1 通目は必ず INFO に出る）。
    """

    def __init__(
        self,
        window: float = DEFAULT_WINDOW,
        *,
        clock: Callable[[], float] = time.monotonic,
        max_keys: int = DEFAULT_MAX_KEYS,
    ) -> None:
        self._window = window
        self._clock = clock
        self._max_keys = max_keys
        self._last_emit: dict[Hashable, float] = {}
        self._suppressed: dict[Hashable, int] = {}

    def sample(self, key: Hashable) -> Sample:
        """`key` の発生を 1 件記録し、INFO に出すべきかを返す。"""
        now = self._clock()
        last = self._last_emit.get(key)
        if last is not None and now - last < self._window:
            self._suppressed[key] = self._suppressed.get(key, 0) + 1
            return Sample(emit=False)
        self._last_emit[key] = now
        suppressed = self._suppressed.pop(key, 0)
        self._prune(now)
        return Sample(emit=True, suppressed=suppressed, window=self._window)

    @property
    def tracked_keys(self) -> int:
        """いま覚えているキーの数。上限が効いていることをテストで見るために公開する。"""
        return len(self._last_emit)

    def _prune(self, now: float) -> None:
        """窓を過ぎたキーを捨てる。上限を超えていたら古い順に落とす。

        窓を過ぎたキーは、次に来たときどうせ emit されるので忘れて構わない（失うのは
        「その間に何件あったか」だけで、そのキーはもう黙っている）。
        """
        if len(self._last_emit) <= self._max_keys:
            return
        for key, emitted_at in list(self._last_emit.items()):
            if now - emitted_at >= self._window:
                del self._last_emit[key]
                self._suppressed.pop(key, None)
        while len(self._last_emit) > self._max_keys:
            oldest = next(iter(self._last_emit))  # dict は挿入順
            del self._last_emit[oldest]
            self._suppressed.pop(oldest, None)
