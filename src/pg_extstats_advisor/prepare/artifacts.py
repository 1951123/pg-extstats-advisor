"""High-level preparation API and structured run artifacts."""

from __future__ import annotations

import json
from dataclasses import dataclass
from datetime import UTC, datetime
from pathlib import Path
from typing import Any

from psycopg import Connection

from pg_extstats_advisor.candidates.model import CandidateCatalog
from pg_extstats_advisor.cost.empirical import EmpiricalMechanismCountCostModel
from pg_extstats_advisor.cost.preset import PresetMaintenanceCostModel
from pg_extstats_advisor.incidence.index import IncidenceIndex
from pg_extstats_advisor.models import AdvisorProblem
from pg_extstats_advisor.payloads.repository import PayloadRepository
from pg_extstats_advisor.prepare.acquisition import AcquisitionResult, acquire_payloads
from pg_extstats_advisor.prepare.candidates import generate_candidates
from pg_extstats_advisor.prepare.config import PreparationConfig
from pg_extstats_advisor.prepare.incidence import derive_incidence
from pg_extstats_advisor.prepare.workload import ingest_workload
from pg_extstats_advisor.search.model import candidate_catalog_digest
from pg_extstats_advisor.statistics import validate_global_statistics_target
from pg_extstats_advisor.workload.model import Workload


@dataclass(frozen=True, slots=True)
class PreparedRun:
    workload: Workload
    catalog: CandidateCatalog
    repository: PayloadRepository
    incidence: IncidenceIndex
    acquisition: AcquisitionResult
    artifact_root: Path
    config_digest: str
    candidate_catalog_digest: str
    incidence_digest: str
    effective_workload_digest: str | None = None

    @property
    def global_statistics_target(self) -> int:
        summary = json.loads((self.artifact_root / "prepare-summary.json").read_text())
        raw = summary.get("global_statistics_target", summary.get("statistics_target", 100))
        return validate_global_statistics_target(int(raw))

    @property
    def advisor_problem(self) -> AdvisorProblem:
        return AdvisorProblem(
            global_statistics_target=self.global_statistics_target,
            workload_digest=self.workload.digest,
            candidate_catalog_digest=self.candidate_catalog_digest,
            realization_digest=self.repository.digest,
            acquisition_identity=self.repository.repository_id,
        )


def _write(path: Path, value: Any) -> None:
    temporary = path.with_suffix(path.suffix + ".tmp")
    temporary.write_text(json.dumps(value, sort_keys=True, indent=2) + "\n")
    temporary.replace(path)


