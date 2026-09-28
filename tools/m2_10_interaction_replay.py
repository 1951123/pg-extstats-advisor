"""Replay the completed M2.7-v2 greedy rounds for interaction evidence only.

The M2.7-v2 runner persisted round aggregates but not individual native ADD
evaluations.  This tool replays exactly those 36 completed rounds, verifies the
frozen accepted sequence/objectives, and persists only native-evaluated moves.
It stops before round 37 and never runs ANALYZE or statistics DDL.
"""

from __future__ import annotations

import argparse
import csv
import hashlib
import json
import time
from pathlib import Path
from typing import Any

import psycopg

from pg_extstats_advisor.cost.model import MaintenanceBudget
from pg_extstats_advisor.evaluator.native import NativeEvaluator
from pg_extstats_advisor.models import Design, Move
from pg_extstats_advisor.orchestration import _write, load_maintenance_model, load_prepared_run
from pg_extstats_advisor.postgres.adapter import PostgresAdapter
from pg_extstats_advisor.search.deterministic import DeterministicBudgetSearch
from pg_extstats_advisor.search.model import SearchConfig

ROOT = Path(__file__).resolve().parents[1]
M27_ROOT = ROOT / "experiments/census-m2-7/budgets/b010-run1"
M27_CHECKPOINT = ROOT / "experiments/census-m2-7/v2/budgets/b100-run1/checkpoint.json"
M29_RESULTS = ROOT / "experiments/census-m2-9-singletons/singleton-results.csv"
OUT_ROOT = ROOT / "experiments/census-m2-10-interactions"
PROTOCOL = OUT_ROOT / "protocol.json"
REPLAY_CHECKPOINT = OUT_ROOT / "replay-checkpoint.json"
CONTEXTS_PATH = OUT_ROOT / "round-contexts.json"
EVALS_PATH = OUT_ROOT / "contextual-evaluations.csv"
DEFAULT_DSN = (
    "host=/root/projects/pg-extstats-advisor/.build/pg16.14-experiment-socket "
    "port=55436 dbname=pgextadv_exp16_census_m27_acq user=postgres"
)
EXPECTED = {
    "workload": "796ab606e825969ad91801c9795cd830ef40686568feef857d856921a8d42215",
    "catalog": "74727a871b3a601977885cbf96d25cda6e5ac38ef448d1815561bdc5988a425a",
    "incidence": "0cd8466f654e21080914cf8444ad3ce3a093d9a7700d370b5ac6a4b9a28f66b4",
    "repository": "c0167aa2a48d2c9cccf1249643dccffd737356ae0d97dcab84ea9ec8a97ad7fe",
    "model": "dbad23611afa778a7ff6aab6ad39e519f7bfc51387874aa07dd33e37a5d3a11e",
}
FIELDS = [
    "round",
    "selected_count_before",
    "candidate_id",
    "mechanism",
    "realization_state",
    "singleton_improvement",
    "singleton_rank",
    "singleton_percentile",
    "objective_before",
    "objective_after",
    "contextual_improvement",
    "interaction_gain",
    "accepted",
    "affected_query_count",
    "maintenance_cost",
]


def sha256(path: Path) -> str:
    return hashlib.sha256(path.read_bytes()).hexdigest()


def write_csv(rows: list[dict[str, Any]]) -> None:
    temporary = EVALS_PATH.with_suffix(".csv.tmp")
    with temporary.open("w", newline="") as handle:
        writer = csv.DictWriter(handle, fieldnames=FIELDS)
        writer.writeheader()
        writer.writerows(rows)
    temporary.replace(EVALS_PATH)


def load_singletons() -> dict[str, dict[str, Any]]:
    with M29_RESULTS.open(newline="") as handle:
        rows = list(csv.DictReader(handle))
    if len(rows) != 4506:
        raise RuntimeError("M2.9 singleton artifact does not contain all 4506 candidates")
    for row in rows:
        row["singleton_improvement"] = float(row["singleton_improvement"])
        row["maintenance_cost_numeric"] = float(row["maintenance_cost_numeric"])
        row["precedence_rank"] = int(row["precedence_rank"])
    ranked = sorted(
        rows,
        key=lambda row: (
            -row["singleton_improvement"],
            row["maintenance_cost_numeric"],
            row["precedence_rank"],
            row["candidate_id"],
        ),
    )
    total = len(ranked)
    for rank, row in enumerate(ranked, start=1):
        row["singleton_rank"] = rank
        row["singleton_percentile"] = 100.0 * (total - rank + 1) / total
    return {row["candidate_id"]: row for row in ranked}


