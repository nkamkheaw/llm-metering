"""Scheduler policies: send now, wait, or drop.

Pure with respect to time -- `now` is always passed in -- so the identical code
runs under the real clock in the proxy and under the virtual clock in the
simulator. That is what "same code, fake clock, fake network" means here: the
decision logic is shared; only the drivers differ.
"""

from __future__ import annotations

from dataclasses import dataclass, field
from enum import Enum

from .limits import (
    INPUT_TOKENS,
    OUTPUT_TOKENS,
    REQUESTS,
    AccelerationGuard,
    TokenBucket,
)


class Action(Enum):
    SEND = "send"
    WAIT = "wait"
    DROP = "drop"


@dataclass(frozen=True)
class Decision:
    action: Action
    delay: float = 0.0
    reason: str = ""


@dataclass
class PolicyConfig:
    name: str = "none"
    # Local token buckets shadowing the provider's, at a fraction of the real
    # ceilings so the scheduler throttles slightly before the provider does.
    admission: bool = False
    fraction: float = 0.9
    # Hold siblings sharing a novel cacheable prefix until the first writer
    # begins streaming, so they read the cache instead of all paying the write.
    cache_gate: bool = False
    cache_gate_timeout: float = 5.0
    # Cap the rate of change, not just the level.
    accel_guard: bool = False
    accel_factor: float = 2.0
    accel_tau: float = 45.0
    accel_floor_rate: float = 2.0
    # Give up rather than queue forever.
    max_wait: float = 120.0


@dataclass
class PendingRequest:
    """What the scheduler knows before it sends. All token figures are
    estimates -- the real counts only arrive in the response `usage`."""

    request_id: str
    run_id: str
    turn: int
    enqueued_at: float
    segments: list[tuple[str, int]] | None
    uncached_prefix_tokens: int
    tail_tokens: int
    expected_output: int
    attempts: int = 0

    @property
    def prefix_key(self) -> str | None:
        if not self.segments:
            return None
        return "|".join(s for s, _t in self.segments)

    @property
    def prefix_tokens(self) -> int:
        return sum(t for _s, t in self.segments) if self.segments else 0


@dataclass
class SchedulerState:
    """The scheduler's own view of the world.

    In production the ceilings come from the Rate Limits API at startup and are
    corrected continuously from `anthropic-ratelimit-*-remaining` response
    headers, so this view tracks the real buckets instead of drifting.
    """

    rpm: float
    itpm: float
    otpm: float
    cfg: PolicyConfig
    cache_ttl: float = 300.0

    buckets: dict[str, TokenBucket] = field(default_factory=dict)
    accel: AccelerationGuard = field(default_factory=AccelerationGuard)
    # prefix_key -> time the first writer is expected to become readable
    inflight_prefix: dict[str, float] = field(default_factory=dict)
    # prefix_key -> (readable_at, cached_tokens) as best the scheduler knows
    known_prefix: dict[str, tuple[float, int]] = field(default_factory=dict)

    def __post_init__(self) -> None:
        f = self.cfg.fraction
        self.buckets = {
            REQUESTS: TokenBucket(self.rpm * f),
            INPUT_TOKENS: TokenBucket(self.itpm * f),
            OUTPUT_TOKENS: TokenBucket(self.otpm * f),
        }
        self.accel = AccelerationGuard(
            factor=self.cfg.accel_factor,
            tau=self.cfg.accel_tau,
            floor_rate=self.cfg.accel_floor_rate,
            enabled=self.cfg.accel_guard,
        )

    def observe_headers(self, now: float, headers: dict[str, str]) -> None:
        """Correct the local view from the provider's own numbers.

        The remaining-token headers are rounded to the nearest thousand, so this
        tracks the real bucket coarsely -- good enough to stop drift, not
        precise enough to schedule on alone.
        """
        wire = {REQUESTS: "requests", INPUT_TOKENS: "input-tokens", OUTPUT_TOKENS: "output-tokens"}
        for key, name in wire.items():
            rem = headers.get(f"anthropic-ratelimit-{name}-remaining")
            lim = headers.get(f"anthropic-ratelimit-{name}-limit")
            if rem is None or lim is None:
                continue
            b = self.buckets[key]
            observed = float(rem) * self.cfg.fraction
            # Only ever correct downward: the provider knows about traffic this
            # scheduler did not send (other clients, other workspaces).
            b._advance(now)
            b.level = min(b.level, observed)

    def predicted_itpm_cost(self, now: float, req: PendingRequest) -> float:
        """Estimate what this request will charge to ITPM.

        Cache reads are exempt, so a predicted hit is dramatically cheaper --
        this prediction is what lets the scheduler admit far more traffic than a
        naive total-input-token estimate would.
        """
        if req.segments is None:
            return req.uncached_prefix_tokens + req.tail_tokens
        key = req.prefix_key
        known = self.known_prefix.get(key)
        if known is not None and known[0] <= now:
            cached_tokens = known[1]
            return max(0, req.prefix_tokens - cached_tokens) + req.tail_tokens
        return req.prefix_tokens + req.tail_tokens

    def note_sent(self, now: float, req: PendingRequest, expected_ttft: float) -> None:
        key = req.prefix_key
        if key is not None and key not in self.known_prefix:
            self.inflight_prefix.setdefault(key, now + expected_ttft)

    def note_response(self, now: float, req: PendingRequest, usage, ttft_at: float) -> None:
        key = req.prefix_key
        if key is None:
            return
        self.inflight_prefix.pop(key, None)
        cached = usage.cache_read_input_tokens + usage.cache_creation_input_tokens
        if cached:
            self.known_prefix[key] = (ttft_at, cached)


