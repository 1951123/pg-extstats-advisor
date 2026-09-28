"""Probe exactly one full local ADD/DROP/SWAP round from M2.11's state."""

from __future__ import annotations

import argparse
import csv
import hashlib
import json
import math
import statistics
import subprocess
import time
from decimal import Decimal
from pathlib import Path
from typing import Any

import psycopg

from pg_extstats_advisor.candidates.model import CandidateCatalog
from pg_extstats_advisor.cost.model import MaintenanceBudget
from pg_extstats_advisor.evaluator.native import NativeEvaluator
from pg_extstats_advisor.models import (
    CandidateId,
    Design,
    EvaluationState,
    QueryEvaluation,
    QueryId,
)
from pg_extstats_advisor.orchestration import load_maintenance_model, load_prepared_run
from pg_extstats_advisor.postgres.adapter import PostgresAdapter
from pg_extstats_advisor.search.deterministic import DeterministicBudgetSearch
from pg_extstats_advisor.search.model import SearchConfig, candidate_catalog_digest

ROOT = Path(__file__).resolve().parents[1]
M27_ROOT = ROOT / "experiments/census-m2-7/budgets/b010-run1"
M211_ROOT = ROOT / "experiments/census-m2-11-top5-add"
OUT_ROOT = ROOT / "experiments/census-m2-12-one-local-round"
M211_PROTOCOL = M211_ROOT / "protocol.json"
M211_FINAL = M211_ROOT / "final-result.json"
M211_SCREENED = M211_ROOT / "screened-candidates.csv"
EXPECTED_BASELINE = 5586.930692724469
EXPECTED_START_OBJECTIVE = 936.3584041825216
EXPECTED_START_COST = "195.4027612476062514"
EXPECTED_SCREENED_DIGEST = "c0c544c703682a13e7447a514f0610d251b4694cb92f2c8eeead3f3ee50880a8"
EXPECTED = {
    "workload": "796ab606e825969ad91801c9795cd830ef40686568feef857d856921a8d42215",
    "catalog": "74727a871b3a601977885cbf96d25cda6e5ac38ef448d1815561bdc5988a425a",
    "incidence": "0cd8466f654e21080914cf8444ad3ce3a093d9a7700d370b5ac6a4b9a28f66b4",
    "repository": "c0167aa2a48d2c9cccf1249643dccffd737356ae0d97dcab84ea9ec8a97ad7fe",
    "model": "dbad23611afa778a7ff6aab6ad39e519f7bfc51387874aa07dd33e37a5d3a11e",
}
PERFORMANCE_CEILING_SECONDS = 600.0
DEFAULT_DSN = (
    "host=/root/projects/pg-extstats-advisor/.build/pg16.14-experiment-socket "
    "port=55436 dbname=pgextadv_exp16_census_m27_acq user=postgres"
)


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


def load_json(path: Path) -> dict[str, Any]:
    return json.loads(path.read_text())


def git_head() -> str:
    return subprocess.check_output(["git", "rev-parse", "HEAD"], cwd=ROOT, text=True).strip()


def load_screened_rows() -> list[dict[str, str]]:
    with M211_SCREENED.open(newline="") as handle:
        return list(csv.DictReader(handle))


