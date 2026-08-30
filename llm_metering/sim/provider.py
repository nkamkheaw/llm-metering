"""Fake provider implementing the documented rate-limit and caching semantics.

Every rule here is traceable to provider documentation, cited inline. If this
file is wrong, every conclusion drawn from the simulator is wrong -- so it is
covered by its own test suite (tests/test_provider_semantics.py) that asserts
each documented behaviour independently of any scenario.

Sources:
  rate limits    https://platform.claude.com/docs/en/api/rate-limits
  prompt caching https://platform.claude.com/docs/en/build-with-claude/prompt-caching
  errors         https://platform.claude.com/docs/en/api/errors
"""

from __future__ import annotations

import math
from dataclasses import dataclass, field

from ..limits import (
    INPUT_TOKENS,
    LIMITERS,
    OUTPUT_TOKENS,
    REQUESTS,
    AccelerationGuard,
    TokenBucket,
)

# --- Documented model constants -------------------------------------------
# Minimum cacheable prefix, in tokens. Below this the request is silently not
# cached -- no error, just cache_creation_input_tokens == 0.
# https://platform.claude.com/docs/en/build-with-claude/prompt-caching
MIN_CACHEABLE = {
    "claude-opus-5": 512,
    "claude-fable-5": 512,
    "claude-opus-4-8": 1024,
    "claude-sonnet-5": 1024,
    "claude-sonnet-4-6": 1024,
    "claude-opus-4-6": 4096,
    "claude-haiku-4-5": 4096,
}
DEFAULT_MIN_CACHEABLE = 1024

TTL_5M = 300.0
TTL_1H = 3600.0

# Cache reads are exempt from ITPM on every current model except Haiku 3.5.
# https://platform.claude.com/docs/en/api/rate-limits  ("Cache-aware ITPM")
CACHE_READ_COUNTS_TO_ITPM = {"claude-haiku-3-5"}


@dataclass
class ProviderConfig:
    model: str = "claude-opus-5"
    rpm: float = 4000
    itpm: float = 2_000_000
    otpm: float = 400_000

    cache_ttl: float = TTL_5M
    # Latency model. Prefill dominates time-to-first-token; cache reads skip
    # most of that work, which is why caching cuts latency as well as cost.
    ttft_base: float = 0.25
    prefill_sec_per_token: float = 5e-5        # ~20k uncached tokens/sec
    cached_prefill_sec_per_token: float = 5e-6  # ~10x faster on a cache read
    output_tokens_per_sec: float = 150.0

    # Acceleration limit (rate-of-change 429s). Off unless a scenario enables it.
    accel_factor: float = 3.0
    accel_tau: float = 45.0
    accel_floor_rate: float = 2.0
    accel_enabled: bool = False

    # Provider-side overload -> 529. Off unless a scenario enables it.
    server_concurrency: int | None = None

    # Monthly spend cap. When tripped: 429 with NO retry-after, and retries
    # keep failing until the month rolls over.
    spend_cap_tripped: bool = False


@dataclass
class Usage:
    input_tokens: int = 0
    cache_creation_input_tokens: int = 0
    cache_read_input_tokens: int = 0
    output_tokens: int = 0

    @property
    def total_input(self) -> int:
        # total = read + creation + input   (docs: "input_tokens" is only the
        # tail after the last cache breakpoint)
        return (
            self.input_tokens
            + self.cache_creation_input_tokens
            + self.cache_read_input_tokens
        )


@dataclass
class Outcome:
    accepted: bool
    status: int = 200
    error_type: str | None = None
    error_code: str | None = None
    retry_after: float | None = None
    # Ground truth: which limiter actually caused a 429. The observable
    # signature is checked against this, never derived from it.
    binding_limiter: str | None = None
    usage: Usage = field(default_factory=Usage)
    ttft_at: float = 0.0
    complete_at: float = 0.0
    headers: dict[str, str] = field(default_factory=dict)
    cache_hit: bool = False


@dataclass
class _CacheEntry:
    # An entry is not readable until the writing response begins streaming.
    # "When you need cache hits for parallel requests, wait for the first
    #  response before sending subsequent requests."
    readable_at: float
    expires_at: float
    tokens: int


