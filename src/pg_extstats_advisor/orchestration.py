"""Artifact-first, restartable MVP stage orchestration."""

from __future__ import annotations

import hashlib
import json
from dataclasses import asdict
from decimal import Decimal
from pathlib import Path
from typing import Any

from psycopg import Connection

from pg_extstats_advisor.cost.model import MaintenanceBudget
from pg_extstats_advisor.cost.preset import PresetMaintenanceCostModel
from pg_extstats_advisor.deploy.physical import PhysicalDeployer
from pg_extstats_advisor.deploy.sql import build_search_deployment_plan
from pg_extstats_advisor.evaluator.native import NativeEvaluator
from pg_extstats_advisor.incidence.index import IncidenceIndex
from pg_extstats_advisor.models import (
    CandidateId,
    Design,
    EvaluationState,
    Move,
    MoveKind,
    QueryEvaluation,
    QueryId,
    WorkloadQuery,
)
from pg_extstats_advisor.payloads.repository import PayloadRepository
from pg_extstats_advisor.postgres.adapter import PostgresAdapter
from pg_extstats_advisor.prepare.acquisition import AcquisitionResult, cleanup_acquisition
from pg_extstats_advisor.prepare.artifacts import PreparedRun
from pg_extstats_advisor.search.deterministic import DeterministicBudgetSearch
from pg_extstats_advisor.search.model import (
    MoveRecord,
    SearchConfig,
    SearchResult,
    candidate_catalog_digest,
)
from pg_extstats_advisor.validate.deployment import (
    build_validation_result,
    collect_fresh_payload_fingerprints,
    evaluate_physical,
    frozen_payload_fingerprints,
)
from pg_extstats_advisor.validate.model import ValidationProvenance
from pg_extstats_advisor.validate.report import validation_report_dict
from pg_extstats_advisor.workload.model import Workload


def _write(path: Path, value: Any) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    temporary = path.with_suffix(path.suffix + ".tmp")
    temporary.write_text(json.dumps(value, sort_keys=True, indent=2) + "\n")
    temporary.replace(path)


def _digest(value: Any) -> str:
    return hashlib.sha256(
        json.dumps(value, sort_keys=True, separators=(",", ":")).encode()
    ).hexdigest()


def load_prepared_run(root: Path) -> PreparedRun:
    summary = json.loads((root / "prepare-summary.json").read_text())
    repository = PayloadRepository.load(root / "repository")
    raw_workload = json.loads((root / "workload.json").read_text())
    workload = Workload(
        str(raw_workload["workload_id"]),
        tuple(
            WorkloadQuery(
                QueryId(str(item["query_id"])),
                str(item["sql"]),
                float(item["truth"]),
                str(item["target_relation"]),
                frozenset(map(int, item["relation_oids"])),
                item.get("label"),
            )
            for item in raw_workload["queries"]
        ),
    )
    if workload.digest != raw_workload["digest"] or workload.digest != summary["workload_digest"]:
        raise ValueError("workload artifact digest mismatch")
    candidates_raw = json.loads((root / "candidates.json").read_text())
    catalog = repository.catalog
    catalog_digest = candidate_catalog_digest(catalog.candidates)
    if (
        catalog_digest != candidates_raw["digest"]
        or catalog_digest != summary["candidate_catalog_digest"]
    ):
        raise ValueError("candidate artifact digest mismatch")
    expected = [
        {
            "candidate_id": c.candidate_id,
            "relation_oid": c.relation_oid,
            "relation_name": c.relation_name,
            "mechanism": c.mechanism.value,
            "attributes": list(c.attributes),
            "precedence_rank": c.precedence_rank,
            "backend_oid": c.backend_oid,
        }
        for c in catalog.candidates
    ]
    if candidates_raw["candidates"] != expected:
        raise ValueError("candidate artifact/repository mismatch")
    incidence_raw = json.loads((root / "incidence.json").read_text())
    edges = tuple(
        sorted(
            (str(e["candidate_id"]), str(e["query_id"]), str(e["reason"]))
            for e in incidence_raw["edges"]
        )
    )
    incidence_digest = _digest(edges)
    if incidence_digest != incidence_raw["digest"] or incidence_raw["total_edges"] != len(edges):
        raise ValueError("incidence artifact digest/count mismatch")
    if (
        incidence_raw["workload_digest"] != workload.digest
        or incidence_raw["candidate_catalog_digest"] != catalog_digest
    ):
        raise ValueError("incidence artifact lineage mismatch")
    known_candidates, known_queries = set(catalog.by_id), set(workload.by_id)
    mapping = {item: set() for item in known_candidates}
    for candidate, query, _ in edges:
        cid, qid = CandidateId(candidate), QueryId(query)
        if cid not in known_candidates or qid not in known_queries:
            raise ValueError("incidence edge references unknown identity")
        mapping[cid].add(qid)
    index = IncidenceIndex(
        tuple((key, frozenset(value)) for key, value in mapping.items()), frozenset(known_queries)
    )
    manifest = json.loads((root / "repository" / "manifest.json").read_text())
    provenance = manifest["acquisition_provenance"]
    acquisition = AcquisitionResult(
        repository,
        tuple(provenance["analyzed_relations"]),
        int(provenance["analyze_count"]),
        tuple(
            str(dict(item.candidate.definition)["statistics_name"]) for item in repository.payloads
        ),
        tuple(
            (
                str(e["relation"]),
                str(e["source_logical_fingerprint"]),
                str(e["acquisition_logical_fingerprint"]),
                bool(e["compatible"]),
            )
            for e in provenance.get("relation_compatibility", [])
        ),
    )
    if (
        repository.digest != summary["repository_digest"]
        or incidence_digest != summary["incidence_digest"]
    ):
        raise ValueError("prepared summary lineage mismatch")
    return PreparedRun(
        workload,
        catalog,
        repository,
        index,
        acquisition,
        root,
        str(summary["config_digest"]),
        catalog_digest,
        incidence_digest,
    )


