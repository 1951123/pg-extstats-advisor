"""Versioned singleton-profile and candidate-set screening artifacts."""

from __future__ import annotations

import csv
import hashlib
import json
import math
import time
from pathlib import Path
from typing import Any

from pg_extstats_advisor.analysis.singleton import (
    classify_improvement,
    deterministic_order,
    percentile_from_rank,
    top_fraction_count,
)
from pg_extstats_advisor.candidates.model import CandidateCatalog
from pg_extstats_advisor.models import Design, Move
from pg_extstats_advisor.optimization.budget import (
    OptimizationBudget,
    OptimizationBudgetExhausted,
    OptimizationStatus,
)
from pg_extstats_advisor.search.model import candidate_catalog_digest

PROFILE_FORMAT_VERSION = 1
CANDIDATE_SET_FORMAT_VERSION = 1
SCREENING_METHOD = "singleton_top_fraction"
RANKING_SEMANTICS = (
    "descending singleton improvement",
    "ascending maintenance cost",
    "ascending candidate precedence",
    "ascending candidate ID",
)
UNPRICED_RANKING_SEMANTICS = (
    "descending singleton improvement",
    "ascending candidate precedence",
    "ascending candidate ID",
)


def _planner_calls(evaluator: Any) -> int:
    adapter = getattr(evaluator, "adapter", None)
    return int(getattr(adapter, "planner_calls_total", getattr(adapter, "explain_calls", 0)))


def _canonical_digest(value: dict[str, Any]) -> str:
    canonical = {key: item for key, item in value.items() if key != "digest"}
    return hashlib.sha256(
        json.dumps(canonical, sort_keys=True, separators=(",", ":")).encode()
    ).hexdigest()


def _workload_digest(prepared: Any) -> str:
    return str(getattr(prepared, "effective_workload_digest", None) or prepared.workload.digest)


def _write(path: Path, value: dict[str, Any]) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    temporary = path.with_suffix(path.suffix + ".tmp")
    temporary.write_text(json.dumps(value, sort_keys=True, indent=2) + "\n")
    temporary.replace(path)


def write_artifact(path: Path, value: dict[str, Any]) -> str:
    result = dict(value)
    result["digest"] = _canonical_digest(result)
    _write(path, result)
    return result["digest"]


def load_artifact(path: Path) -> dict[str, Any]:
    value = json.loads(path.read_text())
    if _canonical_digest(value) != value.get("digest"):
        raise ValueError(f"artifact digest mismatch: {path}")
    return value


def _candidate_rows_from_csv(path: Path) -> list[dict[str, str]]:
    with path.open(newline="") as handle:
        rows = list(csv.DictReader(handle))
    required = {
        "candidate_id",
        "precedence_rank",
        "mechanism",
        "realization_state",
        "singleton_objective",
        "singleton_improvement",
    }
    if not rows or not required <= set(rows[0]):
        raise ValueError("singleton CSV lacks the required frozen-profile fields")
    return rows


def _profile_rows(
    rows: list[dict[str, Any]],
    prepared: Any,
    model: Any,
    baseline_objective: float,
) -> list[dict[str, Any]]:
    expected_ids = set(prepared.catalog.by_id)
    if {str(row["candidate_id"]) for row in rows} != expected_ids:
        raise ValueError("singleton profile candidate IDs do not match raw catalog")
    by_payload = prepared.repository.by_candidate
    normalized: list[dict[str, Any]] = []
    for row in rows:
        candidate_id = str(row["candidate_id"])
        candidate = prepared.catalog.by_id[candidate_id]
        improvement = float(row["singleton_improvement"])
        objective = float(row["singleton_objective"])
        if not math.isclose(improvement, baseline_objective - objective, rel_tol=0.0, abs_tol=1e-9):
            raise ValueError(f"singleton improvement mismatch for {candidate_id}")
        if model is None:
            cost = None
            if row.get("maintenance_cost_numeric") not in (None, "", "unavailable"):
                raise ValueError(f"unpriced profile contains a maintenance cost for {candidate_id}")
        else:
            cost = model.estimate_candidate(candidate)
            if float(row["maintenance_cost_numeric"]) != float(cost):
                raise ValueError(f"singleton maintenance cost mismatch for {candidate_id}")
        normalized_row = {
            "candidate_id": candidate_id,
            "precedence_rank": candidate.precedence_rank,
            "mechanism": candidate.mechanism.value,
            "relation": candidate.relation_name,
            "columns": list(candidate.attributes),
            "realization_state": by_payload[candidate.candidate_id].state.value,
            "maintenance_cost": str(cost) if cost is not None else None,
            "maintenance_cost_numeric": float(cost) if cost is not None else None,
            "maintenance_cost_status": "available" if cost is not None else "unavailable",
            "singleton_objective": objective,
            "singleton_improvement": improvement,
            "relative_improvement": improvement / baseline_objective,
            "incidence_query_count": len(prepared.incidence.by_candidate[candidate.candidate_id]),
        }
        for key in (
            "affected_query_count", "improved_query_count", "unchanged_query_count",
            "worsened_query_count", "elapsed_seconds", "planner_calls",
        ):
            if key in row:
                normalized_row[key] = row[key]
        normalized.append(normalized_row)
    ranked = deterministic_order(
        normalized,
        "singleton_improvement",
        None if model is None else "maintenance_cost_numeric",
    )
    return [
        {
            **row,
            "singleton_rank": rank,
            "singleton_percentile": percentile_from_rank(rank, len(ranked)),
        }
        for rank, row in enumerate(ranked, start=1)
    ]


