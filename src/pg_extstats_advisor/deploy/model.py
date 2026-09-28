"""Typed physical-deployment records."""

from __future__ import annotations

from dataclasses import dataclass

from pg_extstats_advisor.models import Candidate, Design


@dataclass(frozen=True, slots=True)
class DeploymentPlan:
    selected_design: Design
    ordered_candidates: tuple[Candidate, ...]
    statistics_names: tuple[str, ...]
    create_statements: tuple[str, ...]
    target_statements: tuple[str, ...]
    analyze_statements: tuple[str, ...]
    target_relations: tuple[str, ...]
    statistics_target: int | None
    repository_digest: str
    workload_digest: str
    cost_model_digest: str | None
    search_provenance: str | None
    sql_digest: str


@dataclass(frozen=True, slots=True)
class CreatedStatistic:
    candidate_id: str
    statistics_name: str
    catalog_oid: int
    relation_oid: int
    relation_name: str


@dataclass(frozen=True, slots=True)
class DeploymentResult:
    plan: DeploymentPlan
    created_statistics: tuple[CreatedStatistic, ...]
    analyze_commands: tuple[str, ...]
    environment_identity: str
    postgres_version: str
    started_at: str
    completed_at: str
    success: bool
