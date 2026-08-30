"""Each test asserts one documented provider behaviour, independent of any scenario.

If these fail, nothing the simulator says is worth reading.
"""

import math

import pytest

from llm_metering.limits import INPUT_TOKENS, OUTPUT_TOKENS, REQUESTS
from llm_metering.sim.provider import FakeProvider, ProviderConfig


def mk(**kw):
    return FakeProvider(ProviderConfig(**kw))


def submit(p, now, prefix_id="sys", prefix_tokens=4000, tail=200, out=300):
    return p.submit(
        now,
        prefix_id=prefix_id,
        prefix_tokens=prefix_tokens,
        tail_tokens=tail,
        expected_output=out,
    )


# --- ITPM accounting ------------------------------------------------------

def test_cache_write_counts_toward_itpm_and_read_does_not():
    """https://platform.claude.com/docs/en/api/rate-limits -- Cache-aware ITPM"""
    p = mk()
    before = p.buckets[INPUT_TOKENS].remaining(0)

    first = submit(p, 0.0)                       # miss -> writes 4000 tokens
    assert first.usage.cache_creation_input_tokens == 4000
    assert first.usage.cache_read_input_tokens == 0
    spent_on_write = before - p.buckets[INPUT_TOKENS].remaining(0.0)
    assert spent_on_write == pytest.approx(4000 + 200)  # creation + tail

    # Second request after the first is readable -> hit.
    t = first.ttft_at + 0.001
    mid = p.buckets[INPUT_TOKENS].remaining(t)
    second = submit(p, t)
    assert second.usage.cache_read_input_tokens == 4000
    assert second.usage.cache_creation_input_tokens == 0
    spent_on_read = mid - p.buckets[INPUT_TOKENS].remaining(t)
    # Only the 200-token tail is charged; the 4000 cached tokens are exempt.
    assert spent_on_read == pytest.approx(200)


def test_haiku_3_5_counts_cache_reads_toward_itpm():
    """The documented exception, marked with a dagger in the rate-limit tables."""
    p = mk(model="claude-haiku-3-5")
    first = submit(p, 0.0)
    t = first.ttft_at + 0.001
    mid = p.buckets[INPUT_TOKENS].remaining(t)
    submit(p, t)
    assert mid - p.buckets[INPUT_TOKENS].remaining(t) == pytest.approx(4200)


def test_total_input_is_sum_of_three_usage_fields():
    p = mk()
    first = submit(p, 0.0)
    assert first.usage.total_input == 4200
    t = first.ttft_at + 0.001
    second = submit(p, t)
    assert second.usage.total_input == 4200
    # ...but input_tokens alone is only the tail after the last breakpoint.
    assert second.usage.input_tokens == 200


# --- Cache lifetime -------------------------------------------------------

def test_ttl_measured_from_request_start_and_refreshed_free_on_read():
    p = mk(cache_ttl=300.0)
    first = submit(p, 0.0)
    entry = p.cache["sys"]
    assert entry.expires_at == pytest.approx(300.0)  # from request START, not end

    # A read at t=299 refreshes the timer to 299+300, at no token cost.
    r = submit(p, 299.0)
    assert r.cache_hit
    assert p.cache["sys"].expires_at == pytest.approx(599.0)


def test_entry_expires_and_next_request_pays_the_write_again():
    p = mk(cache_ttl=300.0)
    submit(p, 0.0)
    late = submit(p, 301.0)
    assert not late.cache_hit
    assert late.usage.cache_creation_input_tokens == 4000


def test_concurrent_same_prefix_requests_all_miss_before_first_token():
    """An entry is not readable until the writing response begins streaming."""
    p = mk()
    outs = [submit(p, 0.0) for _ in range(8)]
    assert all(not o.cache_hit for o in outs)
    assert all(o.usage.cache_creation_input_tokens == 4000 for o in outs)
    # Every one of them paid the write against ITPM: 8 x (4000 + 200).
    assert p.buckets[INPUT_TOKENS].limit - p.buckets[INPUT_TOKENS].remaining(0.0) == pytest.approx(8 * 4200)

    # Once the first is readable, siblings become reads.
    t = min(o.ttft_at for o in outs) + 1e-6
    assert submit(p, t).cache_hit


def test_prefix_below_model_minimum_is_silently_not_cached():
    p = mk(model="claude-opus-5")          # 512-token minimum
    o = submit(p, 0.0, prefix_tokens=400)
    assert o.usage.cache_creation_input_tokens == 0
    assert o.usage.cache_read_input_tokens == 0
    assert o.usage.input_tokens == 600     # 400 prefix + 200 tail, all uncached
    assert "sys" not in p.cache