def build_singleton_profile_from_csv(
    source_csv: Path,
    prepared: Any,
    model: Any,
    *,
    evaluator_provenance: dict[str, Any] | None = None,
) -> dict[str, Any]:
    """Import an existing frozen singleton table without native reevaluation."""

    rows = _candidate_rows_from_csv(source_csv)
    baseline = float(rows[0]["singleton_objective"]) + float(rows[0]["singleton_improvement"])
    if any(
        not math.isclose(
            float(row["singleton_objective"]) + float(row["singleton_improvement"]),
            baseline,
            rel_tol=0.0,
            abs_tol=1e-9,
        )
        for row in rows
    ):
        raise ValueError("singleton CSV has inconsistent baseline objective")
    provenance = evaluator_provenance or {
        "mode": "imported-frozen-profile",
        "source_sha256": hashlib.sha256(source_csv.read_bytes()).hexdigest(),
        "postgres_version": prepared.repository.postgres_version,
        "patch_commit": prepared.repository.patch_commit,
        "native_singleton_evaluations": 0,
    }
    value = {
        "format_version": PROFILE_FORMAT_VERSION,
        "artifact_type": "singleton-profile",
        "workload_digest": _workload_digest(prepared),
        "candidate_catalog_digest": prepared.candidate_catalog_digest,
        "incidence_digest": prepared.incidence_digest,
        "repository_digest": prepared.repository.digest,
        "maintenance_model_digest": model.digest if model is not None else None,
        "maintenance_cost_status": "available" if model is not None else "unavailable",
        "evaluator_provenance": provenance,
        "candidate_count": len(rows),
        "baseline_objective": baseline,
        "ranking_semantics": list(RANKING_SEMANTICS if model is not None else UNPRICED_RANKING_SEMANTICS),
        "candidates": _profile_rows(rows, prepared, model, baseline),
    }
    value["digest"] = _canonical_digest(value)
    return value