def verify_lineage(prepared: Any, model: Any, final: dict[str, Any]) -> tuple[CandidateCatalog, dict[str, str]]:
    actual = {
        "workload": prepared.workload.digest,
        "catalog": candidate_catalog_digest(prepared.catalog.candidates),
        "incidence": prepared.incidence_digest,
        "repository": prepared.repository.digest,
        "model": model.digest,
    }
    if actual != EXPECTED:
        raise RuntimeError(f"frozen lineage mismatch: expected={EXPECTED}, actual={actual}")
    m211_protocol = load_json(M211_PROTOCOL)
    if m211_protocol["status"] != "development-non-authoritative":
        raise RuntimeError("unexpected M2.11 protocol status")
    if m211_protocol["screened_candidate_catalog_digest"] != EXPECTED_SCREENED_DIGEST:
        raise RuntimeError("M2.11 screened catalog digest mismatch")
    screened_rows = load_screened_rows()
    if len(screened_rows) != 226:
        raise RuntimeError("M2.11 screened candidate count mismatch")
    screened_ids = tuple(str(row["candidate_id"]) for row in screened_rows)
    screened_catalog = CandidateCatalog(tuple(prepared.catalog.by_id[item] for item in screened_ids))
    if candidate_catalog_digest(screened_catalog.candidates) != EXPECTED_SCREENED_DIGEST:
        raise RuntimeError("screened catalog reconstruction mismatch")
    if final["selected_design"] != list(final["selected_design"]):
        raise RuntimeError("invalid persisted selected design")
    if len(final["selected_design"]) != 110:
        raise RuntimeError("M2.11 selected count mismatch")
    if final["selected_objective"] != EXPECTED_START_OBJECTIVE:
        raise RuntimeError("M2.11 objective mismatch")
    if final["selected_maintenance_cost"] != EXPECTED_START_COST:
        raise RuntimeError("M2.11 cost mismatch")
    if final["termination_reason"] != "add-local-optimum":
        raise RuntimeError("M2.11 termination mismatch")
    if final["lineage"]["workload_digest"] != EXPECTED["workload"]:
        raise RuntimeError("M2.11 final workload lineage mismatch")
    if final["lineage"]["repository_digest"] != EXPECTED["repository"]:
        raise RuntimeError("M2.11 final repository lineage mismatch")
    if final["lineage"]["screened_candidate_catalog_digest"] != EXPECTED_SCREENED_DIGEST:
        raise RuntimeError("M2.11 final screened lineage mismatch")
    summary = load_json(M27_ROOT / "prepare-summary.json")
    if summary["sql_analysis"] != {
        "analysis_version": "pg16-mvp-v2",
        "parser": "pglast",
        "parser_version": "v7.18",
    }:
        raise RuntimeError("parser provenance mismatch")
    calibration_provenance = load_json(M27_ROOT / "maintenance-model.json")["calibration_provenance"]
    if summary["postgres_version"] != "16.14" or not calibration_provenance["environment_authoritative"]:
        raise RuntimeError("PostgreSQL build provenance mismatch")
    if not calibration_provenance["postgres_binary_path"].endswith("postgresql-16.14-install/bin/postgres"):
        raise RuntimeError("unexpected PostgreSQL binary provenance")
    return screened_catalog, {
        "parser_provenance": json.dumps(summary["sql_analysis"], sort_keys=True),
        "pg_build_provenance": json.dumps(
            {
                "postgres_version": summary["postgres_version"],
                "postgres_binary_path": calibration_provenance["postgres_binary_path"],
                "pg_patch_sha256": calibration_provenance["pg_patch_sha256"],
            },
            sort_keys=True,
        ),
    }


def persisted_state(final: dict[str, Any], adapter_version: str) -> EvaluationState:
    evaluations = tuple(
        QueryEvaluation(
            QueryId(str(item["query_id"])),
            float(item["estimate"]),
            float(item["truth"]),
            float(item["contribution"]),
            f"native-explain:{adapter_version}",
        )
        for item in final["query_evaluations"]
    )
    state = EvaluationState(
        Design(tuple(map(CandidateId, final["selected_design"]))),
        evaluations,
        float(final["selected_objective"]),
        str(final["lineage"]["repository_digest"]),
        str(final["lineage"]["workload_digest"]),
        adapter_version,
        "m0-b-native-evaluator-v1",
        tuple(sorted(item.query_id for item in evaluations)),
        (),
    )
    if len(evaluations) != 468 or math.fsum(item.contribution for item in evaluations) != EXPECTED_START_OBJECTIVE:
        raise RuntimeError("persisted M2.11 evaluation state mismatch")
    return state


def metric_value(value: float) -> float | None:
    return None if math.isinf(value) else value


