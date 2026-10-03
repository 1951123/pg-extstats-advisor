"""Small real-PostgreSQL validation of the advisor optimization deadline."""

from __future__ import annotations

import json
import sys
import time
from pathlib import Path

BENCH_SRC = Path("/home/wqts/projects/pg-extstats-benchmarks/src")
if str(BENCH_SRC) not in sys.path:
    sys.path.insert(0, str(BENCH_SRC))

from pgextstats_benchmarks.candidate_catalog import CandidateCatalog as BenchmarkCatalog
from pgextstats_benchmarks.postgres.connection import PostgresConnection
from pgextstats_benchmarks.postgres.instance import PostgresInstance
from pgextstats_benchmarks.postgres.loader import PostgreSQLLoader
from pgextstats_benchmarks.statistics_storage import load_repository_artifact

from pg_extstats_advisor.models import Design
from pg_extstats_advisor.optimization import OptimizationBudget
from pg_extstats_advisor.screening import profile_singletons_native
from pg_extstats_advisor.search.deterministic import DeterministicBudgetSearch
from pg_extstats_advisor.search.model import SearchConfig

import arecel_census13_rq12_heldout as heldout


SMOKE_BUDGET_SECONDS = 300.0
SMOKE_QUERIES = 12
SMOKE_CANDIDATES = 4
OUTPUT = heldout.ROOT / "arecel/census13/experiments/optimization-budget-smoke-v1.json"


def _write(value: dict) -> None:
    OUTPUT.parent.mkdir(parents=True, exist_ok=True)
    OUTPUT.write_text(json.dumps(value, sort_keys=True, indent=2) + "\n")