def profile_singletons_native(
    evaluator: Any,
    candidates: list[Any] | tuple[Any, ...],
    baseline_state: Any,
    *,
    budget: OptimizationBudget,
    prepared: Any | None = None,
    model: Any | None = None,
) -> dict[str, Any]:
    """Evaluate a complete candidate pool under one shared optimization budget.

    A partial profile is deliberately returned as a diagnostic artifact only;
    callers must not derive precedence or start greedy search from it.
    """

    budget.start()
    binder = getattr(evaluator, "bind_optimization_budget", None)
    if binder is not None:
        binder(budget, "singleton")
    started = time.perf_counter()
    rows: list[dict[str, Any]] = []
    planner_before = _planner_calls(evaluator)
    stop: OptimizationBudgetExhausted | None = None
    for candidate in candidates:
        try:
            budget.check("singleton")
            cell_started = time.perf_counter()
            before_calls = _planner_calls(evaluator)
            state = evaluator.evaluate_move(
                baseline_state.design,
                Move.add_candidate(candidate.candidate_id),
                baseline_state,
            )
            baseline_by_query = baseline_state.by_query()
            candidate_by_query = state.by_query()
            changes = [
                classify_improvement(
                    baseline_by_query[qid].contribution,
                    candidate_by_query[qid].contribution,
                )
                for qid in baseline_by_query
            ]
            rows.append(
                {
                    "candidate_id": str(candidate.candidate_id),
                    "precedence_rank": candidate.precedence_rank,
                    "mechanism": candidate.mechanism.value,
                    "realization_state": (
                        prepared.repository.by_candidate[candidate.candidate_id].state.value
                        if prepared is not None
                        else "UNKNOWN"
                    ),
                    "maintenance_cost_numeric": (
                        float(model.estimate_candidate(candidate)) if model is not None else None
                    ),
                    "singleton_objective": state.aggregate_objective,
                    "singleton_improvement": baseline_state.aggregate_objective - state.aggregate_objective,
                    "affected_query_count": len(state.affected_query_ids),
                    "improved_query_count": changes.count("positive"),
                    "unchanged_query_count": changes.count("zero"),
                    "worsened_query_count": changes.count("negative"),
                    "elapsed_seconds": time.perf_counter() - cell_started,
                    "planner_calls": _planner_calls(evaluator) - before_calls,
                }
            )
            # A candidate whose last EXPLAIN completed is a completed profile
            # cell, but the profile is still incomplete if the deadline is now
            # reached.  No precedence may be constructed from this result.
            budget.check("singleton")
        except OptimizationBudgetExhausted as error:
            stop = error
            break
    completed = len(rows)
    total = len(candidates)
    status = OptimizationStatus.COMPLETED if completed == total else OptimizationStatus.BUDGET_EXHAUSTED_DURING_SINGLETON
    planner_calls = _planner_calls(evaluator) - planner_before
    result = {
        "status": status.value,
        "singleton_profile_complete": completed == total,
        "total_singleton_candidates": total,
        "completed_singleton_candidates": completed,
        "remaining_singleton_candidates": total - completed,
        "elapsed_seconds": time.perf_counter() - started,
        "planner_calls": planner_calls,
        "rows": rows,
        "budget_seconds": budget.limit_seconds,
        "budget_elapsed_seconds": budget.elapsed_seconds,
        "budget_exhausted": budget.exhausted,
        "stop_phase": stop.phase if stop is not None else None,
        "stop_reason": str(stop) if stop is not None else None,
    }
    return result


def freeze_singleton_precedence(
    rows: list[dict[str, Any]],
    *,
    profile_complete: bool,
    expected_candidate_count: int | None = None,
    optimization_budget: OptimizationBudget | None = None,
) -> list[dict[str, Any]]:
    """Freeze complete singleton precedence only after the profile is complete."""

    if not profile_complete:
        raise ValueError("cannot freeze precedence from an incomplete singleton profile")
    if expected_candidate_count is not None and len(rows) != expected_candidate_count:
        raise ValueError("singleton profile candidate count is incomplete")
    if optimization_budget is not None:
        optimization_budget.check("singleton-precedence")
    if any("singleton_improvement" not in row for row in rows):
        raise ValueError("singleton precedence requires singleton utility rows")
    ordered = deterministic_order(rows, "singleton_improvement")
    result = [
        {**row, "singleton_rank": rank}
        for rank, row in enumerate(ordered, start=1)
    ]
    if optimization_budget is not None:
        optimization_budget.check("singleton-precedence")
    return result


