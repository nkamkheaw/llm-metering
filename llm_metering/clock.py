"""Clock abstraction: the same scheduler logic runs on wall time or virtual time.

The scheduler never calls time.time() directly. It receives `now` as an
argument, so every decision function is pure with respect to time and the
simulator can drive it at whatever speed it likes.
"""

from __future__ import annotations

import heapq
import time
from typing import Callable, Protocol


class Clock(Protocol):
    def now(self) -> float:
        """Seconds since some fixed epoch. Monotonic within a run."""


class RealClock:
    __slots__ = ()

    def now(self) -> float:
        return time.monotonic()


class SimClock:
    """Virtual clock driven by an event heap.

    Determinism: events are ordered by (time, insertion_sequence), so two runs
    with the same seed and the same scheduling produce byte-identical output.
    Ties never resolve on object identity or dict ordering.
    """

    __slots__ = ("_now", "_heap", "_seq", "_stopped")

    def __init__(self) -> None:
        self._now = 0.0
        self._heap: list[tuple[float, int, Callable, tuple]] = []
        self._seq = 0
        self._stopped = False

    def now(self) -> float:
        return self._now

    def at(self, when: float, fn: Callable, *args) -> None:
        if when < self._now:
            when = self._now
        heapq.heappush(self._heap, (when, self._seq, fn, args))
        self._seq += 1

    def after(self, delay: float, fn: Callable, *args) -> None:
        self.at(self._now + max(0.0, delay), fn, *args)

    def stop(self) -> None:
        self._stopped = True

    def run(self, until: float | None = None) -> None:
        while self._heap and not self._stopped:
            when, _, fn, args = self._heap[0]
            if until is not None and when > until:
                self._now = until
                return
            heapq.heappop(self._heap)
            self._now = when
            fn(*args)
