"""Product-shaped offline advisor orchestration for one fixed-T bundle."""

from __future__ import annotations

import json
import os
import shutil
import tempfile
import time
from dataclasses import dataclass
from decimal import Decimal
from pathlib import Path
from typing import Any

import psycopg

from pg_extstats_advisor.candidates.model import CandidateCatalog
from pg_extstats_advisor.capture.bundle import canonical_digest, decode_sample
from pg_extstats_advisor.capture.fixed import verify_fixed_t_bundle
from pg_extstats_advisor.cost.empirical import EmpiricalMechanismCountCostModel
from pg_extstats_advisor.cost.model import MaintenanceBudget
from pg_extstats_advisor.deploy.bundle import RecommendationBundle, validate_recommendation_bundle
from pg_extstats_advisor.deploy.sql import build_rollback_statements, build_search_deployment_plan
from pg_extstats_advisor.errors import AdvisorCLIError, ExitCode
from pg_extstats_advisor.evaluator.native import NativeEvaluator
from pg_extstats_advisor.incidence.index import IncidenceIndex
from pg_extstats_advisor.models import (
    AdvisorProblem,
    Candidate,
    CandidateId,
    Design,
    MechanismKind,
    QueryId,
    WorkloadQuery,
)
from pg_extstats_advisor.payloads.cache import (
    CACHE_SCHEMA_VERSION,
    bind_statistics_target,
    repository_semantic_digest,
)
from pg_extstats_advisor.payloads.repository import PayloadRepository
from pg_extstats_advisor.postgres.adapter import PostgresAdapter
from pg_extstats_advisor.prepare.acquisition import acquire_payloads
from pg_extstats_advisor.prepare.workload import RelationMetadata
from pg_extstats_advisor.search.deterministic import DeterministicBudgetSearch
from pg_extstats_advisor.search.model import SearchConfig
from pg_extstats_advisor.statistics import validate_global_statistics_target
from pg_extstats_advisor.workload.model import Workload


@dataclass(frozen=True, slots=True)
class AdviseConfig:
    bundle_path: Path
    advisor_dsn: str
    candidate_catalog_path: Path
    incidence_path: Path
    maintenance_model_path: Path
    output_path: Path
    cache_path: Path
    budget: Decimal
    statistics_target: int = 100


def _write(path: Path, value: Any) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text(json.dumps(value, sort_keys=True, indent=2, ensure_ascii=False) + "\n")


def _load_catalog(path: Path, relation_name: str, relation_oid: int) -> CandidateCatalog:
    try:
        raw = json.loads(path.read_text())
    except (OSError, json.JSONDecodeError) as error:
        raise AdvisorCLIError(f"cannot read candidate catalog: {path}", ExitCode.USAGE) from error
    candidates = []
    for item in raw.get("candidates", []):
        definition = item.get("definition", {})
        if not isinstance(definition, dict):
            raise AdvisorCLIError("candidate definition is malformed", ExitCode.CORRUPT_ARTIFACT)
        candidates.append(Candidate(
            CandidateId(str(item["candidate_id"])), relation_oid,
            str(item.get("relation_name", relation_name)),
            MechanismKind(str(item["mechanism"])), tuple(map(str, item["attributes"])),
            tuple(sorted(definition.items())), int(item["precedence_rank"]), 0,
        ))
    if not candidates:
        raise AdvisorCLIError("candidate catalog is empty", ExitCode.USAGE)
    return CandidateCatalog(tuple(candidates))


def _load_workload(bundle: Path, relation_id: str, relation_oid: int) -> Workload:
    workload = json.loads((bundle / "workload.json").read_text())
    truth = {str(item["query_id"]): float(item["truth"]) for item in json.loads((bundle / "truth.json").read_text())["queries"]}
    relation_name = relation_id.split(".")[-1]
    queries = tuple(
        WorkloadQuery(QueryId(str(item["query_id"])), str(item["sql"]), truth[str(item["query_id"])], relation_name, frozenset({relation_oid}))
        for item in workload["queries"] if item.get("effective") is True
    )
    if not queries:
        raise AdvisorCLIError("capture bundle has no effective queries", ExitCode.CORRUPT_ARTIFACT)
    return Workload("fixed-t-product-workload", queries)