def decide(now: float, req: PendingRequest, st: SchedulerState) -> Decision:
    cfg = st.cfg

    # 1. Cache-aware admission. Keyed on the shared prompt PREFIX across
    #    different agents, not on agent identity -- a single agent's turns are
    #    already sequential, so there is nothing to serialise there.
    if cfg.cache_gate and req.segments:
        key = req.prefix_key
        if key not in st.known_prefix:
            readable_at = st.inflight_prefix.get(key)
            if readable_at is not None and readable_at > now:
                wait = min(readable_at - now, cfg.cache_gate_timeout)
                return Decision(Action.WAIT, wait, "cache_gate")

    # 2. Level limits.
    if cfg.admission:
        costs = (
            (REQUESTS, 1.0),
            (INPUT_TOKENS, st.predicted_itpm_cost(now, req)),
            (OUTPUT_TOKENS, float(req.expected_output)),
        )
        worst = 0.0
        worst_name = ""
        for name, cost in costs:
            wait = st.buckets[name].time_until(now, cost)
            if wait > worst:
                worst, worst_name = wait, name
        if worst > 0:
            if now - req.enqueued_at + worst > cfg.max_wait:
                return Decision(Action.DROP, 0.0, f"max_wait:{worst_name}")
            return Decision(Action.WAIT, worst, f"bucket:{worst_name}")

    # 3. Rate-of-change limit.
    if cfg.accel_guard and st.accel.would_reject(now):
        if now - req.enqueued_at > cfg.max_wait:
            return Decision(Action.DROP, 0.0, "max_wait:accel")
        return Decision(Action.WAIT, 0.05, "accel_guard")

    return Decision(Action.SEND, 0.0, "")


def charge(now: float, req: PendingRequest, st: SchedulerState) -> None:
    """Debit the local buckets for a request about to be sent."""
    if st.cfg.admission:
        st.buckets[REQUESTS].force_take(now, 1.0)
        st.buckets[INPUT_TOKENS].force_take(now, st.predicted_itpm_cost(now, req))
        st.buckets[OUTPUT_TOKENS].force_take(now, float(req.expected_output))
    if st.cfg.accel_guard:
        st.accel.observe(now)


def reconcile(now: float, st: SchedulerState, predicted: float, actual: float) -> None:
    """Correct the ITPM bucket once real usage is known.

    Mirrors the provider: "ITPM rate limits are estimated at the beginning of
    each request, and the estimate is adjusted during the request to reflect
    the actual number of input tokens used."
    """
    if st.cfg.admission:
        st.buckets[INPUT_TOKENS].force_take(now, actual - predicted)


# Named policies used by the sweep.
POLICIES = {
    "none": PolicyConfig(name="none"),
    "fifo_backoff": PolicyConfig(name="fifo_backoff", max_wait=120.0),
    "admission": PolicyConfig(name="admission", admission=True),
    "admission_cache": PolicyConfig(name="admission_cache", admission=True, cache_gate=True),
    "admission_cache_accel": PolicyConfig(
        name="admission_cache_accel", admission=True, cache_gate=True, accel_guard=True
    ),
}
