"""Run the DMV frozen-sample full-catalog ADD-only feasibility search."""

from __future__ import annotations

import csv
import hashlib
import json
import statistics
import subprocess
import time
from collections import Counter
from decimal import Decimal
from pathlib import Path
from typing import Any

import psycopg

from pg_extstats_advisor.cost.empirical import EmpiricalMechanismCountCostModel
from pg_extstats_advisor.cost.model import MaintenanceBudget
from pg_extstats_advisor.evaluator.native import NativeEvaluator
from pg_extstats_advisor.models import Design, Move
from pg_extstats_advisor.orchestration import load_prepared_run
from pg_extstats_advisor.payloads.repository import PayloadRepository
from pg_extstats_advisor.postgres.adapter import PostgresAdapter
from pg_extstats_advisor.search.deterministic import DeterministicBudgetSearch
from pg_extstats_advisor.search.model import SearchConfig, candidate_catalog_digest

ROOT = Path(__file__).resolve().parents[1]
PREPARED_ROOT = ROOT / "experiments/dmv-m2-15-singletons/prepared-run"
FROZEN_ROOT = ROOT / "datasets/dmv-frozen-acquisition-sample-v1"
FROZEN_REPO_ROOT = ROOT / "experiments/dmv-m2-17b-frozen-acquisition-sample/build-1/repository"
M217B_BUILD = ROOT / "experiments/dmv-m2-17b-frozen-acquisition-sample/build-1/summary.json"
SINGLETON_PATH = ROOT / "experiments/dmv-m2-17c-frozen-singletons/singleton-profile.json"
MODEL_PATH = ROOT / "calibration/dmv-pg16.14-m2-16-r1/maintenance-model.json"
OUT = ROOT / "experiments/dmv-m2-17d-frozen-full72-add"
DSN = f"host={ROOT}/.build/pg16.14-experiment-socket port=55436 dbname=pgextadv_exp16_dmv user=postgres"
TARGET = "public.dmv"
SAMPLE = "public.pgextadv_frozen_sample"
SAMPLE_DIGEST = "59dc8dbe007a81cbd10a11894ff8abf9b4a8f0cac11523221dbd63e34dd4ca7f"
BINARY_DIGEST = "c3b30ddfebf118cb9137bae122e2913e19e88d0cbee14b2c93e980c7693cf463"
EXPECTED_BASELINE = 42791.986480127205
EXPECTED_VECTOR_DIGEST = "f3254350d068cbd625fffd5537cd25573961da71779977e73811c1d6acb4a93f"
EXPECTED_BASE_STATS_DIGEST = "bf6db08fd40e3873e1fc51675b0ff77de011b20fa20bcdf4f2e5817c4f7fc4fc"
EXPECTED_REPOSITORY_DIGEST = "0e928015a57a20624775c355e6dbb08e56cedd4437f5f54a7d0801c5aa7c6807"
EXPECTED_MODEL_DIGEST = "f8885af9b1411bb417dcb1fb93f5e368d5e89c0eac29e7803d0c5e8563204714"
EXPECTED_PATCH_SHA = "22c7f48632585e81fd8a557dc8bffba873ac5da070aca31713e22c60261c3b4f"
UPSTREAM_SHA = "f6d077142737920858ce958ccdb75c6ee137a63b5b0853c70693d401ac7e3471"
PERFORMANCE_CEILING_SECONDS = 600.0
ROUND_FIELDS = [
    "round", "selected_count_before", "objective_before", "cost_before",
    "remaining_candidates", "feasible_candidates", "budget_infeasible",
    "lower_bound_pruned", "native_evaluated", "planner_calls", "affected_replans",
    "accepted_candidate", "mechanism", "realization_state", "frozen_singleton_rank",
    "frozen_singleton_improvement", "contextual_improvement", "objective_after",
    "cost_after", "round_elapsed_seconds",
]
ACCEPTED_FIELDS = [
    "accepted_round", "candidate_id", "mechanism", "realization_state",
    "frozen_singleton_rank", "frozen_singleton_improvement", "before_objective",
    "after_objective", "contextual_improvement", "cost_after", "affected_query_count",
]


def digest(value: Any) -> str:
    return hashlib.sha256(
        json.dumps(value, sort_keys=True, separators=(",", ":"), default=str).encode()
    ).hexdigest()


def git_head() -> str:
    return subprocess.run(
        ["git", "rev-parse", "HEAD"], cwd=ROOT, check=True, capture_output=True, text=True
    ).stdout.strip()


def quote_ident(value: str) -> str:
    return '"' + value.replace('"', '""') + '"'