def build_singleton_profile_native(
    prepared: Any,
    model: Any,
    connection: Any,
    *,
    optimization_budget: OptimizationBudget | None = None,
) -> dict[str, Any]:
    """Profile all raw candidates with the frozen native evaluator."""

    from pg_extstats_advisor.evaluator.native import NativeEvaluator
    from pg_extstats_advisor.postgres.adapter import PostgresAdapter

    evaluator = NativeEvaluator(
        prepared.workload,
        prepared.repository,
        prepared.incidence,
        PostgresAdapter(connection, prepared.repository),
    )
    if optimization_budget is not None:
        optimization_budget.start()
        evaluator.bind_optimization_budget(optimization_budget, "baseline")
    try:
        baseline_state = evaluator.evaluate_design(Design(()))
    except OptimizationBudgetExhausted as stop:
        return {
            "format_version": PROFILE_FORMAT_VERSION,
            "artifact_type": "singleton-profile-partial",
            "status": OptimizationStatus.BUDGET_EXHAUSTED_DURING_BASELINE.value,
            "singleton_profile_complete": False,
            "candidate_count": len(prepared.catalog.candidates),
            "total_singleton_candidates": len(prepared.catalog.candidates),
            "completed_singleton_candidates": 0,
            "remaining_singleton_candidates": len(prepared.catalog.candidates),
            "rows": [],
            "budget_seconds": optimization_budget.limit_seconds,
            "budget_elapsed_seconds": optimization_budget.elapsed_seconds,
            "budget_exhausted": True,
            "stop_phase": stop.phase,
            "stop_reason": str(stop),
            "planner_calls": evaluator.adapter.planner_calls_total,
        }
    if optimization_budget is not None:
        profiled = profile_singletons_native(
            evaluator,
            list(prepared.catalog.candidates),
            baseline_state,
            budget=optimization_budget,
            prepared=prepared,
            model=model,
        )
        if not profiled["singleton_profile_complete"]:
            return {
                "format_version": PROFILE_FORMAT_VERSION,
                "artifact_type": "singleton-profile-partial",
                **profiled,
                "candidate_count": len(prepared.catalog.candidates),
            }
        rows = profiled["rows"]
    else:
        rows = []
        for candidate in prepared.catalog.candidates:
            before_calls = evaluator.adapter.planner_calls_total
            started = time.perf_counter()
            state = evaluator.evaluate_move(
                Design(()), Move.add_candidate(candidate.candidate_id), baseline_state
            )
            elapsed = time.perf_counter() - started
            baseline_by_query = baseline_state.by_query()
            candidate_by_query = state.by_query()
            changes = [
                classify_improvement(
                    baseline_by_query[qid].contribution, candidate_by_query[qid].contribution
                )
                for qid in baseline_by_query
            ]
            rows.append(
                {
                    "candidate_id": str(candidate.candidate_id),
                    "precedence_rank": candidate.precedence_rank,
                    "mechanism": candidate.mechanism.value,
                    "realization_state": prepared.repository.by_candidate[candidate.candidate_id].state.value,
                    "maintenance_cost_numeric": (
                        float(model.estimate_candidate(candidate)) if model is not None else None
                    ),
                    "singleton_objective": state.aggregate_objective,
                    "singleton_improvement": baseline_state.aggregate_objective - state.aggregate_objective,
                    "affected_query_count": len(state.affected_query_ids),
                    "improved_query_count": changes.count("positive"),
                    "unchanged_query_count": changes.count("zero"),
                    "worsened_query_count": changes.count("negative"),
                    "elapsed_seconds": elapsed,
                    "planner_calls": evaluator.adapter.planner_calls_total - before_calls,
                }
            )
    result = build_singleton_profile_from_rows(
        rows,
        prepared,
        model,
        baseline_state.aggregate_objective,
        evaluator_provenance={
            "mode": "native",
            "postgres_version": evaluator.adapter.postgres_version,
            "patch_commit": prepared.repository.patch_commit,
            "native_singleton_evaluations": len(rows),
            "planner_calls": evaluator.adapter.planner_calls_total,
            "affected_query_replans": sum(int(row["affected_query_count"]) for row in rows),
            "total_singleton_elapsed_seconds": sum(float(row["elapsed_seconds"]) for row in rows),
        },
    )
    if optimization_budget is not None:
        result.update(
            {
                "status": profiled["status"],
                "singleton_profile_complete": True,
                "budget_seconds": profiled["budget_seconds"],
                "budget_elapsed_seconds": profiled["budget_elapsed_seconds"],
                "budget_exhausted": False,
                "stop_phase": None,
                "stop_reason": None,
                "planner_calls": _planner_calls(evaluator),
            }
        )
    return result


def build_singleton_profile_from_rows(
    rows: list[dict[str, Any]],
    prepared: Any,
    model: Any,
    baseline_objective: float,
    *,
    evaluator_provenance: dict[str, Any],
) -> dict[str, Any]:
    value = {
        "format_version": PROFILE_FORMAT_VERSION,
        "artifact_type": "singleton-profile",
        "workload_digest": _workload_digest(prepared),
        "candidate_catalog_digest": prepared.candidate_catalog_digest,
        "incidence_digest": prepared.incidence_digest,
        "repository_digest": prepared.repository.digest,
        "maintenance_model_digest": model.digest if model is not None else None,
        "maintenance_cost_status": "available" if model is not None else "unavailable",
        "evaluator_provenance": evaluator_provenance,
        "candidate_count": len(rows),
        "baseline_objective": baseline_objective,
        "ranking_semantics": list(RANKING_SEMANTICS if model is not None else UNPRICED_RANKING_SEMANTICS),
        "candidates": _profile_rows(rows, prepared, model, baseline_objective),
    }
    value["digest"] = _canonical_digest(value)
    return value


