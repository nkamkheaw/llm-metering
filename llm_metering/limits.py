"""Token buckets and the acceleration guard.

These mirror the provider's documented enforcement model, so the scheduler's
local view and the fake provider's authoritative view share identical
mechanics:

  "The API uses the token bucket algorithm to do rate limiting. This means
   that your capacity is continuously replenished up to your maximum limit,
   rather than being reset at fixed intervals."
  -- https://platform.claude.com/docs/en/api/rate-limits

Limits are quoted per minute (RPM/ITPM/OTPM); a bucket refills at limit/60
per second and caps at `limit`.
"""

from __future__ import annotations

import math
from collections import deque
from dataclasses import dataclass, field

# The three things the provider meters separately. Telling them apart is the
# entire point of the exercise.
REQUESTS = "requests"
INPUT_TOKENS = "input_tokens"
OUTPUT_TOKENS = "output_tokens"
LIMITERS = (REQUESTS, INPUT_TOKENS, OUTPUT_TOKENS)


@dataclass
class TokenBucket:
    """Continuously-replenished bucket. `limit` is the per-minute ceiling."""

    limit: float
    level: float | None = None
    updated_at: float = 0.0

    def __post_init__(self) -> None:
        if self.level is None:
            self.level = float(self.limit)

    @property
    def refill_per_sec(self) -> float:
        return self.limit / 60.0

    def _advance(self, now: float) -> None:
        if now > self.updated_at:
            self.level = min(
                float(self.limit),
                self.level + (now - self.updated_at) * self.refill_per_sec,
            )
            self.updated_at = now

    def remaining(self, now: float) -> float:
        self._advance(now)
        return max(0.0, self.level)

    def headroom(self, now: float) -> float:
        """1.0 = untouched, 0.0 = empty. What the dashboard should plot."""
        if self.limit <= 0:
            return 1.0
        return max(0.0, self.remaining(now) / self.limit)

    def try_take(self, now: float, n: float) -> bool:
        self._advance(now)
        if self.level >= n:
            self.level -= n
            return True
        return False

    def force_take(self, now: float, n: float) -> None:
        """Debit unconditionally, allowing the level to go negative.

        Used for reconciliation: the provider estimates input tokens at request
        start and adjusts to the real count during the request, so the true
        cost can exceed what was reserved. Those tokens are already spent.
        """
        self._advance(now)
        self.level -= n

    def time_until(self, now: float, n: float) -> float:
        """Seconds until `n` units are available. 0.0 if available now."""
        self._advance(now)
        if self.level >= n:
            return 0.0
        if self.refill_per_sec <= 0:
            return math.inf
        return (n - self.level) / self.refill_per_sec


@dataclass
class AccelerationGuard:
    """Rate-of-change limit, distinct from any level limit.

    The docs describe a 429 cause that is not about how much you are using but
    how fast that changed:

      "You might also encounter 429 errors because of acceleration limits on
       the API if your organization has a sharp increase in usage. To avoid
       hitting acceleration limits, ramp up your traffic gradually and
       maintain consistent usage patterns."
      -- https://platform.claude.com/docs/en/api/rate-limits

    Modelled as an EWMA baseline of admitted request rate, with rejection when
    the instantaneous rate exceeds `factor` x baseline. The baseline adapts, so
    sustained load is eventually accepted. That adaptation is what gives this
    candidate its distinctive signature: latency spikes at the *onset* of a
    ramp and decays even while load stays high.

    The provider's real parameters are not published. `factor` and `tau` are
    swept, never asserted.
    """

    factor: float = 3.0
    tau: float = 45.0        # baseline growth time constant, seconds
    floor_rate: float = 2.0  # req/s always permitted, regardless of history
    window: float = 1.0      # instantaneous-rate measurement window
    enabled: bool = False

    baseline: float = 0.0
    _events: deque[float] = field(default_factory=deque, repr=False)
    _last_update: float = 0.0

    def _trim(self, now: float) -> None:
        cutoff = now - self.window
        while self._events and self._events[0] < cutoff:
            self._events.popleft()

    def current_rate(self, now: float) -> float:
        """Admitted requests per second over the trailing window."""
        self._trim(now)
        return len(self._events) / self.window

    def _age(self, now: float) -> None:
        """Decay the baseline toward the admitted rate.

        Runs on every query, not only on admissions. If the baseline only moved
        when a request was admitted, a fully-rejected ramp could never adapt and
        the guard would lock out permanently -- which contradicts the documented
        remedy of ramping up gradually until usage is accepted.
        """
        dt = now - self._last_update
        if dt <= 0:
            return
        alpha = 1.0 - math.exp(-dt / self.tau)
        self.baseline += alpha * (self.current_rate(now) - self.baseline)
        self._last_update = now

    def allowed_rate(self, now: float) -> float:
        """The ceiling right now. Grows as sustained traffic lifts the baseline."""
        self._age(now)
        return max(self.floor_rate, self.factor * self.baseline)

    def observe(self, now: float) -> None:
        self._age(now)
        self._events.append(now)

    def would_reject(self, now: float) -> bool:
        if not self.enabled:
            return False
        return self.current_rate(now) >= self.allowed_rate(now)
