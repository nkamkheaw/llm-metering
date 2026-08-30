"""End-to-end check of the diagnostic path.

Run a scenario whose cause is known by construction, export only what
production persists (the anthropic-ratelimit-* headers), and check the
calibration analysis recovers that cause. If it cannot, mining the header log
is not a real Step-0 substitute and the plan should say so.
"""

import pytest

from llm_metering.calibrate.headers import analyse
from llm_metering.policy import POLICIES
from llm_metering.scenarios import SLACK
from llm_metering.sim.provider import ProviderConfig
from llm_metering.sim.runner import ClientConfig, RetryConfig, Simulation
from llm_metering.sim.workload import WorkloadConfig


def header_log(provider, workload, policy, client=None, retry=None):
    sim = Simulation(provider, workload, policy, client, retry)
    sim.run()
    return sim.header_log


def test_recovers_itpm_as_the_binding_limiter():
    log = header_log(
        ProviderConfig(rpm=SLACK, itpm=400_000, otpm=SLACK),
        WorkloadConfig(duration=300, caching_enabled=False),
        POLICIES["none"],
        retry=RetryConfig(max_attempts=7),
    )
    cal = analyse(log)
    assert cal.binding_limiter == "input_tokens"
    assert cal.min_headroom["input_tokens"] < 0.05
    assert cal.min_headroom["requests"] > 0.9      # the dashboard's blind spot
    assert "input_tokens" in cal.verdict
    # And it must admit what headers cannot settle.
    assert any("cache_read_input_tokens" in u for u in cal.unanswerable)


def test_recovers_rpm_as_the_binding_limiter():
    log = header_log(
        ProviderConfig(rpm=700, itpm=SLACK, otpm=SLACK),
        WorkloadConfig(duration=300),
        POLICIES["none"],
        retry=RetryConfig(max_attempts=7),
    )
    cal = analyse(log)
    assert cal.binding_limiter == "requests"
    assert cal.min_headroom["requests"] < 0.05
    assert cal.min_headroom["input_tokens"] > 0.9


def test_recovers_otpm_as_the_binding_limiter():
    log = header_log(
        ProviderConfig(rpm=SLACK, itpm=SLACK, otpm=60_000),
        WorkloadConfig(duration=300),
        POLICIES["none"],
        retry=RetryConfig(max_attempts=7),
    )
    cal = analyse(log)
    assert cal.binding_limiter == "output_tokens"


def test_reports_no_limiter_tripped_for_client_side_queueing():
    """The inconvenient verdict must survive the round trip through headers."""
    log = header_log(
        ProviderConfig(rpm=SLACK, itpm=SLACK, otpm=SLACK),
        WorkloadConfig(duration=300),
        POLICIES["none"],
        client=ClientConfig(pool_size=8),
    )
    cal = analyse(log)
    assert cal.throttled == 0
    assert min(cal.min_headroom.values()) > 0.9
    assert "NO LIMITER TRIPPED" in cal.verdict
    assert "client-side queueing" in cal.verdict


def test_reports_throttling_with_healthy_headroom_for_acceleration():
    log = header_log(
        ProviderConfig(
            rpm=SLACK, itpm=SLACK, otpm=SLACK,
            accel_enabled=True, accel_factor=2.0, accel_floor_rate=5.0,
        ),
        WorkloadConfig(duration=300, spike_ramp=0.5),
        POLICIES["none"],
        retry=RetryConfig(max_attempts=7),
    )
    cal = analyse(log)
    assert cal.throttled > 0
    assert min(cal.min_headroom.values()) > 0.9
    assert "HEALTHY HEADROOM" in cal.verdict


def test_flags_spend_cap_separately_from_rate_limiting():
    log = header_log(
        ProviderConfig(rpm=SLACK, itpm=SLACK, otpm=SLACK, spend_cap_tripped=True),
        WorkloadConfig(duration=60),
        POLICIES["none"],
        retry=RetryConfig(max_attempts=3),
    )
    cal = analyse(log)
    assert cal.spend_cap_hits > 0
    assert "retrying those never succeeds" in cal.verdict
