"""Deterministic, identifier-safe PostgreSQL deployment SQL."""

from __future__ import annotations

import hashlib
from collections.abc import Iterable

from pg_extstats_advisor.candidates.model import CandidateCatalog
from pg_extstats_advisor.deploy.model import DeploymentPlan
from pg_extstats_advisor.models import Candidate, Design, MechanismKind
from pg_extstats_advisor.search.model import SearchResult, candidate_catalog_digest
from pg_extstats_advisor.statistics import validate_global_statistics_target


def quote_identifier(value: str) -> str:
    if not value or "\x00" in value:
        raise ValueError("PostgreSQL identifier must be non-empty and contain no NUL")
    return '"' + value.replace('"', '""') + '"'


def qualified_relation_name(value: str) -> str:
    parts = value.split(".")
    if len(parts) == 1:
        parts.insert(0, "public")
    if len(parts) != 2 or any(not part for part in parts):
        raise ValueError(f"relation name must be [schema.]relation: {value!r}")
    return ".".join(quote_identifier(part) for part in parts)


def statistics_name(candidate_id: str) -> str:
    safe = "".join(
        char.lower() if char.isascii() and char.isalnum() else "_" for char in candidate_id
    )
    safe = safe.strip("_") or "candidate"
    digest = hashlib.sha256(candidate_id.encode()).hexdigest()[:10]
    return f"pgextadv_{safe[:40]}_{digest}"


def _create_statement(candidate: Candidate, name: str) -> str:
    mechanism = {
        MechanismKind.MCV: "mcv",
        MechanismKind.FD: "dependencies",
    }.get(candidate.mechanism)
    if mechanism is None:
        raise ValueError(f"unsupported mechanism: {candidate.mechanism}")
    attributes = ", ".join(quote_identifier(item) for item in candidate.attributes)
    relation = qualified_relation_name(candidate.relation_name)
    schema = candidate.relation_name.split(".")[0] if "." in candidate.relation_name else "public"
    qualified_name = f"{quote_identifier(schema)}.{quote_identifier(name)}"
    return f"CREATE STATISTICS {qualified_name} ({mechanism}) ON {attributes} FROM {relation}"


def _digest_sql(statements: Iterable[str]) -> str:
    return hashlib.sha256("\n".join(statements).encode()).hexdigest()


def build_deployment_plan(
    design: Design,
    catalog: CandidateCatalog,
    *,
    repository_digest: str,
    workload_digest: str,
    statistics_target: int | None = None,
    cost_model_digest: str | None = None,
    search_provenance: str | None = None,
    validation_relations: Iterable[str] = (),
) -> DeploymentPlan:
    catalog.validate_design(design)
    if statistics_target is not None:
        validate_global_statistics_target(statistics_target, field="statistics_target")
    candidates = tuple(catalog.by_id[item] for item in design.candidate_ids)
    names = tuple(statistics_name(str(item.candidate_id)) for item in candidates)
    if len(names) != len(set(names)):
        raise ValueError("generated statistics names collide")
    creates = tuple(
        _create_statement(candidate, name)
        for candidate, name in zip(candidates, names, strict=True)
    )
    targets = (
        tuple(
            f"ALTER STATISTICS {quote_identifier(candidate.relation_name.split('.')[0] if '.' in candidate.relation_name else 'public')}.{quote_identifier(name)} SET STATISTICS {statistics_target}"
            for candidate, name in zip(candidates, names, strict=True)
        )
        if statistics_target is not None
        else ()
    )
    relations = tuple(
        sorted(
            {qualified_relation_name(item.relation_name) for item in candidates}
            | {qualified_relation_name(item) for item in validation_relations}
        )
    )
    analyzes = tuple(f"ANALYZE {relation}" for relation in relations)
    statements = creates + targets + analyzes
    return DeploymentPlan(
        design,
        candidates,
        names,
        creates,
        targets,
        analyzes,
        relations,
        statistics_target,
        repository_digest,
        workload_digest,
        cost_model_digest,
        search_provenance,
        _digest_sql(statements),
    )


def build_search_deployment_plan(
    result: SearchResult,
    catalog: CandidateCatalog,
    *,
    statistics_target: int | None = None,
    validation_relations: Iterable[str] = (),
) -> DeploymentPlan:
    digest = candidate_catalog_digest(catalog.candidates)
    if digest != result.candidate_catalog_digest:
        raise ValueError("search result/candidate catalog lineage mismatch")
    if result.selected_state.design != result.selected_design:
        raise ValueError("search result selected-state lineage mismatch")
    if result.selected_state.repository_digest != result.repository_digest:
        raise ValueError("search result repository lineage mismatch")
    if result.selected_state.workload_digest != result.workload_digest:
        raise ValueError("search result workload lineage mismatch")
    return build_deployment_plan(
        result.selected_design,
        catalog,
        repository_digest=result.repository_digest,
        workload_digest=result.workload_digest,
        statistics_target=statistics_target,
        cost_model_digest=result.cost_model_digest,
        search_provenance=result.config.algorithm_version,
        validation_relations=validation_relations,
    )
