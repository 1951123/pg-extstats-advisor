"""Profile exact first-round pruning against the frozen M2.7 Census run.

This tool intentionally executes only the initial greedy ADD round.  It does
not resume M2.7, run ANALYZE, or write into the M2.7 artifact directory.
"""

from __future__ import annotations

import argparse
import json
import time
from decimal import Decimal
from pathlib import Path
from typing import Any

import psycopg

from pg_extstats_advisor.cost.model import MaintenanceBudget
from pg_extstats_advisor.evaluator.native import NativeEvaluator
from pg_extstats_advisor.models import Design, Move
from pg_extstats_advisor.orchestration import load_maintenance_model, load_prepared_run
from pg_extstats_advisor.postgres.adapter import PostgresAdapter
from pg_extstats_advisor.search.deterministic import DeterministicBudgetSearch
from pg_extstats_advisor.search.model import SearchConfig

ROOT = Path(__file__).resolve().parents[1]
M27_ROOT = ROOT / "experiments/census-m2-7/budgets/b010-run1"
M28_ROOT = ROOT / "experiments/census-m2-8"
DEFAULT_DSN = (
    "host=/root/projects/pg-extstats-advisor/.build/pg16.14-experiment-socket "
    "port=55436 dbname=pgextadv_exp16_census_m27_acq user=postgres"
)


def first_round(dsn: str, optimized: bool, budget: Decimal) -> dict[str, Any]:
    mode = "pruned" if optimized else "exhaustive"
    started = time.perf_counter()
    with psycopg.connect(dsn) as connection:
        prepared = load_prepared_run(M27_ROOT)
        model = load_maintenance_model(M27_ROOT)
        adapter = PostgresAdapter(connection, prepared.repository)
        evaluator = NativeEvaluator(
            prepared.workload, prepared.repository, prepared.incidence, adapter
        )
        config = SearchConfig(
            exact_bound_pruning=optimized,
            record_pruned_moves=False,
        )
        search = DeterministicBudgetSearch(
            evaluator,
            prepared.catalog,
            model,
            budget=MaintenanceBudget(budget, model.unit),
            config=config,
            incidence=prepared.incidence,
        )
        # The profiling API deliberately uses the existing search round
        # implementation without entering later greedy or local rounds.
        search._reset_run_state()
        initial = evaluator.evaluate_design(Design(()))
        search._calls = 1
        current_cost = model.estimate_design(initial.design, prepared.catalog)
        moves = (
            Move.add_candidate(candidate_id)
            for candidate_id in search._ordered_ids(False, initial.design)
        )
        if optimized:
            winner = search._finish_streaming_round(
                "greedy-add", initial, current_cost, moves
            )
        else:
            options = [
                evaluated
                for move in moves
                if (evaluated := search._consider("greedy-add", initial, move, current_cost))
                is not None
            ]
            winner = search._finish_round("greedy-add", initial, current_cost, options)
        elapsed = time.perf_counter() - started
        evaluated_records = [
            record
            for record in search._trajectory
            if record.phase == "greedy-add" and record.after_objective is not None
        ]
        winner_index = next(
            (
                index
                for index, record in enumerate(evaluated_records)
                if record.accepted
            ),
            None,
        )
        query_count = len(prepared.workload.queries)
        return {
            "mode": mode,
            "budget": str(budget),
            "elapsed_seconds": elapsed,
            "total_neighbor_moves_considered": search._considered,
            "budget_infeasible_skipped": search._skipped,
            "feasible_moves": search._considered - search._skipped,
            "bound_pruned_no_improvement": search._bound_pruned_no_improvement,
            "bound_pruned_incumbent": search._bound_pruned_incumbent,
            "bound_pruned_total": (
                search._bound_pruned_no_improvement + search._bound_pruned_incumbent
            ),
            "native_evaluated_moves": search._evaluated,
            "evaluator_calls": search._calls,
            "planner_calls": adapter.planner_calls_total,
            "neighbor_planner_calls": adapter.planner_calls_total - query_count,
            "affected_query_replans": adapter.planner_calls_total - query_count,
            "reused_query_evaluations": search._evaluated * query_count
            - (adapter.planner_calls_total - query_count),
            "accepted_moves": search._accepted,
            "first_evaluated_winner_index": winner_index,
            "best_candidate": (
                str(winner.move.add) if winner is not None and winner.move.add is not None else None
            ),
            "best_objective": winner.state.aggregate_objective if winner is not None else initial.aggregate_objective,
            "best_cost": str(winner.cost if winner is not None else current_cost),
            "best_design": list((winner.state.design if winner is not None else initial.design).candidate_ids),
            "termination": "first-greedy-round-only",
            "registration_calls": adapter.registration_calls,
            "query_count": query_count,
            "candidate_count": len(prepared.catalog.candidates),
        }


def main() -> None:
    parser = argparse.ArgumentParser()
    parser.add_argument("--dsn", default=DEFAULT_DSN)
    parser.add_argument("--budget", default="1146.45031219779468381")
    parser.add_argument("--mode", choices=("optimized", "exhaustive", "both"), default="both")
    args = parser.parse_args()
    budget = Decimal(args.budget)
    results = []
    if args.mode in ("optimized", "both"):
        results.append(first_round(args.dsn, True, budget))
    if args.mode in ("exhaustive", "both"):
        results.append(first_round(args.dsn, False, budget))
    output = {
        "format_version": 1,
        "experiment": "M2.8 Exact Search Scalability Hardening",
        "scope": "Census 10% first greedy ADD round only",
        "m2_7_baseline": "dda623401040f0fda08de1200d2e36036a7c0400",
        "candidate_count": 4506,
        "query_count": 468,
        "budget": str(budget),
        "results": results,
        "previous_m2_7_interrupted_elapsed_seconds": 215.04,
    }
    M28_ROOT.mkdir(parents=True, exist_ok=True)
    (M28_ROOT / "census-first-round.json").write_text(
        json.dumps(output, indent=2, sort_keys=True) + "\n"
    )
    print(json.dumps(output, indent=2, sort_keys=True))


if __name__ == "__main__":
    main()
