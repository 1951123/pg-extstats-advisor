#!/usr/bin/env python3
"""Normalize the M2.23 DMV prototype into Production Capture Bundle v1.

This tool has no production connection.  ``normalize`` reads the ignored
M2.23 capture once; ``reconstruct`` reads only the resulting v1 directory and
the tracked candidate/workload analysis inputs.  It does not run search.
"""

from __future__ import annotations

import argparse
import hashlib
import json
import shutil
import subprocess
from datetime import UTC, datetime
from pathlib import Path
from typing import Any

import psycopg

from pg_extstats_advisor.candidates.model import CandidateCatalog
from pg_extstats_advisor.capture.bundle import (
    BUNDLE_SCHEMA_VERSION,
    SUPPORTED_SERIALIZATION,
    canonical_digest,
    check_advisor_compatibility,
    decode_sample,
    encode_sample,
    verify_production_capture_bundle,
)
from pg_extstats_advisor.evaluator.native import NativeEvaluator
from pg_extstats_advisor.incidence.index import IncidenceIndex
from pg_extstats_advisor.models import (
    Candidate,
    CandidateId,
    Design,
    MechanismKind,
    QueryId,
    WorkloadQuery,
)
from pg_extstats_advisor.postgres.adapter import PostgresAdapter
from pg_extstats_advisor.prepare.acquisition import acquire_payloads
from pg_extstats_advisor.prepare.workload import RelationMetadata
from pg_extstats_advisor.workload.model import Workload

ROOT = Path(__file__).resolve().parents[1]
SOURCE = ROOT / ".build/production-captures/dmv-m2-23-v1"
OUT = ROOT / ".build/production-captures/dmv-m2-24-v1"
REPEAT = ROOT / ".build/production-captures/dmv-m2-24-v1-repeat"
PREPARED = ROOT / "experiments/dmv-m2-15-singletons/prepared-run"
ADVISOR_DSN = "host=/root/projects/pg-extstats-advisor/.build/pg16.14-advisor-socket port=55438 dbname=postgres user=postgres"
STOCK_READY = ROOT / ".build/postgresql-16.14-stock-install/bin/pg_isready"
PROD_SOCKET = ROOT / ".build/pg16.14-production-sim-socket"
PROD_PORT = 55437
RELATION_ID = "public.dmv"
EXPECTED = {
    "source_rows": 11591877,
    "sample_rows": 30000,
    "repository_digest": "eb78679910b1a163c3219a00bf28c1a5ed849fe6e94b48ee97cf7d5a3dceff3f",
    "ordinary_statistics_digest": "b833015894099758cf362d1436319388c83851e0eb067d3ce8561981bc277604",
    "baseline_objective": 88053.61940613187,
    "estimate_vector_digest": "6be99060002f3be7c49070e20ba57ecf520a5970f01f42053707dcf02bad1274",
}


def repository_semantic_digest(repository: Any) -> str:
    rows = []
    for item in repository.payloads:
        candidate = item.candidate
        rows.append({
            "candidate_id": str(candidate.candidate_id),
            "mechanism": candidate.mechanism.value,
            "attributes": list(candidate.attributes),
            "definition": dict(candidate.definition),
            "precedence_rank": candidate.precedence_rank,
            "state": item.state.value,
            "payload_sha256": item.payload_sha256,
            "payload_size": len(item.payload) if item.payload is not None else None,
        })
    return canonical_digest(sorted(rows, key=lambda row: row["candidate_id"]))


def write(path: Path, value: Any) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text(json.dumps(value, sort_keys=True, indent=2, ensure_ascii=False) + "\n")


def query_sort(value: str) -> tuple[str, int]:
    head, _, tail = value.rpartition(".")
    return head, int(tail) if tail.isdigit() else 0