def test_model_minimums_differ():
    """3K-token prefix caches on Opus 5 but silently will not on Opus 4.6."""
    assert mk(model="claude-opus-5").submit(
        0.0, prefix_id="s", prefix_tokens=3000, tail_tokens=10, expected_output=10
    ).usage.cache_creation_input_tokens == 3000
    assert mk(model="claude-opus-4-6").submit(
        0.0, prefix_id="s", prefix_tokens=3000, tail_tokens=10, expected_output=10
    ).usage.cache_creation_input_tokens == 0


# --- Throttle responses ---------------------------------------------------

def test_spend_cap_429_has_no_retry_after():
    p = mk(spend_cap_tripped=True)
    o = submit(p, 0.0)
    assert o.status == 429
    assert o.error_type == "rate_limit_error"
    assert o.error_code == "enforced_spend_limit_reached"
    assert o.retry_after is None          # retrying never helps


def test_rate_limit_429_carries_retry_after_and_names_the_limiter():
    p = mk(rpm=60)                        # 1/sec
    for _ in range(60):
        submit(p, 0.0, prefix_id=None, prefix_tokens=0, tail=10, out=10)
    o = submit(p, 0.0, prefix_id=None, prefix_tokens=0, tail=10, out=10)
    assert o.status == 429
    assert o.binding_limiter == REQUESTS
    assert o.retry_after is not None and o.retry_after > 0


def test_overload_529_is_not_a_quota_error():
    p = mk(server_concurrency=0)
    o = submit(p, 0.0)
    assert o.status == 529
    assert o.error_type == "overloaded_error"
    assert o.binding_limiter == "overloaded"
    # All quota headroom is intact -- this is provider capacity, not your limit.
    assert p.headroom(0.0)[INPUT_TOKENS] == 1.0


def test_itpm_can_be_the_binding_limiter_while_rpm_is_healthy():
    """The exact paradox: request count is fine, tokens are not."""
    p = mk(rpm=100_000, itpm=50_000, otpm=10_000_000)
    for _ in range(12):
        submit(p, 0.0, prefix_id=None, prefix_tokens=0, tail=4000, out=10)
    o = submit(p, 0.0, prefix_id=None, prefix_tokens=0, tail=4000, out=10)
    assert o.binding_limiter == INPUT_TOKENS
    assert p.headroom(0.0)[REQUESTS] > 0.99    # dashboard would show this as fine


def test_otpm_can_be_the_binding_limiter():
    p = mk(rpm=100_000, itpm=100_000_000, otpm=1000)
    for _ in range(4):
        submit(p, 0.0, prefix_id=None, prefix_tokens=0, tail=10, out=250)
    o = submit(p, 0.0, prefix_id=None, prefix_tokens=0, tail=10, out=250)
    assert o.binding_limiter == OUTPUT_TOKENS


# --- Headers --------------------------------------------------------------

def test_ratelimit_headers_are_present_on_successful_responses():
    """This is what makes offline forensics possible without a code change."""
    o = submit(mk(), 0.0)
    assert o.status == 200
    for name in ("requests", "input-tokens", "output-tokens"):
        assert f"anthropic-ratelimit-{name}-limit" in o.headers
        assert f"anthropic-ratelimit-{name}-remaining" in o.headers
        assert f"anthropic-ratelimit-{name}-reset" in o.headers


def test_generic_tokens_header_reports_the_most_restrictive_limiter():
    p = mk(rpm=100_000, itpm=50_000, otpm=10_000_000)
    for _ in range(10):
        submit(p, 0.0, prefix_id=None, prefix_tokens=0, tail=4000, out=10)
    h = p.headers(0.0)
    assert h["anthropic-ratelimit-tokens-limit"] == "50000"


def test_token_remaining_headers_are_rounded_to_nearest_thousand():
    p = mk()
    submit(p, 0.0, prefix_id=None, prefix_tokens=0, tail=1234, out=7)
    rem = int(p.headers(0.0)["anthropic-ratelimit-input-tokens-remaining"])
    assert rem % 1000 == 0


# --- Acceleration ---------------------------------------------------------

