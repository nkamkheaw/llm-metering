"""Discrete-event simulation: workload -> scheduler -> client pool -> provider.

Deterministic by construction. Same scenario, seed and policy produce
byte-identical output, because the event heap breaks ties on insertion order
and every random draw comes from a seeded generator.
"""

from __future__ import annotations

import random
import statistics
from collections import deque
from dataclasses import dataclass, field

from ..clock import SimClock
from ..limits import INPUT_TOKENS, LIMITERS, OUTPUT_TOKENS, REQUESTS
from ..policy import (
    Action,
    PendingRequest,
    PolicyConfig,
    SchedulerState,
    charge,
    decide,
    reconcile,
)
from .provider import FakeProvider, ProviderConfig
from .workload import RunSpec, WorkloadConfig, generate

ERROR_RESPONSE_LATENCY = 0.05   # a 429/529 comes back fast


@dataclass
class ClientConfig:
    """Client-side concurrency. This is candidate 5's governing parameter:
    a pool too small for the offered concurrency queues requests locally, which
    looks exactly like provider throttling from the outside but leaves every
    provider limiter healthy."""

    pool_size: int | None = None    # None = effectively unlimited


@dataclass
class RetryConfig:
    max_attempts: int = 3           # SDK default is 2 retries on top of the try
    base_backoff: float = 0.5
    multiplier: float = 2.0
    max_backoff: float = 60.0
    honor_retry_after: bool = True
    jitter: float = 0.25
    retry_spend_cap: bool = True    # naive clients retry this; it never helps


@dataclass
class Record:
    request_id: str
    run_id: str
    turn: int
    enqueued_at: float
    finished_at: float = 0.0
    first_dispatch_at: float | None = None
    status: int = 0
    attempts: int = 0
    dropped: bool = False
    drop_reason: str = ""
    cache_hit: bool = False
    input_tokens: int = 0
    cache_creation: int = 0
    cache_read: int = 0
    output_tokens: int = 0
    binding: list[str] = field(default_factory=list)
    queue_wait: float = 0.0
    pool_wait: float = 0.0

    @property
    def latency(self) -> float:
        return self.finished_at - self.enqueued_at

    @property
    def ok(self) -> bool:
        return self.status == 200


