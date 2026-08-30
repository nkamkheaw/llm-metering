"""Step 2a (candidate elimination) and Step 2b (policy comparison)."""

from __future__ import annotations

import json
import math
from dataclasses import replace

from .policy import POLICIES
from .scenarios import SCENARIOS, TARGET_P50, TARGET_P99_PEAK, Scenario, _base_workload
from .sim.runner import Simulation


def run_case(case, seed: int = 11) -> dict:
    sim = Simulation(
        provider_cfg=case.provider,
        workload_cfg=case.workload,
        policy_cfg=case.policy,
        client_cfg=case.client,
        retry_cfg=case.retry,
        seed=seed,
    )
    s = sim.run().summary()
    s["param"] = case.param
    s["note"] = case.note
    return s


OBSERVED_P50 = 1.0
OBSERVED_P99_PEAK = 40.0


def target_distance(s: dict) -> float:
    """Log-space distance from the observed latency shape.

    A binary in-band test makes verdicts hinge on where the band edge happens
    to fall -- a candidate landing at 60.6s against a 60s cutoff reads as
    "ruled out" when it is really a near match. Distance is reported alongside
    the band so near misses stay visible.
    """
    p50 = max(s["p50_latency"], 1e-6)
    p99 = max(s["p99_latency_peak"], 1e-6)
    return abs(math.log(p50 / OBSERVED_P50)) + abs(math.log(p99 / OBSERVED_P99_PEAK))


def matches_target(s: dict) -> bool:
    if s.get("peak_requests", 1) == 0:
        return False  # the run never reached a spike window; p99-at-peak is meaningless
    return (
        TARGET_P50[0] <= s["p50_latency"] <= TARGET_P50[1]
        and TARGET_P99_PEAK[0] <= s["p99_latency_peak"] <= TARGET_P99_PEAK[1]
    )


# Retry depth is the second axis, and it is not a detail: under throttling the
# latency tail is largely MADE of retry backoff, so "how long can a throttled
# request take" is a property of the client's retry policy. That policy is
# unknown here, so it is swept rather than assumed.
#   3 = the official SDK default (2 retries on top of the initial attempt)
#   5, 8 = typical homegrown wrappers layered on top of it
RETRY_DEPTHS = (3, 4, 5, 6, 7, 8, 10)


def sweep_candidate(sc: Scenario, duration: float = 600.0, seed: int = 11) -> dict:
    base = _base_workload(duration=duration)
    points = []
    for v in sc.values:
        for depth in RETRY_DEPTHS:
            case = sc.build(v, base)
            case.retry = replace(case.retry, max_attempts=depth)
            s = run_case(case, seed=seed)
            s["retry_attempts"] = depth
            s["matches_target"] = matches_target(s)
            s["distance"] = target_distance(s)
            s["plausible"] = sc.plausible(v)
            points.append(s)
    hits = [p for p in points if p["matches_target"]]
    plausible_hits = [p for p in hits if p["plausible"]]
    plausible = [p for p in points if p["plausible"]]
    best = min(plausible, key=lambda p: p["distance"]) if plausible else None
    if plausible_hits:
        verdict = "SURVIVES"
    elif hits:
        verdict = "DOWNGRADED"       # only reaches the target at implausible params
    elif best is not None and best["distance"] < 0.7:
        verdict = "NEAR MISS"        # close, but never inside the band
    else:
        verdict = "RULED OUT"
    return {
        "key": sc.key,
        "label": sc.label,
        "param_name": sc.param_name,
        "param_help": sc.param_help,
        "mechanism": sc.mechanism,
        "verdict": verdict,
        "matching_params": sorted({(p["param"], p["retry_attempts"]) for p in plausible_hits}),
        "best": best,
        "points": points,
    }


def sweep_all(duration: float = 600.0, seed: int = 11) -> list[dict]:
    return [sweep_candidate(sc, duration=duration, seed=seed) for sc in SCENARIOS]