def load_maintenance_model(root: Path) -> PresetMaintenanceCostModel:
    raw = json.loads((root / "maintenance-model.json").read_text())
    if raw.get("format_version") != 1 or raw.get("model_type") != "preset-development":
        raise ValueError("unsupported maintenance model artifact")
    p = raw["parameters"]
    model = PresetMaintenanceCostModel(
        p["base_mcv"], p["per_column_mcv"], p["base_fd"], p["per_column_fd"], raw["unit"]
    )
    if model.digest != raw["digest"]:
        raise ValueError("maintenance model digest mismatch")
    return model


def _state(value: dict[str, Any]) -> EvaluationState:
    evaluations = tuple(
        QueryEvaluation(
            QueryId(e["query_id"]),
            float(e["estimate"]),
            float(e["truth"]),
            float(e["contribution"]),
            e["provenance"],
        )
        for e in value["query_evaluations"]
    )
    return EvaluationState(
        Design(tuple(map(CandidateId, value["design"]))),
        evaluations,
        float(value["aggregate_objective"]),
        value["repository_digest"],
        value["workload_digest"],
        value["postgres_version"],
        value["evaluator_provenance"],
        tuple(map(QueryId, value["affected_query_ids"])),
        tuple(map(QueryId, value["reused_query_ids"])),
    )


def _state_dict(state: EvaluationState) -> dict[str, Any]:
    return {
        "design": list(state.design.candidate_ids),
        "query_evaluations": [asdict(e) for e in state.query_evaluations],
        "aggregate_objective": state.aggregate_objective,
        "repository_digest": state.repository_digest,
        "workload_digest": state.workload_digest,
        "postgres_version": state.postgres_version,
        "evaluator_provenance": state.evaluator_provenance,
        "affected_query_ids": list(state.affected_query_ids),
        "reused_query_ids": list(state.reused_query_ids),
    }


def persist_search_result(root: Path, result: SearchResult) -> None:
    trajectory = [
        {
            "phase": r.phase,
            "kind": r.move.kind.value,
            "add": r.move.add,
            "drop": r.move.drop,
            "before_design": list(r.before_design.candidate_ids),
            "after_design": list(r.after_design.candidate_ids),
            "before_objective": r.before_objective,
            "after_objective": r.after_objective,
            "before_cost": str(r.before_cost),
            "after_cost": str(r.after_cost),
            "accepted": r.accepted,
            "rejection_reason": r.rejection_reason,
            "affected_query_count": r.affected_query_count,
        }
        for r in result.trajectory
    ]
    _write(
        root / "search" / "trajectory.json",
        {"format_version": 1, "records": trajectory, "digest": _digest(trajectory)},
    )
    value = {
        "format_version": 1,
        "selected_design": list(result.selected_design.candidate_ids),
        "selected_objective": result.selected_objective,
        "selected_maintenance_cost": str(result.selected_maintenance_cost),
        "budget": {"value": str(result.budget.value), "unit": result.budget.unit},
        "cost_model_digest": result.cost_model_digest,
        "initial_design": list(result.initial_design.candidate_ids),
        "final_design": list(result.final_design.candidate_ids),
        "evaluated_moves_count": result.evaluated_moves_count,
        "infeasible_moves_skipped_count": result.infeasible_moves_skipped_count,
        "evaluator_calls_count": result.evaluator_calls_count,
        "accepted_moves_count": result.accepted_moves_count,
        "termination_reason": result.termination_reason,
        "config": asdict(result.config),
        "workload_digest": result.workload_digest,
        "repository_digest": result.repository_digest,
        "candidate_catalog_digest": result.candidate_catalog_digest,
        "selected_state": _state_dict(result.selected_state),
        "trajectory_digest": _digest(trajectory),
    }
    value["digest"] = _digest(value)
    _write(root / "search" / "result.json", value)