def _logical_workload_digest(workload: Workload) -> str:
    return canonical_digest([
        {"id": str(query.query_id), "sql": query.sql, "truth": query.truth, "target_relation": query.target_relation}
        for query in sorted(workload.queries, key=lambda item: str(item.query_id))
    ])


def _logical_catalog_digest(catalog: CandidateCatalog) -> str:
    return canonical_digest([
        {"candidate_id": str(item.candidate_id), "relation_name": item.relation_name, "mechanism": item.mechanism.value, "attributes": list(item.attributes), "precedence_rank": item.precedence_rank}
        for item in catalog.candidates
    ])


def _load_incidence(path: Path, workload: Workload, catalog: CandidateCatalog) -> IncidenceIndex:
    raw = json.loads(path.read_text())
    known_q = frozenset(workload.by_id)
    known_c = {str(item.candidate_id) for item in catalog.candidates}
    mapping: dict[CandidateId, set[QueryId]] = {CandidateId(item): set() for item in known_c}
    for edge in raw.get("edges", []):
        cid, qid = str(edge["candidate_id"]), str(edge["query_id"])
        if cid in known_c and qid in known_q:
            mapping[CandidateId(cid)].add(QueryId(qid))
    return IncidenceIndex(tuple(sorted((cid, frozenset(qids)) for cid, qids in mapping.items())), known_q)


def _stage(connection: psycopg.Connection[Any], relation: str, columns: list[dict[str, Any]], rows: list[list[str | None]], total_rows: int) -> int:
    schema, name = relation.split(".", 1)
    sample_name = "pgextadv_product_sample"
    connection.execute(f'DROP TABLE IF EXISTS "{schema}"."{name}"')
    connection.execute(f'DROP TABLE IF EXISTS "{schema}"."{sample_name}"')
    definitions = ", ".join(f'"{item["name"]}" text' for item in columns)
    connection.execute(f'CREATE UNLOGGED TABLE "{schema}"."{name}" ({definitions})')
    connection.execute(f'CREATE UNLOGGED TABLE "{schema}"."{sample_name}" ({definitions})')
    with connection.cursor().copy(f'COPY "{schema}"."{name}" FROM STDIN') as copy:
        for row in rows:
            copy.write_row(tuple(row))
    with connection.cursor().copy(f'COPY "{schema}"."{sample_name}" FROM STDIN') as copy:
        for row in rows:
            copy.write_row(tuple(row))
    connection.commit()
    connection.execute("SELECT set_config('pg_extstats.frozen_sample_mode','replay',false)")
    connection.execute("SELECT set_config('pg_extstats.frozen_sample_relation',%s,false)", (f'{schema}.{sample_name}',))
    connection.execute("SELECT set_config('pg_extstats.frozen_totalrows',%s,false)", (str(total_rows),))
    return int(connection.execute(f"SELECT '{schema}.{name}'::regclass::oid").fetchone()[0])


def _recreate_shells(
    connection: psycopg.Connection[Any], repository: PayloadRepository, relation: str
) -> None:
    schema, relation_name = relation.split(".", 1)
    for item in repository.payloads:
        stats_name = str(dict(item.candidate.definition)["statistics_name"])
        connection.execute(f'DROP STATISTICS IF EXISTS "{schema}"."{stats_name}"')
    for item in repository.payloads:
        candidate = item.candidate
        stats_name = str(dict(candidate.definition)["statistics_name"])
        kind = "mcv" if candidate.mechanism is MechanismKind.MCV else "dependencies"
        attrs = ", ".join(f'"{column}"' for column in candidate.attributes)
        connection.execute(
            f'CREATE STATISTICS "{schema}"."{stats_name}" ({kind}) ON {attrs} '
            f'FROM "{schema}"."{relation_name}"'
        )
        connection.execute(f'ALTER STATISTICS "{schema}"."{stats_name}" SET STATISTICS 100')
    connection.execute(f'ANALYZE "{schema}"."{relation_name}"')
    connection.commit()


