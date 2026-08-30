"""Scenario and policy comparison UI over the simulator.

Not a dashboard. The point is the scenario picker: it makes "which of these
stories matches what we actually see" something you can look at, before any
production telemetry exists.
"""

from __future__ import annotations

import pathlib
from dataclasses import replace

import asyncio
import gzip
import json
import os
import threading
import time
from collections import OrderedDict

from fastapi import FastAPI
from fastapi.concurrency import run_in_threadpool
from fastapi.responses import HTMLResponse, StreamingResponse
from pydantic import BaseModel

from ..policy import POLICIES
from ..scenarios import SCENARIOS, TARGET_P50, TARGET_P99_PEAK
from ..sim.runner import Simulation
from ..sim.workload import WorkloadConfig

app = FastAPI(title="LLM ramp metering - simulator")
HERE = pathlib.Path(__file__).parent


class RunRequest(BaseModel):
    scenario: str
    param: float
    policies: list[str] = ["none"]
    retries: list[int] = [3]
    duration: float = 400.0


_SUMMARY_CACHE: dict = {}


@app.get("/", response_class=HTMLResponse)
def explorer() -> str:
    """Front door: the working tool for choosing a scheduler policy."""
    return (HERE / "index.html").read_text()


@app.get("/overview", response_class=HTMLResponse)
def overview() -> str:
    """The plain-language findings, for a non-technical reader."""
    return (HERE / "overview.html").read_text()


@app.get("/brief", response_class=HTMLResponse)
def brief() -> HTMLResponse:
    """Local preview of the built leadership brief.

    The brief is a generated file, not source: run `python build_artifact.py` to
    produce it. Missing is a normal state, not an error, so this answers 404
    with an instruction rather than raising.
    """
    path = HERE.parent.parent / "artifact" / "why-our-agents-are-slow.html"
    if not path.exists():
        return HTMLResponse(
            "<p>No brief built yet. Run <code>python build_artifact.py</code> "
            "to generate it from <code>exec_summary.json</code>.</p>",
            status_code=404,
        )
    return HTMLResponse(path.read_text())


@app.get("/api/overview")
def api_overview() -> dict:
    """Distilled findings. Cached -- the underlying sweep is expensive."""
    if not _SUMMARY_CACHE:
        import json as _json

        from ..exec_summary import build

        # Resolve relative to the package, not the process cwd -- the server is
        # often launched from a parent directory.
        root = HERE.parent.parent
        precomputed = root / "exec_summary.json"
        if precomputed.exists():
            _SUMMARY_CACHE.update(_json.loads(precomputed.read_text()))
        else:
            _SUMMARY_CACHE.update(
                build(
                    _json.loads((root / "sweep_2a.json").read_text()),
                    _json.loads((root / "sweep_2b.json").read_text()),
                )
            )
    return _SUMMARY_CACHE


# Retry depths offered in the UI. Kept here so the production default can only
# ever name a depth the interface can actually display.
RETRY_CHOICES = [1, 3, 5, 7, 10]
DEFAULT_DURATION = 400.0

# How long one simulated second costs in wall time, per simulation. The UI
# multiplies this by (simulations x duration) to decide whether a selection is
# cheap enough to run automatically. A laptop sits near 1.0; a small App Service
# instance is several times slower, so the value is MEASURED from real runs
# rather than assumed -- otherwise the auto-run budget silently becomes a
# promise the host cannot keep.
_COST = {"factor": 3.0, "samples": 0}


# Simulations are deterministic: the same scenario, parameter, policy, retry
# depth, duration and seed always produce byte-identical output, so results can
# be cached with no staleness risk.
#
# The cache is keyed per SIMULATION rather than per request. Keying on the whole
# request only helps when someone repeats a comparison exactly; keyed per
# simulation, adding one policy to an existing comparison computes just that
# policy and reuses the rest. Incremental exploration -- which is how the tool
# is actually used -- becomes nearly free.
_CACHE: "OrderedDict[tuple, dict]" = OrderedDict()
# Raised at load time to fit the precomputed set plus headroom. A cap smaller
# than the preloaded set silently evicts part of it -- which is exactly how a
# precomputed file once shipped missing its first 200 entries.
_CACHE_MAX = 1200
_CACHE_LOCK = threading.Lock()
_CACHE_STATS = {"hits": 0, "misses": 0, "preloaded": 0}


def _sim_key(scenario: str, param: float, policy: str, retries: int, duration: float) -> tuple:
    return (scenario, round(float(param), 6), policy, int(retries), float(duration))


def _wire_key(key: tuple) -> str:
    """Canonical string form shared with the browser.

    "%g" and JavaScript's String(parseFloat(x)) agree on these values (1.8, 32,
    400), so both sides derive the same key without exchanging a format.
    """
    scenario, param, policy, retries, duration = key
    return f"{scenario}|{param:g}|{policy}|{int(retries)}|{duration:g}"


