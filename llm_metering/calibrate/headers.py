"""Step 5: replace assumed parameters with measured ones.

Reads persisted `anthropic-ratelimit-*` response headers -- which the API
returns on EVERY response, not only on 429s -- and derives:

  * the real ceiling for each limiter (the `-limit` fields)
  * headroom over time, 1 - remaining/limit, at whatever resolution the log has
  * which limiter is binding, via the generic `anthropic-ratelimit-tokens-*`
    triplet, documented as reporting the most restrictive limit in effect
  * whether a workspace override is the real ceiling, by grouping on
    `anthropic-workspace-id`

This is the whole reason the header log is worth mining before any production
change ships: it answers "which limiter" without touching the application.

What it CANNOT answer: cache hit rate, and therefore whether ITPM pressure
comes from broken caching or from concurrent cache writes. That needs
`usage.cache_read_input_tokens`, which is not in any header.
"""

from __future__ import annotations

import json
import statistics
from dataclasses import dataclass, field

LIMITER_HEADERS = {
    "requests": "requests",
    "input_tokens": "input-tokens",
    "output_tokens": "output-tokens",
}


@dataclass
class Calibration:
    ceilings: dict[str, float] = field(default_factory=dict)
    min_headroom: dict[str, float] = field(default_factory=dict)
    p05_headroom: dict[str, float] = field(default_factory=dict)
    binding_limiter: str | None = None
    binding_evidence: str = ""
    throttled: int = 0
    spend_cap_hits: int = 0
    overloaded: int = 0
    workspaces: dict[str, dict] = field(default_factory=dict)
    samples: int = 0
    verdict: str = ""
    unanswerable: list[str] = field(default_factory=list)

    def to_dict(self) -> dict:
        d = dict(self.__dict__)
        return d


def load_jsonl(path: str) -> list[dict]:
    with open(path) as f:
        return [json.loads(line) for line in f if line.strip()]


def analyse(records: list[dict]) -> Calibration:
    cal = Calibration(samples=len(records))
    series: dict[str, list[float]] = {k: [] for k in LIMITER_HEADERS}
    generic_limits: dict[float, int] = {}

    for rec in records:
        h = rec.get("headers", {})
        for key, wire in LIMITER_HEADERS.items():
            lim = h.get(f"anthropic-ratelimit-{wire}-limit")
            rem = h.get(f"anthropic-ratelimit-{wire}-remaining")
            if lim is None or rem is None:
                continue
            lim_f, rem_f = float(lim), float(rem)
            if lim_f <= 0:
                continue
            cal.ceilings[key] = lim_f
            series[key].append(max(0.0, min(1.0, rem_f / lim_f)))

        gl = h.get("anthropic-ratelimit-tokens-limit")
        if gl is not None:
            generic_limits[float(gl)] = generic_limits.get(float(gl), 0) + 1

        if rec.get("status") == 429:
            cal.throttled += 1
            if rec.get("error_code") == "enforced_spend_limit_reached":
                cal.spend_cap_hits += 1
        elif rec.get("status") == 529:
            cal.overloaded += 1

        ws = rec.get("workspace_id")
        if ws:
            w = cal.workspaces.setdefault(ws, {"count": 0, "ceilings": {}})
            w["count"] += 1
            for key in LIMITER_HEADERS:
                if key in cal.ceilings:
                    w["ceilings"][key] = cal.ceilings[key]

    for key, vals in series.items():
        if vals:
            cal.min_headroom[key] = min(vals)
            cal.p05_headroom[key] = sorted(vals)[max(0, int(0.05 * len(vals)) - 1)]

    # The generic triplet names the most restrictive limiter in effect. Match
    # its reported ceiling back to one of the three specific limiters.
    if generic_limits:
        dominant = max(generic_limits, key=lambda k: generic_limits[k])
        for key, ceiling in cal.ceilings.items():
            if abs(ceiling - dominant) < 1e-6:
                cal.binding_limiter = key
                cal.binding_evidence = (
                    f"anthropic-ratelimit-tokens-limit reported {dominant:,.0f} "
                    f"in {generic_limits[dominant]}/{cal.samples} responses, "
                    f"matching the {key} ceiling"
                )
                break

    collapsed = [k for k, v in cal.min_headroom.items() if v < 0.05]
    if not collapsed and cal.throttled == 0:
        cal.verdict = (
            "NO LIMITER TRIPPED. No provider limiter approached empty and nothing "
            "was throttled. Latency is not coming from provider rate limiting -- "
            "look at client-side queueing (connection pool, in-flight concurrency)."
        )
    elif not collapsed and cal.throttled > 0:
        cal.verdict = (
            "THROTTLED WITH HEALTHY HEADROOM. 429s occurred while every level "
            "limiter had room. Consistent with an acceleration (rate-of-change) "
            "limit; check whether the 429s cluster at the onset of load ramps."
        )
    else:
        cal.verdict = f"LIMITER COLLAPSED: {', '.join(collapsed)}."
        if "input_tokens" in collapsed:
            cal.unanswerable.append(
                "Headers cannot say whether ITPM pressure is broken caching or "
                "concurrent cache writes. Instrument usage.cache_read_input_tokens."
            )
    if cal.spend_cap_hits:
        cal.verdict += (
            f" NOTE: {cal.spend_cap_hits} spend-cap 429s (no retry-after); "
            "retrying those never succeeds."
        )
    return cal


def format_report(cal: Calibration) -> str:
    out = ["CALIBRATION FROM anthropic-ratelimit-* HEADERS", ""]
    out.append(f"  samples: {cal.samples}")
    out.append(f"  {'limiter':<16}{'ceiling':>14}{'min headroom':>15}{'p05 headroom':>15}")
    for k in LIMITER_HEADERS:
        if k in cal.ceilings:
            out.append(
                f"  {k:<16}{cal.ceilings[k]:>14,.0f}"
                f"{cal.min_headroom.get(k, float('nan')):>15.3f}"
                f"{cal.p05_headroom.get(k, float('nan')):>15.3f}"
            )
    out.append("")
    out.append(f"  binding limiter: {cal.binding_limiter or 'undetermined'}")
    if cal.binding_evidence:
        out.append(f"    evidence: {cal.binding_evidence}")
    out.append(f"  throttled responses: {cal.throttled}  (spend-cap {cal.spend_cap_hits}, 529 {cal.overloaded})")
    out.append("")
    out.append(f"  VERDICT: {cal.verdict}")
    for u in cal.unanswerable:
        out.append(f"  NOT ANSWERABLE FROM HEADERS: {u}")
    return "\n".join(out)
