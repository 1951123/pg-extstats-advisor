"""Native physical evaluation, payload fingerprinting, and drift comparison."""

from __future__ import annotations

import hashlib
from typing import Any

from psycopg import Connection

from pg_extstats_advisor.deploy.model import DeploymentResult
from pg_extstats_advisor.models import Design, EvaluationState, QueryEvaluation
from pg_extstats_advisor.objective.qerror import aggregate_objective, q_error
from pg_extstats_advisor.payloads.repository import PayloadRepository
from pg_extstats_advisor.postgres.extraction import extract_target_estimate
from pg_extstats_advisor.validate.model import (
    AggregateComparison,
    PayloadComparison,
    PayloadFingerprint,
    QueryComparison,
    ValidationProvenance,
    ValidationResult,
)
from pg_extstats_advisor.workload.model import Workload


def evaluate_physical(
    connection: Connection[Any], workload: Workload, design: Design, repository_digest: str
) -> EvaluationState:
    evaluations: list[QueryEvaluation] = []
    version = str(connection.execute("SHOW server_version").fetchone()[0])
    for query in sorted(workload.queries, key=lambda item: item.query_id):
        row = connection.execute(f"EXPLAIN (FORMAT JSON) {query.sql}").fetchone()
        if row is None:
            raise RuntimeError(f"EXPLAIN returned no row for {query.query_id}")
        estimate = extract_target_estimate(row[0], query.target_relation)
        evaluations.append(
            QueryEvaluation(
                query.query_id,
                estimate,
                query.truth,
                q_error(estimate, query.truth),
                f"physical-native-explain:{version}",
            )
        )
    ordered = tuple(evaluations)
    return EvaluationState(
        design,
        ordered,
        aggregate_objective(ordered),
        repository_digest,
        workload.digest,
        version,
        "m2-fresh-physical-native-v1",
        tuple(item.query_id for item in ordered),
        (),
    )


def collect_fresh_payload_fingerprints(
    connection: Connection[Any], deployment: DeploymentResult
) -> tuple[PayloadFingerprint, ...]:
    values: list[PayloadFingerprint] = []
    by_candidate = {item.candidate_id: item for item in deployment.plan.ordered_candidates}
    for created in deployment.created_statistics:
        candidate = by_candidate[created.candidate_id]
        if candidate.mechanism.value == "mcv":
            expression = "pg_mcv_list_send(d.stxdmcv)"
        elif candidate.mechanism.value == "fd":
            expression = "pg_dependencies_send(d.stxddependencies)"
        else:
            raise ValueError(f"unexpected mechanism: {candidate.mechanism}")
        row = connection.execute(
            f"SELECT {expression},c.reltuples::bigint "
            "FROM pg_statistic_ext e "
            "JOIN pg_statistic_ext_data d ON d.stxoid=e.oid "
            "JOIN pg_class c ON c.oid=e.stxrelid WHERE e.oid=%s",
            (created.catalog_oid,),
        ).fetchone()
        if row is None:
            raise RuntimeError(f"missing fresh pg_statistic_ext_data row for {created.candidate_id}")
        payload = None if row[0] is None else bytes(row[0])
        values.append(
            PayloadFingerprint(
                created.candidate_id,
                candidate.mechanism.value,
                created.statistics_name,
                hashlib.sha256(payload).hexdigest() if payload else None,
                len(payload) if payload else None,
                created.catalog_oid,
                created.relation_oid,
                hashlib.sha256(
                    f"{created.relation_name}:{created.relation_oid}:{int(row[1])}".encode()
                ).hexdigest(),
                "PRESENT" if payload else "ABSENT_NATIVE",
            )
        )
    return tuple(values)