def run_probe(
    prepared: Any,
    model: Any,
    screened_catalog: CandidateCatalog,
    start: EvaluationState,
    dsn: str,
) -> dict[str, Any]:
    budget = MaintenanceBudget("414.0398034077412444", model.unit)
    start_cost = Decimal(EXPECTED_START_COST)
    started = time.perf_counter()
    with psycopg.connect(dsn) as connection:
        adapter = PostgresAdapter(connection, prepared.repository)
        evaluator = NativeEvaluator(prepared.workload, prepared.repository, prepared.incidence, adapter)
        search = DeterministicBudgetSearch(
            evaluator,
            screened_catalog,
            model,
            budget,
            SearchConfig(exact_bound_pruning=True, record_pruned_moves=False, add_only=False),
            prepared.incidence,
        )
        search._reset_run_state()
        after, after_cost, winner, metrics = search.evaluate_one_local_round(start, start_cost)
        elapsed = time.perf_counter() - started
        if elapsed > PERFORMANCE_CEILING_SECONDS:
            raise TimeoutError("M2.12 one-local-round performance ceiling exceeded")
        total_moves = sum(item["conceptual_moves"] for item in metrics.values())
        bound_pruned = sum(
            item["no_improvement_bound_pruned"] + item["incumbent_bound_pruned"]
            for item in metrics.values()
        )
        native = sum(item["native_evaluated"] for item in metrics.values())
        if total_moves != 12986:
            raise RuntimeError(f"local neighborhood count mismatch: {total_moves}")
        add_best = metrics["add"]["best_objective"]
        if add_best < EXPECTED_START_OBJECTIVE:
            raise RuntimeError("ADD-local optimum inconsistency: improving ADD observed")
        winner_data = None
        if winner is not None:
            move = winner.move
            outgoing = str(move.drop) if move.drop is not None else None
            incoming = str(move.add) if move.add is not None else None
            winner_data = {
                "move_type": move.kind.value,
                "outgoing_candidate": outgoing,
                "incoming_candidate": incoming,
                "cost_before": str(start_cost),
                "cost_after": str(after_cost),
                "objective_before": EXPECTED_START_OBJECTIVE,
                "objective_after": after.aggregate_objective,
                "absolute_improvement": EXPECTED_START_OBJECTIVE - after.aggregate_objective,
                "relative_improvement": (EXPECTED_START_OBJECTIVE - after.aggregate_objective) / EXPECTED_START_OBJECTIVE,
            }
        return {
            "start_objective": EXPECTED_START_OBJECTIVE,
            "start_cost": EXPECTED_START_COST,
            "start_selected_count": len(start.design.candidate_ids),
            "start_unselected_count": len(screened_catalog.candidates) - len(start.design.candidate_ids),
            "selected_design": list(start.design.candidate_ids),
            "after_design": list(after.design.candidate_ids),
            "after_objective": after.aggregate_objective,
            "after_cost": str(after_cost),
            "accepted": winner is not None,
            "termination": "one-round-complete",
            "best_move": winner_data,
            "metrics": metrics,
            "aggregate": {
                "total_conceptual_moves": total_moves,
                "budget_infeasible": search._skipped,
                "no_improvement_bound_pruned": search._bound_pruned_no_improvement,
                "incumbent_bound_pruned": search._bound_pruned_incumbent,
                "total_bound_pruned": bound_pruned,
                "native_evaluated": native,
                "pruning_rate": bound_pruned / (total_moves - search._skipped),
                "evaluator_calls": search._calls,
                "planner_calls": adapter.planner_calls_total,
                "affected_query_replans": adapter.planner_calls_total,
                "elapsed_seconds": elapsed,
            },
        }


