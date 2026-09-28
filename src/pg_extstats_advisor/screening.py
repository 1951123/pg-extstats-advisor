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
        raise ValueError("singleton CSV lacks the frozen M2.9 fields")
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
        "maintenance_cost_status": "available" if model is not None else "unavailable_for_DMV",
        "evaluator_provenance": provenance,
        "candidate_count": len(rows),
        "baseline_objective": baseline,
        "ranking_semantics": list(RANKING_SEMANTICS if model is not None else UNPRICED_RANKING_SEMANTICS),
        "candidates": _profile_rows(rows, prepared, model, baseline),
    }
    value["digest"] = _canonical_digest(value)
    return value


def build_singleton_profile_native(
    prepared: Any,
    model: Any,
    connection: Any,
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
    baseline_state = evaluator.evaluate_design(Design(()))
    rows: list[dict[str, Any]] = []
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
    return build_singleton_profile_from_rows(
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
        "maintenance_cost_status": "available" if model is not None else "unavailable_for_DMV",
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
) -> dict[str, Any]:
    if model is None:
        raise ValueError("screening requires a validated maintenance cost model")
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