def load_search_result(root: Path) -> SearchResult:
    value = json.loads((root / "search" / "result.json").read_text())
    stored = value.pop("digest")
    if _digest(value) != stored:
        raise ValueError("search result digest mismatch")
    trajectory_raw = json.loads((root / "search" / "trajectory.json").read_text())
    if (
        _digest(trajectory_raw["records"]) != trajectory_raw["digest"]
        or trajectory_raw["digest"] != value["trajectory_digest"]
    ):
        raise ValueError("search trajectory digest mismatch")
    trajectory = tuple(
        MoveRecord(
            r["phase"],
            Move(
                MoveKind(r["kind"]),
                CandidateId(r["add"]) if r["add"] else None,
                CandidateId(r["drop"]) if r["drop"] else None,
            ),
            Design(tuple(map(CandidateId, r["before_design"]))),
            Design(tuple(map(CandidateId, r["after_design"]))),
            float(r["before_objective"]),
            float(r["after_objective"]) if r["after_objective"] is not None else None,
            Decimal(r["before_cost"]),
            Decimal(r["after_cost"]),
            bool(r["accepted"]),
            r["rejection_reason"],
            r["affected_query_count"],
        )
        for r in trajectory_raw["records"]
    )
    state = _state(value["selected_state"])
    result = SearchResult(
        state,
        Design(tuple(map(CandidateId, value["selected_design"]))),
        float(value["selected_objective"]),
        Decimal(value["selected_maintenance_cost"]),
        MaintenanceBudget(value["budget"]["value"], value["budget"]["unit"]),
        value["cost_model_digest"],
        Design(tuple(map(CandidateId, value["initial_design"]))),
        Design(tuple(map(CandidateId, value["final_design"]))),
        trajectory,
        int(value["evaluated_moves_count"]),
        int(value["infeasible_moves_skipped_count"]),
        int(value["evaluator_calls_count"]),
        int(value["accepted_moves_count"]),
        value["termination_reason"],
        SearchConfig(**value["config"]),
        value["workload_digest"],
        value["repository_digest"],
        value["candidate_catalog_digest"],
    )
    if (
        result.selected_state.design != result.selected_design
        or result.selected_state.aggregate_objective != result.selected_objective
    ):
        raise ValueError("search selected-state mismatch")
    prepared = load_prepared_run(root)
    model = load_maintenance_model(root)
    if (
        result.workload_digest != prepared.workload.digest
        or result.repository_digest != prepared.repository.digest
        or result.candidate_catalog_digest != prepared.candidate_catalog_digest
        or result.cost_model_digest != model.digest
    ):
        raise ValueError("search result lineage mismatch")
    if set(result.selected_state.by_query()) != set(prepared.workload.by_id):
        raise ValueError("search selected-state query universe mismatch")
    if (
        model.estimate_design(result.selected_design, prepared.catalog)
        != result.selected_maintenance_cost
    ):
        raise ValueError("search selected maintenance cost mismatch")
    if sum(record.accepted for record in result.trajectory) != result.accepted_moves_count:
        raise ValueError("search accepted-move count mismatch")
    return result


def execute_search_stage(root: Path, connection: Connection[Any], budget: str) -> SearchResult:
    prepared, model = load_prepared_run(root), load_maintenance_model(root)
    evaluator = NativeEvaluator(
        prepared.workload,
        prepared.repository,
        prepared.incidence,
        PostgresAdapter(connection, prepared.repository),
    )
    result = DeterministicBudgetSearch(
        evaluator, prepared.catalog, model, MaintenanceBudget(budget, model.unit)
    ).run()
    persist_search_result(root, result)
    _update_manifest(root, "search", result.repository_digest)
    return result