def format_report(results: list[dict]) -> str:
    out = []
    out.append("STEP 2a -- CANDIDATE ELIMINATION BY LATENCY SHAPE")
    out.append(f"target: p50 in {TARGET_P50}s, p99-at-peak in {TARGET_P99_PEAK}s")
    out.append("")
    for r in results:
        out.append(f"[{r['verdict']:>10}]  {r['label']}")
        out.append(f"              {r['mechanism']}")
        out.append(
            f"              {'param':>9} {'try':>4} {'p50':>7} {'p99pk':>8} {'hdrm req':>9}"
            f" {'hdrm in':>8} {'hdrm out':>9} {'cache':>6} {'retryX':>7} {'fail':>6}"
        )
        for p in r["points"]:
            mark = " <-- matches" if p["matches_target"] else ""
            if p["matches_target"] and not p["plausible"]:
                mark = " <-- matches (implausible)"
            h = p["min_headroom"]
            out.append(
                f"              {p['param']:>9.2f} {p['retry_attempts']:>4d}"
                f" {p['p50_latency']:>7.2f}"
                f" {p['p99_latency_peak']:>8.2f} {h['requests']:>9.2f}"
                f" {h['input_tokens']:>8.2f} {h['output_tokens']:>9.2f}"
                f" {p['cache_hit_rate']:>6.2f} {p['retry_amplification']:>7.2f}"
                f" {p['requests_failed']:>6d}{mark}"
            )
        if r["matching_params"]:
            out.append(f"              matching plausible params: {r['matching_params']}")
        out.append("")
    return "\n".join(out)


if __name__ == "__main__":
    import sys

    dur = float(sys.argv[1]) if len(sys.argv) > 1 else 600.0
    res = sweep_all(duration=dur)
    print(format_report(res))
    with open("sweep_2a.json", "w") as f:
        json.dump(res, f, indent=1, default=float)
    print("wrote sweep_2a.json")


# --- Step 2b: policy comparison on the surviving candidates ----------------

POLICY_ORDER = ["none", "fifo_backoff", "admission", "admission_cache", "admission_cache_accel"]
RETRY_GRID = [1, 3, 7]   # none / SDK default / the depth that reproduces the tail


def representative_param(result: dict) -> float:
    hits = [p for p in result["points"] if p["matches_target"] and p["plausible"]]
    if hits:
        return min(hits, key=lambda p: p["distance"])["param"]
    return result["best"]["param"]


def sweep_policies(results_2a: list[dict], duration: float = 600.0, seed: int = 11) -> list[dict]:
    from .scenarios import SCENARIOS

    by_key = {s.key: s for s in SCENARIOS}
    base = _base_workload(duration=duration)
    out = []
    for r in results_2a:
        if r["verdict"] == "RULED OUT":
            continue
        sc = by_key[r["key"]]
        param = representative_param(r)
        for pol in POLICY_ORDER:
            for depth in RETRY_GRID:
                case = sc.build(param, base)
                case.policy = POLICIES[pol]
                case.retry = replace(case.retry, max_attempts=depth)
                s = run_case(case, seed=seed)
                s.update(candidate=r["key"], policy=pol, retry_attempts=depth, param=param)
                out.append(s)
    return out


def format_2b(rows: list[dict]) -> str:
    out = ["STEP 2b -- POLICY x RETRY COMPARISON (on surviving candidates)", ""]
    for cand in dict.fromkeys(r["candidate"] for r in rows):
        out.append(f"  {cand}")
        out.append(
            f"    {'policy':<24}{'try':>4}{'p50':>7}{'p99pk':>8}{'done':>8}"
            f"{'fail':>7}{'drop':>7}{'retryX':>8}{'cache':>7}{'effITPM':>10}"
        )
        for r in rows:
            if r["candidate"] != cand:
                continue
            out.append(
                f"    {r['policy']:<24}{r['retry_attempts']:>4}{r['p50_latency']:>7.2f}"
                f"{r['p99_latency_peak']:>8.2f}{r['requests_completed']:>8d}"
                f"{r['requests_failed']:>7d}{r['requests_dropped']:>7d}"
                f"{r['retry_amplification']:>8.2f}{r['cache_hit_rate']:>7.2f}"
                f"{r['effective_itpm']:>10,.0f}"
            )
        out.append("")
    return "\n".join(out)