def validate_profile(profile: dict[str, Any], prepared: Any, model: Any) -> None:
    if profile.get("digest") != _canonical_digest(profile):
        raise ValueError("singleton profile digest mismatch")
    expected = {
        "workload_digest": _workload_digest(prepared),
        "candidate_catalog_digest": prepared.candidate_catalog_digest,
        "incidence_digest": prepared.incidence_digest,
        "repository_digest": prepared.repository.digest,
        "maintenance_model_digest": model.digest if model is not None else None,
    }
    if any(profile.get(key) != value for key, value in expected.items()):
        raise ValueError("singleton profile lineage mismatch")
    if profile.get("artifact_type") != "singleton-profile" or profile.get("format_version") != 1:
        raise ValueError("unsupported singleton profile artifact")
    if len(profile.get("candidates", ())) != len(prepared.catalog.candidates):
        raise ValueError("singleton profile candidate count mismatch")


def build_candidate_set(
    profile: dict[str, Any],
    prepared: Any,
    model: Any,
    *,
    top_fraction: float,
    optimization_budget: OptimizationBudget | None = None,
) -> dict[str, Any]:
    if model is None:
        raise ValueError("screening requires a validated maintenance cost model")
    if optimization_budget is not None:
        optimization_budget.check("screening")
    validate_profile(profile, prepared, model)
    if not math.isfinite(top_fraction) or not 0 < top_fraction <= 1:
        raise ValueError("top_fraction must be finite and in (0, 1]")
    rows = deterministic_order(profile["candidates"], "singleton_improvement")
    retained_count = top_fraction_count(len(rows), top_fraction)
    retained_rows = rows[:retained_count]
    retained_ids = [str(row["candidate_id"]) for row in retained_rows]
    candidates = tuple(prepared.catalog.by_id[item] for item in retained_ids)
    screened_digest = candidate_catalog_digest(candidates)
    excluded_rows = rows[retained_count:]
    value = {
        "format_version": CANDIDATE_SET_FORMAT_VERSION,
        "artifact_type": "screened-candidate-set",
        "raw_candidate_catalog_digest": prepared.candidate_catalog_digest,
        "singleton_profile_digest": profile["digest"],
        "workload_digest": _workload_digest(prepared),
        "incidence_digest": prepared.incidence_digest,
        "repository_digest": prepared.repository.digest,
        "maintenance_model_digest": model.digest,
        "screening_method": SCREENING_METHOD,
        "ranking_semantics": list(RANKING_SEMANTICS),
        "top_fraction": top_fraction,
        "rounding_rule": "ceil(N * fraction)",
        "raw_count": len(rows),
        "retained_count": retained_count,
        "excluded_count": len(excluded_rows),
        "cutoff_singleton_improvement": float(retained_rows[-1]["singleton_improvement"]),
        "best_excluded_singleton_improvement": (
            float(excluded_rows[0]["singleton_improvement"]) if excluded_rows else None
        ),
        "candidate_ids": retained_ids,
        "screened_candidate_catalog_digest": screened_digest,
        "experimental_role": "development",
        "heuristic_restriction": True,
    }
    value["digest"] = _canonical_digest(value)
    if optimization_budget is not None:
        optimization_budget.check("screening")
    return value


def validate_candidate_set(
    artifact: dict[str, Any], prepared: Any, model: Any
) -> CandidateCatalog:
    if artifact.get("digest") != _canonical_digest(artifact):
        raise ValueError("screened candidate-set digest mismatch")
    expected = {
        "raw_candidate_catalog_digest": prepared.candidate_catalog_digest,
        "workload_digest": _workload_digest(prepared),
        "incidence_digest": prepared.incidence_digest,
        "repository_digest": prepared.repository.digest,
        "maintenance_model_digest": model.digest,
    }
    if any(artifact.get(key) != value for key, value in expected.items()):
        raise ValueError("screened candidate-set lineage mismatch")
    if (
        artifact.get("artifact_type") != "screened-candidate-set"
        or artifact.get("format_version") != CANDIDATE_SET_FORMAT_VERSION
    ):
        raise ValueError("unsupported candidate-set artifact")
    ids = tuple(str(item) for item in artifact.get("candidate_ids", ()))
    if len(ids) != artifact.get("retained_count") or len(ids) != len(set(ids)):
        raise ValueError("invalid screened candidate IDs")
    try:
        candidates = tuple(prepared.catalog.by_id[item] for item in ids)
    except KeyError as error:
        raise ValueError(f"screened candidate is absent from raw catalog: {error.args[0]}") from error
    digest = candidate_catalog_digest(candidates)
    if digest != artifact.get("screened_candidate_catalog_digest"):
        raise ValueError("screened candidate-set digest mismatch")
    return CandidateCatalog(candidates)


def candidate_set_digest(path: Path) -> str:
    return load_artifact(path)["digest"]