def relation_columns(source_schema: dict[str, Any]) -> list[dict[str, Any]]:
    env = json.loads((SOURCE / "environment.json").read_text())
    by_name = {str(c["column"]): c for c in env["stats_summary"]}
    columns = []
    for column in source_schema["columns"]:
        columns.append({
            "attnum": int(column["attnum"]),
            "name": str(column["name"]),
            "type": {"namespace": "pg_catalog", "name": "text", "portable_name": "pg_catalog.text", "kind": "builtin"},
            "typmod": str(column["typmod"]),
            "collation": {"namespace": "pg_catalog", "name": "default", "portable_name": "pg_catalog.default"},
            "nullable": not bool(column["not_null"]),
            "statistics": {"target": 100, "source_summary_present": str(column["name"]) in by_name},
        })
    return columns


def normalize(source: Path, destination: Path) -> None:
    if destination.exists():
        shutil.rmtree(destination)
    destination.mkdir(parents=True)
    env_source = json.loads((source / "environment.json").read_text())
    schema_source = json.loads((source / "schema.json").read_text())
    workload_source = json.loads((source / "workload.json").read_text())
    truth_source = json.loads((source / "truth.json").read_text())
    sample_source = json.loads((source / "acquisition/sample.json").read_text())
    columns = relation_columns(schema_source)
    stable_relation = {
        "relation_id": RELATION_ID,
        "schema": "public",
        "name": "dmv",
        "kind": "ordinary_table",
        "persistence": "u",
        "columns": columns,
        "source_population_rows": str(EXPECTED["source_rows"]),
        "provenance": {"production_oid": schema_source["relation"]["oid"], "reltuples": schema_source["relation"]["reltuples"], "relpages": schema_source["relation"]["relpages"]},
    }
    stable_relation["schema_digest"] = canonical_digest({k: v for k, v in stable_relation.items() if k != "provenance" and k != "schema_digest"})
    schema = {"schema_version": 1, "relations": [stable_relation]}
    write(destination / "schema.json", schema)
    env_fields = {
        "postgres_version": {"value": env_source["postgres_version"].split(" ")[0], "classification": "required"},
        "server_version_num": {"value": env_source["server_version_num"], "classification": "required"},
        "encoding": {"value": "UTF8", "classification": "required"},
        "locale": {"value": "C", "classification": "required"},
        "database_collation": {"value": "C", "classification": "required"},
        "collation_provider": {"value": "libc", "classification": "required"},
        "collation_version": {"value": None, "classification": "advisory"},
        "default_statistics_target": {"value": int(env_source["gucs"]["default_statistics_target"]), "classification": "required"},
        "jit": {"value": env_source["gucs"]["jit"], "classification": "advisory"},
        "max_parallel_workers_per_gather": {"value": env_source["gucs"]["max_parallel_workers_per_gather"], "classification": "advisory"},
        "work_mem": {"value": env_source["gucs"]["work_mem"], "classification": "advisory"},
        "random_page_cost": {"value": env_source["gucs"]["random_page_cost"], "classification": "advisory"},
        "effective_cache_size": {"value": env_source["gucs"]["effective_cache_size"], "classification": "advisory"},
        "workload_analysis_version": {"value": "pg16-mvp-v2", "classification": "required"},
    }
    environment = {"schema_version": 1, "postgres_version": "16.14", "server_version_num": env_source["server_version_num"], "fields": env_fields, "extensions": {"value": [], "classification": "informational"}, "production_binary_sha256": env_source["stock_binary_sha256"]}
    write(destination / "environment.json", environment)
    truth_by_id = {str(item["query_id"]): item for item in truth_source["queries"]}
    effective_ids = {str(item["query_id"]) for item in workload_source["effective_queries"]}
    workload_queries = []
    for query_id in sorted(truth_by_id, key=query_sort):
        item = truth_by_id[query_id]
        workload_queries.append({"query_id": query_id, "sql": item["sql"], "effective": query_id in effective_ids, "weight": 1.0, "target_relation_id": RELATION_ID, "ce_target_id": query_id, "parsed_support": {"status": "supported", "analysis_version": "pg16-mvp-v2", "parser": "pglast"}})
    workload = {"schema_version": 1, "query_order_semantic": False, "raw_query_count": 1965, "raw_sql_sha256": workload_source["raw_source_sha256"], "raw_workload_digest": canonical_digest(workload_queries), "effective_workload_digest": workload_source["effective_workload_digest"], "excluded_query_ids": sorted(set(truth_by_id) - effective_ids, key=query_sort), "queries": workload_queries}
    write(destination / "workload.json", workload)
    truth_queries = [{"query_id": item["query_id"], "ce_target_id": item["query_id"], "exact_cardinality": int(item["truth"]), "source_relation_id": RELATION_ID, "quality": "authoritative_exact", "method": "exact_full_data_count", "snapshot_binding": truth_source["snapshot"]} for item in sorted(truth_source["queries"], key=lambda x: query_sort(str(x["query_id"]))) ]
    truth = {"schema_version": 1, "mode": "exact_full_data_count", "source_relation_id": RELATION_ID, "queries": truth_queries, "semantic_digest": canonical_digest(truth_queries)}
    write(destination / "truth.json", truth)
    acquisition_dir = destination / "acquisition/relations" / RELATION_ID
    acquisition_dir.mkdir(parents=True)
    rows = sample_source["rows"]
    binary = encode_sample(rows)
    (acquisition_dir / "sample.copy.bin").write_bytes(binary)
    sample_semantic = canonical_digest({"relation_id": RELATION_ID, "columns": columns, "rows": rows})
    sample_manifest = {"schema_version": 1, "relation_id": RELATION_ID, "sample_method_id": "deterministic_reservoir_v1", "sample_method_version": 1, "sample_row_count": len(rows), "source_population_rows": str(EXPECTED["source_rows"]), "statistics_population_rows": str(EXPECTED["source_rows"]), "selected_columns": columns, "serialization": SUPPORTED_SERIALIZATION[0], "serialization_version": SUPPORTED_SERIALIZATION[1], "semantic_digest": sample_semantic, "binary_sha256": hashlib.sha256(binary).hexdigest(), "seed": 22323, "snapshot_binding": sample_source["sample_metadata"], "production_compatible": True, "stock_postgresql_only": True, "deterministic_replay": True, "native_analyze_equivalent": False, "native_analyze_distribution_claim": "none", "authoritative_for_bundle": True, "capture_fidelity_note": "fixed statistics-realization input for this bundle; not native ANALYZE-equivalent"}
    write(acquisition_dir / "manifest.json", sample_manifest)
    relation_inventory = [{"relation_id": RELATION_ID, "schema_digest": stable_relation["schema_digest"], "source_population_rows": str(EXPECTED["source_rows"])}]
    components = {"environment.json": canonical_digest(environment), "schema.json": canonical_digest(schema), "workload.json": canonical_digest(workload), "truth.json": canonical_digest(truth), f"acquisition/{RELATION_ID}/manifest.json": canonical_digest(sample_manifest)}
    snapshot = {"mode": "best_effort_multi_snapshot", "components": {"environment": "metadata_snapshot", "schema": "metadata_snapshot", "truth": truth_source["snapshot"], "acquisition": sample_source["sample_metadata"]["snapshot"], "workload": "external_static_input"}, "limitations": ["truth and sample use separate repeatable-read snapshots", "workload is external to the data snapshot"]}
    production_identity = {"postgres_version": "16.14", "server_version_num": env_source["server_version_num"], "source_kind": "pristine_stock_upstream", "source_relation_ids": [RELATION_ID]}
    workload_identity = {"raw_query_count": 1965, "effective_query_count": len(effective_ids), "raw_sql_sha256": workload_source["raw_source_sha256"], "effective_workload_digest": workload_source["effective_workload_digest"]}
    truth_identity = {"mode": "exact_full_data_count", "semantic_digest": truth["semantic_digest"], "query_count": len(truth_queries)}
    acquisition_identity = {"relation_ids": [RELATION_ID], "sample_method_id": "deterministic_reservoir_v1", "native_analyze_equivalent": False}
    compatibility = {"postgres_version_policy": "exact", "required_postgres_version": "16.14", "supported_types": ["pg_catalog.text"], "supported_serialization": [SUPPORTED_SERIALIZATION[0], SUPPORTED_SERIALIZATION[1]], "supported_sample_method": ["deterministic_reservoir_v1", 1], "workload_analysis_version": "pg16-mvp-v2", "ce_target_scope": "single_relation_base_count", "joins_supported": False}
    sensitivity = {"contains_sampled_row_values": True, "contains_full_base_table": False, "contains_exact_truth": True, "anonymized": False, "encrypted": False}
    binding = {"bundle_schema_version": BUNDLE_SCHEMA_VERSION, "production_identity": production_identity, "snapshot_consistency": snapshot, "components": components, "relation_inventory": relation_inventory, "workload_identity": workload_identity, "truth_identity": truth_identity, "acquisition_identity": acquisition_identity, "compatibility": compatibility, "capture_mode": "prototype_stock_read_only_normalized", "sensitivity": sensitivity}
    bundle = {**binding, "sealed": True, "semantic_digest_algorithm": "sha256-canonical-json-v1", "semantic_binding": binding, "semantic_digest": canonical_digest(binding), "created_timestamp": datetime.now(UTC).isoformat()}
    write(destination / "bundle.json", bundle)
    verification = verify_production_capture_bundle(destination)
    write(destination / "normalization.json", {"source": "M2.23 prototype migration", "source_root_semantic_digest": json.loads((source / "manifest.json").read_text())["semantic_digest"], "verification": verification})


