from __future__ import annotations

import json
import unittest

from server.models import (
    ConstraintOperator,
    FitnessMetric,
    JobConfig,
    MetricConstraint,
    RankingRule,
    SortDirection,
)
from server.ranking import best_ranked_pass, build_ranked_population, rank_pass_rows


def make_row(
    pass_id: str,
    result: dict,
    params: dict | None = None,
    status: str = "done",
) -> dict:
    return {
        "id": pass_id,
        "job_id": "job-1",
        "params_json": json.dumps(params or {"p": pass_id}),
        "status": status,
        "result_json": json.dumps(result),
        "started_at": None,
        "finished_at": f"2026-04-04T00:00:0{pass_id[-1]}Z" if pass_id[-1].isdigit() else None,
    }


class RankingTests(unittest.TestCase):
    def test_mixed_direction_rules_are_applied_in_order(self) -> None:
        config = JobConfig(
            ranking=[
                RankingRule(metric="profit_factor", direction=SortDirection.desc),
                RankingRule(metric="max_drawdown_pct", direction=SortDirection.asc),
                RankingRule(metric="average_trade", direction=SortDirection.desc),
            ]
        )
        rows = [
            make_row("pass-1", {"profit_factor": 1.5, "max_drawdown_pct": 18, "average_trade": 20}),
            make_row("pass-2", {"profit_factor": 1.5, "max_drawdown_pct": 12, "average_trade": 14}),
            make_row("pass-3", {"profit_factor": 1.3, "max_drawdown_pct": 8, "average_trade": 25}),
        ]

        ranked = rank_pass_rows(rows, config)

        self.assertEqual([record.row["id"] for record in ranked], ["pass-2", "pass-1", "pass-3"])

    def test_constraints_push_ineligible_rows_behind_eligible_ones(self) -> None:
        config = JobConfig(
            ranking=[RankingRule(metric="profit_factor", direction=SortDirection.desc)],
            constraints=[
                MetricConstraint(metric="total_trades", operator=ConstraintOperator.gte, value=100),
                MetricConstraint(metric="max_drawdown_pct", operator=ConstraintOperator.lte, value=25),
            ],
        )
        rows = [
            make_row("pass-1", {"profit_factor": 2.0, "total_trades": 150, "max_drawdown_pct": 30}),
            make_row("pass-2", {"profit_factor": 1.4, "total_trades": 120, "max_drawdown_pct": 20}),
            make_row("pass-3", {"profit_factor": 1.8, "total_trades": 80, "max_drawdown_pct": 18}),
        ]

        ranked = rank_pass_rows(rows, config, respect_constraints=True, include_ineligible=True)

        self.assertEqual(ranked[0].row["id"], "pass-2")
        self.assertTrue(ranked[0].eligible)
        self.assertFalse(ranked[1].eligible)
        self.assertFalse(ranked[2].eligible)

    def test_legacy_fitness_fallback_still_works(self) -> None:
        config = JobConfig(fitness=FitnessMetric.win_rate)
        rows = [
            make_row("pass-1", {"win_rate": 51}),
            make_row("pass-2", {"win_rate": 63}),
        ]

        best = best_ranked_pass(rows, config)

        self.assertIsNotNone(best)
        self.assertEqual(best.row["id"], "pass-2")

    def test_genetic_population_uses_rank_scores_and_excludes_ineligible(self) -> None:
        config = JobConfig(
            ranking=[RankingRule(metric="profit_factor", direction=SortDirection.desc)],
            constraints=[
                MetricConstraint(metric="total_trades", operator=ConstraintOperator.gte, value=100),
            ],
        )
        rows = [
            make_row("pass-1", {"profit_factor": 2.0, "total_trades": 120}),
            make_row("pass-2", {"profit_factor": 1.5, "total_trades": 110}),
            make_row("pass-3", {"profit_factor": 9.9, "total_trades": 2}),
        ]

        population = build_ranked_population(rows, config)

        self.assertEqual(population, [({"p": "pass-1"}, 2.0), ({"p": "pass-2"}, 1.0)])

    def test_best_pass_falls_back_to_ineligible_rows_when_needed(self) -> None:
        config = JobConfig(
            ranking=[RankingRule(metric="profit_factor", direction=SortDirection.desc)],
            constraints=[
                MetricConstraint(metric="total_trades", operator=ConstraintOperator.gte, value=100),
            ],
        )
        rows = [
            make_row("pass-1", {"profit_factor": 2.0, "total_trades": 50}),
            make_row("pass-2", {"profit_factor": 1.0, "total_trades": 10}),
        ]

        best = best_ranked_pass(rows, config)

        self.assertIsNotNone(best)
        self.assertEqual(best.row["id"], "pass-1")
        self.assertFalse(best.eligible)


if __name__ == "__main__":
    unittest.main()
