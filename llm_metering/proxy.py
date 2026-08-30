"""Metering proxy. Ships in SHADOW MODE: it decides, logs, and forwards.

Shadow mode is not caution theatre. One of the five candidates
(client-side pool exhaustion) is made WORSE by a scheduler, and it has been
eliminated by model reasoning rather than by measurement. Until production
telemetry confirms which candidate is real, enforcement stays off and the
proxy's job is to collect exactly the fields that tell them apart.

Deployment notes that matter here:
  * One shared httpx.AsyncClient for the whole process. A per-request client
    exhausts connections and produces intermittent ConnectTimeouts under load
    -- which would look exactly like provider throttling.
  * max_retries=0 upstream, so every retry is this service's decision and is
    countable. The official SDKs retry twice by default; layering your own
    retries on top multiplies rather than replaces them.
"""

from __future__ import annotations

import json
import os
import time
import uuid
from contextlib import asynccontextmanager
from dataclasses import asdict, dataclass, field

import httpx
from fastapi import FastAPI, Request
from fastapi.responses import JSONResponse

from .clock import RealClock
from .limits import LIMITERS
from .policy import Action, PendingRequest, PolicyConfig, SchedulerState, charge, decide

UPSTREAM = os.environ.get("ANTHROPIC_BASE_URL", "https://api.anthropic.com")
ENFORCE = os.environ.get("METERING_ENFORCE", "0") == "1"
TELEMETRY_PATH = os.environ.get("METERING_LOG", "metering.jsonl")


@dataclass
class Counters:
    """Exactly the fields Step 3 identified as discriminating, plus the two
    that separate the eliminated candidates. Nothing else is worth the deploy."""

    requests: int = 0
    attempts: int = 0
    completed: int = 0
    throttled_429: int = 0
    spend_cap_429: int = 0
    overloaded_529: int = 0
    in_flight: int = 0
    max_in_flight: int = 0
    cache_read_tokens: int = 0
    cache_creation_tokens: int = 0
    input_tokens: int = 0
    output_tokens: int = 0
    min_headroom: dict = field(default_factory=lambda: {k: 1.0 for k in LIMITERS})
    would_have: dict = field(default_factory=lambda: {"send": 0, "wait": 0, "drop": 0})
    would_have_waited_sec: float = 0.0

    @property
    def cache_hit_rate(self) -> float:
        total = self.cache_read_tokens + self.cache_creation_tokens + self.input_tokens
        return self.cache_read_tokens / total if total else 0.0

    @property
    def retry_amplification(self) -> float:
        return self.attempts / self.completed if self.completed else 0.0


class Metering:
    def __init__(self, policy: PolicyConfig, rpm: float, itpm: float, otpm: float) -> None:
        self.clock = RealClock()
        self.state = SchedulerState(rpm=rpm, itpm=itpm, otpm=otpm, cfg=policy)
        self.counters = Counters()
        self.client: httpx.AsyncClient | None = None

    def observe(self, headers: httpx.Headers) -> None:
        wire = {"requests": "requests", "input_tokens": "input-tokens", "output_tokens": "output-tokens"}
        for key, name in wire.items():
            lim = headers.get(f"anthropic-ratelimit-{name}-limit")
            rem = headers.get(f"anthropic-ratelimit-{name}-remaining")
            if lim and rem and float(lim) > 0:
                hr = max(0.0, min(1.0, float(rem) / float(lim)))
                self.counters.min_headroom[key] = min(self.counters.min_headroom[key], hr)
        self.state.observe_headers(self.clock.now(), dict(headers))


def estimate(body: dict) -> tuple[list[tuple[str, int]] | None, int, int, int]:
    """Crude pre-send token estimate.

    4 chars/token is deliberately rough: the scheduler only needs an estimate,
    and the response `usage` reconciles it. For a production deployment prefer
    the token-counting endpoint for the prefix and keep this as the fallback.
    """
    def size(x) -> int:
        return len(json.dumps(x, sort_keys=True)) // 4

    sys_tokens = size(body.get("system", "")) + size(body.get("tools", []))
    msgs = body.get("messages", [])
    history = size(msgs[:-1]) if len(msgs) > 1 else 0
    tail = size(msgs[-1]) if msgs else 0
    max_out = int(body.get("max_tokens", 1024))
    if sys_tokens < 512:                     # below the smallest documented minimum
        return None, sys_tokens + history, tail, max_out
    segments = [("system", sys_tokens)]
    if history:
        segments.append(("history", history))
    return segments, 0, tail, max_out