def test_acceleration_rejects_a_sharp_ramp_then_adapts_to_sustained_load():
    """The distinctive signature: rejection at the ONSET of a ramp, recovering
    while offered load stays high. No level limiter behaves this way."""
    p = mk(accel_enabled=True, accel_factor=3.0, accel_tau=20.0, accel_floor_rate=2.0)

    # Establish a low baseline at 1 req/s.
    t = 0.0
    for _ in range(60):
        submit(p, t, prefix_id=None, prefix_tokens=0, tail=10, out=10)
        t += 1.0

    # Then offer 40 req/s CONTINUOUSLY for three minutes.
    onset_rejects = onset_total = 0
    late_rejects = late_total = 0
    burst_start = t
    step = 1.0 / 40.0
    while t < burst_start + 180.0:
        o = submit(p, t, prefix_id=None, prefix_tokens=0, tail=10, out=10)
        rejected = o.binding_limiter == "acceleration"
        if t < burst_start + 5.0:
            onset_total += 1
            onset_rejects += rejected
        elif t > burst_start + 150.0:
            late_total += 1
            late_rejects += rejected
        t += step

    onset_rate = onset_rejects / onset_total
    late_rate = late_rejects / late_total
    assert onset_rate > 0.5, f"a sharp ramp should be rejected at onset ({onset_rate:.2f})"
    assert late_rate < 0.05, f"sustained load should be accepted once adapted ({late_rate:.2f})"


def test_acceleration_ignores_load_below_the_floor():
    p = mk(accel_enabled=True, accel_floor_rate=5.0)
    t = 0.0
    for _ in range(200):
        o = submit(p, t, prefix_id=None, prefix_tokens=0, tail=10, out=10)
        assert o.binding_limiter != "acceleration"
        t += 0.5   # 2 req/s, below the 5 req/s floor


def test_growing_conversation_reads_prior_prefix_and_writes_only_the_delta():
    """The documented healthy-loop signature: reads grow, writes stay small.

    This is the whole reason a working cache keeps a 10-turn agent cheap at the
    ITPM limiter despite resending the entire history every turn.
    """
    p = mk()
    t = 0.0
    reads, writes = [], []
    prefix = 4000
    for turn in range(6):
        o = p.submit(t, prefix_id="conv-1", prefix_tokens=prefix,
                     tail_tokens=150, expected_output=300)
        reads.append(o.usage.cache_read_input_tokens)
        writes.append(o.usage.cache_creation_input_tokens)
        t = o.ttft_at + 0.001          # next turn starts after first token
        prefix += 800                  # history grows

    assert writes[0] == 4000                       # first turn writes the base
    assert all(w == 800 for w in writes[1:])       # later turns write only the delta
    assert reads[1:] == [4000, 4800, 5600, 6400, 7200]   # reads grow turn over turn

    # ITPM cost of the whole 6-turn run, with caching working...
    with_cache = sum(writes) + 6 * 150
    # ...versus the same run with caching broken (every turn resends in full).
    broken = mk()
    prefix, t, uncached = 4000, 0.0, 0
    for turn in range(6):
        o = broken.submit(t, prefix_id=None, prefix_tokens=prefix,
                          tail_tokens=150, expected_output=300)
        uncached += o.usage.input_tokens
        t = o.ttft_at + 0.001
        prefix += 800
    # 6 turns: 36,900 vs 8,900 uncached ITPM tokens -- a 4.1x penalty, and the
    # ratio grows with conversation length because the resent history grows.
    assert uncached > 4 * with_cache
    assert uncached / with_cache > 4.0


def test_shared_system_segment_is_hit_across_different_agents():
    """Agents share the system+tools prefix; only the conversation differs."""
    p = mk()
    SYS = ("sys/v1", 4000)
    a = p.submit(0.0, segments=[SYS, ("conv/a", 500)], tail_tokens=100, expected_output=200)
    assert a.usage.cache_creation_input_tokens == 4500     # cold: writes everything
    t = a.ttft_at + 1e-6
    b = p.submit(t, segments=[SYS, ("conv/b", 700)], tail_tokens=100, expected_output=200)
    # Agent B reads the shared 4000-token system prefix it never wrote, and
    # pays only for its own 700-token conversation.
    assert b.usage.cache_read_input_tokens == 4000
    assert b.usage.cache_creation_input_tokens == 700


def test_broken_prefix_prevents_deeper_segments_from_hitting():
    """Caching is a prefix match: change segment 1 and segment 2 cannot hit."""
    p = mk()
    p.submit(0.0, segments=[("sys/v1", 4000), ("conv/a", 800)], tail_tokens=50, expected_output=100)
    t = 5.0
    # Same conversation, but the system prefix changed (a redeploy, a timestamp).
    o = p.submit(t, segments=[("sys/v2", 4000), ("conv/a", 800)], tail_tokens=50, expected_output=100)
    assert o.usage.cache_read_input_tokens == 0
    assert o.usage.cache_creation_input_tokens == 4800