def _cache_repository(connection: psycopg.Connection[Any], config: AdviseConfig, catalog: CandidateCatalog, relation_oid: int, relation: str, columns: list[dict[str, Any]], sample_digest: str, bundle_digest: str) -> tuple[PayloadRepository, bool]:
    config.cache_path.mkdir(parents=True, exist_ok=True)
    identity = bind_statistics_target({"bundle_digest": bundle_digest, "sample_digest": sample_digest, "candidate_catalog": _logical_catalog_digest(catalog), "postgres_version": "16.14"}, config.statistics_target)
    entry = config.cache_path / "fixed-t-product"
    try:
        manifest = json.loads((entry / "cache-manifest.json").read_text())
        repository = PayloadRepository.load(entry / "repository")
        if manifest.get("cache_schema_version") != CACHE_SCHEMA_VERSION or manifest.get("identity") != identity or manifest.get("semantic_digest") != repository_semantic_digest(repository):
            raise ValueError("cache identity or semantic digest mismatch")
        return repository, True
    except (OSError, ValueError, json.JSONDecodeError):
        if entry.exists():
            shutil.rmtree(entry)
    temporary = Path(tempfile.mkdtemp(prefix=".fixed-t-product.", dir=config.cache_path))
    try:
        schema, name = relation.split(".", 1)
        metadata = RelationMetadata(schema, name, relation_oid, tuple((int(item["attnum"]), str(item["name"]), "text", not bool(item["nullable"])) for item in columns))
        result = acquire_payloads(connection, catalog, temporary / "repository", statistics_target=config.statistics_target, global_statistics_target=config.statistics_target, upstream_sha256="product-capture-bound", patch_commit="advisor-build", repository_id="fixed-t-product", source_relations=(metadata,))
        repository = result.repository
        semantic = repository_semantic_digest(repository)
        entry.parent.mkdir(parents=True, exist_ok=True)
        os.replace(temporary, entry)
        _write(entry / "cache-manifest.json", {"cache_schema_version": CACHE_SCHEMA_VERSION, "identity": identity, "identity_digest": canonical_digest(identity), "semantic_digest": semantic, "repository": "repository"})
        return repository, False
    except Exception:
        shutil.rmtree(temporary, ignore_errors=True)
        raise