def verify_sample() -> dict[str, Any]:
    manifest = json.loads((FROZEN_ROOT / "manifest.json").read_text())
    binary = (FROZEN_ROOT / "sample.copy.bin").read_bytes()
    if manifest["semantic_sha256"] != SAMPLE_DIGEST:
        raise RuntimeError("frozen sample semantic digest mismatch")
    if manifest["sample_file_sha256"] != BINARY_DIGEST or hashlib.sha256(binary).hexdigest() != BINARY_DIGEST:
        raise RuntimeError("frozen sample binary digest mismatch")
    if manifest["row_count"] != 30000 or manifest["source_relation_row_count"] != 11591877:
        raise RuntimeError("frozen sample cardinality mismatch")
    if int(manifest["totalrows_used_by_builder"]) != 11687702:
        raise RuntimeError("frozen totalrows mismatch")
    return manifest


def ordinary_stats_digest(conn: psycopg.Connection[Any]) -> str:
    rows = conn.execute(
        "SELECT starelid::regclass::text,staattnum,stainherit,stanullfrac,stawidth,stadistinct,"
        "stakind1,stakind2,stakind3,stakind4,stakind5,"
        "staop1::oid::text,staop2::oid::text,staop3::oid::text,staop4::oid::text,staop5::oid::text,"
        "stacoll1::oid::text,stacoll2::oid::text,stacoll3::oid::text,stacoll4::oid::text,stacoll5::oid::text,"
        "stanumbers1::text,stanumbers2::text,stanumbers3::text,stanumbers4::text,stanumbers5::text,"
        "stavalues1::text,stavalues2::text,stavalues3::text,stavalues4::text,stavalues5::text "
        "FROM pg_statistic WHERE starelid='public.dmv'::regclass ORDER BY staattnum,stainherit"
    ).fetchall()
    keys = (
        "relation", "attnum", "inherit", "nullfrac", "width", "distinct", "kind1", "kind2",
        "kind3", "kind4", "kind5", "op1", "op2", "op3", "op4", "op5", "coll1", "coll2",
        "coll3", "coll4", "coll5", "numbers1", "numbers2", "numbers3", "numbers4", "numbers5",
        "values1", "values2", "values3", "values4", "values5",
    )
    return digest([dict(zip(keys, row, strict=True)) for row in rows])


def estimate_vector_digest(state: Any) -> str:
    return digest([
        {
            "query_id": str(item.query_id), "estimate": item.estimate, "truth": item.truth,
            "contribution": item.contribution, "provenance": item.provenance,
        }
        for item in state.query_evaluations
    ])


def stat_names(repository: PayloadRepository) -> tuple[str, ...]:
    return tuple(str(dict(item.candidate.definition)["statistics_name"]) for item in repository.payloads)


def drop_definitions(conn: psycopg.Connection[Any], repository: PayloadRepository) -> None:
    for item in repository.payloads:
        candidate = item.candidate
        schema = candidate.relation_name.split(".", 1)[0]
        name = str(dict(candidate.definition)["statistics_name"])
        conn.execute(f"DROP STATISTICS IF EXISTS {quote_ident(schema)}.{quote_ident(name)}")


def create_definitions(conn: psycopg.Connection[Any], repository: PayloadRepository) -> None:
    for item in repository.payloads:
        candidate = item.candidate
        definition = dict(candidate.definition)
        schema, relation = candidate.relation_name.split(".", 1)
        mechanism = "mcv" if candidate.mechanism.value == "mcv" else "dependencies"
        attrs = ", ".join(quote_ident(name) for name in candidate.attributes)
        name = quote_ident(str(definition["statistics_name"]))
        conn.execute(
            f"CREATE STATISTICS {quote_ident(schema)}.{name} ({mechanism}) ON {attrs} "
            f"FROM {quote_ident(schema)}.{quote_ident(relation)}"
        )
        conn.execute(f"ALTER STATISTICS {quote_ident(schema)}.{name} SET STATISTICS 100")


def stat_counts(conn: psycopg.Connection[Any]) -> tuple[int, int]:
    stats = int(conn.execute("SELECT count(*) FROM pg_statistic_ext WHERE stxname LIKE 'pgextadv_acq_%'").fetchone()[0])
    data = int(conn.execute(
        "SELECT count(*) FROM pg_statistic_ext_data d JOIN pg_statistic_ext e ON e.oid=d.stxoid "
        "WHERE e.stxname LIKE 'pgextadv_acq_%'"
    ).fetchone()[0])
    return stats, data


