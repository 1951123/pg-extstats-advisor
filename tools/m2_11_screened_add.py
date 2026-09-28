"""Census M2.11 top-5% singleton screening and ADD-only feasibility run.

This development experiment reuses the frozen M2.9 singleton results and the
frozen M2.7 payload repository.  It only changes the visible candidate catalog
for a deterministic ADD-only search; it never performs acquisition, ANALYZE, or
statistics DDL.
"""

from __future__ import annotations

import argparse
import csv
import hashlib
import json
import statistics
import subprocess
import time
from pathlib import Path
from typing import Any

import psycopg

from pg_extstats_advisor.analysis.singleton import (
    screen_singleton_rows,
    top_fraction_count,
)
from pg_extstats_advisor.candidates.model import CandidateCatalog
from pg_extstats_advisor.cost.model import MaintenanceBudget
from pg_extstats_advisor.evaluator.native import NativeEvaluator
from pg_extstats_advisor.models import Design, Move
from pg_extstats_advisor.orchestration import load_maintenance_model, load_prepared_run
from pg_extstats_advisor.postgres.adapter import PostgresAdapter
from pg_extstats_advisor.search.deterministic import DeterministicBudgetSearch
from pg_extstats_advisor.search.model import SearchConfig, candidate_catalog_digest

ROOT = Path(__file__).resolve().parents[1]
M27_ROOT = ROOT / "experiments/census-m2-7/budgets/b010-run1"
M27_V2_CHECKPOINT = ROOT / "experiments/census-m2-7/v2/budgets/b100-run1/checkpoint.json"
M29_ROOT = ROOT / "experiments/census-m2-9-singletons"
M29_RESULTS = M29_ROOT / "singleton-results.csv"
M29_PROTOCOL = M29_ROOT / "protocol.json"
M210_PROTOCOL = ROOT / "experiments/census-m2-10-interactions/protocol.json"
OUT_ROOT = ROOT / "experiments/census-m2-11-top5-add"
EXPECTED_BASELINE = 5586.930692724469
EXPECTED = {
    "workload": "796ab606e825969ad91801c9795cd830ef40686568feef857d856921a8d42215",
    "catalog": "74727a871b3a601977885cbf96d25cda6e5ac38ef448d1815561bdc5988a425a",
    "incidence": "0cd8466f654e21080914cf8444ad3ce3a093d9a7700d370b5ac6a4b9a28f66b4",
    "repository": "c0167aa2a48d2c9cccf1249643dccffd737356ae0d97dcab84ea9ec8a97ad7fe",
    "model": "dbad23611afa778a7ff6aab6ad39e519f7bfc51387874aa07dd33e37a5d3a11e",
}
SCREENING_FRACTION = 0.05
PERFORMANCE_CEILING_SECONDS = 600.0
BOUNDARY_ID = "cand_ea654d977d77db152ccf"
DEFAULT_DSN = (
    "host=/root/projects/pg-extstats-advisor/.build/pg16.14-experiment-socket "
    "port=55436 dbname=pgextadv_exp16_census_m27_acq user=postgres"
)

SCREENED_FIELDS = [
    "screening_rank",
    "candidate_id",
    "mechanism",
    "columns",
    "singleton_improvement",
    "singleton_percentile",
    "maintenance_cost",
    "realization_state",
    "incidence_query_count",
]
ROUND_FIELDS = [
    "round",
    "selected_count_before",
    "current_objective",
    "current_cost",
    "remaining_add_candidates",
    "budget_infeasible",
    "no_improvement_bound_pruned",
    "incumbent_bound_pruned",
    "native_evaluated_moves",
    "planner_calls",
    "affected_query_replans",
    "round_elapsed_seconds",
    "accepted_candidate",
    "objective_after",
    "cost_after",
    "accepted",
    "prune_rate",
]


def digest_file(path: Path) -> str:
    return hashlib.sha256(path.read_bytes()).hexdigest()


def digest_value(value: Any) -> str:
    return hashlib.sha256(
        json.dumps(value, sort_keys=True, separators=(",", ":")).encode()
    ).hexdigest()