def verify_lineage(prepared: Any, model: Any) -> None:
    from pg_extstats_advisor.search.model import candidate_catalog_digest

    actual = {
        "workload": prepared.workload.digest,
        "catalog": candidate_catalog_digest(prepared.catalog.candidates),
        "incidence": prepared.incidence_digest,
        "repository": prepared.repository.digest,
        "model": model.digest,
    }
    if actual != EXPECTED:
        raise RuntimeError(f"frozen lineage mismatch: expected={EXPECTED}, actual={actual}")


def replay(dsn: str) -> dict[str, Any]:
    prepared = load_prepared_run(M27_ROOT)
    model = load_maintenance_model(M27_ROOT)
    verify_lineage(prepared, model)
    singletons = load_singletons()
    checkpoint = json.loads(M27_CHECKPOINT.read_text())
    rounds = checkpoint["rounds"]
    if len(rounds) != 36 or any(item["phase"] != "greedy-add" for item in rounds):
        raise RuntimeError("M2.7-v2 checkpoint is not the expected 36 greedy rounds")
    expected_budget = checkpoint["lineage"]["budget"]
    OUT_ROOT.mkdir(parents=True, exist_ok=True)
    all_rows: list[dict[str, Any]] = []
    context_rows: list[dict[str, Any]] = []
    started = time.perf_counter()
    with psycopg.connect(dsn) as connection:
        adapter = PostgresAdapter(connection, prepared.repository)
        evaluator = NativeEvaluator(
            prepared.workload, prepared.repository, prepared.incidence, adapter
        )
        config = SearchConfig(exact_bound_pruning=True, record_pruned_moves=False)
        search = DeterministicBudgetSearch(
            evaluator,
            prepared.catalog,
            model,
            MaintenanceBudget(expected_budget, model.unit),
            config,
            prepared.incidence,
        )
        search._reset_run_state()
        current = evaluator.evaluate_design(Design(()))
        if current.aggregate_objective != 5586.930692724469:
            raise RuntimeError("replay empty-design objective mismatch")
        search._calls = 1
        current_cost = model.estimate_design(current.design, prepared.catalog)
        for expected in rounds:
            round_started = time.perf_counter()
            if len(current.design.candidate_ids) != expected["selected_count_before"]:
                raise RuntimeError(f"round {expected['round']} selected-count mismatch")
            context_rows.append(
                {
                    "round": expected["round"],
                    "selected_count_before": len(current.design.candidate_ids),
                    "selected_design_before": list(current.design.candidate_ids),
                    "objective_before": current.aggregate_objective,
                }
            )
            trajectory_before = len(search._trajectory)
            counters = (
                search._considered,
                search._skipped,
                search._bound_pruned_no_improvement,
                search._bound_pruned_incumbent,
                search._evaluated,
                adapter.planner_calls_total,
            )
            moves = (
                Move.add_candidate(item)
                for item in search._ordered_ids(False, current.design)
            )
            winner = search._finish_streaming_round(
                "greedy-add", current, current_cost, moves
            )
            if winner is None:
                raise RuntimeError(f"round {expected['round']} unexpectedly had no winner")
            records = search._trajectory[trajectory_before:]
            for record in records:
                if record.after_objective is None or record.move.add is None:
                    continue
                candidate_id = str(record.move.add)
                singleton = singletons[candidate_id]
                contextual = record.before_objective - record.after_objective
                all_rows.append(
                    {
                        "round": expected["round"],
                        "selected_count_before": expected["selected_count_before"],
                        "candidate_id": candidate_id,
                        "mechanism": singleton["mechanism"],
                        "realization_state": singleton["realization_state"],
                        "singleton_improvement": singleton["singleton_improvement"],
                        "singleton_rank": singleton["singleton_rank"],
                        "singleton_percentile": singleton["singleton_percentile"],
                        "objective_before": record.before_objective,
                        "objective_after": record.after_objective,
                        "contextual_improvement": contextual,
                        "interaction_gain": contextual - singleton["singleton_improvement"],
                        "accepted": record.accepted,
                        "affected_query_count": record.affected_query_count,
                        "maintenance_cost": str(model.estimate_candidate(
                            prepared.catalog.by_id[record.move.add]
                        )),
                    }
                )
            native_delta = search._evaluated - counters[4]
            considered_delta = search._considered - counters[0]
            bound_delta = (
                search._bound_pruned_no_improvement
                + search._bound_pruned_incumbent
                - counters[2]
                - counters[3]
            )
            planner_delta = adapter.planner_calls_total - counters[5]
            expected_native = expected["native_evaluated"]
            expected_bound = expected["bound_pruned"]
            if native_delta != expected_native or bound_delta != expected_bound:
                raise RuntimeError(
                    f"round {expected['round']} counter mismatch: "
                    f"native {native_delta}/{expected_native}, bound {bound_delta}/{expected_bound}"
                )
            if considered_delta != expected["neighborhood_size"]:
                raise RuntimeError(f"round {expected['round']} neighborhood mismatch")
            if planner_delta != expected["planner_calls"]:
                raise RuntimeError(
                    f"round {expected['round']} planner-call mismatch: "
                    f"{planner_delta}/{expected['planner_calls']}"
                )
            accepted_id = str(winner.move.add)
            expected_id = str(expected["accepted_move"]).split(":", 1)[1]
            if accepted_id != expected_id:
                raise RuntimeError(
                    f"round {expected['round']} accepted candidate mismatch: "
                    f"{accepted_id}/{expected_id}"
                )
            if winner.state.aggregate_objective != expected["objective_after"]:
                raise RuntimeError(
                    f"round {expected['round']} accepted objective mismatch: "
                    f"{winner.state.aggregate_objective}/{expected['objective_after']}"
                )
            current, current_cost = winner.state, winner.cost
            _write(
                REPLAY_CHECKPOINT,
                {
                    "status": "in_progress",
                    "completed_rounds": expected["round"],
                    "native_evaluations": search._evaluated,
                    "conceptual_moves": search._considered,
                    "bound_pruned": search._bound_pruned_no_improvement
                    + search._bound_pruned_incumbent,
                    "planner_calls": adapter.planner_calls_total,
                    "elapsed_seconds": time.perf_counter() - started,
                    "last_round_elapsed_seconds": time.perf_counter() - round_started,
                },
            )
            write_csv(all_rows)
            _write(CONTEXTS_PATH, context_rows)
        expected_totals = {
            "native_evaluations": 86098,
            "conceptual_moves": 161586,
            "bound_pruned": 75488,
            "planner_calls": 446733,
        }
        actual_totals = {
            "native_evaluations": search._evaluated,
            "conceptual_moves": search._considered,
            "bound_pruned": search._bound_pruned_no_improvement
            + search._bound_pruned_incumbent,
            "planner_calls": adapter.planner_calls_total - len(prepared.workload.queries),
        }
        if actual_totals != expected_totals:
            raise RuntimeError(f"replay totals mismatch: {actual_totals}/{expected_totals}")
    result = {
        "status": "complete",
        "rounds": 36,
        "native_evaluations": len(all_rows),
        "unique_candidates": len({row["candidate_id"] for row in all_rows}),
        "conceptual_moves": search._considered,
        "bound_pruned": search._bound_pruned_no_improvement + search._bound_pruned_incumbent,
        "planner_calls": actual_totals["planner_calls"],
        "total_elapsed_seconds": time.perf_counter() - started,
        "m2_7_checkpoint_sha256": sha256(M27_CHECKPOINT),
        "m2_9_results_sha256": sha256(M29_RESULTS),
        "no_analyze": True,
        "statistics_ddl": 0,
    }
    _write(REPLAY_CHECKPOINT, result)
    write_csv(all_rows)
    _write(CONTEXTS_PATH, context_rows)
    return result


def main() -> None:
    parser = argparse.ArgumentParser()
    parser.add_argument("--dsn", default=DEFAULT_DSN)
    args = parser.parse_args()
    print(json.dumps(replay(args.dsn), indent=2, sort_keys=True))


if __name__ == "__main__":
    main()