def main() -> None:
    records = heldout.read_records()
    adapter = heldout.AreCELearnedYetAdapter(
        dataset="census13", data_root=heldout.ROOT, audit_root=heldout.AUDIT_ROOT
    )
    prepared = heldout.promote(adapter)
    repo = load_repository_artifact(heldout.BENCHMARK_ID, heldout.REPO_ID, heldout.ROOT)
    full_catalog = heldout.build_catalog()
    present = {item.candidate_id for item in repo.candidate_states if item.state == "PRESENT"}
    smoke_definitions = [
        item for item in full_catalog.candidates if item.candidate_id in present
    ][:SMOKE_CANDIDATES]
    smoke_catalog = BenchmarkCatalog.from_mapping(
        {
            "catalog_id": "arecel-census13-budget-smoke-catalog-v1",
            "relation_identity": heldout.RELATION,
            "statistics_target": heldout.TARGET,
            "candidates": [item.to_dict() for item in smoke_definitions],
        }
    )
    smoke_records = [
        item
        for item in records
        if item["split"] == "valid" and int(item["source_label"]["cardinality"]) > 0
    ][:SMOKE_QUERIES]
    loader = PostgreSQLLoader(
        PostgresConnection(host="localhost", port=heldout.DB_PORT, user=heldout.DB_USER, database="postgres")
    )
    instance: PostgresInstance | None = None
    evaluator = None
    smoke_adapter = None
    started = time.perf_counter()
    budget = OptimizationBudget(SMOKE_BUDGET_SECONDS)
    try:
        instance = loader.create_instance("arecel_census13_budget_smoke")
        instance = loader.load_artifact(instance, prepared)
        relation_connection = PostgresConnection(
            host="localhost", port=heldout.DB_PORT, user=heldout.DB_USER, database=instance.database_name
        )
        relation_oid = relation_connection.relation_oid(heldout.RELATION)
        relation_connection.close()
        order = [item.candidate_id for item in smoke_catalog.candidates]
        evaluator, smoke_adapter = heldout.make_evaluator(
            repo, instance, smoke_catalog, order, smoke_records, "valid", relation_oid
        )
        budget.start()
        evaluator.bind_optimization_budget(budget, "baseline")
        baseline_started = time.perf_counter()
        baseline = evaluator.evaluate_design(Design(()))
        baseline_elapsed = time.perf_counter() - baseline_started
        baseline_calls = smoke_adapter.explain_calls
        profile = profile_singletons_native(
            evaluator,
            list(evaluator.catalog.candidates),
            baseline,
            budget=budget,
        )
        if not profile["singleton_profile_complete"]:
            result = {
                "status": profile["status"],
                "budget_seconds": SMOKE_BUDGET_SECONDS,
                "advisor_elapsed_seconds": budget.elapsed_seconds,
                "stop_phase": profile["stop_phase"],
                "stop_reason": profile["stop_reason"],
                "planner_calls": smoke_adapter.explain_calls,
                "baseline_planner_calls": baseline_calls,
                "singleton_planner_calls": profile["planner_calls"],
                "greedy_planner_calls": 0,
                "phase_elapsed_seconds": {
                    "baseline": baseline_elapsed,
                    "singleton": profile["elapsed_seconds"],
                    "greedy": 0.0,
                },
                "singleton_candidates_total": profile["total_singleton_candidates"],
                "singleton_candidates_completed": profile["completed_singleton_candidates"],
                "singleton_profile_complete": False,
                "greedy_rounds_completed": 0,
                "accepted_design": [],
                "accepted_state": False,
                "validation_elapsed_seconds": 0.0,
                "scope": {"queries": len(smoke_records), "candidates": len(smoke_catalog.candidates)},
            }
            _write(result)
            return
        precedence = [
            str(row["candidate_id"])
            for row in sorted(
                profile["rows"],
                key=lambda row: (-float(row["singleton_improvement"]), str(row["candidate_id"])),
            )
        ]
        search_catalog = heldout.advisor_catalog(smoke_catalog, relation_oid, precedence)
        zero = heldout.ZeroCostModel()
        search = DeterministicBudgetSearch(
            evaluator,
            search_catalog,
            zero,
            heldout.MaintenanceBudget(heldout.Decimal(0), zero.unit),
            SearchConfig(add_only=True, exact_bound_pruning=False, visible_candidate_count=len(precedence)),
            evaluator.incidence,
        )
        outcome = search.run_bounded(
            budget,
            initial_state=baseline,
            baseline_planner_calls=baseline_calls,
            singleton_planner_calls=profile["planner_calls"],
        )
        result = {
            "status": outcome.status.value,
            "budget_seconds": outcome.budget_seconds,
            "advisor_elapsed_seconds": outcome.elapsed_seconds,
            "budget_exhausted": outcome.budget_exhausted,
            "stop_phase": outcome.stop_phase,
            "stop_reason": outcome.stop_reason,
            "planner_calls": outcome.planner_calls_completed,
            "baseline_planner_calls": outcome.baseline_planner_calls,
            "singleton_planner_calls": outcome.singleton_planner_calls,
            "greedy_planner_calls": outcome.greedy_planner_calls,
            "phase_elapsed_seconds": {
                "baseline": baseline_elapsed,
                "singleton": profile["elapsed_seconds"],
                **dict(outcome.phase_elapsed_seconds),
            },
            "singleton_candidates_total": profile["total_singleton_candidates"],
            "singleton_candidates_completed": profile["completed_singleton_candidates"],
            "singleton_profile_complete": True,
            "greedy_rounds_completed": outcome.search_result.accepted_moves_count if outcome.search_result else 0,
            "accepted_design": (
                [str(item) for item in outcome.search_result.selected_design.candidate_ids]
                if outcome.search_result
                else []
            ),
            "accepted_state": bool(outcome.search_result and outcome.search_result.accepted_moves_count),
            "validation_elapsed_seconds": 0.0,
            "scope": {"queries": len(smoke_records), "candidates": len(smoke_catalog.candidates)},
            "precedence": precedence,
        }
        _write(result)
    finally:
        if smoke_adapter is not None:
            smoke_adapter.close()
        if instance is not None:
            loader.destroy_instance(instance)
        loader.close()


if __name__ == "__main__":
    main()
