"""Step 3: what observable separates the surviving candidates?

Builds a signature per candidate from the sweep output, then searches for the
SMALLEST set of observable fields that tells every surviving candidate apart.
That set -- not a wishlist -- is what needs to be instrumented in production.
"""

from __future__ import annotations

import itertools
import json

# Each field: how to read it from a sweep point, and how to bucket it into the
# coarse category an operator could actually read off a dashboard.
FIELDS = {
    "headroom_requests": (
        lambda p: p["min_headroom"]["requests"],
        lambda v: "collapsed" if v < 0.05 else ("healthy" if v > 0.5 else "partial"),
        "anthropic-ratelimit-requests-remaining / -limit",
    ),
    "headroom_input_tokens": (
        lambda p: p["min_headroom"]["input_tokens"],
        lambda v: "collapsed" if v < 0.05 else ("healthy" if v > 0.5 else "partial"),
        "anthropic-ratelimit-input-tokens-remaining / -limit",
    ),
    "headroom_output_tokens": (
        lambda p: p["min_headroom"]["output_tokens"],
        lambda v: "collapsed" if v < 0.05 else ("healthy" if v > 0.5 else "partial"),
        "anthropic-ratelimit-output-tokens-remaining / -limit",
    ),
    "cache_hit_rate": (
        lambda p: p["cache_hit_rate"],
        lambda v: "none" if v < 0.05 else ("working" if v > 0.5 else "partial"),
        "usage.cache_read_input_tokens / total input",
    ),
    "throttled": (
        lambda p: sum(p["throttle_by_cause"].values()),
        lambda v: "yes" if v > 0 else "no",
        "count of 429/529 responses",
    ),
    "retry_amplification": (
        lambda p: p["retry_amplification"],
        lambda v: "high" if v > 1.3 else "low",
        "attempts / successes",
    ),
    "max_in_flight": (
        lambda p: p["max_in_flight"],
        lambda v: "capped" if v < 200 else "uncapped",
        "concurrent in-flight requests",
    ),
}


def representative(result: dict) -> dict | None:
    """The point that best represents this candidate: a plausible in-band match
    if one exists, otherwise its closest plausible approach."""
    hits = [p for p in result["points"] if p["matches_target"] and p["plausible"]]
    if hits:
        return min(hits, key=lambda p: p["distance"])
    return result.get("best")


def signature(point: dict) -> dict[str, str]:
    return {name: bucket(read(point)) for name, (read, bucket, _src) in FIELDS.items()}


def build(results: list[dict]) -> dict:
    surviving = [r for r in results if r["verdict"] in ("SURVIVES", "DOWNGRADED", "NEAR MISS")]
    sigs = {}
    raw = {}
    for r in results:
        pt = representative(r)
        if pt is None:
            continue
        sigs[r["key"]] = signature(pt)
        raw[r["key"]] = {n: read(pt) for n, (read, _b, _s) in FIELDS.items()}

    keys = [r["key"] for r in surviving if r["key"] in sigs]

    def separates(fields: tuple[str, ...]) -> bool:
        seen = set()
        for k in keys:
            v = tuple(sigs[k][f] for f in fields)
            if v in seen:
                return False
            seen.add(v)
        return True

    # A field set that separates candidates only at one sampled parameter is a
    # coincidence, not a signature. Require the separation to hold across EVERY
    # point where a candidate reproduces the observed shape: build the full set
    # of signature vectors per candidate and demand they be disjoint.
    def vectors(key: str, fields: tuple[str, ...]) -> set[tuple[str, ...]]:
        r = next(x for x in results if x["key"] == key)
        pts = [p for p in r["points"] if p["matches_target"] and p["plausible"]]
        if not pts:
            pts = [representative(r)]
        return {tuple(signature(p)[f] for f in fields) for p in pts if p}

    def search(target_keys: list[str]) -> tuple[str, ...] | None:
        for size in range(1, len(FIELDS) + 1):
            for combo in itertools.combinations(FIELDS, size):
                vs = [vectors(k, combo) for k in target_keys]
                if all(
                    not (vs[i] & vs[j])
                    for i in range(len(vs))
                    for j in range(i + 1, len(vs))
                ):
                    return combo
        return None

    minimal = search(keys)
    # A candidate ruled out by the model is ruled out by ARGUMENT, not by
    # measurement. Instrumenting only enough to separate the survivors makes
    # that elimination permanently unfalsifiable in production. The robust set
    # separates all five, so the ruled-out ones stay checkable against reality.
    robust = search(list(sigs))

    return {
        "surviving": keys,
        "robust_field_set": list(robust) if robust else None,
        "robust_sources": {n: FIELDS[n][2] for n in (robust or ())},
        "all_signatures": sigs,
        "raw_values": raw,
        "minimal_field_set": list(minimal) if minimal else None,
        "sources": {n: FIELDS[n][2] for n in (minimal or ())},
    }


def format_table(results: list[dict], sig: dict) -> str:
    order = [r["key"] for r in results if r["key"] in sig["all_signatures"]]
    fields = list(FIELDS)
    w = max(len(k) for k in order) + 2
    out = ["STEP 3 -- DISCRIMINATING SIGNATURES", ""]
    header = "candidate".ljust(w) + "".join(f.replace("headroom_", "hd_")[:13].rjust(15) for f in fields)
    out.append(header)
    out.append("-" * len(header))
    for k in order:
        row = k.ljust(w) + "".join(sig["all_signatures"][k][f].rjust(15) for f in fields)
        out.append(row)
    out.append("")
    ms = sig["minimal_field_set"]
    out.append(f"Smallest set separating the SURVIVING candidates: {ms}")
    for f in ms or []:
        out.append(f"   {f:<26} <- {FIELDS[f][2]}")
    out.append("")
    rs = sig["robust_field_set"]
    out.append(f"Smallest set separating ALL FIVE (keeps the eliminations falsifiable): {rs}")
    for f in rs or []:
        out.append(f"   {f:<26} <- {FIELDS[f][2]}")
    out.append("")
    out.append("RECOMMENDED: instrument the robust set. The extra field is cheap, and")
    out.append("without it a candidate eliminated by model reasoning can never be")
    out.append("checked against production reality.")
    return "\n".join(out)


if __name__ == "__main__":
    results = json.load(open("sweep_2a.json"))
    sig = build(results)
    print(format_table(results, sig))
    with open("signatures.json", "w") as f:
        json.dump(sig, f, indent=1, default=float)