def build_preflight(repository: PayloadRepository, manifest: dict[str, Any]) -> dict[str, Any]:
    """Build ordinary stats once from the persisted sample, then leave definitions empty."""
    with psycopg.connect(DSN) as conn:
        drop_definitions(conn, repository)
        conn.execute(f"DROP TABLE IF EXISTS {SAMPLE}")
        conn.execute(f"CREATE UNLOGGED TABLE {SAMPLE} (LIKE {TARGET} INCLUDING DEFAULTS)")
        with conn.cursor().copy(f"COPY {SAMPLE} FROM STDIN (FORMAT binary)") as copy:
            copy.write((FROZEN_ROOT / "sample.copy.bin").read_bytes())
        create_definitions(conn, repository)
        conn.commit()
        conn.execute("SELECT set_config(%s,%s,false)", ("pg_extstats.frozen_sample_mode", "replay"))
        conn.execute("SELECT set_config(%s,%s,false)", ("pg_extstats.frozen_sample_relation", SAMPLE))
        conn.execute("SELECT set_config(%s,%s,false)", ("pg_extstats.frozen_totalrows", "11687702"))
        conn.execute(f"ANALYZE {TARGET}")
        conn.commit()
        base_digest = ordinary_stats_digest(conn)
        payload_counts = stat_counts(conn)
        if base_digest != EXPECTED_BASE_STATS_DIGEST or payload_counts != (72, 72):
            raise RuntimeError(f"frozen preflight mismatch: base={base_digest}, counts={payload_counts}")
        # Ordinary pg_statistic rows remain valid after dropping extstats. Recreate
        # definitions without ANALYZE so the search starts with zero data rows.
        drop_definitions(conn, repository)
        conn.execute(f"DROP TABLE IF EXISTS {SAMPLE}")
        conn.execute("SELECT pg_hypothetical_extstats_reset()")
        conn.execute("SELECT set_config(%s,%s,false)", ("pg_extstats.frozen_sample_mode", "off"))
        conn.execute("SELECT set_config(%s,%s,false)", ("pg_extstats.frozen_sample_relation", ""))
        conn.execute("SELECT set_config(%s,%s,false)", ("pg_extstats.frozen_totalrows", "0"))
        create_definitions(conn, repository)
        conn.commit()
        definition_counts = stat_counts(conn)
        if definition_counts != (72, 0):
            raise RuntimeError(f"definition-only preflight mismatch: {definition_counts}")
        return {
            "sample_manifest": str(FROZEN_ROOT / "manifest.json"),
            "sample_digest": SAMPLE_DIGEST,
            "sample_binary_sha256": BINARY_DIGEST,
            "sample_rows": manifest["row_count"],
            "original_relation_rows": manifest["source_relation_row_count"],
            "frozen_totalrows": manifest["totalrows_used_by_builder"],
            "ordinary_statistics_digest": base_digest,
            "replay_analyze_count_before_search": 1,
            "payload_rows_during_replay": payload_counts[1],
            "definition_count_before_search": definition_counts[0],
            "definition_data_rows_before_search": definition_counts[1],
            "sample_relation_removed_before_search": conn.execute(
                "SELECT to_regclass('public.pgextadv_frozen_sample')"
            ).fetchone()[0] is None,
            "physical_oid_resolution": "definition-name lookup by PostgresAdapter",
        }


def write_json(path: Path, value: Any) -> None:
    path.write_text(json.dumps(value, sort_keys=True, indent=2, default=str) + "\n")


def write_csv(path: Path, rows: list[dict[str, Any]], fields: list[str]) -> None:
    with path.open("w", newline="") as handle:
        writer = csv.DictWriter(handle, fieldnames=fields, lineterminator="\n")
        writer.writeheader()
        writer.writerows({key: row.get(key) for key in fields} for row in rows)


def quantile(values: list[float], probability: float) -> float:
    if not values:
        raise ValueError("cannot compute quantile of empty values")
    ordered = sorted(values)
    position = (len(ordered) - 1) * probability
    lower = int(position)
    upper = min(lower + 1, len(ordered) - 1)
    fraction = position - lower
    return ordered[lower] + (ordered[upper] - ordered[lower]) * fraction


def query_distribution(state: Any) -> dict[str, float | int]:
    values = [float(item.contribution) for item in state.query_evaluations]
    return {
        "mean": statistics.fmean(values), "median": statistics.median(values),
        "p90": quantile(values, 0.9), "max": max(values), "query_count": len(values),
    }


