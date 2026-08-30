# llm-metering

Ramp metering for LLM API calls: a scheduler every request passes through, a
simulator that runs the same decision code against a fake clock and fake
network, and a web UI for comparing candidate explanations and scheduler
policies.

## What problem this solves

A fleet of agents shares one provider quota. The dashboard tracks requests per
minute and shows plenty of headroom, yet tail latency is bad. Requests per
minute is the wrong signal: the provider meters three limits independently, and
the one that binds is usually not the one being watched.

This harness enumerates candidate causes, sweeps each one's governing
parameter, and asks which of them can even produce the latency shape you
actually observe. It is built to return inconvenient answers — "no limiter is
tripped", "retries are fine", "the scheduler makes it worse" — and there are
tests asserting each of those verdicts is reachable.

The worked example throughout uses a fleet of several hundred agents at roughly
ten turns per run, with a ~1s typical call and a ~40s worst-1% at peak. Those
numbers live in `llm_metering/scenarios.py` (`TARGET_P50`, `TARGET_P99_PEAK`)
and `llm_metering/sim/workload.py`; change them to match your own workload.

## Layout

    llm_metering/
      clock.py            Clock protocol; RealClock and a deterministic SimClock
      limits.py           Token buckets and the acceleration guard
      policy.py           decide() -> Send | Wait | Drop; shared by both drivers
      scenarios.py        The candidate causes and their swept parameters
      sweep.py            Step 2a elimination, Step 2b policy comparison
      signatures.py       Minimal telemetry that separates the candidates
      report.py           Findings, with mandatory sensitivity labelling
      exec_summary.py     Distils the sweep into plain-language figures
      proxy.py            Shadow-mode metering proxy (FastAPI)
      sim/provider.py     Fake provider: documented rate-limit + cache semantics
      sim/workload.py     Agent traffic generator
      sim/runner.py       Discrete-event simulation
      calibrate/headers.py  Turns persisted ratelimit headers into a verdict
      ui/                 Policy explorer (/) and plain-language summary (/overview)
    main.py               ASGI entry point (main:app)
    precompute.py         Builds the shipped warm cache

## Running

    python -m pytest tests/ -q            # 44 tests
    python -m llm_metering.sweep 600      # Step 2a  -> sweep_2a.json
    python run_2b.py                      # Step 2b  -> sweep_2b.json
    python -m llm_metering.signatures     # Step 3   -> signatures.json
    python -m llm_metering.report         # findings, with sensitivity labels
    python -m llm_metering.exec_summary   # -> exec_summary.json (powers /overview)
    python precompute.py                  # -> precomputed.json.gz (warm cache)
    uvicorn main:app --port 8077          # the UI

The generated JSON files are committed so a fresh clone runs immediately. Re-run
the commands above after changing scenarios, workload parameters or limits.

### UI routes

    /            Scheduler policy explorer — the working tool. Pick a candidate,
                 set its parameter, run policies against it, read the trade-off.
                 Every control lives in the query string, so any view can be
                 copied and reopened exactly:
                   /?scenario=cache_herd&param=32&policies=none,admission
                    &retries=3,7&duration=600
                 With no query string the page shows the PRODUCTION BASELINE:
                 no scheduler, current retry depth, run against whichever
                 candidate reproduces the observed latency shape most closely.
                 That default is computed from sweep_2a.json, and the view bar
                 labels it "closest, not confirmed" — several candidates fit,
                 and a default must not read as a conclusion.
                 Unknown scenarios, out-of-range parameters and retired policy
                 names degrade to the nearest valid value and the URL is
                 rewritten, so a stale shared link still opens on something sane.
    /overview    Plain-language findings for a non-technical reader.
    /brief       The built leadership brief (run `python build_artifact.py` first).
    /health      Liveness, host speed factor, and cache statistics.

Results stream in one simulation at a time (`/api/run/stream`, NDJSON) and the
page repaints as each lands, so a slow batch fills in progressively instead of
freezing until the last one finishes. Each simulation runs in a worker thread,
so one person's long comparison does not block everyone else's requests — with
a single worker and a blocking endpoint, it would.

While a batch runs, the results area shows a progress banner and the table
lists the runs that have not arrived yet by name, one marked `computing…` and
the rest `queued`. Progress only in the sidebar goes unread: the eye is on the
table where the rows are appearing.

Every change runs automatically, debounced 400ms — there is no cost cap. One
existed while the whole batch had to finish before anything appeared, but a
superseded run is aborted and, measured, the server finishes the simulation
already in flight and then stops. A cancelled selection costs one simulation,
not a batch, so gating heavy selections behind a button would protect against
nothing. The estimate is still shown so a long run is chosen knowingly.

