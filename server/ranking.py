"""Ranking helpers for optimization passes."""

from __future__ import annotations

import json
from dataclasses import dataclass
from enum import Enum
from typing import Any, Dict, Iterable, List, Optional, Sequence, Tuple

from server.models import (
    ConstraintOperator,
    JobConfig,
    MetricConstraint,
    RankingRule,
    SortDirection,
)


@dataclass(frozen=True)
class RankedPass:
    row: Dict[str, Any]
    params: Dict[str, Any]
    result: Dict[str, Any]
    eligible: bool


def effective_ranking_rules(
    config: JobConfig,
    sort_by: Optional[str] = None,
) -> List[RankingRule]:
    """Return the ranking rules for a job, falling back to legacy fitness."""
    if sort_by:
        return [RankingRule(metric=sort_by, direction=SortDirection.desc)]
    if config.ranking:
        return list(config.ranking)
    fitness = config.fitness.value if isinstance(config.fitness, Enum) else str(config.fitness)
    return [RankingRule(metric=fitness, direction=SortDirection.desc)]


def effective_constraints(
    config: JobConfig,
    respect_constraints: bool = True,
) -> List[MetricConstraint]:
    """Return the effective constraints for ranking."""
    if not respect_constraints:
        return []
    return list(config.constraints)


def rank_pass_rows(
    rows: Iterable[Dict[str, Any]],
    config: JobConfig,
    *,
    sort_by: Optional[str] = None,
    respect_constraints: bool = True,
    include_ineligible: bool = True,
) -> List[RankedPass]:
    """Rank pass rows using ordered rules and optional hard constraints."""
    rules = effective_ranking_rules(config, sort_by=sort_by)
    constraints = effective_constraints(config, respect_constraints=respect_constraints)

    ranked_rows: List[RankedPass] = []
    for row in rows:
        params = _load_json_object(row.get("params_json"))
        result = _load_json_object(row.get("result_json"))
        eligible = _passes_constraints(result, constraints)
        if not eligible and not include_ineligible:
            continue
        ranked_rows.append(
            RankedPass(
                row=dict(row),
                params=params,
                result=result,
                eligible=eligible,
            )
        )

    return sorted(ranked_rows, key=lambda record: _sort_key(record, rules))


def best_ranked_pass(
    rows: Iterable[Dict[str, Any]],
    config: JobConfig,
    *,
    sort_by: Optional[str] = None,
    respect_constraints: bool = True,
) -> Optional[RankedPass]:
    """Return the single best ranked pass, falling back to ineligible rows if needed."""
    ranked = rank_pass_rows(
        rows,
        config,
        sort_by=sort_by,
        respect_constraints=respect_constraints,
        include_ineligible=True,
    )
    return ranked[0] if ranked else None


def build_ranked_population(
    rows: Iterable[Dict[str, Any]],
    config: JobConfig,
) -> List[Tuple[Dict[str, Any], float]]:
    """Build a rank-based population score list for the genetic optimizer."""
    ranked = rank_pass_rows(
        rows,
        config,
        respect_constraints=True,
        include_ineligible=False,
    )
    total = len(ranked)
    return [
        (record.params, float(total - index))
        for index, record in enumerate(ranked)
    ]


def format_ranking_summary(result: Dict[str, Any], rules: Sequence[RankingRule]) -> str:
    """Create a compact summary string using the active ranking rules."""
    parts: List[str] = []
    for rule in rules:
        value = result.get(rule.metric)
        if value is None:
            continue
        if isinstance(value, float):
            formatted = f"{value:.2f}"
        else:
            formatted = str(value)
        parts.append(f"{rule.metric}={formatted}")
    return " | ".join(parts)


def _sort_key(record: RankedPass, rules: Sequence[RankingRule]) -> tuple:
    key_parts = [0 if record.eligible else 1]
    for rule in rules:
        value = _coerce_float(record.result.get(rule.metric))
        if value is None:
            key_parts.append(float("inf"))
            continue
        key_parts.append(-value if rule.direction == SortDirection.desc else value)
    key_parts.append(record.row.get("finished_at") or "")
    key_parts.append(record.row.get("id") or "")
    return tuple(key_parts)


def _passes_constraints(result: Dict[str, Any], constraints: Sequence[MetricConstraint]) -> bool:
    if not constraints:
        return True

    for constraint in constraints:
        actual = _coerce_float(result.get(constraint.metric))
        if actual is None:
            return False
        expected = float(constraint.value)
        if constraint.operator == ConstraintOperator.gt and not actual > expected:
            return False
        if constraint.operator == ConstraintOperator.gte and not actual >= expected:
            return False
        if constraint.operator == ConstraintOperator.lt and not actual < expected:
            return False
        if constraint.operator == ConstraintOperator.lte and not actual <= expected:
            return False
        if constraint.operator == ConstraintOperator.eq and not actual == expected:
            return False
    return True


def _coerce_float(value: Any) -> Optional[float]:
    try:
        return float(value)
    except (TypeError, ValueError):
        return None


def _load_json_object(payload: Any) -> Dict[str, Any]:
    if isinstance(payload, dict):
        return dict(payload)
    if isinstance(payload, str) and payload:
        try:
            data = json.loads(payload)
        except json.JSONDecodeError:
            return {}
        if isinstance(data, dict):
            return data
    return {}
