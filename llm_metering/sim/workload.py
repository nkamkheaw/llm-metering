"""Deterministic agent-traffic generator.

Models the described shape: several hundred agents, ~10 turns per run, the
whole conversation resent every turn, flat baseline load with work-hours
spikes covering roughly 10% of minutes.
"""

from __future__ import annotations

import math
import random
from dataclasses import dataclass, field


@dataclass
class WorkloadConfig:
    duration: float = 1800.0          # seconds of simulated traffic
    base_rate: float = 1.5            # agent runs started per second, off-peak
    spike_multiplier: float = 8.0
    spike_duty: float = 0.10          # fraction of minutes that are spiky
    spike_period: float = 300.0       # one spike window per 5 simulated minutes
    spike_ramp: float = 2.0           # seconds to reach full spike rate

    turns_mean: float = 10.0
    turns_jitter: int = 3

    system_prefix_tokens: int = 4000  # shared system prompt + tool definitions
    n_prefix_variants: int = 1        # distinct shared prefixes across the fleet
    user_tokens_mean: int = 180
    # Calibrated so an UNCONSTRAINED run reproduces the observed ~1s median.
    # Output size and generation speed are jointly unidentifiable from a
    # median alone -- only their ratio is pinned. Both are swept.
    output_tokens_mean: int = 90
    output_tokens_sd: int = 30
    think_time: float = 0.15          # agent-side gap between turns

    caching_enabled: bool = True
    seed: int = 7


@dataclass
class RunSpec:
    run_id: str
    start_at: float
    turns: int
    system_prefix_id: str
    system_prefix_tokens: int
    user_tokens: list[int] = field(default_factory=list)
    output_tokens: list[int] = field(default_factory=list)
    caching_enabled: bool = True

    def segments_for(self, turn: int) -> tuple[list[tuple[str, int]] | None, int, int]:
        """Return (segments, uncached_prefix_tokens, tail_tokens) for a turn.

        History (everything before this turn's user message) is resent every
        turn. With caching on it is a cacheable prefix under two breakpoints:
        the shared system prompt, then this conversation. With caching off it
        is plain input, and every token of it counts toward ITPM.
        """
        history = sum(self.user_tokens[:turn]) + sum(self.output_tokens[:turn])
        tail = self.user_tokens[turn]
        if not self.caching_enabled:
            return None, self.system_prefix_tokens + history, tail
        segments = [(self.system_prefix_id, self.system_prefix_tokens)]
        if history > 0:
            segments.append((f"conv/{self.run_id}", history))
        return segments, 0, tail


def _rate_at(t: float, cfg: WorkloadConfig) -> float:
    """Piecewise arrival rate: flat baseline with periodic spike windows."""
    phase = t % cfg.spike_period
    spike_len = cfg.spike_duty * cfg.spike_period
    if phase >= cfg.spike_period - spike_len:
        into = phase - (cfg.spike_period - spike_len)
        ramp = min(1.0, into / max(cfg.spike_ramp, 1e-9))
        return cfg.base_rate * (1.0 + (cfg.spike_multiplier - 1.0) * ramp)
    return cfg.base_rate


def generate(cfg: WorkloadConfig) -> list[RunSpec]:
    """Thinning-method Poisson process with a time-varying rate."""
    rng = random.Random(cfg.seed)
    peak = cfg.base_rate * cfg.spike_multiplier
    runs: list[RunSpec] = []
    t = 0.0
    i = 0
    while t < cfg.duration:
        t += rng.expovariate(peak)
        if t >= cfg.duration:
            break
        if rng.random() > _rate_at(t, cfg) / peak:
            continue  # thinned out
        turns = max(1, int(cfg.turns_mean) + rng.randint(-cfg.turns_jitter, cfg.turns_jitter))
        variant = rng.randrange(cfg.n_prefix_variants)
        runs.append(
            RunSpec(
                run_id=f"r{i}",
                start_at=t,
                turns=turns,
                system_prefix_id=f"sys/v{variant}",
                system_prefix_tokens=cfg.system_prefix_tokens,
                user_tokens=[
                    max(20, int(rng.gauss(cfg.user_tokens_mean, cfg.user_tokens_mean * 0.3)))
                    for _ in range(turns)
                ],
                output_tokens=[
                    max(20, int(rng.gauss(cfg.output_tokens_mean, cfg.output_tokens_sd)))
                    for _ in range(turns)
                ],
                caching_enabled=cfg.caching_enabled,
            )
        )
        i += 1
    return runs


def offered_load(cfg: WorkloadConfig, runs: list[RunSpec]) -> dict:
    """Back-of-envelope offered load, for sanity-checking a scenario."""
    total_turns = sum(r.turns for r in runs)
    uncached_input = 0
    cached_input = 0
    output = 0
    for r in runs:
        for k in range(r.turns):
            _seg, uncached, tail = r.segments_for(k)
            history = sum(r.user_tokens[:k]) + sum(r.output_tokens[:k])
            if r.caching_enabled:
                cached_input += history + r.system_prefix_tokens
            uncached_input += uncached + tail
            output += r.output_tokens[k]
    minutes = cfg.duration / 60.0
    return {
        "runs": len(runs),
        "turns": total_turns,
        "mean_rpm": total_turns / minutes,
        "uncached_itpm_if_no_cache": uncached_input / minutes,
        "cacheable_tokens": cached_input,
        "mean_otpm": output / minutes,
    }