def advise_fixed_t(config: AdviseConfig) -> dict[str, Any]:
    target = validate_global_statistics_target(config.statistics_target)
    verification = verify_fixed_t_bundle(config.bundle_path, expected_target=target, require_supported_profile=True)
    bundle = json.loads((config.bundle_path / "bundle.json").read_text())
    relation_id = str(verification["relation_ids"][0])
    schema = json.loads((config.bundle_path / "schema.json").read_text()).get("relations", [])
    relation_record = next(item for item in schema if item["relation_id"] == relation_id)
    sample_manifest = json.loads((config.bundle_path / "acquisition/relations" / relation_id / "manifest.json").read_text())
    rows = decode_sample(config.bundle_path / "acquisition/relations" / relation_id / "sample.copy.bin", int(sample_manifest["sample_row_count"]), len(relation_record["columns"]))
    connection = psycopg.connect(config.advisor_dsn)
    try:
        relation_oid = _stage(connection, relation_id, relation_record["columns"], rows, int(sample_manifest["source_population_rows"]))
        catalog = _load_catalog(config.candidate_catalog_path, relation_id, relation_oid)
        workload = _load_workload(config.bundle_path, relation_id, relation_oid)
        incidence = _load_incidence(config.incidence_path, workload, catalog)
        repository, cache_hit = _cache_repository(
            connection,
            config,
            catalog,
            relation_oid,
            relation_id,
            relation_record["columns"],
            relation_id + ":" + sample_manifest["semantic_digest"],
            verification["semantic_digest"],
        )
        if cache_hit:
            _recreate_shells(connection, repository, relation_id)
        model = EmpiricalMechanismCountCostModel.load(config.maintenance_model_path)
        model.validate_runtime(target)
        evaluator = NativeEvaluator(workload, repository, incidence, PostgresAdapter(connection, repository))
        baseline = evaluator.evaluate_design(Design(()))
        search = DeterministicBudgetSearch(evaluator, catalog, model, MaintenanceBudget(config.budget, model.unit), SearchConfig(add_only=True, candidate_set_mode="full", visible_candidate_count=len(catalog.candidates), global_statistics_target=target), incidence)
        started = time.perf_counter(); result = search.run(); runtime = time.perf_counter() - started
        plan = build_search_deployment_plan(result, catalog, statistics_target=target, validation_relations=(relation_id,))
        rollback = build_rollback_statements(plan)
        deployment = tuple(plan.create_statements + plan.target_statements + plan.analyze_statements)
        selected_objects = tuple({"candidate_id": str(candidate.candidate_id), "relation": candidate.relation_name, "attributes": list(candidate.attributes), "mechanism": candidate.mechanism.value, "statistics_name": name, "maintenance_cost": str(model.estimate_candidate(candidate)), "payload_state": repository.by_candidate[candidate.candidate_id].state.value} for candidate, name in zip(plan.ordered_candidates, plan.statistics_names, strict=True))
        logical_workload = _logical_workload_digest(workload)
        logical_catalog = _logical_catalog_digest(catalog)
        problem_record = AdvisorProblem(target, logical_workload, logical_catalog, repository_semantic_digest(repository), verification["semantic_digest"])
        problem = {"target": target, "workload_digest": logical_workload, "candidate_catalog_digest": logical_catalog, "realization_digest": repository_semantic_digest(repository), "problem_digest": problem_record.digest}
        search_metadata = {"algorithm": result.config.algorithm_version, "budget": str(config.budget), "evaluated_moves": result.evaluated_moves_count, "evaluator_calls": result.evaluator_calls_count, "accepted_moves": result.accepted_moves_count, "termination_reason": result.termination_reason}
        recommendation = RecommendationBundle(target, problem, {"capture_bundle_digest": verification["semantic_digest"], "repository_semantic_digest": repository_semantic_digest(repository), "sample_semantic_digest": sample_manifest["semantic_digest"]}, {"workload_digest": logical_workload, "query_count": len(workload.queries)}, {"truth_semantic_digest": bundle["truth_identity"]["semantic_digest"], "query_count": int(bundle["truth_identity"]["query_count"])}, logical_catalog, model.digest, baseline.aggregate_objective, result.selected_objective, tuple(map(str, result.selected_design.candidate_ids)), str(result.selected_maintenance_cost), search_metadata, deployment, rollback, {"postgres_version": "16.14", "requires_patch": False, "advisor_replay_requires_patch": True, "target_precondition": target, "sample_realization_authoritative": True}, capture_bundle_digest=verification["semantic_digest"], selected_objects=selected_objects, scope={"relation_count": 1, "query_scope": "base_relation_selection", "candidate_arity": 2, "mechanisms": ["mcv", "fd"]})
        output = config.output_path
        if output.exists():
            raise AdvisorCLIError(f"output already exists: {output}", ExitCode.USAGE)
        output.parent.mkdir(parents=True, exist_ok=True)
        temporary = Path(tempfile.mkdtemp(prefix=f".{output.name}.", dir=output.parent))
        try:
            recommendation.write(temporary / "recommendation.json")
            (temporary / "deploy.sql").write_text(";\n".join(deployment) + ";\n")
            (temporary / "rollback.sql").write_text(";\n".join(rollback) + ";\n")
            _write(temporary / "summary.json", {"artifact_type": "recommendation", "capture_bundle_digest": verification["semantic_digest"], "evaluated_statistics_target": target, "baseline_objective": baseline.aggregate_objective, "final_objective": result.selected_objective, "selected_count": len(result.selected_design.candidate_ids), "selected_maintenance_cost": str(result.selected_maintenance_cost), "cache_hit": cache_hit, "runtime_seconds": runtime, "search": search_metadata})
            validate_recommendation_bundle(temporary / "recommendation.json", candidate_ids={str(item.candidate_id) for item in catalog.candidates}, expected_target=target, expected_capture_digest=verification["semantic_digest"], require_product_profile=True)
            os.replace(temporary, output)
        finally:
            if temporary.exists():
                shutil.rmtree(temporary, ignore_errors=True)
        return {"artifact_type": "recommendation", "digest": recommendation.digest, "capture_bundle_digest": verification["semantic_digest"], "evaluated_statistics_target": target, "baseline_objective": baseline.aggregate_objective, "final_objective": result.selected_objective, "selected_count": len(result.selected_design.candidate_ids), "selected_maintenance_cost": str(result.selected_maintenance_cost), "cache_hit": cache_hit}
    except AdvisorCLIError:
        raise
    except Exception as error:
        raise AdvisorCLIError(f"offline advise failed: {error}", ExitCode.EXECUTION) from error
    finally:
        try:
            connection.execute("DROP TABLE IF EXISTS public.pgextadv_product_sample")
            connection.execute(f'DROP TABLE IF EXISTS "{relation_id.split(".")[0]}"."{relation_id.split(".")[1]}"')
            connection.commit()
        finally:
            connection.close()


__all__ = ["AdviseConfig", "advise_fixed_t"]