def write_json(path: Path, value: Any) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    temporary = path.with_suffix(path.suffix + ".tmp")
    temporary.write_text(json.dumps(value, indent=2, sort_keys=True) + "\n")
    temporary.replace(path)


def write_csv(path: Path, rows: list[dict[str, Any]], fields: list[str]) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    temporary = path.with_suffix(path.suffix + ".tmp")
    with temporary.open("w", newline="") as handle:
        writer = csv.DictWriter(handle, fieldnames=fields, extrasaction="ignore")
        writer.writeheader()
        writer.writerows(rows)
    temporary.replace(path)


def git_head() -> str:
    return subprocess.check_output(
        ["git", "rev-parse", "HEAD"], cwd=ROOT, text=True
    ).strip()


def load_singletons() -> list[dict[str, Any]]:
    with M29_RESULTS.open(newline="") as handle:
        return list(csv.DictReader(handle))


def verify_lineage(prepared: Any, model: Any, rows: list[dict[str, Any]]) -> None:
    actual = {
        "workload": prepared.workload.digest,
        "catalog": candidate_catalog_digest(prepared.catalog.candidates),
        "incidence": prepared.incidence_digest,
        "repository": prepared.repository.digest,
        "model": model.digest,
    }
    if actual != EXPECTED:
        raise RuntimeError(f"frozen lineage mismatch: expected={EXPECTED}, actual={actual}")
    if len(prepared.workload.queries) != 468 or len(prepared.catalog.candidates) != 4506:
        raise RuntimeError("frozen Census cardinality mismatch")
    if len(rows) != 4506 or {row["candidate_id"] for row in rows} != set(prepared.catalog.by_id):
        raise RuntimeError("M2.9 singleton rows do not cover the frozen catalog")
    if any(float(row["singleton_objective"]) < 0 for row in rows):
        raise RuntimeError("invalid singleton objective")
    if any(float(row["singleton_improvement"]) != EXPECTED_BASELINE - float(row["singleton_objective"]) for row in rows):
        raise RuntimeError("singleton improvement is not the frozen raw definition")