@asynccontextmanager
async def lifespan(app: FastAPI):
    # ONE client for the process. Limits sized well above expected concurrency.
    app.state.metering.client = httpx.AsyncClient(
        base_url=UPSTREAM,
        timeout=httpx.Timeout(600.0, connect=10.0),
        limits=httpx.Limits(max_connections=500, max_keepalive_connections=200),
    )
    try:
        yield
    finally:
        await app.state.metering.client.aclose()


app = FastAPI(title="LLM ramp metering proxy", lifespan=lifespan)
app.state.metering = Metering(
    policy=PolicyConfig(name="shadow", admission=True, cache_gate=True),
    rpm=float(os.environ.get("METERING_RPM", 4000)),
    itpm=float(os.environ.get("METERING_ITPM", 2_000_000)),
    otpm=float(os.environ.get("METERING_OTPM", 400_000)),
)


def _log(rec: dict) -> None:
    try:
        with open(TELEMETRY_PATH, "a") as f:
            f.write(json.dumps(rec, default=float) + "\n")
    except OSError:
        pass


@app.post("/v1/messages")
async def messages(request: Request) -> JSONResponse:
    m: Metering = request.app.state.metering
    c = m.counters
    body = await request.json()
    now = m.clock.now()

    segments, uncached, tail, max_out = estimate(body)
    req = PendingRequest(
        request_id=str(uuid.uuid4()),
        run_id=request.headers.get("x-agent-run-id", "unknown"),
        turn=int(request.headers.get("x-agent-turn", 0)),
        enqueued_at=now,
        segments=segments,
        uncached_prefix_tokens=uncached,
        tail_tokens=tail,
        expected_output=max_out,
    )

    d = decide(now, req, m.state)
    c.would_have[d.action.value] += 1
    if d.action is Action.WAIT:
        c.would_have_waited_sec += d.delay

    # SHADOW MODE: the decision is recorded, never applied.
    if ENFORCE and d.action is Action.DROP:
        return JSONResponse(
            status_code=429,
            content={"type": "error", "error": {"type": "rate_limit_error",
                                                "message": f"shed by metering: {d.reason}"}},
        )

    charge(now, req, m.state)
    c.requests += 1
    c.attempts += 1
    c.in_flight += 1
    c.max_in_flight = max(c.max_in_flight, c.in_flight)

    started = time.monotonic()
    headers = {k: v for k, v in request.headers.items()
               if k.lower() in ("x-api-key", "anthropic-version", "anthropic-beta", "content-type")}
    try:
        resp = await m.client.post("/v1/messages", json=body, headers=headers)
    except httpx.HTTPError as exc:
        c.in_flight -= 1
        _log({"t": now, "request_id": req.request_id, "transport_error": str(exc),
              "would_have": d.action.value, "reason": d.reason})
        return JSONResponse(status_code=502, content={"type": "error", "error": {
            "type": "api_error", "message": f"upstream transport error: {exc}"}})
    finally:
        pass

    c.in_flight -= 1
    m.observe(resp.headers)
    payload = resp.json()
    usage = payload.get("usage", {}) if resp.status_code == 200 else {}
    if resp.status_code == 200:
        c.completed += 1
        c.cache_read_tokens += usage.get("cache_read_input_tokens", 0)
        c.cache_creation_tokens += usage.get("cache_creation_input_tokens", 0)
        c.input_tokens += usage.get("input_tokens", 0)
        c.output_tokens += usage.get("output_tokens", 0)
    elif resp.status_code == 429:
        c.throttled_429 += 1
        code = payload.get("error", {}).get("details", {}).get("error_code")
        if code == "enforced_spend_limit_reached":
            c.spend_cap_429 += 1
    elif resp.status_code == 529:
        c.overloaded_529 += 1

    _log({
        "t": now,
        "request_id": req.request_id,
        "run_id": req.run_id,
        "turn": req.turn,
        "status": resp.status_code,
        "error_code": payload.get("error", {}).get("details", {}).get("error_code"),
        "retry_after": resp.headers.get("retry-after"),
        "workspace_id": resp.headers.get("anthropic-workspace-id"),
        "provider_seconds": time.monotonic() - started,
        "in_flight": c.in_flight,
        "usage": usage,
        "would_have": d.action.value,
        "reason": d.reason,
        "would_have_waited": d.delay,
        "headers": {k: v for k, v in resp.headers.items() if k.startswith("anthropic-ratelimit")},
    })
    return JSONResponse(status_code=resp.status_code, content=payload)


@app.get("/metrics")
def metrics() -> dict:
    c = app.state.metering.counters
    d = asdict(c)
    d["cache_hit_rate"] = c.cache_hit_rate
    d["retry_amplification"] = c.retry_amplification
    d["enforcing"] = ENFORCE
    return d
