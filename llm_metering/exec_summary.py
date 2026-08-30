"""Distils the sweep output into the handful of numbers a non-technical
reader needs, in business language rather than limiter language.

Deliberately lossy. The explorer keeps the full detail; this exists so the
headline finding does not have to be reconstructed from a parameter table.
"""

from __future__ import annotations

import json

from .policy import POLICIES
from .scenarios import SLACK
from .sim.provider import ProviderConfig
from .sim.runner import RetryConfig, Simulation
from .sim.workload import WorkloadConfig, generate, offered_load

# Plain-language names. "Token" is unavoidable jargon, so it gets glossed once
# in the page rather than silently swapped for "word", which is not accurate.
PLAIN = {
    "itpm_broken_cache": {
        "title": "The provider isn't remembering our conversations",
        "short": "Re-sent history counts against the token budget",
        "body": (
            "Each agent re-sends its whole conversation every step. The provider can "
            "remember that history for us, and what it remembers is free against our "
            "budget. If that isn't switched on, or is silently broken, every step pays "
            "for the whole conversation again."
        ),
        "group": "token budget",
    },
    "cache_herd": {
        "title": "Agents arriving together, all paying full price",
        "short": "Simultaneous starts each pay for the same shared text",
        "body": (
            "The provider can only reuse remembered text once the first answer has "
            "started coming back. When hundreds of agents start at the same instant, "
            "none of them can reuse anything the others are still writing, so they all "
            "pay full price for the same shared instructions at once."
        ),
        "group": "token budget",
    },
    "rpm_burst": {
        "title": "Too many calls in the same second",
        "short": "The per-minute average hides sub-second bursts",
        "body": (
            "The call limit is enforced continuously, not once a minute. A limit of "
            "60 calls a minute behaves like one call per second. Our dashboard averages "
            "over a minute, so a burst that trips the limit is invisible to it."
        ),
        "group": "call rate",
    },
    "acceleration": {
        "title": "Ramping up too suddenly",
        "short": "Sharp increases are throttled even when totals look fine",
        "body": (
            "The provider limits how fast our usage can climb, separately from how much "
            "we use. A sudden jump at the start of a busy period can be refused even "
            "though every total is well within budget."
        ),
        "group": "ramp rate",
    },
    "pool_exhaustion": {
        "title": "The bottleneck is inside our own service",
        "short": "Ruled out: this would slow every call, not just the worst ones",
        "body": (
            "Our own connection limit could queue calls before they ever reach the "
            "provider. We ruled this out: it would slow down typical calls as much as "
            "the worst ones, and what we see is a normal typical call with a small "
            "number of very slow ones."
        ),
        "group": "our own service",
    },
}


def _match(result: dict) -> dict:
    hits = [p for p in result["points"] if p["matches_target"] and p["plausible"]]
    return min(hits, key=lambda p: p["distance"]) if hits else result["best"]


def build(res_2a: list[dict], rows_2b: list[dict]) -> dict:
    out: dict = {}

    # --- candidates, grouped and ranked
    cands = []
    for r in res_2a:
        p = _match(r)
        info = PLAIN[r["key"]]
        cands.append({
            "key": r["key"],
            "verdict": r["verdict"],
            "ruled_out": r["verdict"] == "RULED OUT",
            "requests_headroom": p["min_headroom"]["requests"],
            "tokens_headroom": p["min_headroom"]["input_tokens"],
            "reuse_rate": p["cache_hit_rate"],
            **info,
        })
    out["candidates"] = cands

    # --- the paradox: one demonstration run, plotted as budget USED not headroom
    w = WorkloadConfig(duration=600, caching_enabled=False)
    offered = offered_load(w, generate(w))["uncached_itpm_if_no_cache"]
    sim = Simulation(
        ProviderConfig(rpm=4000, itpm=offered / 0.9, otpm=SLACK),
        w, POLICIES["none"], retry_cfg=RetryConfig(max_attempts=7),
    )
    result = sim.run()
    s = result.summary()
    out["paradox"] = {
        "series": [
            {
                "t": p["t"],
                "calls_used": round(1 - p["headroom_requests"], 4),
                "tokens_used": round(1 - p["headroom_input_tokens"], 4),
            }
            for p in result.samples[::3]
        ],
        "peak_calls_used": round(1 - s["min_headroom"]["requests"], 3),
        "peak_tokens_used": round(1 - s["min_headroom"]["input_tokens"], 3),
        "typical_seconds": round(s["p50_latency"], 1),
        "slowest_seconds": round(s["p99_latency_peak"], 1),
    }

    # --- the retry finding
    retry_rows = []
    for cand in dict.fromkeys(r["candidate"] for r in rows_2b):
        one = next((r for r in rows_2b if r["candidate"] == cand
                    and r["policy"] == "none" and r["retry_attempts"] == 1), None)
        deep = next((r for r in rows_2b if r["candidate"] == cand
                     and r["policy"] == "none" and r["retry_attempts"] == 7), None)
        if one and deep:
            retry_rows.append({
                "key": cand,
                "title": PLAIN[cand]["title"],
                "no_retry_done": one["requests_completed"],
                "no_retry_failed": one["requests_failed"],
                "retry_done": deep["requests_completed"],
                "retry_failed": deep["requests_failed"],
                "gain": (deep["requests_completed"] - one["requests_completed"])
                / max(one["requests_completed"], 1),
                "slowest_no_retry": round(one["p99_latency_peak"], 1),
                "slowest_retry": round(deep["p99_latency_peak"], 1),
            })
    out["retries"] = retry_rows

    # --- how the wait grows with attempts
    depths = sorted({p["retry_attempts"] for r in res_2a for p in r["points"]})
    out["wait_by_attempts"] = [
        {
            "attempts": d,
            "slowest": round(
                max((p["p99_latency_peak"] for r in res_2a for p in r["points"]
                     if p["retry_attempts"] == d and p["plausible"]
                     and 0.6 <= p["p50_latency"] <= 1.6), default=0.0), 1),
        }
        for d in depths
    ]
    return out


if __name__ == "__main__":
    data = build(json.load(open("sweep_2a.json")), json.load(open("sweep_2b.json")))
    json.dump(data, open("exec_summary.json", "w"), indent=1, default=float)
    print(json.dumps({k: v for k, v in data.items() if k != "paradox"}, indent=1, default=float)[:1400])
    print("...")
    print("paradox:", {k: v for k, v in data["paradox"].items() if k != "series"})