def make_screen(prepared: Any, model: Any) -> tuple[list[dict[str, Any]], dict[str, Any]]:
    rows = load_singletons()
    verify_lineage(prepared, model, rows)
    ranked = screen_singleton_rows(rows, SCREENING_FRACTION)
    retained_count = top_fraction_count(len(rows), SCREENING_FRACTION)
    full_by_id = prepared.catalog.by_id
    screened_candidates = tuple(full_by_id[row["candidate_id"]] for row in ranked)
    screened_catalog = CandidateCatalog(screened_candidates)
    screened_digest = candidate_catalog_digest(screened_candidates)
    by_id = prepared.repository.by_candidate
    incidence = prepared.incidence.by_candidate
    screened_rows = []
    for row in ranked:
        candidate = full_by_id[row["candidate_id"]]
        screened_rows.append(
            {
                "screening_rank": row["screening_rank"],
                "candidate_id": row["candidate_id"],
                "mechanism": candidate.mechanism.value,
                "columns": json.dumps(list(candidate.attributes), separators=(",", ":")),
                "singleton_improvement": row["singleton_improvement"],
                "singleton_percentile": row["singleton_percentile"],
                "maintenance_cost": row["maintenance_cost"],
                "realization_state": by_id[candidate.candidate_id].state.value,
                "incidence_query_count": len(incidence[candidate.candidate_id]),
            }
        )
    write_csv(OUT_ROOT / "screened-candidates.csv", screened_rows, SCREENED_FIELDS)
    total_cost = model.estimate_design(
        Design(tuple(item.candidate_id for item in sorted(screened_candidates, key=lambda item: item.precedence_rank))),
        screened_catalog,
    )
    ranked_all = sorted(
        rows,
        key=lambda row: (
            -float(row["singleton_improvement"]),
            float(row["maintenance_cost_numeric"]),
            int(row["precedence_rank"]),
            str(row["candidate_id"]),
        ),
    )
    retained_ids = {row["candidate_id"] for row in ranked}
    excluded_rows = [row for row in ranked_all if row["candidate_id"] not in retained_ids]
    summary = {
        "format_version": 1,
        "raw_candidate_count": len(rows),
        "retained_fraction": SCREENING_FRACTION,
        "retained_count": retained_count,
        "retained_mcv_count": sum(row["mechanism"] == "mcv" for row in screened_rows),
        "retained_fd_count": sum(row["mechanism"] == "fd" for row in screened_rows),
        "retained_present_count": sum(row["realization_state"] == "PRESENT" for row in screened_rows),
        "retained_absent_native_count": sum(row["realization_state"] == "ABSENT_NATIVE" for row in screened_rows),
        "singleton_cutoff_rank": retained_count,
        "singleton_cutoff_improvement": float(ranked[-1]["singleton_improvement"]),
        "min_retained_improvement": float(ranked[-1]["singleton_improvement"]),
        "max_excluded_improvement": float(excluded_rows[0]["singleton_improvement"]),
        "screened_total_maintenance_cost": str(total_cost),
        "screened_candidate_catalog_digest": screened_digest,
        "rank_768_boundary": {
            "candidate_id": BOUNDARY_ID,
            "singleton_rank": next(
                index for index, row in enumerate(ranked_all, start=1) if row["candidate_id"] == BOUNDARY_ID
            ),
            "excluded": BOUNDARY_ID not in retained_ids,
            "singleton_improvement": next(
                float(row["singleton_improvement"])
                for row in ranked_all
                if row["candidate_id"] == BOUNDARY_ID
            ),
        },
        "ranking": [
            "descending singleton_improvement",
            "ascending maintenance_cost_numeric",
            "ascending precedence_rank",
            "ascending candidate_id",
        ],
        "lineage": {
            "m29_results_sha256": digest_file(M29_RESULTS),
            "m29_protocol_sha256": digest_file(M29_PROTOCOL),
            "m210_protocol_sha256": digest_file(M210_PROTOCOL),
            "workload_digest": EXPECTED["workload"],
            "raw_candidate_catalog_digest": EXPECTED["catalog"],
            "incidence_digest": EXPECTED["incidence"],
            "repository_digest": EXPECTED["repository"],
            "maintenance_model_digest": EXPECTED["model"],
        },
    }
    write_json(OUT_ROOT / "screening-summary.json", summary)
    protocol = {
        "format_version": 1,
        "experiment": "M2.11-Census-Top-5%-Singleton-Screening-ADD-only",
        "status": "development-non-authoritative",
        "repo_commit": git_head(),
        "m29_protocol_digest": digest_file(M29_PROTOCOL),
        "m210_protocol_digest": digest_file(M210_PROTOCOL),
        "workload_digest": EXPECTED["workload"],
        "raw_candidate_catalog_digest": EXPECTED["catalog"],
        "screened_candidate_catalog_digest": screened_digest,
        "incidence_digest": EXPECTED["incidence"],
        "repository_digest": EXPECTED["repository"],
        "maintenance_model_digest": EXPECTED["model"],
        "raw_candidate_count": len(rows),
        "retained_fraction": SCREENING_FRACTION,
        "retained_count": retained_count,
        "screening_rounding": "ceil(raw_count * retained_fraction)",
        "ranking_semantics": summary["ranking"],
        "search_mode": "ADD-only deterministic best-improvement",
        "exact_bound_pruning": True,
        "budget_semantics": "100% of screened-candidate universe total maintenance cost",
        "budget": str(total_cost),
        "performance_ceiling_seconds": PERFORMANCE_CEILING_SECONDS,
        "forbidden_operations": ["DROP", "SWAP", "ANALYZE", "statistics DDL", "M2.7 resume", "M3"],
    }
    write_json(OUT_ROOT / "protocol.json", protocol)
    return screened_rows, summary


def move_text(move: Move) -> str:
    if move.add is not None:
        return str(move.add)
    return ""


