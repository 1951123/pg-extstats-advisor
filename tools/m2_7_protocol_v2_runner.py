"""Authoritative M2.7 protocol-v2 exact-pruned search runner.

The runner uses only the already frozen M2.7 acquisition repository.  It
executes one budget/run at a time, writes a round checkpoint after every
greedy or local round, and never performs ANALYZE or statistics DDL.
"""

from __future__ import annotations

import argparse
import hashlib
import json
import time
from pathlib import Path
from typing import Any

import psycopg

from pg_extstats_advisor.cost.model import MaintenanceBudget
from pg_extstats_advisor.evaluator.native import NativeEvaluator
from pg_extstats_advisor.models import Design, Move
from pg_extstats_advisor.orchestration import (
    _write,
    load_maintenance_model,
    load_prepared_run,
    persist_search_result,
)
from pg_extstats_advisor.postgres.adapter import PostgresAdapter
from pg_extstats_advisor.search.deterministic import DeterministicBudgetSearch
from pg_extstats_advisor.search.model import SearchConfig, SearchResult, candidate_catalog_digest

ROOT = Path(__file__).resolve().parents[1]
M27_ROOT = ROOT / "experiments/census-m2-7/budgets/b010-run1"
V2_ROOT = ROOT / "experiments/census-m2-7/v2"
PROTOCOL_PATH = ROOT / "experiments/census-m2-7/protocol-v2.json"
DEFAULT_DSN = (
    "host=/root/projects/pg-extstats-advisor/.build/pg16.14-experiment-socket "
    "port=55436 dbname=pgextadv_exp16_census_m27_acq user=postgres"
)
PER_BUDGET_CEILING_SECONDS = 1800.0


def digest_file(path: Path) -> str:
    return hashlib.sha256(path.read_bytes()).hexdigest()


def protocol_digest() -> str:
    return digest_file(PROTOCOL_PATH)


def setup_run_root(run_root: Path) -> None:
    run_root.mkdir(parents=True, exist_ok=True)
    for name in (
        "config.json",
        "workload.json",
        "candidates.json",
        "incidence.json",
        "prepare-summary.json",
        "run-manifest.json",
        "maintenance-model.json",
    ):
        target = M27_ROOT / name
        link = run_root / name
        if link.exists() or link.is_symlink():
            link.unlink()
        link.symlink_to(target)
    repository_link = run_root / "repository"
    if repository_link.exists() or repository_link.is_symlink():
        repository_link.unlink()
    repository_link.symlink_to(M27_ROOT / "repository")


def move_text(move: Move) -> str:
    if move.drop is not None and move.add is not None:
        return f"swap:{move.drop}->{move.add}"
    if move.add is not None:
        return f"add:{move.add}"
    return f"drop:{move.drop}"


