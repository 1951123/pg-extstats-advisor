"""Records for frozen-prediction versus fresh-deployment validation."""

from __future__ import annotations

from dataclasses import dataclass

from pg_extstats_advisor.deploy.model import DeploymentResult
from pg_extstats_advisor.models import Design, EvaluationState


@dataclass(frozen=True, slots=True)
class PayloadFingerprint:
    candidate_id: str
    mechanism: str
    definition_identity: str
    payload_sha256: str | None
    payload_size: int | None
    catalog_oid: int
    relation_oid: int
    relation_fingerprint: str
    state: str = "PRESENT"


@dataclass(frozen=True, slots=True)
class PayloadComparison:
    candidate_id: str
    frozen_sha256: str
    fresh_sha256: str
    same_realization: bool
    transition: str = "present-same"


@dataclass(frozen=True, slots=True)
class QueryComparison:
    query_id: str
    truth: float
    frozen_estimate: float
    fresh_estimate: float
    frozen_q_error: float
    fresh_q_error: float
    absolute_estimate_difference: float
    relative_estimate_difference: float | None
    q_error_difference: float
    estimate_changed: bool


@dataclass(frozen=True, slots=True)
class AggregateComparison:
    frozen_objective: float
    fresh_objective: float
    absolute_objective_drift: float
    relative_objective_drift: float | None


@dataclass(frozen=True, slots=True)
class ValidationProvenance:
    system_commit: str
    upstream_sha256: str
    patch_commit: str
    workload_digest: str
    repository_digest: str
    cost_model_digest: str | None
    budget_value: str | None
    budget_unit: str | None
    search_algorithm: str | None
    deployment_sql_digest: str
    validation_environment: str
    statistics_target: int | None
    postgres_version: str
    relation_row_counts: tuple[tuple[str, int], ...] = ()
    planner_settings: tuple[tuple[str, str], ...] = ()


@dataclass(frozen=True, slots=True)
class ValidationResult:
    selected_design: Design
    frozen_hypothetical_state: EvaluationState
    same_realization_control_state: EvaluationState | None
    fresh_physical_state: EvaluationState
    per_query: tuple[QueryComparison, ...]
    aggregate: AggregateComparison
    frozen_payload_fingerprints: tuple[PayloadFingerprint, ...]
    fresh_payload_fingerprints: tuple[PayloadFingerprint, ...]
    payload_comparisons: tuple[PayloadComparison, ...]
    deployment: DeploymentResult
    provenance: ValidationProvenance