def run_search(
    prepared: Any,
    model: Any,
    screened_rows: list[dict[str, Any]],
    summary: dict[str, Any],
    dsn: str,
    write_round_artifacts: bool,
) -> dict[str, Any]:
    screened_ids = tuple(str(row["candidate_id"]) for row in screened_rows)
    screened_catalog = CandidateCatalog(tuple(prepared.catalog.by_id[item] for item in screened_ids))
    budget = MaintenanceBudget(summary["screened_total_maintenance_cost"], model.unit)
    started = time.perf_counter()
    round_rows: list[dict[str, Any]] = []
    accepted_rows: list[dict[str, Any]] = []
    with psycopg.connect(dsn) as connection:
        adapter = PostgresAdapter(connection, prepared.repository)
        evaluator = NativeEvaluator(prepared.workload, prepared.repository, prepared.incidence, adapter)
        config = SearchConfig(exact_bound_pruning=True, record_pruned_moves=False, add_only=True)
        search = DeterministicBudgetSearch(
            evaluator, screened_catalog, model, budget, config, prepared.incidence
        )
        search._reset_run_state()
        initial = evaluator.evaluate_design(Design(()))
        if initial.aggregate_objective != EXPECTED_BASELINE:
            raise RuntimeError(f"baseline mismatch: {initial.aggregate_objective}")
        search._calls = 1
        current = initial
        current_cost = model.estimate_design(current.design, screened_catalog)
        round_no = 0
        while True:
            round_no += 1
            before = current
            before_cost = current_cost
            before_counters = (
                search._considered,
                search._skipped,
                search._bound_pruned_no_improvement,
                search._bound_pruned_incumbent,
                search._evaluated,
                search._accepted,
                adapter.planner_calls_total,
            )
            remaining = search._ordered_ids(False, before.design)
            round_started = time.perf_counter()
            winner = search._finish_streaming_round(
                "greedy-add", before, before_cost, (Move.add_candidate(item) for item in remaining)
            )
            elapsed = time.perf_counter() - round_started
            after = winner.state if winner is not None else before
            after_cost = winner.cost if winner is not None else before_cost
            considered = search._considered - before_counters[0]
            infeasible = search._skipped - before_counters[1]
            no_improvement = search._bound_pruned_no_improvement - before_counters[2]
            incumbent = search._bound_pruned_incumbent - before_counters[3]
            native = search._evaluated - before_counters[4]
            planners = adapter.planner_calls_total - before_counters[6]
            available = considered - infeasible
            round_row = {
                "round": round_no,
                "selected_count_before": len(before.design.candidate_ids),
                "current_objective": before.aggregate_objective,
                "current_cost": str(before_cost),
                "remaining_add_candidates": len(remaining),
                "budget_infeasible": infeasible,
                "no_improvement_bound_pruned": no_improvement,
                "incumbent_bound_pruned": incumbent,
                "native_evaluated_moves": native,
                "planner_calls": planners,
                "affected_query_replans": planners,
                "round_elapsed_seconds": elapsed,
                "accepted_candidate": move_text(winner.move) if winner is not None else "",
                "objective_after": after.aggregate_objective,
                "cost_after": str(after_cost),
                "accepted": winner is not None,
                "prune_rate": (no_improvement + incumbent) / available if available else 0.0,
            }
            round_rows.append(round_row)
            if winner is not None:
                accepted_record = next(
                    record for record in reversed(search._trajectory)
                    if record.accepted and record.move == winner.move
                )
                accepted_rows.append(
                    {
                        "accepted_round": round_no,
                        "candidate_id": str(winner.move.add),
                        "before_objective": before.aggregate_objective,
                        "after_objective": after.aggregate_objective,
                        "contextual_improvement": before.aggregate_objective - after.aggregate_objective,
                        "cost_after": str(after_cost),
                        "affected_query_count": accepted_record.affected_query_count,
                    }
                )
            current, current_cost = after, after_cost
            if write_round_artifacts:
                write_csv(OUT_ROOT / "round-results.csv", round_rows, ROUND_FIELDS)
                write_csv(
                    OUT_ROOT / "accepted-moves.csv",
                    accepted_rows,
                    [
                        "accepted_round",
                        "candidate_id",
                        "before_objective",
                        "after_objective",
                        "contextual_improvement",
                        "cost_after",
                        "affected_query_count",
                    ],
                )
                write_json(
                    OUT_ROOT / "checkpoint.json",
                    {
                        "status": "running",
                        "rounds": round_rows,
                        "accepted_moves": accepted_rows,
                        "elapsed_seconds": time.perf_counter() - started,
                    },
                )
            if time.perf_counter() - started > PERFORMANCE_CEILING_SECONDS:
                raise TimeoutError("M2.11 ten-minute performance ceiling exceeded")
            if winner is None:
                break
        elapsed_total = time.perf_counter() - started
        query_values = [item.contribution for item in current.query_evaluations]
        baseline_values = [item.contribution for item in initial.query_evaluations]
        deltas = [before - after for before, after in zip(baseline_values, query_values)]
        final = {
            "format_version": 1,
            "status": "complete",
            "termination_reason": "add-local-optimum",
            "selected_design": list(current.design.candidate_ids),
            "selected_objective": current.aggregate_objective,
            "selected_maintenance_cost": str(current_cost),
            "budget": str(budget.value),
            "budget_unit": budget.unit,
            "round_count": len(round_rows),
            "accepted_move_count": search._accepted,
            "total_elapsed_seconds": elapsed_total,
            "baseline_objective": initial.aggregate_objective,
            "absolute_improvement": initial.aggregate_objective - current.aggregate_objective,
            "relative_improvement": (initial.aggregate_objective - current.aggregate_objective) / initial.aggregate_objective,
            "mean_q_error": statistics.fmean(query_values),
            "median_q_error": statistics.median(query_values),
            "p90_q_error": statistics.quantiles(query_values, n=10, method="inclusive")[8],
            "max_q_error": max(query_values),
            "improved_query_count": sum(delta > 0 for delta in deltas),
            "unchanged_query_count": sum(delta == 0 for delta in deltas),
            "worsened_query_count": sum(delta < 0 for delta in deltas),
            "total_conceptual_add_moves": search._considered,
            "budget_infeasible_moves": search._skipped,
            "bound_pruned_moves": search._bound_pruned_no_improvement + search._bound_pruned_incumbent,
            "native_evaluated_moves": search._evaluated,
            "pruning_rate": (
                (search._bound_pruned_no_improvement + search._bound_pruned_incumbent)
                / (search._considered - search._skipped)
                if search._considered - search._skipped
                else 0.0
            ),
            "planner_calls": adapter.planner_calls_total,
            "affected_query_replans": adapter.planner_calls_total,
            "config": {"add_only": True, "exact_bound_pruning": True},
            "lineage": {
                "workload_digest": prepared.workload.digest,
                "repository_digest": prepared.repository.digest,
                "screened_candidate_catalog_digest": candidate_catalog_digest(screened_catalog.candidates),
            },
            "accepted_sequence": accepted_rows,
            "query_evaluations": [
                {
                    "query_id": str(item.query_id),
                    "estimate": item.estimate,
                    "truth": item.truth,
                    "contribution": item.contribution,
                }
                for item in current.query_evaluations
            ],
        }
        final["candidates"] = selected_candidate_details(prepared, rows=load_singletons(), accepted_rows=accepted_rows, design=current.design)
        if write_round_artifacts:
            write_json(OUT_ROOT / "final-result.json", final)
            write_json(OUT_ROOT / "checkpoint.json", {"status": "complete", "final": final, "rounds": round_rows})
        return {"final": final, "rounds": round_rows, "accepted": accepted_rows}


