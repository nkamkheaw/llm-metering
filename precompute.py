#!/usr/bin/env python3
"""Build a warm cache on a dev machine and ship it with the app.

The App Service instance measures ~11x slower than this laptop per simulation,
so the views people actually open should be computed here, once, rather than
there, repeatedly. Unlike a startup warm-up, a shipped file survives restarts
and costs the instance nothing.

Runs are deterministic, so a precomputed entry is indistinguishable from one
computed live.
"""

from __future__ import annotations

import gzip
import json
import pathlib
import time

from llm_metering.scenarios import SCENARIOS
from llm_metering.ui.server import RETRY_CHOICES, _production_default, _sim_key, run
from llm_metering.ui.server import RunRequest, _CACHE

DURATION = 400.0
POLICIES = ["none", "fifo_backoff", "admission", "admission_cache", "admission_cache_accel"]
COMMON_RETRIES = [3, 7]


def matching_param(key: str, sweep: list) -> float | None:
    r = next((x for x in sweep if x["key"] == key), None)
    if not r:
        return None
    hits = [p for p in r["points"] if p.get("matches_target") and p.get("plausible")
            and p.get("retry_attempts") in RETRY_CHOICES]
    if hits:
        return min(hits, key=lambda p: p["distance"])["param"]
    return (r.get("best") or {}).get("param")


def main() -> None:
    sweep_path = pathlib.Path("sweep_2a.json")
    sweep = json.loads(sweep_path.read_text()) if sweep_path.exists() else []
    prod = _production_default()

    jobs: list[tuple[str, float, str, int]] = []

    def add(scenario: str, param: float, policies, retries):
        for pol in policies:
            for d in retries:
                jobs.append((scenario, param, pol, d))

    for sc in SCENARIOS:
        params = {sc.values[len(sc.values) // 2]}          # the UI's default
        mp = matching_param(sc.key, sweep)
        if mp is not None:
            params.add(mp)                                  # the view that matches production
        for prm in params:
            add(sc.key, prm, POLICIES, COMMON_RETRIES)

    # The landing view, and the retry-depth walk people do from it.
    add(prod["scenario"], prod["param"], ["none"], RETRY_CHOICES)

    seen, uniq = set(), []
    for j in jobs:
        if j not in seen:
            seen.add(j)
            uniq.append(j)

    print(f"Computing {len(uniq)} simulations at {DURATION:.0f}s each...")
    t0 = time.monotonic()
    for i, (scenario, param, pol, depth) in enumerate(uniq, 1):
        run(RunRequest(scenario=scenario, param=param, policies=[pol],
                       retries=[depth], duration=DURATION))
        if i % 20 == 0:
            print(f"  {i}/{len(uniq)}  ({time.monotonic()-t0:.0f}s)")

    entries = [{"key": list(k), "value": v} for k, v in _CACHE.items()]
    out = pathlib.Path("precomputed.json.gz")
    with gzip.open(out, "wt", encoding="utf-8", compresslevel=9) as f:
        json.dump(entries, f, separators=(",", ":"), default=float)
    print(f"\nWrote {out} — {len(entries)} entries, "
          f"{out.stat().st_size/1e6:.1f} MB gzipped, built in {time.monotonic()-t0:.0f}s")


if __name__ == "__main__":
    main()