def accepted_candidate_row(
    candidate_id: str, singleton: dict[str, dict[str, Any]], repository: PayloadRepository,
    accepted_round: int, before: Any, after: Any, affected: int,
) -> dict[str, Any]:
    item = singleton[candidate_id]
    return {
        "accepted_round": accepted_round, "candidate_id": candidate_id,
        "mechanism": item["mechanism"], "realization_state": item["realization_state"],
        "frozen_singleton_rank": item["singleton_rank"],
        "frozen_singleton_improvement": item["singleton_improvement"],
        "before_objective": before.aggregate_objective, "after_objective": after.aggregate_objective,
        "contextual_improvement": before.aggregate_objective - after.aggregate_objective,
        "cost_after": None, "affected_query_count": affected,
    }


def run_once(
    prepared: Any, repository: PayloadRepository, model: EmpiricalMechanismCountCostModel,
    budget: MaintenanceBudget, singleton: dict[str, dict[str, Any]], run_id: str,
    write_rounds: bool,
) -> dict[str, Any]:
    started = time.perf_counter()
    rows: list[dict[str, Any]] = []
    accepted_rows: list[dict[str, Any]] = []
    timed_out = False
    with psycopg.connect(DSN) as conn:
        adapter = PostgresAdapter(conn, repository)
        evaluator = NativeEvaluator(prepared.workload, repository, prepared.incidence, adapter)
        config = SearchConfig(
            exact_bound_pruning=True, record_pruned_moves=False, add_only=True,
            candidate_set_mode="full", budget_mode="full-catalog-total", visible_candidate_count=72,
        )
        search = DeterministicBudgetSearch(evaluator, repository.catalog, model, budget, config, prepared.incidence)
        search._reset_run_state()
        initial = evaluator.evaluate_design(Design(()))
        if initial.aggregate_objective != EXPECTED_BASELINE:
            raise RuntimeError(f"search baseline mismatch: {initial.aggregate_objective}")
        if estimate_vector_digest(initial) != EXPECTED_VECTOR_DIGEST:
            raise RuntimeError("search baseline estimate-vector mismatch")
        search._calls = 1
        current = initial
        current_cost = Decimal(0)
        round_no = 0
        while True:
            if time.perf_counter() - started >= PERFORMANCE_CEILING_SECONDS:
                timed_out = True
                break
            round_no += 1
            before = current
            before_cost = current_cost
            baseline_counters = (
                search._considered, search._skipped, search._bound_pruned_no_improvement,
                search._bound_pruned_incumbent, search._evaluated, adapter.planner_calls_total,
            )
            remaining = search._ordered_ids(False, before.design)
            round_started = time.perf_counter()
            winner = search._finish_streaming_round(
                "greedy-add", before, before_cost,
                (Move.add_candidate(item) for item in remaining),
            )
            elapsed = time.perf_counter() - round_started
            after = winner.state if winner is not None else before
            after_cost = winner.cost if winner is not None else before_cost
            considered = search._considered - baseline_counters[0]
            infeasible = search._skipped - baseline_counters[1]
            no_bound = search._bound_pruned_no_improvement - baseline_counters[2]
            incumbent_bound = search._bound_pruned_incumbent - baseline_counters[3]
            native = search._evaluated - baseline_counters[4]
            planners = adapter.planner_calls_total - baseline_counters[5]
            feasible = considered - infeasible
            accepted_id = str(winner.move.add) if winner is not None and winner.move.add else ""
            accepted_item = singleton.get(accepted_id, {})
            round_row = {
                "round": round_no, "selected_count_before": len(before.design.candidate_ids),
                "objective_before": before.aggregate_objective, "cost_before": str(before_cost),
                "remaining_candidates": len(remaining), "feasible_candidates": feasible,
                "budget_infeasible": infeasible, "lower_bound_pruned": no_bound + incumbent_bound,
                "native_evaluated": native, "planner_calls": planners, "affected_replans": planners,
                "accepted_candidate": accepted_id, "mechanism": accepted_item.get("mechanism", ""),
                "realization_state": accepted_item.get("realization_state", ""),
                "frozen_singleton_rank": accepted_item.get("singleton_rank"),
                "frozen_singleton_improvement": accepted_item.get("singleton_improvement"),
                "contextual_improvement": (before.aggregate_objective - after.aggregate_objective) if winner else 0.0,
                "objective_after": after.aggregate_objective, "cost_after": str(after_cost),
                "round_elapsed_seconds": elapsed,
            }
            rows.append(round_row)
            if winner is not None:
                accepted_rows.append(accepted_candidate_row(
                    accepted_id, singleton, repository, round_no, before, after,
                    len(after.affected_query_ids),
                ))
                accepted_rows[-1]["cost_after"] = str(after_cost)
            current, current_cost = after, after_cost
            if write_rounds:
                write_csv(OUT / "round-results.csv", rows, ROUND_FIELDS)
                write_csv(OUT / "accepted-moves.csv", accepted_rows, ACCEPTED_FIELDS)
            if time.perf_counter() - started >= PERFORMANCE_CEILING_SECONDS:
                timed_out = True
                break
            if winner is None:
                break
        stats_before_cleanup = stat_counts(conn)
        native_evaluated_candidate_ids = sorted({
            str(record.move.add)
            for record in search._trajectory
            if record.move.add is not None
        })
        adapter.reset_overlay()
        return {
            "run_id": run_id, "status": "performance-ceiling" if timed_out else "complete",
            "timed_out": timed_out, "initial": initial, "final": current, "rounds": rows,
            "accepted": accepted_rows, "elapsed_seconds": time.perf_counter() - started,
            "stats_during_search": stats_before_cleanup,
            "adapter_planner_calls": adapter.planner_calls_total,
            "adapter_registration_calls": adapter.registration_calls,
            "adapter_activation_calls": adapter.activation_calls,
            "native_evaluated_candidate_ids": native_evaluated_candidate_ids,
            "search": search,
        }