def _catalog_and_incidence(bundle: Path, conn: psycopg.Connection[Any]) -> tuple[CandidateCatalog, Workload, IncidenceIndex, RelationMetadata]:
    schema = json.loads((bundle / "schema.json").read_text())["relations"][0]
    rel_oid = int(conn.execute("SELECT 'public.dmv'::regclass::oid").fetchone()[0])
    columns = tuple((int(c["attnum"]), str(c["name"]), str(c["type"]["portable_name"]).split(".")[-1], bool(c["nullable"] is False)) for c in schema["columns"])
    metadata = RelationMetadata("public", "dmv", rel_oid, columns)
    raw = json.loads((PREPARED / "candidates.json").read_text())
    candidates = []
    attnums = {str(c["name"]): int(c["attnum"]) for c in schema["columns"]}
    import hashlib as _hashlib
    for item in raw["candidates"]:
        cid = str(item["candidate_id"])
        stats_name = "pgextadv_acq_" + _hashlib.sha256(cid.encode()).hexdigest()[:24]
        kind = MechanismKind(str(item["mechanism"]))
        candidates.append(Candidate(CandidateId(cid), rel_oid, RELATION_ID, kind, tuple(item["attributes"]), tuple(sorted({"attnums": [attnums[a] for a in item["attributes"]], "statistics_name": stats_name, "stxkind": kind.postgres_code}.items())), int(item["precedence_rank"]), 0))
    catalog = CandidateCatalog(tuple(candidates))
    workload_raw = json.loads((bundle / "workload.json").read_text())
    truth_raw = json.loads((bundle / "truth.json").read_text())
    truth_by_id = {str(item["query_id"]): item for item in truth_raw["queries"]}

    def truth_cardinality(query_id: str) -> float:
        return float(truth_by_id[query_id]["exact_cardinality"])

    queries = tuple(
        WorkloadQuery(
            QueryId(str(item["query_id"])),
            str(item["sql"]),
            truth_cardinality(str(item["query_id"])),
            "dmv",
            frozenset({rel_oid}),
        )
        for item in workload_raw["queries"]
        if item["effective"]
    )
    workload = Workload("dmv-m2-24", queries)
    edges_raw = json.loads((PREPARED / "incidence.json").read_text())["edges"]
    mapping: dict[CandidateId, set[QueryId]] = {c.candidate_id: set() for c in candidates}
    known = set(workload.by_id)
    for edge in edges_raw:
        if str(edge["query_id"]) in known and CandidateId(str(edge["candidate_id"])) in mapping:
            mapping[CandidateId(str(edge["candidate_id"]))].add(QueryId(str(edge["query_id"])))
    incidence = IncidenceIndex(tuple((key, frozenset(value)) for key, value in mapping.items()), frozenset(known))
    return catalog, workload, incidence, metadata