def selected_candidate_details(prepared: Any, rows: list[dict[str, Any]], accepted_rows: list[dict[str, Any]], design: Design) -> list[dict[str, Any]]:
    ranked = sorted(rows, key=lambda row: (-float(row["singleton_improvement"]), float(row["maintenance_cost_numeric"]), int(row["precedence_rank"]), str(row["candidate_id"])))
    rank_by_id = {row["candidate_id"]: (index, row) for index, row in enumerate(ranked, start=1)}
    accepted_by_id = {row["candidate_id"]: row for row in accepted_rows}
    by_payload = prepared.repository.by_candidate
    return [
        {
            "candidate_id": str(candidate_id),
            "singleton_rank": rank_by_id[str(candidate_id)][0],
            "singleton_improvement": float(rank_by_id[str(candidate_id)][1]["singleton_improvement"]),
            "mechanism": prepared.catalog.by_id[candidate_id].mechanism.value,
            "maintenance_cost": str(rank_by_id[str(candidate_id)][1]["maintenance_cost"]),
            "realization_state": by_payload[candidate_id].state.value,
            "accepted_round": accepted_by_id[str(candidate_id)]["accepted_round"],
            "contextual_improvement": accepted_by_id[str(candidate_id)]["contextual_improvement"],
        }
        for candidate_id in design.candidate_ids
    ]