def state_semantics(run: dict[str, Any]) -> dict[str, Any]:
    final = run["final"]
    return {
        "accepted_sequence": [row["candidate_id"] for row in run["accepted"]],
        "mechanism_sequence": [row["mechanism"] for row in run["accepted"]],
        "realization_sequence": [row["realization_state"] for row in run["accepted"]],
        "objective_trajectory": [row["objective_after"] for row in run["rounds"]],
        "cost_trajectory": [row["cost_after"] for row in run["rounds"]],
        "selected_design": [str(item) for item in final.design.candidate_ids],
        "selected_objective": final.aggregate_objective,
        "selected_cost": str(run["search"].cost_model.estimate_design(final.design, run["search"].catalog)),
        "round_count": len(run["rounds"]),
        "accepted_move_count": run["search"]._accepted,
        "termination_reason": "performance-ceiling" if run["timed_out"] else "add-local-optimum",
        "total_neighbor_moves_considered": run["search"]._considered,
        "budget_infeasible": run["search"]._skipped,
        "bound_pruned_no_improvement": run["search"]._bound_pruned_no_improvement,
        "bound_pruned_incumbent": run["search"]._bound_pruned_incumbent,
        "native_evaluated": run["search"]._evaluated,
        "evaluator_calls": run["search"]._calls,
    }


def final_result(
    run: dict[str, Any], singleton: dict[str, dict[str, Any]], budget: MaintenanceBudget,
    prepared: Any,
) -> dict[str, Any]:
    initial, final = run["initial"], run["final"]
    before = [item.contribution for item in initial.query_evaluations]
    after = [item.contribution for item in final.query_evaluations]
    deltas = [x - y for x, y in zip(before, after)]
    selected = [str(item) for item in final.design.candidate_ids]
    details = []
    for cid in selected:
        item = singleton[cid]
        details.append({
            "candidate_id": cid, "mechanism": item["mechanism"], "realization_state": item["realization_state"],
            "frozen_singleton_rank": item["singleton_rank"], "frozen_singleton_improvement": item["singleton_improvement"],
            "accepted_round": next(row["accepted_round"] for row in run["accepted"] if row["candidate_id"] == cid),
        })
    return {
        "format_version": 1, "status": run["status"], "termination_reason": state_semantics(run)["termination_reason"],
        "baseline_objective": initial.aggregate_objective, "final_objective": final.aggregate_objective,
        "absolute_improvement": initial.aggregate_objective - final.aggregate_objective,
        "relative_improvement": (initial.aggregate_objective - final.aggregate_objective) / initial.aggregate_objective,
        "selected_design": selected, "selected_count": len(selected),
        "selected_mcv_count": sum(item["mechanism"] == "mcv" for item in details),
        "selected_fd_count": sum(item["mechanism"] == "fd" for item in details),
        "selected_present_count": sum(item["realization_state"] == "PRESENT" for item in details),
        "selected_absent_native_count": sum(item["realization_state"] == "ABSENT_NATIVE" for item in details),
        "selected_maintenance_cost": str(run["search"].cost_model.estimate_design(final.design, run["search"].catalog)),
        "budget": str(budget.value), "budget_unit": budget.unit,
        "accepted_move_count": len(run["accepted"]), "round_count_including_final_no_improvement": len(run["rounds"]),
        "elapsed_seconds": run["elapsed_seconds"], "candidate_details": details,
        "empty_ce_distribution": query_distribution(initial), "final_ce_distribution": query_distribution(final),
        "improved_query_count": sum(delta > 0 for delta in deltas),
        "unchanged_query_count": sum(delta == 0 for delta in deltas),
        "worsened_query_count": sum(delta < 0 for delta in deltas),
        "baseline_estimate_vector_digest": estimate_vector_digest(initial),
        "final_estimate_vector_digest": estimate_vector_digest(final),
        "accepted_sequence": run["accepted"],
        "search_counters": {
            "conceptual_add_moves": run["search"]._considered,
            "feasible_moves": run["search"]._considered - run["search"]._skipped,
            "budget_infeasible_moves": run["search"]._skipped,
            "bound_pruned_moves": run["search"]._bound_pruned_no_improvement + run["search"]._bound_pruned_incumbent,
            "bound_pruned_no_improvement": run["search"]._bound_pruned_no_improvement,
            "bound_pruned_incumbent": run["search"]._bound_pruned_incumbent,
            "native_evaluated_moves": run["search"]._evaluated,
            "evaluator_calls": run["search"]._calls,
            "planner_calls": run["adapter_planner_calls"],
            "affected_query_replans": run["adapter_planner_calls"],
            "pruning_rate": ((run["search"]._bound_pruned_no_improvement + run["search"]._bound_pruned_incumbent) /
                             (run["search"]._considered - run["search"]._skipped)
                             if run["search"]._considered - run["search"]._skipped else 0.0),
        },
        "lineage": {
            "acquisition_sample_digest": SAMPLE_DIGEST, "repository_digest": EXPECTED_REPOSITORY_DIGEST,
            "candidate_catalog_digest": candidate_catalog_digest(run["search"].catalog.candidates),
            "incidence_digest": prepared.incidence_digest,
            "workload_digest": run["final"].workload_digest,
            "effective_workload_digest": prepared.effective_workload_digest,
            "truth_policy_digest": digest({
                "policy": "positive_truth_only",
                "effective_workload_digest": prepared.effective_workload_digest,
            }),
            "baseline_stats_digest": EXPECTED_BASE_STATS_DIGEST,
            "baseline_estimate_vector_digest": EXPECTED_VECTOR_DIGEST,
        },
    }