def _cache_get(key: tuple):
    with _CACHE_LOCK:
        if key in _CACHE:
            _CACHE.move_to_end(key)
            _CACHE_STATS["hits"] += 1
            return _CACHE[key]
        _CACHE_STATS["misses"] += 1
        return None


def _cache_put(key: tuple, value: dict) -> None:
    with _CACHE_LOCK:
        _CACHE[key] = value
        _CACHE.move_to_end(key)
        while len(_CACHE) > _CACHE_MAX:
            _CACHE.popitem(last=False)


def _load_precomputed() -> int:
    """Seed the cache from a file built on a dev machine and shipped with the app.

    The instance is roughly 11x slower than the laptop that generates this, so
    the common views cost nothing here and survive restarts -- unlike a warm-up
    that has to be re-earned after every deploy.
    """
    path = HERE.parent.parent / "precomputed.json.gz"
    if not path.exists():
        return 0
    try:
        with gzip.open(path, "rt", encoding="utf-8") as f:
            entries = json.load(f)
    except (OSError, ValueError):
        return 0
    global _CACHE_MAX
    _CACHE_MAX = max(_CACHE_MAX, len(entries) + 300)
    n = 0
    for e in entries:
        _cache_put(tuple(e["key"]), e["value"])
        n += 1
    _CACHE_STATS["preloaded"] = n
    if len(_CACHE) < n:  # must not happen; loudly better than silently partial
        raise RuntimeError(f"cache holds {len(_CACHE)} of {n} preloaded entries")
    return n


def _record_cost(elapsed: float, n_sims: int, duration: float) -> None:
    work = max(n_sims * duration / 1000.0, 1e-6)
    observed = elapsed / work
    n = _COST["samples"]
    # Converge quickly at first, then smooth; clamped so one stalled request
    # cannot make the UI permanently refuse to auto-run.
    alpha = 1.0 if n == 0 else max(0.2, 1.0 / (n + 1))
    _COST["factor"] = min(60.0, max(0.2, (1 - alpha) * _COST["factor"] + alpha * observed))
    _COST["samples"] = n + 1