class Simulation:
    def __init__(
        self,
        provider_cfg: ProviderConfig,
        workload_cfg: WorkloadConfig,
        policy_cfg: PolicyConfig,
        client_cfg: ClientConfig | None = None,
        retry_cfg: RetryConfig | None = None,
        runs: list[RunSpec] | None = None,
        seed: int = 11,
    ) -> None:
        self.clock = SimClock()
        self.provider = FakeProvider(provider_cfg)
        self.wcfg = workload_cfg
        self.pcfg = policy_cfg
        self.ccfg = client_cfg or ClientConfig()
        self.rcfg = retry_cfg or RetryConfig()
        self.rng = random.Random(seed)
        self.runs = runs if runs is not None else generate(workload_cfg)

        self.state = SchedulerState(
            rpm=provider_cfg.rpm,
            itpm=provider_cfg.itpm,
            otpm=provider_cfg.otpm,
            cfg=policy_cfg,
            cache_ttl=provider_cfg.cache_ttl,
        )
        self.queue: deque[PendingRequest] = deque()
        self.records: dict[str, Record] = {}
        self.pool_free = self.ccfg.pool_size if self.ccfg.pool_size is not None else 10**9
        self.pool_waiters: deque[PendingRequest] = deque()
        self.in_flight = 0
        self.samples: list[dict] = []
        self.header_log: list[dict] = []   # what production would persist
        self._pump_pending = False
        self._runs_by_id = {r.run_id: r for r in self.runs}
        self._predicted: dict[str, float] = {}

    # -- lifecycle --------------------------------------------------------

    def run(self, drain: float = 300.0) -> "Result":
        for r in self.runs:
            self.clock.at(r.start_at, self._start_run, r)
        self.clock.at(0.0, self._sample)
        self.clock.run(until=self.wcfg.duration + drain)
        return Result(self)

    def _sample(self) -> None:
        now = self.clock.now()
        h = self.provider.headroom(now)
        self.samples.append(
            {
                "t": now,
                "headroom_requests": h[REQUESTS],
                "headroom_input_tokens": h[INPUT_TOKENS],
                "headroom_output_tokens": h[OUTPUT_TOKENS],
                "in_flight": self.in_flight,
                "queue_depth": len(self.queue),
                "pool_waiting": len(self.pool_waiters),
            }
        )
        if now < self.wcfg.duration:
            self.clock.after(1.0, self._sample)

    def _start_run(self, run: RunSpec) -> None:
        self._issue(run, 0)

    def _issue(self, run: RunSpec, turn: int) -> None:
        now = self.clock.now()
        segments, uncached, tail = run.segments_for(turn)
        rid = f"{run.run_id}:{turn}"
        req = PendingRequest(
            request_id=rid,
            run_id=run.run_id,
            turn=turn,
            enqueued_at=now,
            segments=segments,
            uncached_prefix_tokens=uncached,
            tail_tokens=tail,
            expected_output=run.output_tokens[turn],
        )
        self.records[rid] = Record(
            request_id=rid, run_id=run.run_id, turn=turn, enqueued_at=now
        )
        self.queue.append(req)
        self._pump()

    # -- scheduling -------------------------------------------------------

    def _schedule_pump(self, delay: float) -> None:
        if not self._pump_pending:
            self._pump_pending = True
            self.clock.after(max(delay, 1e-4), self._wake_pump)

    def _wake_pump(self) -> None:
        self._pump_pending = False
        self._pump()

    def _pump(self) -> None:
        now = self.clock.now()
        while self.queue:
            req = self.queue[0]
            d = decide(now, req, self.state)
            if d.action is Action.SEND:
                self.queue.popleft()
                self._dispatch(req)
            elif d.action is Action.WAIT:
                self._schedule_pump(d.delay)
                return
            else:
                self.queue.popleft()
                rec = self.records[req.request_id]
                rec.dropped = True
                rec.drop_reason = d.reason
                rec.finished_at = now
                rec.attempts = req.attempts

    def _dispatch(self, req: PendingRequest) -> None:
        rec = self.records[req.request_id]
        if rec.first_dispatch_at is None:
            rec.first_dispatch_at = self.clock.now()
            rec.queue_wait = rec.first_dispatch_at - rec.enqueued_at
        if self.pool_free > 0:
            self.pool_free -= 1
            self._send(req)
        else:
            # No client connection available. Pure client-side queueing.
            req_wait_start = self.clock.now()
            self.pool_waiters.append((req, req_wait_start))  # type: ignore[arg-type]

    def _release_slot(self) -> None:
        if self.pool_waiters:
            req, waited_from = self.pool_waiters.popleft()  # type: ignore[misc]
            self.records[req.request_id].pool_wait += self.clock.now() - waited_from
            self._send(req)
        else:
            self.pool_free += 1

    def _send(self, req: PendingRequest) -> None:
        now = self.clock.now()
        req.attempts += 1
        self.in_flight += 1
        predicted = self.state.predicted_itpm_cost(now, req)
        self._predicted[req.request_id] = predicted
        charge(now, req, self.state)

        if req.segments is not None:
            out = self.provider.submit(
                now,
                segments=req.segments,
                tail_tokens=req.tail_tokens,
                expected_output=req.expected_output,
            )
        else:
            out = self.provider.submit(
                now,
                prefix_id=None,
                prefix_tokens=req.uncached_prefix_tokens,
                tail_tokens=req.tail_tokens,
                expected_output=req.expected_output,
            )

        self.state.observe_headers(now, out.headers)
        self.header_log.append(
            {
                "t": now,
                "status": out.status,
                "error_code": out.error_code,
                "retry_after": out.retry_after,
                "workspace_id": "wrkspc_sim",
                "headers": out.headers,
            }
        )
        est_ttft = max(out.ttft_at - now, 0.05)
        self.state.note_sent(now, req, est_ttft)

        if out.accepted:
            actual = out.usage.input_tokens + out.usage.cache_creation_input_tokens
            reconcile(now, self.state, predicted, actual)
            self.clock.at(out.ttft_at, self._note_readable, req, out.usage, out.ttft_at)
            self.clock.at(out.complete_at, self._on_complete, req, out)
        else:
            self.clock.after(ERROR_RESPONSE_LATENCY, self._on_error, req, out)

    def _note_readable(self, req, usage, ttft_at: float) -> None:
        """The cache entry this request wrote becomes visible to siblings."""
        self.state.note_response(self.clock.now(), req, usage, ttft_at)

    # -- completion -------------------------------------------------------

    def _on_complete(self, req: PendingRequest, out) -> None:
        now = self.clock.now()
        self.in_flight -= 1
        rec = self.records[req.request_id]
        rec.status = 200
        rec.finished_at = now
        rec.attempts = req.attempts
        rec.cache_hit = out.cache_hit
        rec.input_tokens = out.usage.input_tokens
        rec.cache_creation = out.usage.cache_creation_input_tokens
        rec.cache_read = out.usage.cache_read_input_tokens
        rec.output_tokens = out.usage.output_tokens
        self._release_slot()

        run = self._runs_by_id[req.run_id]
        if req.turn + 1 < run.turns:
            self.clock.after(self.wcfg.think_time, self._issue, run, req.turn + 1)
        self._pump()

    def _on_error(self, req: PendingRequest, out) -> None:
        now = self.clock.now()
        self.in_flight -= 1
        rec = self.records[req.request_id]
        rec.attempts = req.attempts
        rec.status = out.status
        if out.binding_limiter:
            rec.binding.append(out.binding_limiter)
        self._release_slot()

        retryable = out.status in (429, 529)
        if out.error_code == "enforced_spend_limit_reached" and not self.rcfg.retry_spend_cap:
            retryable = False

        if retryable and req.attempts < self.rcfg.max_attempts:
            backoff = self.rcfg.base_backoff * (self.rcfg.multiplier ** (req.attempts - 1))
            if self.rcfg.honor_retry_after and out.retry_after is not None:
                backoff = max(backoff, out.retry_after)
            backoff = min(backoff, self.rcfg.max_backoff)
            backoff *= 1.0 + self.rng.uniform(-self.rcfg.jitter, self.rcfg.jitter)
            self.clock.after(max(backoff, 0.0), self._requeue, req)
        else:
            rec.finished_at = now
        self._pump()

    def _requeue(self, req: PendingRequest) -> None:
        self.queue.append(req)
        self._pump()


