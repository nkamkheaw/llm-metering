"""The five candidate causes, each parameterised by one governing quantity.

Step 2a sweeps each candidate's parameter with the OTHER limiters held slack,
and asks which candidates can even produce the observed latency shape
(median ~1s, p99 ~40s at peak) at plausible parameters. A candidate that
cannot reach that shape anywhere in its plausible range is ruled out without
any production data.

Nothing here encodes a preferred answer: each candidate gets the same
treatment and the same target.
"""

from __future__ import annotations

from dataclasses import dataclass, replace
from typing import Callable

from .policy import POLICIES, PolicyConfig
from .sim.provider import ProviderConfig
from .sim.runner import ClientConfig, RetryConfig
from .sim.workload import WorkloadConfig, generate, offered_load

SLACK = 10**12          # a limiter that is deliberately not the constraint

# Observed target the candidates are matched against.
TARGET_P50 = (0.6, 1.6)
TARGET_P99_PEAK = (25.0, 60.0)

# Published Opus 5 ceilings, for judging whether a swept value is plausible.
# https://platform.claude.com/docs/en/api/rate-limits
TIER_LIMITS = {
    "start": {"rpm": 1000, "itpm": 2_000_000, "otpm": 400_000},
    "build": {"rpm": 5000, "itpm": 5_000_000, "otpm": 1_000_000},
    "scale": {"rpm": 10_000, "itpm": 10_000_000, "otpm": 2_000_000},
}


@dataclass
class Case:
    """One point in a candidate's parameter sweep."""

    provider: ProviderConfig
    workload: WorkloadConfig
    policy: PolicyConfig
    client: ClientConfig
    retry: RetryConfig
    param: float
    note: str = ""


@dataclass
class Scenario:
    key: str
    label: str
    param_name: str
    param_help: str
    values: list[float]
    build: Callable[[float, WorkloadConfig], Case]
    plausible: Callable[[float], bool]
    mechanism: str


def _base_workload(duration: float = 600.0, **kw) -> WorkloadConfig:
    return WorkloadConfig(duration=duration, **kw)


def _sdk_default_retries() -> RetryConfig:
    # The official SDKs retry twice by default, honouring retry-after.
    return RetryConfig(max_attempts=3, honor_retry_after=True)


# --- candidate 1: ITPM pressure from broken / absent caching ---------------

def _c1(rho: float, base: WorkloadConfig) -> Case:
    w = replace(base, caching_enabled=False)
    offered = offered_load(w, generate(w))["uncached_itpm_if_no_cache"]
    return Case(
        provider=ProviderConfig(rpm=SLACK, itpm=offered / rho, otpm=SLACK),
        workload=w,
        policy=POLICIES["none"],
        client=ClientConfig(),
        retry=_sdk_default_retries(),
        param=rho,
        note=f"itpm_ceiling={offered / rho:,.0f}",
    )


# --- candidate 2: cache-write thundering herd ------------------------------

def _c2(spike: float, base: WorkloadConfig) -> Case:
    # Caching is configured and working. The only thing that varies is how many
    # agents arrive at once sharing the same novel prefix.
    w = replace(base, caching_enabled=True, spike_multiplier=spike, spike_ramp=0.5)
    return Case(
        provider=ProviderConfig(rpm=SLACK, itpm=TIER_LIMITS["start"]["itpm"], otpm=SLACK),
        workload=w,
        policy=POLICIES["none"],
        client=ClientConfig(),
        retry=_sdk_default_retries(),
        param=spike,
        note="caching on; ITPM at Start-tier ceiling",
    )


# --- candidate 3: sub-minute RPM bursts ------------------------------------

def _c3(rho: float, base: WorkloadConfig) -> Case:
    w = replace(base, caching_enabled=True)
    offered_rpm = offered_load(w, generate(w))["mean_rpm"]
    return Case(
        provider=ProviderConfig(rpm=offered_rpm / rho, itpm=SLACK, otpm=SLACK),
        workload=w,
        policy=POLICIES["none"],
        client=ClientConfig(),
        retry=_sdk_default_retries(),
        param=rho,
        note=f"rpm_ceiling={offered_rpm / rho:,.0f}",
    )


# --- candidate 4: acceleration limit ---------------------------------------

def _c4(factor: float, base: WorkloadConfig) -> Case:
    w = replace(base, caching_enabled=True, spike_ramp=0.5)
    return Case(
        provider=ProviderConfig(
            rpm=SLACK, itpm=SLACK, otpm=SLACK,
            accel_enabled=True, accel_factor=factor, accel_tau=45.0, accel_floor_rate=5.0,
        ),
        workload=w,
        policy=POLICIES["none"],
        client=ClientConfig(),
        retry=_sdk_default_retries(),
        param=factor,
        note="all level limiters slack; only rate-of-change constrains",
    )


# --- candidate 5: client-side pool exhaustion ------------------------------

def _c5(pool: float, base: WorkloadConfig) -> Case:
    w = replace(base, caching_enabled=True)
    return Case(
        provider=ProviderConfig(rpm=SLACK, itpm=SLACK, otpm=SLACK),
        workload=w,
        policy=POLICIES["none"],
        client=ClientConfig(pool_size=int(pool)),
        retry=_sdk_default_retries(),
        param=pool,
        note="every provider limiter slack; the queue is ours",
    )


SCENARIOS: list[Scenario] = [
    Scenario(
        key="itpm_broken_cache",
        label="ITPM pressure, caching broken/absent",
        param_name="itpm_pressure",
        param_help="offered uncached input tokens / ITPM ceiling",
        values=[0.5, 0.6, 0.7, 0.8, 0.9, 1.0, 1.2, 1.5, 2.0],
        build=_c1,
        plausible=lambda v: v <= 6.0,
        mechanism="Whole history resent uncached every turn; every token counts to ITPM.",
    ),
    Scenario(
        key="cache_herd",
        label="Cache-write thundering herd",
        param_name="spike_multiplier",
        param_help="peak arrival rate / baseline arrival rate",
        values=[8.0, 12.0, 16.0, 20.0, 24.0, 28.0, 32.0, 40.0],
        build=_c2,
        plausible=lambda v: v <= 32.0,
        mechanism="Concurrent same-prefix requests all miss and all pay the ITPM write.",
    ),
    Scenario(
        key="rpm_burst",
        label="Sub-minute RPM bursts",
        param_name="rpm_pressure",
        param_help="offered requests per minute / RPM ceiling",
        values=[0.5, 0.6, 0.7, 0.8, 0.9, 1.0, 1.2, 1.5, 2.0],
        build=_c3,
        plausible=lambda v: v <= 6.0,
        mechanism="Token bucket enforced per-second; a per-minute average hides the burst.",
    ),
    Scenario(
        key="acceleration",
        label="Acceleration (rate-of-change) limit",
        param_name="accel_factor",
        param_help="permitted rate / adapted baseline (lower = stricter)",
        values=[1.5, 1.8, 2.0, 2.2, 2.5, 2.8, 3.0, 4.0],
        build=_c4,
        plausible=lambda v: 1.2 <= v <= 12.0,
        mechanism="Sharp ramps rejected until the baseline adapts; levels stay healthy.",
    ),
    Scenario(
        key="pool_exhaustion",
        label="Client-side connection-pool exhaustion",
        param_name="pool_size",
        param_help="concurrent client connections available",
        values=[4, 6, 8, 12, 16, 24, 32, 40, 48, 64, 96],
        build=_c5,
        plausible=lambda v: v <= 512,
        mechanism="Requests queue locally for a connection; no provider limiter is touched.",
    ),
]