def main() -> int:
    if OUT.exists():
        raise RuntimeError(f"refusing to overwrite existing artifact directory: {OUT}")
    manifest = verify_sample()
    prepared = load_prepared_run(PREPARED_ROOT)
    repository = PayloadRepository.load(FROZEN_REPO_ROOT)
    if repository.digest != EXPECTED_REPOSITORY_DIGEST:
        raise RuntimeError("frozen repository digest mismatch")
    if [str(item.candidate_id) for item in repository.catalog.candidates] != [str(item.candidate_id) for item in prepared.catalog.candidates]:
        raise RuntimeError("candidate identity/order changed")
    if sum(item.mechanism.value == "mcv" for item in repository.catalog.candidates) != 36 or sum(item.mechanism.value == "fd" for item in repository.catalog.candidates) != 36:
        raise RuntimeError("candidate universe is not 36 MCV + 36 FD")
    singleton_raw = json.loads(SINGLETON_PATH.read_text())
    if singleton_raw["acquisition_sample_digest"] != SAMPLE_DIGEST or singleton_raw["profile_digest"] == "":
        raise RuntimeError("M2.17c singleton lineage mismatch")
    singleton = {row["candidate_id"]: row for row in singleton_raw["candidates"]}
    if len(singleton) != 72 or singleton_raw["run1_run2_exact"] is not True:
        raise RuntimeError("M2.17c singleton profile is incomplete")
    model = EmpiricalMechanismCountCostModel.load(MODEL_PATH)
    if model.digest != EXPECTED_MODEL_DIGEST:
        raise RuntimeError("accepted M2.16 model digest mismatch")
    budget_value = sum((model.estimate_candidate(item) for item in repository.catalog.candidates), Decimal(0))
    budget = MaintenanceBudget(budget_value, model.unit)
    if budget_value != Decimal("372.045872636249472"):
        raise RuntimeError(f"full catalog budget mismatch: {budget_value}")
    OUT.mkdir(parents=True)
    preflight = build_preflight(repository, manifest)
    protocol = {
        "milestone": "M2.17d", "artifact_type": "authoritative-frozen-sample-full72-add-search",
        "status": "running", "repo_head": git_head(), "acquisition_sample_digest": SAMPLE_DIGEST,
        "acquisition_sample_binary_sha256": BINARY_DIGEST, "frozen_repository_digest": repository.digest,
        "base_statistics_digest": EXPECTED_BASE_STATS_DIGEST, "baseline_objective": EXPECTED_BASELINE,
        "baseline_estimate_vector_digest": EXPECTED_VECTOR_DIGEST,
        "candidate_catalog_digest": candidate_catalog_digest(repository.catalog.candidates),
        "incidence_digest": prepared.incidence_digest,
        "workload_digest": prepared.workload.digest,
        "effective_workload_digest": prepared.effective_workload_digest,
        "truth_policy": "positive_truth_only",
        "truth_policy_digest": digest({
            "policy": "positive_truth_only",
            "effective_workload_digest": prepared.effective_workload_digest,
        }),
        "candidate_set_mode": "full", "visible_candidate_count": 72,
        "search_mode": "deterministic-best-improvement-ADD-only", "exact_bound_pruning": True,
        "budget_mode": "full-catalog-total", "budget": str(budget.value), "budget_unit": budget.unit,
        "maintenance_model_digest": model.digest, "statistics_target": model.statistics_target,
        "performance_ceiling_seconds": PERFORMANCE_CEILING_SECONDS,
        "singleton_profile_digest": singleton_raw["profile_digest"], "historical_singleton_profile_used_as_input": False,
        "random_sampling": 0, "payload_acquisition": 0, "repository_writes": 0, "maintenance_calibration": 0,
        "hot_path_create_statistics": 0, "hot_path_drop_statistics": 0, "hot_path_analyze": 0,
        "forbidden_operations": ["DROP", "SWAP", "candidate screening", "M3"],
        "upstream_tarball_sha256": UPSTREAM_SHA, "patch_sha256": EXPECTED_PATCH_SHA,
        "preflight": preflight,
    }
    write_json(OUT / "protocol.json", protocol)
    write_json(OUT / "environment.json", {
        "git_head": git_head(), "postgres_source": "/root/projects/postgresql-reference/postgresql-16.14",
        "postgres_version": "16.14", "dsn_socket": str(ROOT / ".build/pg16.14-experiment-socket"),
        "database": "pgextadv_exp16_dmv", "target_relation": TARGET,
        "source_tarball_sha256": UPSTREAM_SHA, "patch_sha256": EXPECTED_PATCH_SHA,
        "frozen_build_summary": str(M217B_BUILD), "preflight": preflight,
    })
    first = run_once(prepared, repository, model, budget, singleton, "run-1", True)
    first_final = final_result(first, singleton, budget, prepared)
    if first["timed_out"]:
        write_json(OUT / "final-result.json", first_final)
        write_json(OUT / "repeatability.json", {"status": "not-run", "reason": "first run hit 600-second ceiling"})
        protocol["status"] = "performance-ceiling"
        write_json(OUT / "protocol.json", protocol)
        raise RuntimeError("M2.17d performance ceiling exceeded")
    repeat = run_once(prepared, repository, model, budget, singleton, "run-2-fresh-backend-session", False)
    semantic_first = state_semantics(first)
    semantic_repeat = state_semantics(repeat)
    exact = semantic_first == semantic_repeat
    write_json(OUT / "final-result.json", first_final)
    write_json(OUT / "repeatability.json", {
        "run1_elapsed_seconds": first["elapsed_seconds"], "run2_elapsed_seconds": repeat["elapsed_seconds"],
        "run1": semantic_first, "run2": semantic_repeat, "exact_match": exact,
    })
    if not exact:
        raise RuntimeError("M2.17d repeatability gate failed")
    counters = first_final["search_counters"]
    round_times = [float(item["round_elapsed_seconds"]) for item in first["rounds"]]
    write_json(OUT / "performance.json", {
        **counters, "round_count": len(first["rounds"]), "mean_round_seconds": statistics.fmean(round_times),
        "median_round_seconds": statistics.median(round_times), "p90_round_seconds": quantile(round_times, 0.9),
        "max_round_seconds": max(round_times), "total_elapsed_seconds": first["elapsed_seconds"],
        "search_mode": "deterministic-best-improvement-ADD-only", "exact_bound_pruning": True,
        "performance_ceiling_seconds": PERFORMANCE_CEILING_SECONDS,
    })
    accepted = first_final["accepted_sequence"]
    nonpositive = [row for row in accepted if float(row["frozen_singleton_improvement"]) <= 0]
    absent_rows = []
    for row in singleton.values():
        if row["realization_state"] == "ABSENT_NATIVE":
            cid = row["candidate_id"]
            absent_rows.append({
                "candidate_id": cid, "attributes": row["attributes"], "mechanism": row["mechanism"],
                "singleton_objective": row["singleton_objective"], "singleton_improvement": row["singleton_improvement"],
                "singleton_rank": row["singleton_rank"],
                "native_evaluated_in_search": cid in first["native_evaluated_candidate_ids"],
                "positive_contextual_effect": any(r["accepted_candidate"] == cid and float(r["contextual_improvement"]) > 0 for r in first["rounds"]),
                "selected": cid in first_final["selected_design"],
            })
    rank_values = [int(row["frozen_singleton_rank"]) for row in accepted]
    interaction = {
        "accepted_singleton_rank": {"min": min(rank_values), "median": statistics.median(rank_values), "p90": quantile([float(x) for x in rank_values], 0.9), "max": max(rank_values)},
        "accepted_singleton_signs": dict(Counter("positive" if float(row["frozen_singleton_improvement"]) > 0 else "zero" if float(row["frozen_singleton_improvement"]) == 0 else "negative" for row in accepted)),
        "nonpositive_singleton_contextual_rescues": nonpositive,
        "absent_native_histories": absent_rows,
        "mechanism_progression": {str(n): {"mcv": sum(row["mechanism"] == "mcv" for row in accepted[:n]), "fd": sum(row["mechanism"] == "fd" for row in accepted[:n])} for n in (5, 10, 20, len(accepted))},
    }
    write_json(OUT / "interaction-evidence.json", interaction)
    cleanup = None
    with psycopg.connect(DSN) as conn:
        conn.execute("SELECT pg_hypothetical_extstats_reset()")
        drop_definitions(conn, repository)
        conn.execute(f"DROP TABLE IF EXISTS {SAMPLE}")
        conn.commit()
        cleanup = {
            "residual_statistics": stat_counts(conn)[0], "residual_data_rows": stat_counts(conn)[1],
            "sample_relation_removed": conn.execute("SELECT to_regclass('public.pgextadv_frozen_sample')").fetchone()[0] is None,
            "frozen_sample_artifact_unchanged": verify_sample()["semantic_sha256"] == SAMPLE_DIGEST and verify_sample()["sample_file_sha256"] == BINARY_DIGEST,
            "frozen_repository_unchanged": PayloadRepository.load(FROZEN_REPO_ROOT).digest == EXPECTED_REPOSITORY_DIGEST,
        }
    write_json(OUT / "cleanup.json", cleanup)
    protocol["status"] = "complete"
    write_json(OUT / "protocol.json", protocol)
    report = [
        "# M2.17d — DMV frozen-sample full-72 ADD-only search", "",
        "This authoritative development search uses the persisted frozen-sample lineage, the complete 72-candidate catalog, the accepted M2.16 mechanism-count model, deterministic best-improvement ADD-only semantics, and the existing M2.8 exact lower-bound pruning. No candidate screening, DROP, SWAP, acquisition, repository write, calibration, or hot-path ANALYZE was performed.", "",
        f"The full-catalog-total budget is `{budget.value}` {budget.unit}; the pre-search replay preflight built ordinary statistics once and then left 72 physical definitions with zero `pg_statistic_ext_data` rows. The search reached `{first_final['termination_reason']}` after {first_final['round_count_including_final_no_improvement']} rounds and {first_final['elapsed_seconds']:.3f}s.", "",
        f"Objective decreased from {first_final['baseline_objective']:.12f} to {first_final['final_objective']:.12f} (absolute improvement {first_final['absolute_improvement']:.12f}; relative improvement {first_final['relative_improvement']:.6%}). The selected design contains {first_final['selected_count']} candidates ({first_final['selected_mcv_count']} MCV, {first_final['selected_fd_count']} FD; {first_final['selected_present_count']} PRESENT and {first_final['selected_absent_native_count']} ABSENT_NATIVE) at cost {first_final['selected_maintenance_cost']}.", "",
        f"The independent repeat matched accepted sequence, mechanism and realization sequences, objective/cost trajectory, final design, counters, and termination exactly: `{exact}`.", "",
        f"Efficiency: {counters['conceptual_add_moves']} conceptual ADD moves, {counters['feasible_moves']} feasible, {counters['budget_infeasible_moves']} budget-infeasible, {counters['bound_pruned_moves']} bound-pruned, {counters['native_evaluated_moves']} native-evaluated, {counters['planner_calls']} planner calls, and {counters['pruning_rate']:.6%} pruning. Round mean/median/p90/max was {statistics.fmean(round_times):.3f}/{statistics.median(round_times):.3f}/{quantile(round_times, 0.9):.3f}/{max(round_times):.3f}s.", "",
        f"Cleanup left {cleanup['residual_statistics']} experiment statistics and {cleanup['residual_data_rows']} extended-statistics data rows; the replay sample relation was removed: `{cleanup['sample_relation_removed']}`.",
    ]
    (OUT / "report.md").write_text("\n".join(report) + "\n")
    print(json.dumps({"status": "complete", "final": first_final, "repeat_exact": exact, "cleanup": cleanup}, sort_keys=True, indent=2))
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