def execute_recommendation_stage(root: Path) -> Path:
    prepared, result = load_prepared_run(root), load_search_result(root)
    summary = json.loads((root / "prepare-summary.json").read_text())
    plan = build_search_deployment_plan(
        result,
        prepared.catalog,
        statistics_target=int(summary["statistics_target"]),
        validation_relations=tuple(q.target_relation for q in prepared.workload.queries),
    )
    recommendation = {
        "format_version": 1,
        "selected_design": list(result.selected_design.candidate_ids),
        "selected_candidates": [
            {
                "candidate_id": c.candidate_id,
                "mechanism": c.mechanism.value,
                "relation": c.relation_name,
                "columns": c.attributes,
                "precedence_rank": c.precedence_rank,
            }
            for c in plan.ordered_candidates
        ],
        "predicted_objective": result.selected_objective,
        "predicted_queries": [asdict(e) for e in result.selected_state.query_evaluations],
        "maintenance_cost": str(result.selected_maintenance_cost),
        "budget": str(result.budget.value),
        "budget_unit": result.budget.unit,
        "workload_digest": result.workload_digest,
        "repository_digest": result.repository_digest,
        "candidate_catalog_digest": result.candidate_catalog_digest,
        "cost_model_digest": result.cost_model_digest,
        "deployment_plan_digest": plan.sql_digest,
        "statistics_target": plan.statistics_target,
    }
    _write(root / "recommendation" / "recommendation.json", recommendation)
    _write(
        root / "recommendation" / "deployment-plan.json",
        {
            "format_version": 1,
            "sql_digest": plan.sql_digest,
            "create": plan.create_statements,
            "target": plan.target_statements,
            "analyze": plan.analyze_statements,
        },
    )
    sql = (
        "-- generated by pg-extstats-advisor\n-- repository: "
        + result.repository_digest
        + "\n"
        + ";\n".join(plan.create_statements + plan.target_statements + plan.analyze_statements)
        + ";\n"
    )
    path = root / "recommendation" / "deployment.sql"
    path.write_text(sql)
    _update_manifest(root, "recommendation", plan.sql_digest)
    return path


def execute_validation_stage(root: Path, connection: Connection[Any]) -> dict[str, Any]:
    prepared, result = load_prepared_run(root), load_search_result(root)
    summary = json.loads((root / "prepare-summary.json").read_text())
    plan = build_search_deployment_plan(
        result,
        prepared.catalog,
        statistics_target=int(summary["statistics_target"]),
        validation_relations=tuple(q.target_relation for q in prepared.workload.queries),
    )
    deployment = PhysicalDeployer(connection, "cli-validation").deploy(plan)
    fresh = evaluate_physical(
        connection, prepared.workload, result.selected_design, prepared.repository.digest
    )
    provenance = ValidationProvenance(
        "cli",
        prepared.repository.upstream_sha256,
        prepared.repository.patch_commit,
        prepared.workload.digest,
        prepared.repository.digest,
        result.cost_model_digest,
        str(result.budget.value),
        result.budget.unit,
        result.config.algorithm_version,
        plan.sql_digest,
        "cli-validation",
        plan.statistics_target,
        deployment.postgres_version,
    )
    validation = build_validation_result(
        frozen=result.selected_state,
        fresh=fresh,
        frozen_fingerprints=frozen_payload_fingerprints(
            prepared.repository, result.selected_design
        ),
        fresh_fingerprints=collect_fresh_payload_fingerprints(connection, deployment),
        deployment=deployment,
        provenance=provenance,
    )
    report = validation_report_dict(validation)
    _write(root / "validation" / "validation-report.json", report)
    compact = {
        "frozen_objective": validation.aggregate.frozen_objective,
        "fresh_objective": validation.aggregate.fresh_objective,
        "absolute_drift": validation.aggregate.absolute_objective_drift,
        "relative_drift": validation.aggregate.relative_objective_drift,
        "changed_payload_count": sum(
            not p.same_realization for p in validation.payload_comparisons
        ),
        "unchanged_payload_count": sum(p.same_realization for p in validation.payload_comparisons),
        "query_count": len(validation.per_query),
        "selected_candidate_count": len(result.selected_design.candidate_ids),
    }
    _write(root / "validation" / "summary.json", compact)
    _update_manifest(root, "validation", _digest(compact))
    return compact


def cleanup_acquisition_stage(root: Path, connection: Connection[Any]) -> None:
    prepared = load_prepared_run(root)
    cleanup_acquisition(connection, prepared.acquisition)
    _update_manifest(root, "acquisition-cleaned", prepared.repository.digest)


def _update_manifest(root: Path, stage: str, digest: str) -> None:
    path = root / "run-manifest.json"
    value = json.loads(path.read_text()) if path.exists() else {"format_version": 1, "stages": {}}
    value["stages"][stage] = {"complete": True, "artifact_digest": digest}
    _write(path, value)