def run_one(
    fraction: float,
    budget_value: str,
    run_index: int,
    dsn: str,
    full_reference: bool = False,
) -> tuple[SearchResult, list[dict[str, Any]]]:
    run_name = f"b{round(fraction * 1000):03.0f}-run{run_index}"
    run_root = V2_ROOT / ("full-reference-control" if full_reference else "budgets") / run_name
    setup_run_root(run_root)
    lineage = {
        "protocol_v2_digest": protocol_digest(),
        "workload_digest": "796ab606e825969ad91801c9795cd830ef40686568fe857d856921a8d42215",
        "candidate_catalog_digest": "74727a871b3a601977885cbf96d25cda6e5ac38ef448d1815561bdc5988a425a",
        "incidence_digest": "0cd8466f654e21080914cf8444ad3ce3a093d9a7700d370b5ac6a4b9a28f66b4",
        "repository_digest": "c0167aa2a48d2c9cccf1249643dccffd737356ae0d97dcab84ea9ec8a97ad7fe",
        "maintenance_model_digest": "dbad23611afa778a7ff6aab6ad39e519f7bfc51387874aa07dd33e37a5d3a11e",
        "exact_bound_pruning": True,
        "full_reference": full_reference,
        "fraction": fraction,
        "budget": budget_value,
        "run_index": run_index,
        "status": "started",
    }
    _write(run_root / "lineage.json", lineage)
    round_rows: list[dict[str, Any]] = []
    started = time.perf_counter()
    with psycopg.connect(dsn) as connection:
        prepared = load_prepared_run(M27_ROOT)
        model = load_maintenance_model(M27_ROOT)
        adapter = PostgresAdapter(connection, prepared.repository)
        evaluator = NativeEvaluator(
            prepared.workload, prepared.repository, prepared.incidence, adapter
        )
        config = SearchConfig(
            full_reference=full_reference,
            exact_bound_pruning=True,
            record_pruned_moves=False,
        )
        search = DeterministicBudgetSearch(
            evaluator,
            prepared.catalog,
            model,
            MaintenanceBudget(budget_value, model.unit),
            config,
            prepared.incidence,
        )
        search._reset_run_state()
        initial = evaluator.evaluate_design(Design(()))
        search._calls = 1
        current = initial
        current_cost = model.estimate_design(current.design, prepared.catalog)
        global_round = 0

        def run_round(phase: str, phase_round: int, moves: Any) -> Any:
            nonlocal current, current_cost, global_round
            global_round += 1
            before = current
            before_cost = current_cost
            counters = (
                search._considered,
                search._skipped,
                search._bound_pruned_no_improvement,
                search._bound_pruned_incumbent,
                search._evaluated,
                search._accepted,
                adapter.planner_calls_total,
            )
            round_start = time.perf_counter()
            winner = search._finish_streaming_round(phase, before, before_cost, moves)
            elapsed = time.perf_counter() - round_start
            after = winner.state if winner is not None else before
            after_cost = winner.cost if winner is not None else before_cost
            row = {
                "budget_fraction": fraction,
                "budget": budget_value,
                "run": run_index,
                "phase": phase,
                "round": phase_round,
                "global_round": global_round,
                "selected_count_before": len(before.design.candidate_ids),
                "neighborhood_size": search._considered - counters[0],
                "infeasible": search._skipped - counters[1],
                "bound_pruned_no_improvement": search._bound_pruned_no_improvement - counters[2],
                "bound_pruned_incumbent": search._bound_pruned_incumbent - counters[3],
                "bound_pruned": (
                    search._bound_pruned_no_improvement
                    + search._bound_pruned_incumbent
                    - counters[2]
                    - counters[3]
                ),
                "native_evaluated": search._evaluated - counters[4],
                "planner_calls": adapter.planner_calls_total - counters[6],
                "elapsed_seconds": elapsed,
                "accepted_move": move_text(winner.move) if winner is not None else "",
                "selected_count_after": len(after.design.candidate_ids),
                "objective_before": before.aggregate_objective,
                "objective_after": after.aggregate_objective,
                "cost_before": str(before_cost),
                "cost_after": str(after_cost),
                "accepted": winner is not None,
            }
            row["prune_rate"] = (
                row["bound_pruned"] / (row["neighborhood_size"] - row["infeasible"])
                if row["neighborhood_size"] - row["infeasible"]
                else 0.0
            )
            round_rows.append(row)
            _write(run_root / "checkpoint.json", {"lineage": lineage, "rounds": round_rows})
            if time.perf_counter() - started > PER_BUDGET_CEILING_SECONDS:
                raise TimeoutError(
                    f"protocol-v2 per-budget ceiling exceeded in {run_name}: "
                    f"{PER_BUDGET_CEILING_SECONDS:.0f}s"
                )
            current, current_cost = after, after_cost
            return winner

        greedy_round = 0
        while True:
            greedy_round += 1
            moves = (
                Move.add_candidate(item)
                for item in search._ordered_ids(False, current.design)
            )
            if run_round("greedy-add", greedy_round, moves) is None:
                break

        local_round = 0
        while True:
            local_round += 1
            selected = search._ordered_ids(True, current.design)
            unselected = search._ordered_ids(False, current.design)
            moves = search._iter_local_moves(selected, unselected)
            if run_round("local", local_round, moves) is None:
                break

        catalog_digest = candidate_catalog_digest(prepared.catalog.candidates)
        result = SearchResult(
            selected_state=current,
            selected_design=current.design,
            selected_objective=current.aggregate_objective,
            selected_maintenance_cost=current_cost,
            budget=MaintenanceBudget(budget_value, model.unit),
            cost_model_digest=model.digest,
            initial_design=Design(()),
            final_design=current.design,
            trajectory=tuple(search._trajectory),
            evaluated_moves_count=search._evaluated,
            infeasible_moves_skipped_count=search._skipped,
            evaluator_calls_count=search._calls,
            accepted_moves_count=search._accepted,
            termination_reason="one-move-local-optimum",
            config=config,
            workload_digest=current.workload_digest,
            repository_digest=current.repository_digest,
            candidate_catalog_digest=catalog_digest,
            total_neighbor_moves_considered=search._considered,
            bound_pruned_no_improvement_count=search._bound_pruned_no_improvement,
            bound_pruned_incumbent_count=search._bound_pruned_incumbent,
        )
        persist_search_result(run_root, result)
        lineage["status"] = "complete"
        lineage["elapsed_seconds"] = time.perf_counter() - started
        lineage["selected_design"] = list(result.selected_design.candidate_ids)
        lineage["selected_objective"] = result.selected_objective
        lineage["selected_cost"] = str(result.selected_maintenance_cost)
        _write(run_root / "lineage.json", lineage)
        return result, round_rows


def accepted_signature(result: SearchResult) -> tuple[tuple[str, str, float | None], ...]:
    return tuple(
        (record.move.kind.value, move_text(record.move), record.after_objective)
        for record in result.trajectory
        if record.accepted
    )


def main() -> None:
    parser = argparse.ArgumentParser()
    parser.add_argument("--fraction", type=float, required=True)
    parser.add_argument("--budget", required=True)
    parser.add_argument("--run-index", type=int, choices=(1, 2), required=True)
    parser.add_argument("--dsn", default=DEFAULT_DSN)
    parser.add_argument("--full-reference", action="store_true")
    args = parser.parse_args()
    result, rows = run_one(
        args.fraction,
        args.budget,
        args.run_index,
        args.dsn,
        args.full_reference,
    )
    print(
        json.dumps(
            {
                "fraction": args.fraction,
                "budget": args.budget,
                "run": args.run_index,
                "full_reference": args.full_reference,
                "selected_design": list(result.selected_design.candidate_ids),
                "objective": result.selected_objective,
                "cost": str(result.selected_maintenance_cost),
                "accepted_moves": result.accepted_moves_count,
                "evaluated_moves": result.evaluated_moves_count,
                "bound_pruned": result.bound_pruned_no_improvement_count
                + result.bound_pruned_incumbent_count,
                "rounds": len(rows),
            },
            sort_keys=True,
        )
    )


if __name__ == "__main__":
    main()