class Result:
    """Aggregated outcome of one simulation run."""

    def __init__(self, sim: Simulation) -> None:
        self.sim = sim
        self.records = list(sim.records.values())
        self.samples = sim.samples
        self.provider = sim.provider

    def _spike_windows(self) -> list[tuple[float, float]]:
        c = self.sim.wcfg
        out = []
        n = int(c.duration // c.spike_period) + 1
        span = c.spike_duty * c.spike_period
        for i in range(n):
            start = i * c.spike_period + (c.spike_period - span)
            out.append((start, start + span))
        return out

    def _in_spike(self, t: float) -> bool:
        return any(a <= t <= b for a, b in self._spike_windows())

    def summary(self) -> dict:
        done = [r for r in self.records if r.finished_at > 0]
        ok = [r for r in done if r.ok]
        lat = sorted(r.latency for r in ok)
        peak = sorted(r.latency for r in ok if self._in_spike(r.enqueued_at))
        attempts = sum(r.attempts for r in done)
        cr = sum(r.cache_read for r in ok)
        cc = sum(r.cache_creation for r in ok)
        it = sum(r.input_tokens for r in ok)
        minutes = self.sim.wcfg.duration / 60.0

        by_cause: dict[str, int] = {}
        for r in done:
            for b in r.binding:
                by_cause[b] = by_cause.get(b, 0) + 1

        return {
            "requests_completed": len(ok),
            "requests_failed": len([r for r in done if not r.ok and not r.dropped]),
            "requests_dropped": len([r for r in done if r.dropped]),
            "p50_latency": _pct(lat, 0.50),
            "p95_latency": _pct(lat, 0.95),
            "p99_latency": _pct(lat, 0.99),
            "p99_latency_peak": _pct(peak, 0.99),
            # Guards against a run too short to contain a spike window: with no
            # peak requests, p99_latency_peak is 0.0 by construction, not by
            # measurement. Callers must check this before trusting it.
            "peak_requests": len(peak),
            "max_latency": lat[-1] if lat else 0.0,
            "attempts": attempts,
            "retry_amplification": attempts / len(ok) if ok else float("inf"),
            "throttle_by_cause": by_cause,
            "cache_hit_rate": cr / (cr + cc + it) if (cr + cc + it) else 0.0,
            "effective_itpm": (it + cc) / minutes,
            "observed_rpm": len(ok) / minutes,
            "observed_otpm": sum(r.output_tokens for r in ok) / minutes,
            "min_headroom": {
                k: min((s[f"headroom_{k}"] for s in self.samples), default=1.0)
                for k in LIMITERS
            },
            "max_in_flight": max((s["in_flight"] for s in self.samples), default=0),
            "provider_rejections": dict(self.provider.rejections),
        }


def _pct(sorted_vals: list[float], q: float) -> float:
    if not sorted_vals:
        return 0.0
    i = min(len(sorted_vals) - 1, int(q * len(sorted_vals)))
    return sorted_vals[i]