def _production_default() -> dict:
    """The baseline as production runs today: no scheduler, current retries.

    The scenario is chosen as whichever candidate reproduces the observed
    latency shape most closely -- NOT because it has been established as the
    cause. Four candidates still fit; this is only the closest of them, and the
    UI labels it that way so a default is never read as a conclusion.
    """
    fallback = {
        "scenario": SCENARIOS[0].key,
        "param": SCENARIOS[0].values[len(SCENARIOS[0].values) // 2],
        "policies": ["none"],
        "retries": [3],
        "duration": DEFAULT_DURATION,
        "matched": False,
        "n_candidates": len(SCENARIOS),
    }
    path = HERE.parent.parent / "sweep_2a.json"
    if not path.exists():
        return fallback
    try:
        res = json.loads(path.read_text())
    except (OSError, ValueError):
        return fallback

    hits = [
        (r["key"], p)
        for r in res
        for p in r["points"]
        if p.get("matches_target") and p.get("plausible")
        and p.get("retry_attempts") in RETRY_CHOICES
    ]
    if not hits:
        return fallback
    key, pt = min(hits, key=lambda kp: kp[1]["distance"])
    return {
        "scenario": key,
        "param": pt["param"],
        "policies": ["none"],
        "retries": [pt["retry_attempts"]],
        "duration": DEFAULT_DURATION,
        "matched": True,
        "n_candidates": sum(1 for r in res if r["verdict"] != "RULED OUT"),
        "p50": pt["p50_latency"],
        "p99_peak": pt["p99_latency_peak"],
    }


def _warm_production_view() -> None:
    """Make sure the landing view is resident, whatever the shipped file held."""
    try:
        prod = _production_default()
        run(RunRequest(scenario=prod["scenario"], param=prod["param"],
                       policies=prod["policies"], retries=prod["retries"],
                       duration=prod["duration"]))
    except Exception:  # a warm-up must never take the site down
        pass


@app.on_event("startup")
def _on_startup() -> None:
    _load_precomputed()
    threading.Thread(target=_warm_production_view, daemon=True).start()


@app.get("/health")
def health() -> dict:
    """Liveness probe. Deliberately does no simulation and touches no third
    party, so an outage elsewhere cannot read as a failed deployment."""
    return {
        "status": "ok",
        "on_azure": bool(os.environ.get("WEBSITE_SITE_NAME")),
        "cost_factor": round(_COST["factor"], 2),
        "cost_samples": _COST["samples"],
        "cpu_count": os.cpu_count(),
        "cache": {**_CACHE_STATS, "entries": len(_CACHE)},
    }


@app.get("/api/scenarios")
def scenarios() -> dict:
    return {
        "target": {"p50": TARGET_P50, "p99_peak": TARGET_P99_PEAK},
        "policies": list(POLICIES),
        "retry_choices": RETRY_CHOICES,
        "cost_factor": _COST["factor"],
        # Which simulations are already computed. Without this the browser
        # assumes every selection it has not personally fetched costs full
        # price, and gates precomputed views behind the button for work the
        # server would serve in milliseconds.
        "cached_keys": [_wire_key(k) for k in list(_CACHE.keys())],
        "production": _production_default(),
        "scenarios": [
            {
                "key": s.key,
                "label": s.label,
                "param_name": s.param_name,
                "param_help": s.param_help,
                "mechanism": s.mechanism,
                "values": s.values,
            }
            for s in SCENARIOS
        ],
    }


def _one_sim(scenario: str, param: float, pol: str, depth: int, duration: float) -> dict:
    """Run a single simulation and shape it for the wire."""
    sc = next(s for s in SCENARIOS if s.key == scenario)
    case = sc.build(param, WorkloadConfig(duration=duration))
    case.policy = POLICIES[pol]
    case.retry = replace(case.retry, max_attempts=depth)
    result = Simulation(
        case.provider, case.workload, case.policy, case.client, case.retry
    ).run()
    lat = sorted(r.latency for r in result.records if r.ok)
    return {
        "label": f"{pol} / {depth} tries",
        "policy": pol,
        "retries": depth,
        "summary": result.summary(),
        "samples": _thin(result.samples),
        "latency_cdf": _cdf(lat),
    }


@app.post("/api/run/stream")
async def run_stream(req: RunRequest) -> StreamingResponse:
    """Stream one JSON object per simulation, as each finishes.

    Two problems with computing the whole batch before responding: nothing
    appears until the slowest simulation is done, and with a single worker the
    CPU-bound loop blocks every other request -- so one person exploring can
    stop everyone else loading the page.

    Each simulation runs in a worker thread, so the event loop stays free to
    serve cached and static requests in between.
    """

    async def gen():
        pairs = [(p, d) for p in req.policies for d in req.retries]
        yield json.dumps({"type": "start", "total": len(pairs),
                          "cost_factor": _COST["factor"]}) + "\n"
        for i, (pol, depth) in enumerate(pairs, 1):
            key = _sim_key(req.scenario, req.param, pol, depth, req.duration)
            entry = _cache_get(key)
            cached = entry is not None
            if entry is None:
                started = time.monotonic()
                entry = await run_in_threadpool(
                    _one_sim, req.scenario, req.param, pol, depth, req.duration
                )
                _record_cost(time.monotonic() - started, 1, req.duration)
                _cache_put(key, entry)
            yield json.dumps({
                "type": "sim", "index": i, "total": len(pairs),
                "cached": cached, "key": _wire_key(key), "entry": entry,
                "cost_factor": _COST["factor"],
            }, default=float) + "\n"
            await asyncio.sleep(0)          # let queued requests through
        yield json.dumps({"type": "done", "cost_factor": _COST["factor"]}) + "\n"

    return StreamingResponse(gen(), media_type="application/x-ndjson")


@app.post("/api/run")
def run(req: RunRequest) -> dict:
    sc = next(s for s in SCENARIOS if s.key == req.scenario)
    base = WorkloadConfig(duration=req.duration)
    series = []
    computed = 0
    elapsed = 0.0

    for pol in req.policies:
        for depth in req.retries:
            key = _sim_key(req.scenario, req.param, pol, depth, req.duration)
            hit = _cache_get(key)
            if hit is not None:
                series.append(hit)
                continue

            started = time.monotonic()
            entry = _one_sim(req.scenario, req.param, pol, depth, req.duration)
            elapsed += time.monotonic() - started
            computed += 1
            _cache_put(key, entry)
            series.append(entry)

    # Only real work informs the speed model. Timing cache hits would collapse
    # the factor and make the UI promise instant updates for work never done.
    if computed:
        _record_cost(elapsed, computed, req.duration)

    return {
        "scenario": sc.key,
        "param": req.param,
        "series": series,
        "cost_factor": _COST["factor"],
        "computed": computed,
        "from_cache": len(series) - computed,
        "keys": [
            _wire_key(_sim_key(req.scenario, req.param, e["policy"], e["retries"], req.duration))
            for e in series
        ],
    }


# Charts need a couple of hundred points; a 15-minute run otherwise carries
# twice the samples of a 6.7-minute one for no visible gain, inflating both the
# payload and the precomputed cache.
TARGET_SAMPLES = 200


def _thin(samples: list) -> list:
    step = max(1, len(samples) // TARGET_SAMPLES)
    return samples[::step]


def _cdf(values: list[float], n: int = 60) -> list[dict]:
    if not values:
        return []
    out = []
    for i in range(n + 1):
        q = i / n
        idx = min(len(values) - 1, int(q * len(values)))
        out.append({"q": q, "v": values[idx]})
    return out