def decorate_best_move(result: dict[str, Any], prepared: Any, rank_by_id: dict[str, int]) -> dict[str, Any] | None:
    best = result["best_move"]
    if best is None:
        return None
    values = []
    for label in ("outgoing_candidate", "incoming_candidate"):
        candidate_id = best[label]
        if candidate_id is None:
            continue
        candidate = prepared.catalog.by_id[CandidateId(candidate_id)]
        frozen = prepared.repository.by_candidate[candidate.candidate_id]
        values.append(
            {
                "role": "outgoing" if label == "outgoing_candidate" else "incoming",
                "candidate_id": candidate_id,
                "singleton_rank": rank_by_id[candidate_id],
                "mechanism": candidate.mechanism.value,
                "realization_state": frozen.state.value,
            }
        )
    return {**best, "candidates": values}


def main() -> None:
    parser = argparse.ArgumentParser()
    parser.add_argument("--dsn", default=DEFAULT_DSN)
    args = parser.parse_args()
    prepared = load_prepared_run(M27_ROOT)
    model = load_maintenance_model(M27_ROOT)
    final = load_json(M211_FINAL)
    screened_catalog, provenance = verify_lineage(prepared, model, final)
    start_digest = digest_value(final["selected_design"])
    OUT_ROOT.mkdir(parents=True, exist_ok=True)
    protocol = {
        "format_version": 1,
        "experiment": "M2.12-Census-Top-5%-One-Local-ADD-DROP-SWAP-Round-Probe",
        "status": "development-non-authoritative",
        "repo_commit": git_head(),
        "m211_protocol_digest": digest_file(M211_PROTOCOL),
        "m211_final_result_digest": digest_file(M211_FINAL),
        "screened_catalog_digest": EXPECTED_SCREENED_DIGEST,
        "workload_digest": EXPECTED["workload"],
        "incidence_digest": EXPECTED["incidence"],
        "repository_digest": EXPECTED["repository"],
        "maintenance_model_digest": EXPECTED["model"],
        "start_design_digest": start_digest,
        "start_objective": EXPECTED_START_OBJECTIVE,
        "start_cost": EXPECTED_START_COST,
        "selected_count": 110,
        "unselected_count": 116,
        "screened_candidate_count": 226,
        "scope": "exactly one complete local ADD/DROP/SWAP round; do not run a second round",
        "neighborhood_order": ["ADD", "DROP", "SWAP"],
        "budget": "414.0398034077412444",
        "performance_ceiling_seconds": PERFORMANCE_CEILING_SECONDS,
        "parser_provenance": provenance["parser_provenance"],
        "pg_build_provenance": provenance["pg_build_provenance"],
    }
    write_json(OUT_ROOT / "protocol.json", protocol)
    start = persisted_state(final, "16.14")
    first = run_probe(prepared, model, screened_catalog, start, args.dsn)
    ranks = {str(row["candidate_id"]): int(row["screening_rank"]) for row in load_screened_rows()}
    first["best_move"] = decorate_best_move(first, prepared, ranks)
    metrics_summary = {}
    for move_type, value in first["metrics"].items():
        metrics_summary[move_type] = {
            **value,
            "total_bound_pruned": value["no_improvement_bound_pruned"] + value["incumbent_bound_pruned"],
            "pruning_rate": (
                (value["no_improvement_bound_pruned"] + value["incumbent_bound_pruned"])
                / (value["conceptual_moves"] - value["infeasible"])
                if value["conceptual_moves"] - value["infeasible"]
                else 0.0
            ),
            "best_objective": metric_value(value["best_objective"]),
        }
    write_json(OUT_ROOT / "move-type-summary.json", metrics_summary)
    write_json(OUT_ROOT / "local-round-result.json", {k: v for k, v in first.items() if k != "metrics"})
    write_json(OUT_ROOT / "best-move.json", first["best_move"] or {"move_type": None})
    repeat = run_probe(prepared, model, screened_catalog, start, args.dsn)
    repeat["best_move"] = decorate_best_move(repeat, prepared, ranks)
    repeatability = {
        "first_elapsed_seconds": first["aggregate"]["elapsed_seconds"],
        "repeat_elapsed_seconds": repeat["aggregate"]["elapsed_seconds"],
        "exact_move_counts": first["aggregate"]["total_conceptual_moves"] == repeat["aggregate"]["total_conceptual_moves"],
        "exact_pruning_counts": (
            first["aggregate"]["total_bound_pruned"] == repeat["aggregate"]["total_bound_pruned"]
            and first["aggregate"]["native_evaluated"] == repeat["aggregate"]["native_evaluated"]
        ),
        "exact_best_move": first["best_move"] == repeat["best_move"],
        "exact_objective_and_cost": (
            first["after_objective"] == repeat["after_objective"]
            and first["after_cost"] == repeat["after_cost"]
        ),
        "same_start_design": first["selected_design"] == repeat["selected_design"],
    }
    repeatability["exact"] = all(repeatability[key] for key in repeatability if key.startswith("exact_"))
    write_json(OUT_ROOT / "repeatability.json", repeatability)
    final_sequence = final["accepted_sequence"]
    gains = [float(item["contextual_improvement"]) for item in final_sequence]
    last10 = gains[-10:]
    comparison = {
        "m211_last_add_gain": gains[-1],
        "m211_last_10_add_gain_median": statistics.median(last10),
        "m211_total_add_only_improvement": EXPECTED_BASELINE - EXPECTED_START_OBJECTIVE,
        "local_round_absolute_improvement": first["best_move"]["absolute_improvement"] if first["best_move"] else 0.0,
        "local_round_relative_improvement": first["best_move"]["relative_improvement"] if first["best_move"] else 0.0,
    }
    summary = {
        "protocol_digest": digest_file(OUT_ROOT / "protocol.json"),
        "screened_catalog_digest": EXPECTED_SCREENED_DIGEST,
        "first": first,
        "repeatability": repeatability,
        "improvement_comparison": comparison,
        "no_improving_add": first["metrics"]["add"]["best_objective"] >= EXPECTED_START_OBJECTIVE,
    }
    write_json(OUT_ROOT / "summary.json", summary)
    report = [
        "# M2.12 Census top-5% one-local-round probe",
        "",
        "This development probe starts from the persisted M2.11 ADD-local optimum and evaluates exactly one complete local ADD/DROP/SWAP neighborhood. It does not run a second round, resume M2.7, perform physical validation, or change the screening universe.",
        "",
        f"The starting design has 110 selected and 116 unselected candidates, objective {EXPECTED_START_OBJECTIVE:.12f}, and cost {EXPECTED_START_COST}. The conceptual neighborhood is {first['aggregate']['total_conceptual_moves']} moves (expected 116 ADD + 110 DROP + 110*116 SWAP = 12986).",
        "",
        f"The first round took {first['aggregate']['elapsed_seconds']:.3f}s, evaluated {first['aggregate']['native_evaluated']} moves, bound-pruned {first['aggregate']['total_bound_pruned']} ({first['aggregate']['pruning_rate']:.6%}), and issued {first['aggregate']['planner_calls']} planner calls. It found {first['best_move']['move_type'] if first['best_move'] else 'no'} strict-improving best move.",
        "",
        f"The exact repeat matched move counts, pruning/native counts, best move, objective, and cost: {repeatability['exact']}. Repeat elapsed time was {repeatability['repeat_elapsed_seconds']:.3f}s.",
        "",
        f"No improving ADD existed: {summary['no_improving_add']}. Relative to M2.11's last ADD gain ({comparison['m211_last_add_gain']:.12f}) and median last-10 gain ({comparison['m211_last_10_add_gain_median']:.12f}), the local refinement gain was {comparison['local_round_absolute_improvement']:.12f}.",
        "",
        "This is descriptive development evidence; it does not change the Census default profile or establish a policy.",
    ]
    (OUT_ROOT / "report.md").write_text("\n".join(report) + "\n")
    print(json.dumps(summary, indent=2, sort_keys=True))


if __name__ == "__main__":
    main()