def reconstruct(bundle: Path) -> dict[str, Any]:
    verify = verify_production_capture_bundle(bundle)
    compatibility = check_advisor_compatibility(bundle, {"postgres_version": "16.14", "database_collation": "C", "supported_types": ["pg_catalog.text"], "sample_serialization": SUPPORTED_SERIALIZATION[0], "sample_method": "deterministic_reservoir_v1", "workload_analysis_version": "pg16-mvp-v2"})
    ready = subprocess.run([str(STOCK_READY), "-h", str(PROD_SOCKET), "-p", str(PROD_PORT)], capture_output=True, text=True, check=False)
    if ready.returncode == 0:
        raise RuntimeError("production simulator is reachable during advisor reconstruction")
    conn = psycopg.connect(ADVISOR_DSN)
    try:
        relation = json.loads((bundle / "schema.json").read_text())["relations"][0]
        sample_manifest = json.loads((bundle / "acquisition/relations/public.dmv/manifest.json").read_text())
        rows = decode_sample(bundle / "acquisition/relations/public.dmv/sample.copy.bin", int(sample_manifest["sample_row_count"]), len(relation["columns"]))
        conn.execute("DROP TABLE IF EXISTS public.pgextadv_m224_sample")
        conn.execute("DROP TABLE IF EXISTS public.dmv")
        ddl = "CREATE UNLOGGED TABLE public.dmv (" + ",".join(f'"{c["name"]}" text' for c in relation["columns"]) + ")"
        conn.execute(ddl)
        conn.execute(ddl.replace("public.dmv", "public.pgextadv_m224_sample"))
        with conn.cursor().copy("COPY public.pgextadv_m224_sample FROM STDIN") as copy:
            for row in rows: copy.write_row(tuple(row))
        with conn.cursor().copy("COPY public.dmv FROM STDIN") as copy:
            for row in rows: copy.write_row(tuple(row))
        conn.commit()
        catalog, workload, incidence, metadata = _catalog_and_incidence(bundle, conn)
        conn.execute("SELECT set_config('pg_extstats.frozen_sample_mode','replay',false)")
        conn.execute("SELECT set_config('pg_extstats.frozen_sample_relation','public.pgextadv_m224_sample',false)")
        conn.execute("SELECT set_config('pg_extstats.frozen_totalrows','11591877',false)")
        # Derived payloads and the reconstruction report are deliberately kept
        # beside, not inside, the sealed input bundle.  The bundle itself must
        # remain limited to its portable contract components.
        repository_path = bundle.parent / f"{bundle.name}-advisor-derived-repository"
        if repository_path.exists(): shutil.rmtree(repository_path)
        result = acquire_payloads(conn, catalog, repository_path, statistics_target=100, upstream_sha256="f6d077142737920858ce958ccdb75c6ee137a63b5b0853c70693d401ac7e3471", patch_commit="prototype-stock-capture", repository_id="dmv-m2-23-prototype", source_relations=(metadata,))
        repository = result.repository
        ordinary = conn.execute("SELECT attname,null_frac,avg_width,n_distinct FROM pg_stats WHERE schemaname='public' AND tablename='dmv' ORDER BY attname").fetchall()
        ordinary_digest = canonical_digest([dict(zip(("column", "nullfrac", "avg_width", "distinct"), row, strict=True)) for row in ordinary])
        evaluator = NativeEvaluator(workload, repository, incidence, PostgresAdapter(conn, repository))
        state = evaluator.evaluate_design(Design(()))
        vector = [{"query_id": str(x.query_id), "estimate": x.estimate, "truth": x.truth, "contribution": x.contribution} for x in state.query_evaluations]
        states = {state_name: sum(item.state.value == state_name for item in repository.payloads) for state_name in ("PRESENT", "ABSENT_NATIVE")}
        semantic_repository_digest = repository_semantic_digest(repository)
        vector_digest = canonical_digest(vector)
        comparison = {
            "reference": "M2.23 semantic reconstruction result",
            "ordinary_statistics_digest": {
                "actual": ordinary_digest,
                "expected": EXPECTED["ordinary_statistics_digest"],
                "equal": ordinary_digest == EXPECTED["ordinary_statistics_digest"],
            },
            "repository_semantic_digest": {
                "actual": semantic_repository_digest,
                "expected": EXPECTED["repository_digest"],
                "equal": semantic_repository_digest == EXPECTED["repository_digest"],
            },
            "realization_state_counts": {
                "actual": states,
                "expected": {"PRESENT": 70, "ABSENT_NATIVE": 2},
                "equal": states == {"PRESENT": 70, "ABSENT_NATIVE": 2},
            },
            "baseline_objective": {
                "actual": state.aggregate_objective,
                "expected": EXPECTED["baseline_objective"],
                "equal": state.aggregate_objective == EXPECTED["baseline_objective"],
            },
            "estimate_vector_digest": {
                "actual": vector_digest,
                "expected": EXPECTED["estimate_vector_digest"],
                "equal": vector_digest == EXPECTED["estimate_vector_digest"],
            },
        }
        comparison["all_exact"] = all(item["equal"] for key, item in comparison.items() if key != "reference" and key != "all_exact")
        result_doc = {"bundle_verification": verify, "compatibility": compatibility, "production_offline": True, "advisor_public_dmv_rows": int(conn.execute("SELECT count(*) FROM public.dmv").fetchone()[0]), "advisor_sample_rows": int(conn.execute("SELECT count(*) FROM public.pgextadv_m224_sample").fetchone()[0]), "original_relation_rows": 11591877, "frozen_totalrows": 11591877, "ordinary_statistics_digest": ordinary_digest, "repository_digest": semantic_repository_digest, "repository_runtime_digest": repository.digest, "realization_state_counts": states, "baseline_objective": state.aggregate_objective, "estimate_vector_digest": vector_digest, "estimate_vector": vector, "comparison_to_m223": comparison, "used_only_formal_bundle": True, "used_prototype_capture": False, "used_canonical_csv": False, "search_started": False}
        write(bundle.parent / f"{bundle.name}-advisor-reconstruction.json", result_doc)
        conn.execute("DROP TABLE IF EXISTS public.pgextadv_m224_sample")
        conn.execute("DROP TABLE IF EXISTS public.dmv")
        conn.commit()
        return result_doc
    finally:
        shutil.rmtree(bundle.parent / f"{bundle.name}-advisor-derived-repository", ignore_errors=True)
        conn.close()


def main() -> int:
    parser = argparse.ArgumentParser()
    parser.add_argument("command", choices=("normalize", "verify", "reconstruct"))
    parser.add_argument("--source", type=Path, default=SOURCE)
    parser.add_argument("--destination", type=Path, default=OUT)
    args = parser.parse_args()
    if args.command == "normalize":
        normalize(args.source, args.destination)
        print(json.dumps(verify_production_capture_bundle(args.destination), indent=2))
    elif args.command == "verify":
        print(json.dumps(verify_production_capture_bundle(args.destination), indent=2))
    else:
        print(json.dumps(reconstruct(args.destination), indent=2))
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