def comparison_to_raw(result: dict[str, Any]) -> dict[str, Any]:
    raw = json.loads(M27_V2_CHECKPOINT.read_text())["rounds"]
    screened = result["rounds"]
    points = []
    for number in (1, 5, 10, 20, 36):
        item = {"round": number}
        if len(screened) >= number:
            row = screened[number - 1]
            item["screened_objective"] = row["objective_after"]
            item["screened_elapsed_seconds"] = sum(float(x["round_elapsed_seconds"]) for x in screened[:number])
            item["screened_accepted_ids"] = [x["accepted_candidate"] for x in screened[:number] if x["accepted"]]
        if len(raw) >= number:
            row = raw[number - 1]
            item["raw_m2_7_v2_objective"] = row["objective_after"]
            item["raw_m2_7_v2_elapsed_seconds"] = sum(float(x["elapsed_seconds"]) for x in raw[:number])
            item["raw_m2_7_v2_accepted_ids"] = [x["accepted_move"].removeprefix("add:") for x in raw[:number] if x["accepted"]]
        if "screened_accepted_ids" in item and "raw_m2_7_v2_accepted_ids" in item:
            a, b = set(item["screened_accepted_ids"]), set(item["raw_m2_7_v2_accepted_ids"])
            item["accepted_design_overlap_count"] = len(a & b)
            item["accepted_design_overlap_jaccard"] = len(a & b) / len(a | b) if a | b else 1.0
        points.append(item)
    screened_seq = [x["accepted_candidate"] for x in screened if x["accepted"]]
    raw_seq = [x["accepted_move"].removeprefix("add:") for x in raw if x["accepted"]]
    divergence = next((i + 1 for i, (a, b) in enumerate(zip(screened_seq, raw_seq)) if a != b), None)
    return {
        "warning": "descriptive only; screened and raw candidate universes are not apples-to-apples",
        "raw_checkpoint": str(M27_V2_CHECKPOINT.relative_to(ROOT)),
        "common_rounds": points,
        "accepted_sequence_divergence_round": divergence,
    }