Duration options all cover at least one busy period. Shorter runs would contain
none, and `p99_latency_peak` would then be 0.0 by construction rather than by
measurement — `peak_requests` in the summary guards that, and both the UI and
`matches_target()` check it.

Candidate and policy names are explained behind a "?" beside each control,
rather than permanently on the page: needed once, in the way every time after.
Policy text comes from `PolicyConfig.description` in `policy.py`, so the help
cannot quietly describe behaviour a policy no longer has; scenario text reuses
the plain-language bodies in `exec_summary.py`. Chips also carry the same text
as a native tooltip.

The trade-off chart is the policy-choosing view: there is no single best policy,
so it plots completed work against worst-case wait and marks the runs nothing
else beats on both axes at once. Picking among those is a judgement about
whether slow or failed is worse for a given agent.

### Speed

Simulations are CPU-bound and deterministic. Three things exploit that:

* **Per-simulation caching.** Keyed per simulation rather than per request, so
  adding one policy to an existing comparison computes only that policy.
* **A precomputed cache** (`precomputed.json.gz`, built by `precompute.py`) is
  loaded at startup, so common views cost nothing and survive restarts. It
  covers every scenario at its default and matching parameter, across all
  policies, retry depths and durations. The runtime cache cap grows to fit it:
  a cap smaller than the preloaded set silently evicts part of it, and a
  partially-loaded cache raises rather than serving quietly-slow views.
* **A measured host speed factor.** The server times its own runs and reports
  `cost_factor`; the UI multiplies its auto-run estimate by it and seeds its
  "already computed" set from the server's cache keys. Estimating from a
  hardcoded local timing would gate precomputed views behind the button.

More CPU cores do not help on their own: the app runs as a single process and
the simulation is pure-Python CPU work, so it is GIL-bound. To use more cores
you would need to fan simulations across processes (`ProcessPoolExecutor`)
*and* provision cores to feed them — either alone buys nothing.

## Deployment

The app is a standard ASGI application. Any host that can run

    uvicorn main:app --host 0.0.0.0 --port $PORT

will serve it; `requirements.txt` pins the runtime dependencies and `main.py` is
the entry point. Three things are worth setting wherever you deploy:

* **Keep the instance warm.** A platform that unloads idle workers will make the
  first visitor after a quiet period pay a multi-second start. Most PaaS hosts
  have an "always on" style setting; it usually requires a paid tier.
* **Ship `precomputed.json.gz`.** It is loaded at startup, so the common views
  cost the instance nothing and survive restarts — unlike a warm-up that has to
  be re-earned after every deploy.
* **Verify a *new* payload correctly.** Many platforms keep the old worker
  serving until the replacement is warm, so a health check straight after a
  deploy returns unbroken 200s and proves nothing. To time a genuine cold start,
  stop the site, poll until it stops answering, then start it — with the
  stopwatch running before the start call, since that call usually blocks until
  the site is up.

`/health` reports `cpu_count`, the measured `cost_factor` and cache statistics,
which is enough to tell whether a slow instance is the cause of a slow UI.

## Provider semantics this depends on

`sim/provider.py` is covered by its own test suite because everything else rests
on it. Each rule is cited inline:

- ITPM counts `input_tokens` + `cache_creation_input_tokens`;
  `cache_read_input_tokens` does not (Haiku 3.5 excepted)
- Token buckets replenish continuously, not on fixed windows
- Cache TTL is measured from the *start* of the writing or reading request, and
  a read refreshes it for free
- A cache entry is not readable until the writing response begins streaming, so
  concurrent same-prefix requests all miss and all pay the write
- Minimum cacheable prefix is model-dependent; below it, nothing caches and no
  error is raised
- Spend-cap 429s carry no `retry-after` and retrying never succeeds
- Acceleration limits reject sharp ramps while every level limiter is healthy

Sources: [rate limits](https://platform.claude.com/docs/en/api/rate-limits),
[prompt caching](https://platform.claude.com/docs/en/build-with-claude/prompt-caching),
[errors](https://platform.claude.com/docs/en/api/errors),
[Rate Limits API](https://platform.claude.com/docs/en/manage-claude/rate-limits-api).

## Known fidelity limits

- OTPM is debited at completion rather than progressively as tokens stream.
- Service time is calibrated so an unconstrained run reproduces the observed
  median. Output size and generation speed are jointly unidentifiable from a
  median alone; only their ratio is pinned.
- Retry-after uses the bucket's own time-to-refill, capped at 60s.
- All ceilings are assumed until `calibrate/headers.py` is fed real data.

Everything the simulator reports is labelled parameter-robust (holds across the
whole swept range) or parameter-dependent (holds only in part of it). An
uncalibrated simulator will happily confirm whatever theory seeded it; the
labelling is the guard against that.