def frozen_payload_fingerprints(
    repository: PayloadRepository, design: Design
) -> tuple[PayloadFingerprint, ...]:
    result = []
    for candidate_id in design.candidate_ids:
        frozen = repository.by_candidate[candidate_id]
        result.append(
            PayloadFingerprint(
                str(candidate_id),
                frozen.candidate.mechanism.value,
                str(dict(frozen.candidate.definition).get("statistics_name", candidate_id)),
                frozen.payload_sha256,
                len(frozen.payload) if frozen.payload else None,
                frozen.candidate.backend_oid,
                frozen.candidate.relation_oid,
                frozen.relation_fingerprint,
                frozen.state.value,
            )
        )
    return tuple(result)


def build_validation_result(
    *,
    frozen: EvaluationState,
    fresh: EvaluationState,
    frozen_fingerprints: tuple[PayloadFingerprint, ...],
    fresh_fingerprints: tuple[PayloadFingerprint, ...],
    deployment: DeploymentResult,
    provenance: ValidationProvenance,
    same_realization_control: EvaluationState | None = None,
) -> ValidationResult:
    if frozen.design != fresh.design or frozen.design != deployment.plan.selected_design:
        raise ValueError("selected design lineage mismatch")
    if frozen.repository_digest != provenance.repository_digest:
        raise ValueError("frozen state/repository lineage mismatch")
    if (
        frozen.workload_digest != fresh.workload_digest
        or frozen.workload_digest != provenance.workload_digest
    ):
        raise ValueError("frozen/fresh workload lineage mismatch")
    frozen_by, fresh_by = frozen.by_query(), fresh.by_query()
    if set(frozen_by) != set(fresh_by):
        raise ValueError("frozen/fresh query universe mismatch")
    per_query = tuple(
        QueryComparison(
            str(query_id),
            frozen_by[query_id].truth,
            frozen_by[query_id].estimate,
            fresh_by[query_id].estimate,
            frozen_by[query_id].contribution,
            fresh_by[query_id].contribution,
            abs(fresh_by[query_id].estimate - frozen_by[query_id].estimate),
            abs(fresh_by[query_id].estimate - frozen_by[query_id].estimate)
            / abs(frozen_by[query_id].estimate)
            if frozen_by[query_id].estimate != 0
            else None,
            fresh_by[query_id].contribution - frozen_by[query_id].contribution,
            frozen_by[query_id].estimate != fresh_by[query_id].estimate,
        )
        for query_id in sorted(frozen_by)
    )
    drift = fresh.aggregate_objective - frozen.aggregate_objective
    aggregate = AggregateComparison(
        frozen.aggregate_objective,
        fresh.aggregate_objective,
        drift,
        drift / abs(frozen.aggregate_objective) if frozen.aggregate_objective else None,
    )
    fresh_by_candidate = {item.candidate_id: item for item in fresh_fingerprints}
    if set(fresh_by_candidate) != {item.candidate_id for item in frozen_fingerprints}:
        raise ValueError("fresh payload candidate universe mismatch")
    payload_comparisons = tuple(
        PayloadComparison(
            item.candidate_id,
            item.payload_sha256,
            fresh_by_candidate[item.candidate_id].payload_sha256,
            item.state == fresh_by_candidate[item.candidate_id].state
            and item.payload_sha256 == fresh_by_candidate[item.candidate_id].payload_sha256,
            f"{item.state.lower()}-to-{fresh_by_candidate[item.candidate_id].state.lower()}"
            if item.state != fresh_by_candidate[item.candidate_id].state
            else (
                "present-same"
                if item.state == "PRESENT" and item.payload_sha256 == fresh_by_candidate[item.candidate_id].payload_sha256
                else ("absent-same" if item.state == "ABSENT_NATIVE" else "present-changed")
            ),
        )
        for item in frozen_fingerprints
    )
    return ValidationResult(
        frozen.design,
        frozen,
        same_realization_control,
        fresh,
        per_query,
        aggregate,
        frozen_fingerprints,
        fresh_fingerprints,
        payload_comparisons,
        deployment,
        provenance,
    )