def prepare_mvp(
    config: PreparationConfig,
    source_connection: Connection[Any],
    acquisition_connection: Connection[Any],
    *,
    upstream_sha256: str,
    patch_commit: str,
) -> PreparedRun:
    root = config.output_path
    if root.exists():
        raise FileExistsError(f"preparation artifact directory exists: {root}")
    root.mkdir(parents=True)
    ingested = ingest_workload(
        config.workload_path,
        source_connection,
        objective_membership_policy=config.objective_membership_policy,
    )
    catalog = generate_candidates(config, ingested)
    if not catalog.candidates:
        raise ValueError("candidate generation produced no candidates")
    acquisition = acquire_payloads(
        acquisition_connection,
        catalog,
        root / "repository",
        statistics_target=config.global_statistics_target,
        global_statistics_target=config.global_statistics_target,
        upstream_sha256=upstream_sha256,
        patch_commit=patch_commit,
        repository_id=f"prepared-{config.digest[:16]}",
        source_relations=tuple(
            {item.relation.qualified_name: item.relation for item in ingested.inspections}.values()
        ),
    )
    incidence = derive_incidence(ingested, acquisition.repository.catalog)
    _write(root / "config.json", json.loads(config.canonical_json()))
    maintenance = dict(config.maintenance)
    maintenance_type = maintenance.get("type", "preset-development")
    if maintenance_type == "empirical-mechanism-count-v1":
        artifact_path = Path(str(maintenance["artifact_path"]))
        artifact = json.loads(artifact_path.read_text())
        empirical = EmpiricalMechanismCountCostModel.from_artifact(artifact)
        empirical.validate_runtime(config.statistics_target)
        for candidate in catalog.candidates:
            empirical.estimate_candidate(candidate)
        _write(root / "maintenance-model.json", artifact)
    elif maintenance_type == "unpriced-singleton-profile":
        _write(
            root / "maintenance-model.json",
            {
                "format_version": 1,
                "model_type": "unpriced-singleton-profile",
                "status": "unavailable",
                "reason": str(
                    maintenance.get(
                        "reason", "no accepted benchmark-specific maintenance model"
                    )
                ),
                "feature_schema": ["mechanism", "arity"],
                "digest": "unavailable",
            },
        )
    else:
        model = PresetMaintenanceCostModel(
            maintenance.get("base_mcv", "0"),
            maintenance.get("per_column_mcv", "1"),
            maintenance.get("base_fd", "1"),
            maintenance.get("per_column_fd", "1"),
            str(maintenance.get("unit", "maintenance-cost-unit")),
        )
        _write(root / "maintenance-model.json", {
            "format_version": 1,
            "model_type": "preset-development",
            "model_version": model.model_version,
            "unit": model.unit,
            "parameters": dict(model.provenance.parameters),
            "feature_schema": model.provenance.feature_schema,
            "fitting_provenance": model.fitting_provenance,
            "digest": model.digest,
        })
    workload_artifact = {
            "workload_id": ingested.workload.workload_id,
            "digest": ingested.workload.digest,
            "queries": [
                {
                    "query_id": q.query_id,
                    "sql": q.sql,
                    "truth": q.truth,
                    "target_relation": q.target_relation,
                    "relation_oids": sorted(q.relation_oids),
                    "label": q.label,
                }
                for q in ingested.workload.queries
            ],
        }
    if config.objective_membership_policy != "require_all_positive" or ingested.raw_source_path:
        workload_artifact["provenance"] = {
            "raw_source_path": ingested.raw_source_path,
            "raw_source_sha256": ingested.raw_source_sha256,
            "raw_query_count": ingested.raw_query_count,
            "objective_membership_policy": ingested.objective_membership_policy,
            "excluded_query_ids": list(ingested.excluded_query_ids),
            "excluded_count": len(ingested.excluded_query_ids),
            "exclusion_reason": "truth == 0" if ingested.excluded_query_ids else None,
            "effective_query_count": len(ingested.workload.queries),
            "raw_workload_digest": ingested.raw_workload_digest,
            "effective_workload_digest": ingested.effective_workload_digest,
        }
    _write(root / "workload.json", workload_artifact)
    catalog_digest = candidate_catalog_digest(acquisition.repository.catalog.candidates)
    _write(
        root / "candidates.json",
        {
            "digest": catalog_digest,
            "candidates": [
                {
                    "candidate_id": c.candidate_id,
                    "relation_oid": c.relation_oid,
                    "relation_name": c.relation_name,
                    "mechanism": c.mechanism.value,
                    "attributes": c.attributes,
                    "precedence_rank": c.precedence_rank,
                    "backend_oid": c.backend_oid,
                }
                for c in acquisition.repository.catalog.candidates
            ],
        },
    )
    incidence.write(root / "incidence.json")
    summary = {
        "format_version": 1,
        "config_digest": config.digest,
        "workload_digest": ingested.workload.digest,
        "candidate_catalog_digest": catalog_digest,
        "repository_digest": acquisition.repository.digest,
        "incidence_digest": incidence.digest,
        "postgres_version": acquisition.repository.postgres_version,
        "upstream_sha256": upstream_sha256,
        "patch_commit": patch_commit,
        "statistics_target": config.statistics_target,
        "global_statistics_target": config.global_statistics_target,
        "target_scope": "database/advisor_run",
        "target_optimization": "outside_current_scope",
        "query_count": len(ingested.workload.queries),
        "candidate_count": len(catalog.candidates),
        "realization_state_counts": {
            "PRESENT": sum(
                item.state.value == "PRESENT" for item in acquisition.repository.payloads
            ),
            "ABSENT_NATIVE": sum(
                item.state.value == "ABSENT_NATIVE" for item in acquisition.repository.payloads
            ),
        },
        "relation_count": len(acquisition.analyzed_relations),
        "incidence_edge_count": len(incidence.edges),
        "fallback_query_count": incidence.fallback_query_count,
        "sql_analysis": {
            "parser": ingested.inspections[0].parser if ingested.inspections else "pglast",
            "parser_version": ingested.inspections[0].parser_version if ingested.inspections else "unknown",
            "analysis_version": ingested.inspections[0].analysis_version if ingested.inspections else "unknown",
            "precise_query_count": sum(
                item.derivation_mode == "precise-structural" for item in ingested.inspections
            ),
            "fallback_query_count": sum(
                item.derivation_mode == "conservative-fallback" for item in ingested.inspections
            ),
            "rejected_query_count": 0,
            "error_query_count": 0,
        },
        "acquisition_analyze_count": acquisition.analyze_count,
        "relation_compatibility": [
            {
                "relation": relation,
                "source_logical_fingerprint": source,
                "acquisition_logical_fingerprint": acquired,
                "compatible": compatible,
            }
            for relation, source, acquired, compatible in acquisition.compatibility
        ],
        "completed_at": datetime.now(UTC).isoformat(),
    }
    if config.objective_membership_policy != "require_all_positive" or ingested.raw_source_path:
        summary["effective_workload_digest"] = ingested.effective_workload_digest
    if config.objective_membership_policy != "require_all_positive" or ingested.raw_source_path:
        summary["workload_provenance"] = workload_artifact["provenance"]
    _write(root / "prepare-summary.json", summary)
    _write(
        root / "run-manifest.json",
        {
            "format_version": 1,
            "global_statistics_target": config.global_statistics_target,
            "stages": {
                "prepare": {"complete": True, "artifact_digest": acquisition.repository.digest}
            },
        },
    )
    return PreparedRun(
        ingested.workload,
        acquisition.repository.catalog,
        acquisition.repository,
        incidence.index,
        acquisition,
        root,
        config.digest,
        catalog_digest,
        incidence.digest,
        ingested.effective_workload_digest,
    )
