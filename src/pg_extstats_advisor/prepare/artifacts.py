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
from pg_extstats_advisor.payloads.repository import PayloadRepository
from pg_extstats_advisor.prepare.acquisition import AcquisitionResult, acquire_payloads
from pg_extstats_advisor.prepare.candidates import generate_candidates
from pg_extstats_advisor.prepare.config import PreparationConfig
from pg_extstats_advisor.prepare.incidence import derive_incidence
from pg_extstats_advisor.prepare.workload import ingest_workload
from pg_extstats_advisor.search.model import candidate_catalog_digest
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
    ingested = ingest_workload(config.workload_path, source_connection)
    catalog = generate_candidates(config, ingested)
    if not catalog.candidates:
        raise ValueError("candidate generation produced no candidates")
    acquisition = acquire_payloads(
        acquisition_connection,
        catalog,
        root / "repository",
        statistics_target=config.statistics_target,
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
    if maintenance.get("type", "preset-development") == "empirical-mechanism-count-v1":
        artifact_path = Path(str(maintenance["artifact_path"]))
        artifact = json.loads(artifact_path.read_text())
        empirical = EmpiricalMechanismCountCostModel.from_artifact(artifact)
        empirical.validate_runtime(config.statistics_target)
        for candidate in catalog.candidates:
            empirical.estimate_candidate(candidate)
        _write(root / "maintenance-model.json", artifact)
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
    _write(
        root / "workload.json",
        {
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
        },
    )
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
        "query_count": len(ingested.workload.queries),
        "candidate_count": len(catalog.candidates),
        "relation_count": len(acquisition.analyzed_relations),
        "incidence_edge_count": len(incidence.edges),
        "fallback_query_count": incidence.fallback_query_count,
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
    _write(root / "prepare-summary.json", summary)
    _write(
        root / "run-manifest.json",
        {
            "format_version": 1,
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
    )
