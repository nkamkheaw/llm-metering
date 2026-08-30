"""Checks that the harness is capable of returning inconvenient answers.

A simulator that can only confirm the hypothesis it was seeded with is not
evidence. These tests assert the negative results are reachable and that runs
are reproducible.
"""

import json

from llm_metering.policy import POLICIES
from llm_metering.scenarios import SLACK
from llm_metering.sim.provider import ProviderConfig
from llm_metering.sim.runner import ClientConfig, RetryConfig, Simulation
from llm_metering.sim.workload import WorkloadConfig


def run(provider, workload, policy, client=None, retry=None, seed=11):
    return Simulation(provider, workload, policy, client, retry, seed=seed).run().summary()


def test_determinism_same_seed_same_output():
    args = (
        ProviderConfig(rpm=2000, itpm=1_500_000, otpm=SLACK),
        WorkloadConfig(duration=200),
        POLICIES["admission"],
    )
    a = run(*args)
    b = run(*args)
    assert json.dumps(a, sort_keys=True, default=float) == json.dumps(
        b, sort_keys=True, default=float
    )


def test_different_seed_changes_output():
    """Determinism must come from seeding, not from the model being degenerate.

    Two independent seeds exist: WorkloadConfig.seed drives the traffic itself,
    and the Simulation seed drives only retry jitter -- so the latter has no
    effect on a run where nothing is ever throttled.
    """
    provider = ProviderConfig(rpm=1200, itpm=SLACK, otpm=SLACK)
    a = run(provider, WorkloadConfig(duration=200, seed=1), POLICIES["none"])
    b = run(provider, WorkloadConfig(duration=200, seed=2), POLICIES["none"])
    assert a["requests_completed"] != b["requests_completed"]

    # And the jitter seed does matter once retries are actually happening.
    throttled = ProviderConfig(rpm=700, itpm=SLACK, otpm=SLACK)
    w = WorkloadConfig(duration=200)
    r = RetryConfig(max_attempts=7)
    assert run(throttled, w, POLICIES["none"], retry=r, seed=1) != run(
        throttled, w, POLICIES["none"], retry=r, seed=2
    )


def test_harness_can_report_no_limiter_tripped():
    """The inconvenient verdict: latency is bad and NOTHING at the provider is
    constrained. If this is unreachable, every 'it's the provider' conclusion
    the harness produces is unfalsifiable."""
    s = run(
        ProviderConfig(rpm=SLACK, itpm=SLACK, otpm=SLACK),
        WorkloadConfig(duration=400),
        POLICIES["none"],
        client=ClientConfig(pool_size=8),
    )
    assert s["p99_latency"] > 20.0, "latency should be bad"
    assert min(s["min_headroom"].values()) > 0.99, "yet every limiter is untouched"
    assert sum(s["provider_rejections"].values()) == 0, "and nothing was throttled"


def test_harness_can_report_retries_helping():
    """The sweep must be able to say retries are fine, not only that they hurt."""
    provider = ProviderConfig(rpm=900, itpm=SLACK, otpm=SLACK)
    w = WorkloadConfig(duration=400)
    no_retry = run(provider, w, POLICIES["none"], retry=RetryConfig(max_attempts=1))
    with_retry = run(provider, w, POLICIES["none"], retry=RetryConfig(max_attempts=7))
    assert with_retry["requests_completed"] > no_retry["requests_completed"], (
        "under throttling, retries should convert failures into slow successes"
    )


def test_harness_can_report_the_scheduler_making_things_worse():
    """A scheduler is not free. If the harness cannot show it hurting, it is
    not a fair test of whether to deploy one."""
    provider = ProviderConfig(rpm=SLACK, itpm=SLACK, otpm=SLACK)
    w = WorkloadConfig(duration=300)
    without = run(provider, w, POLICIES["none"])
    withsched = run(provider, w, POLICIES["admission_cache_accel"])
    assert withsched["p50_latency"] > without["p50_latency"], (
        "adding admission control to unconstrained traffic should only add delay"
    )


def test_caching_off_costs_far_more_itpm_than_caching_on():
    provider = ProviderConfig(rpm=SLACK, itpm=SLACK, otpm=SLACK)
    on = run(provider, WorkloadConfig(duration=300, caching_enabled=True), POLICIES["none"])
    off = run(provider, WorkloadConfig(duration=300, caching_enabled=False), POLICIES["none"])
    assert off["effective_itpm"] > 5 * on["effective_itpm"]
    assert on["cache_hit_rate"] > 0.8
    assert off["cache_hit_rate"] == 0.0


def test_precomputed_cache_is_never_silently_truncated():
    """A cache cap smaller than the preloaded set evicts part of it.

    This shipped once: precompute generated ~800 entries into an LRU capped at
    600, so the file was written missing its first 200 -- and the scenario a
    user actually opened was among them. Nothing errored; the views were simply
    slow again.
    """
    import llm_metering.ui.server as S

    entries = [
        {"key": ["scen", float(i), "none", 3, 400.0], "value": {"i": i}}
        for i in range(S._CACHE_MAX + 250)
    ]
    S._CACHE.clear()
    original_max = S._CACHE_MAX
    try:
        S._CACHE_MAX = max(S._CACHE_MAX, len(entries) + 300)
        for e in entries:
            S._cache_put(tuple(e["key"]), e["value"])
        assert len(S._CACHE) == len(entries), (
            f"cache holds {len(S._CACHE)} of {len(entries)} entries"
        )
    finally:
        S._CACHE_MAX = original_max
        S._CACHE.clear()
