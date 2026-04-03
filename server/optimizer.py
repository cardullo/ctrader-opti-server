"""
Parameter sweep optimizer — grid, random, and genetic strategies.

Each function returns a list of parameter dictionaries, where each dict
maps parameter names to concrete values for a single backtest pass.
"""

from __future__ import annotations

import itertools
import json
import math
import random
from typing import Any, Dict, List, Optional, Tuple

from server.models import ParamRange


# ── Public API ──────────────────────────────────────────────────────────────

def generate_combinations(
    params: Dict[str, ParamRange],
    strategy: str,
    max_passes: int,
) -> List[Dict[str, float]]:
    """
    Generate parameter combinations according to the chosen strategy.

    Returns at most *max_passes* dicts, each mapping param names → values.
    """
    if strategy == "grid":
        return _grid(params, max_passes)
    elif strategy == "random":
        return _random(params, max_passes)
    elif strategy == "genetic":
        # For genetic, return only the initial population.
        # Subsequent generations are created via `next_generation()`.
        return _random(params, min(max_passes, 20))
    else:
        raise ValueError(f"Unknown strategy: {strategy}")


# ── Grid ────────────────────────────────────────────────────────────────────

def _grid(params: Dict[str, ParamRange], max_passes: int) -> List[Dict[str, float]]:
    """Cartesian product of all parameter ranges, capped at *max_passes*."""
    names = list(params.keys())
    ranges = [_range_values(params[n]) for n in names]
    combos: List[Dict[str, float]] = []
    for values in itertools.product(*ranges):
        if len(combos) >= max_passes:
            break
        combos.append(dict(zip(names, values)))
    return combos


# ── Random ──────────────────────────────────────────────────────────────────

def _random(params: Dict[str, ParamRange], max_passes: int) -> List[Dict[str, float]]:
    """Random uniform sample, each value snapped to step increments."""
    names = list(params.keys())
    combos: List[Dict[str, float]] = []
    seen: set = set()
    attempts = 0
    max_attempts = max_passes * 20  # avoid infinite loop with small spaces
    while len(combos) < max_passes and attempts < max_attempts:
        attempts += 1
        individual = {}
        for name in names:
            individual[name] = _random_value(params[name])
        key = tuple(sorted(individual.items()))
        if key not in seen:
            seen.add(key)
            combos.append(individual)
    return combos


# ── Genetic helpers ─────────────────────────────────────────────────────────

def next_generation(
    params: Dict[str, ParamRange],
    population_with_fitness: List[Tuple[Dict[str, float], float]],
    generation_size: int = 20,
    mutation_rate: float = 0.2,
) -> List[Dict[str, float]]:
    """
    Given a scored population [(params_dict, fitness_score), ...],
    produce the next generation via selection, crossover, and mutation.

    Selection: keep top 50 %
    Crossover: uniform crossover between random parent pairs
    Mutation: each gene has `mutation_rate` chance of ±1 step
    """
    # Sort by fitness descending and select top half
    population_with_fitness.sort(key=lambda x: x[1], reverse=True)
    top_n = max(2, len(population_with_fitness) // 2)
    parents = [p for p, _ in population_with_fitness[:top_n]]

    children: List[Dict[str, float]] = []
    names = list(params.keys())

    while len(children) < generation_size:
        p1, p2 = random.sample(parents, 2)
        child: Dict[str, float] = {}
        for name in names:
            # Uniform crossover
            child[name] = random.choice([p1[name], p2[name]])
            # Mutation
            if random.random() < mutation_rate:
                pr = params[name]
                delta = random.choice([-1, 1]) * pr.step
                child[name] = _clamp(child[name] + delta, pr)
        children.append(child)

    return children


# ── Internal helpers ────────────────────────────────────────────────────────

def _range_values(pr: ParamRange) -> List[float]:
    """Generate discrete values for a parameter range."""
    values: List[float] = []
    steps = int(round((pr.max - pr.min) / pr.step)) + 1
    for i in range(steps):
        v = pr.min + i * pr.step
        if v > pr.max + 1e-9:
            break
        values.append(round(v, 8))
    return values


def _random_value(pr: ParamRange) -> float:
    """Pick a random value from the parameter range, snapped to step."""
    steps = int(round((pr.max - pr.min) / pr.step))
    n = random.randint(0, steps)
    return round(pr.min + n * pr.step, 8)


def _clamp(value: float, pr: ParamRange) -> float:
    """Clamp value within [min, max] and snap to step."""
    value = max(pr.min, min(pr.max, value))
    steps = round((value - pr.min) / pr.step)
    return round(pr.min + steps * pr.step, 8)