def main() -> None:
    parser = argparse.ArgumentParser()
    parser.add_argument("--dsn", default=DEFAULT_DSN)
    args = parser.parse_args()
    prepared = load_prepared_run(M27_ROOT)
    model = load_maintenance_model(M27_ROOT)
    screened_rows, summary = make_screen(prepared, model)
    first = run_search(prepared, model, screened_rows, summary, args.dsn, True)
    # A complete first run is the only condition under which the exact repeat is run.
    repeat = run_search(prepared, model, screened_rows, summary, args.dsn, False)
    first_final, repeat_final = first["final"], repeat["final"]
    repeat_summary = {
        "total_elapsed_seconds": repeat_final["total_elapsed_seconds"],
        "termination_reason": repeat_final["termination_reason"],
        "round_count": repeat_final["round_count"],
        "accepted_sequence": [row["candidate_id"] for row in repeat["accepted"]],
        "selected_design": repeat_final["selected_design"],
        "selected_objective": repeat_final["selected_objective"],
        "selected_maintenance_cost": repeat_final["selected_maintenance_cost"],
        "exact_match": (
            [row["candidate_id"] for row in repeat["accepted"]]
            == [row["candidate_id"] for row in first["accepted"]]
            and repeat_final["selected_design"] == first_final["selected_design"]
            and repeat_final["selected_objective"] == first_final["selected_objective"]
            and repeat_final["selected_maintenance_cost"] == first_final["selected_maintenance_cost"]
            and repeat_final["round_count"] == first_final["round_count"]
            and repeat_final["termination_reason"] == first_final["termination_reason"]
        ),
    }
    write_json(OUT_ROOT / "repeat-result.json", repeat_summary)
    comparison = comparison_to_raw(first)
    write_json(OUT_ROOT / "comparison-to-m2-7-v2.json", comparison)
    final_summary = {
        **summary,
        "search": first_final,
        "repeat": repeat_summary,
        "comparison_to_m2_7_v2": comparison,
    }
    write_json(OUT_ROOT / "report-summary.json", final_summary)
    selected_mcv = sum(item["mechanism"] == "mcv" for item in first_final["candidates"])
    selected_fd = sum(item["mechanism"] == "fd" for item in first_final["candidates"])
    round_times = [float(item["round_elapsed_seconds"]) for item in first["rounds"]]
    pruning_rates = [float(item["prune_rate"]) for item in first["rounds"]]
    report = [
        "# M2.11 Census Top-5% Singleton Screening, ADD-only Search",
        "",
        "This is a development feasibility experiment, not an authoritative screening policy or M2.7 replacement.",
        "",
        f"The frozen M2.9 singleton ranking retained {summary['retained_count']} of {summary['raw_candidate_count']} candidates ({summary['retained_fraction']:.0%}) using ceil rounding. The screened-universe budget is {summary['screened_total_maintenance_cost']} maintenance units.",
        "",
        f"The screen contains {summary['retained_mcv_count']} MCV and {summary['retained_fd_count']} FD candidates, all in PRESENT state. The ADD-only search terminated at an add-local-optimum after {first_final['round_count']} rounds and {first_final['total_elapsed_seconds']:.3f}s. It selected {len(first_final['selected_design'])} candidates ({selected_mcv} MCV and {selected_fd} FD) with objective {first_final['selected_objective']:.12f} (baseline {EXPECTED_BASELINE:.12f}) and cost {first_final['selected_maintenance_cost']}.",
        "",
        f"Efficiency: {first_final['total_conceptual_add_moves']} conceptual ADD moves, {first_final['budget_infeasible_moves']} budget-infeasible, {first_final['bound_pruned_moves']} bound-pruned, {first_final['native_evaluated_moves']} natively evaluated, {first_final['planner_calls']} planner calls, and {first_final['pruning_rate']:.6%} overall pruning. Round elapsed mean/median/max was {statistics.fmean(round_times):.3f}/{statistics.median(round_times):.3f}/{max(round_times):.3f}s; lowest round pruning rate was {min(pruning_rates):.6%}.",
        "",
        f"The exact repeat matched the accepted sequence, final design, objective, cost, round count, and termination reason: {repeat_summary['exact_match']}.",
        "",
        f"The M2.10 rank-768 boundary candidate {BOUNDARY_ID} was excluded: {summary['rank_768_boundary']['excluded']}. Raw-v2 comparisons are descriptive only because the candidate universes differ.",
        "",
        "No DROP, SWAP, ANALYZE, statistics DDL, M2.7 resume, physical validation, or M3 work was performed.",
    ]
    (OUT_ROOT / "report.md").write_text("\n".join(report) + "\n")
    print(json.dumps({"final": first_final, "repeat": repeat_summary}, indent=2, sort_keys=True))


if __name__ == "__main__":
    main()