class FakeProvider:
    """Authoritative model of the provider under simulation."""

    def __init__(self, cfg: ProviderConfig) -> None:
        self.cfg = cfg
        self.buckets = {
            REQUESTS: TokenBucket(cfg.rpm),
            INPUT_TOKENS: TokenBucket(cfg.itpm),
            OUTPUT_TOKENS: TokenBucket(cfg.otpm),
        }
        self.accel = AccelerationGuard(
            factor=cfg.accel_factor,
            tau=cfg.accel_tau,
            floor_rate=cfg.accel_floor_rate,
            enabled=cfg.accel_enabled,
        )
        self.cache: dict[str, _CacheEntry] = {}
        self.in_flight = 0
        self.min_cacheable = MIN_CACHEABLE.get(cfg.model, DEFAULT_MIN_CACHEABLE)
        self.read_counts_to_itpm = cfg.model in CACHE_READ_COUNTS_TO_ITPM
        # Ground-truth tallies, for checking the observable signatures.
        self.rejections: dict[str, int] = {k: 0 for k in LIMITERS}
        self.rejections["acceleration"] = 0
        self.rejections["overloaded"] = 0
        self.rejections["spend_cap"] = 0

    # -- caching ----------------------------------------------------------

    def _resolve_cache(
        self,
        now: float,
        segments: list[tuple[str, int]],
        ttft_estimate: float,
    ) -> tuple[int, int, bool]:
        """Resolve a multi-breakpoint prefix. Returns (read, creation, hit).

        `segments` is an ordered list of (breakpoint_id, tokens) covering the
        cacheable prefix -- typically [(shared system+tools, N), (conversation
        history, M)]. Caching is a prefix match, so a segment can only be read
        if every segment before it also matched; once the chain breaks, nothing
        after it can hit. The deepest readable breakpoint is the read, and
        everything past it is the write.
        """
        total = 0
        cumulative: list[tuple[str, int]] = []
        parts: list[str] = []
        for sid, tok in segments:
            total += tok
            parts.append(sid)
            cumulative.append(("|".join(parts), total))

        if total < self.min_cacheable or not cumulative:
            # Silently not cached -- no error, just no cache fields.
            return 0, 0, False

        read = 0
        hit = False
        for key, _ct in cumulative:
            entry = self.cache.get(key)
            if entry is not None and entry.readable_at <= now and entry.expires_at > now:
                read = entry.tokens
                hit = True
                # A read refreshes the timer at no cost; lifetime is measured
                # from the START of the reading request.
                entry.expires_at = now + self.cfg.cache_ttl
            else:
                break  # prefix match broken; nothing deeper can hit

        creation = max(0, total - read)

        # Write every breakpoint at or past the break. Concurrent siblings all
        # land here before the first writer begins streaming, which is the
        # thundering-herd effect.
        new_readable = now + ttft_estimate
        for key, ct in cumulative:
            if ct <= read:
                continue
            existing = self.cache.get(key)
            readable_at = new_readable
            if existing is not None:
                readable_at = min(existing.readable_at, new_readable)
            self.cache[key] = _CacheEntry(
                readable_at=readable_at,
                expires_at=now + self.cfg.cache_ttl,
                tokens=ct,
            )
        return read, creation, hit

    # -- headers ----------------------------------------------------------

    def headers(self, now: float) -> dict[str, str]:
        """The anthropic-ratelimit-* headers, returned on EVERY response.

        Note `remaining` is rounded to the nearest thousand for the token
        limiters, exactly as the real API does -- which is why sub-thousand
        precision is not available to the calibration path either.
        """
        h: dict[str, str] = {}
        wire = {
            REQUESTS: "requests",
            INPUT_TOKENS: "input-tokens",
            OUTPUT_TOKENS: "output-tokens",
        }
        for key, name in wire.items():
            b = self.buckets[key]
            rem = b.remaining(now)
            if key != REQUESTS:
                rem = round(rem / 1000.0) * 1000
            h[f"anthropic-ratelimit-{name}-limit"] = str(int(b.limit))
            h[f"anthropic-ratelimit-{name}-remaining"] = str(int(rem))
            h[f"anthropic-ratelimit-{name}-reset"] = _rfc3339(
                now + b.time_until(now, b.limit)
            )
        # The generic triplet reports the MOST RESTRICTIVE limit in effect.
        tightest = min(LIMITERS, key=lambda k: self.buckets[k].headroom(now))
        b = self.buckets[tightest]
        h["anthropic-ratelimit-tokens-limit"] = str(int(b.limit))
        h["anthropic-ratelimit-tokens-remaining"] = str(int(round(b.remaining(now) / 1000.0) * 1000))
        h["anthropic-ratelimit-tokens-reset"] = _rfc3339(now + b.time_until(now, b.limit))
        return h

    def headroom(self, now: float) -> dict[str, float]:
        return {k: self.buckets[k].headroom(now) for k in LIMITERS}

    # -- main entry point -------------------------------------------------

    def submit(
        self,
        now: float,
        *,
        prefix_id: str | None = None,
        prefix_tokens: int = 0,
        segments: list[tuple[str, int]] | None = None,
        tail_tokens: int = 0,
        expected_output: int = 0,
    ) -> Outcome:
        """Submit one request.

        Pass either a single `prefix_id`/`prefix_tokens`, or an ordered
        `segments` list for a multi-breakpoint prefix.
        """
        if segments is None:
            if prefix_id is not None:
                segments = [(prefix_id, prefix_tokens)]
            else:
                # No cache_control on the request: the prefix is just plain
                # input tokens, and every one of them counts toward ITPM.
                segments = []
                tail_tokens += prefix_tokens
        prefix_tokens = sum(t for _s, t in segments)
        # 1. Spend cap. 429, but NO retry-after, and retrying never helps.
        if self.cfg.spend_cap_tripped:
            self.rejections["spend_cap"] += 1
            return Outcome(
                accepted=False,
                status=429,
                error_type="rate_limit_error",
                error_code="enforced_spend_limit_reached",
                retry_after=None,
                binding_limiter="spend_cap",
                headers=self.headers(now),
            )

        # 2. Provider-side overload -> 529, unrelated to your quota.
        if self.cfg.server_concurrency is not None and self.in_flight >= self.cfg.server_concurrency:
            self.rejections["overloaded"] += 1
            return Outcome(
                accepted=False,
                status=529,
                error_type="overloaded_error",
                retry_after=None,
                binding_limiter="overloaded",
                headers=self.headers(now),
            )

        # 3. Acceleration limit: rate-of-change, not level.
        if self.accel.would_reject(now):
            self.rejections["acceleration"] += 1
            return Outcome(
                accepted=False,
                status=429,
                error_type="rate_limit_error",
                retry_after=1.0,
                binding_limiter="acceleration",
                headers=self.headers(now),
            )

        # 4. Resolve caching. ttft estimate is needed up front because it sets
        #    when this entry becomes readable to concurrent siblings.
        est_ttft = self.cfg.ttft_base + (prefix_tokens + tail_tokens) * self.cfg.prefill_sec_per_token
        cache_read, cache_creation, hit = self._resolve_cache(now, segments, est_ttft)
        uncached_prefix = prefix_tokens - cache_read - cache_creation
        input_tokens = tail_tokens + uncached_prefix

        # 5. ITPM counts input_tokens + cache_creation_input_tokens.
        #    cache_read_input_tokens does NOT count (except Haiku 3.5).
        itpm_cost = input_tokens + cache_creation
        if self.read_counts_to_itpm:
            itpm_cost += cache_read

        checks = (
            (REQUESTS, 1.0),
            (INPUT_TOKENS, float(itpm_cost)),
            (OUTPUT_TOKENS, float(expected_output)),
        )
        for name, cost in checks:
            if self.buckets[name].remaining(now) < cost:
                self.rejections[name] += 1
                wait = self.buckets[name].time_until(now, cost)
                return Outcome(
                    accepted=False,
                    status=429,
                    error_type="rate_limit_error",
                    retry_after=min(wait, 60.0),
                    binding_limiter=name,
                    headers=self.headers(now),
                )

        # 6. Admitted. Debit, and compute timings.
        for name, cost in checks:
            self.buckets[name].force_take(now, cost)
        self.accel.observe(now)

        ttft = (
            self.cfg.ttft_base
            + (input_tokens + cache_creation) * self.cfg.prefill_sec_per_token
            + cache_read * self.cfg.cached_prefill_sec_per_token
        )
        gen = expected_output / max(self.cfg.output_tokens_per_sec, 1e-9)

        return Outcome(
            accepted=True,
            status=200,
            usage=Usage(
                input_tokens=input_tokens,
                cache_creation_input_tokens=cache_creation,
                cache_read_input_tokens=cache_read,
                output_tokens=expected_output,
            ),
            ttft_at=now + ttft,
            complete_at=now + ttft + gen,
            headers=self.headers(now),
            cache_hit=hit,
        )


def _rfc3339(t: float) -> str:
    """Virtual-time stand-in for the RFC 3339 reset header.

    Calibration only ever uses this as a duration relative to the response, so
    a synthetic epoch is sufficient and keeps runs deterministic.
    """
    return f"T+{t:.3f}"
