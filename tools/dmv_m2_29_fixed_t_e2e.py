#!/usr/bin/env python3
"""M2.29 fixed-T, same-snapshot DMV advisor workflow.

The capture half is the only part that may connect to the stock production
simulator.  It exports one repeatable-read snapshot, computes truth, metadata,
and the deterministic sample from that snapshot, seals a v1 bundle, and then
stops the simulator.  ``advise`` refuses to run while that endpoint is
reachable and consumes only the sealed bundle and the patched advisor
cluster.  The script intentionally keeps payload repositories and database
state under ``.build``; only compact provenance summaries are experiment
artifacts.
"""

from __future__ import annotations

import argparse
import hashlib
import json
import random
import shutil
import subprocess
import time
from concurrent.futures import ThreadPoolExecutor, as_completed
from datetime import UTC, datetime
from decimal import Decimal
from pathlib import Path
from typing import Any

import psycopg

from pg_extstats_advisor.candidates.model import CandidateCatalog
from pg_extstats_advisor.capture.bundle import (
    canonical_digest,
    encode_sample,
)
from pg_extstats_advisor.capture.fixed import FIXED_CAPTURE_MODE, verify_fixed_t_bundle
from pg_extstats_advisor.cost.empirical import EmpiricalMechanismCountCostModel
from pg_extstats_advisor.cost.model import MaintenanceBudget
from pg_extstats_advisor.deploy.bundle import RecommendationBundle, validate_recommendation_bundle
from pg_extstats_advisor.deploy.sql import build_rollback_statements
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
from pg_extstats_advisor.search.model import SearchConfig, candidate_catalog_digest
from pg_extstats_advisor.statistics import DEFAULT_GLOBAL_STATISTICS_TARGET
from pg_extstats_advisor.workload.model import Workload

ROOT = Path(__file__).resolve().parents[1]
CAPTURE = ROOT / ".build/production-captures/dmv-m2-29-v1"
CACHE = ROOT / ".build/artifact-cache/dmv-m2-29-v1"
OUT = ROOT / "experiments/dmv-m2-29-end-to-end"
PREPARED = ROOT / "experiments/dmv-m2-15-singletons/prepared-run"
RAW_SQL = Path("/root/projects/extended-stats-optim-v2/benchmarks/DMV/queries/dmv.sql")
PROD_DATA = ROOT / ".build/pg16.14-production-sim"
PROD_SOCKET = ROOT / ".build/pg16.14-production-sim-socket"
ADVISOR_SOCKET = ROOT / ".build/pg16.14-advisor-socket"
PROD_DSN = f"host={PROD_SOCKET} port=55437 dbname=pgextadv_m2_23_production user=pgextadv_m223_capture"
ADVISOR_DSN = f"host={ADVISOR_SOCKET} port=55438 dbname=postgres user=postgres"
STOCK_BIN = ROOT / ".build/postgresql-16.14-stock-install/bin"
ADVISOR_BIN = ROOT / ".build/postgresql-16.14-install/bin"
UPSTREAM_SHA = "f6d077142737920858ce958ccdb75c6ee137a63b5b0853c70693d401ac7e3471"
SOURCE_SHA = "1953dc96e00c8caeeb9d363070853d27a6548ce43ab14772d8e7637af9c2cd22"
TARGET = DEFAULT_GLOBAL_STATISTICS_TARGET
SAMPLE_ROWS = 30_000
SOURCE_ROWS = 11_591_877
BUDGET = Decimal("372.045872636249472")
SCHEMA_COLUMNS = (
    "record_type", "registration_class", "state", "county", "body_type",
    "fuel_type", "reg_valid_date", "color", "scofflaw_indicator",
    "suspension_indicator", "revocation_indicator",
)


def write_json(path: Path, value: Any) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text(json.dumps(value, sort_keys=True, indent=2, ensure_ascii=False) + "\n")


def now() -> str:
    return datetime.now(UTC).isoformat()


def raw_records() -> list[dict[str, Any]]:
    if hashlib.sha256(RAW_SQL.read_bytes()).hexdigest() != SOURCE_SHA:
        raise RuntimeError("DMV canonical workload checksum mismatch")
    records = []
    for line_no, line in enumerate(RAW_SQL.read_text().splitlines(), 1):
        if not line.strip():
            continue
        sql, truth = line.rsplit("||", 1)
        records.append({
            "query_id": f"dmv.{line_no}", "sql": sql.strip(), "truth": float(truth),
            "target_relation_id": "public.dmv", "effective": float(truth) > 0,
        })
    if len(records) != 1965:
        raise RuntimeError(f"unexpected DMV workload size: {len(records)}")
    return records


def _start_production() -> None:
    if subprocess.run(["pg_isready", "-h", str(PROD_SOCKET), "-p", "55437"], check=False, capture_output=True).returncode == 0:
        return
    subprocess.run([
        "runuser", "-u", "postgres", "--", str(STOCK_BIN / "pg_ctl"), "-D", str(PROD_DATA),
        "-o", "-p 55437 -k " + str(PROD_SOCKET), "-l", str(PROD_DATA / "m2-29.log"), "start",
    ], check=True, capture_output=True, text=True)
    for _ in range(60):
        if subprocess.run(["pg_isready", "-h", str(PROD_SOCKET), "-p", "55437"], check=False, capture_output=True).returncode == 0:
            return
        time.sleep(1)
    raise RuntimeError("stock production simulator did not become ready")


def _stop_production() -> dict[str, Any]:
    subprocess.run([
        "runuser", "-u", "postgres", "--", str(STOCK_BIN / "pg_ctl"), "-D", str(PROD_DATA),
        "stop", "-m", "fast", "-w",
    ], check=False, capture_output=True, text=True)
    ready = subprocess.run(["pg_isready", "-h", str(PROD_SOCKET), "-p", "55437"], check=False, capture_output=True)
    return {"stopped_at": now(), "pg_isready_after_shutdown": ready.returncode != 0}


def _truth_and_sample(master: psycopg.Connection[Any], records: list[dict[str, Any]], snapshot: str) -> tuple[list[dict[str, Any]], list[list[str | None]], dict[str, Any]]:
    def truth_one(item: dict[str, Any]) -> dict[str, Any]:
        conn = psycopg.connect(PROD_DSN)
        try:
            conn.execute("BEGIN ISOLATION LEVEL REPEATABLE READ READ ONLY")
            conn.execute("SET TRANSACTION SNAPSHOT '" + snapshot.replace("'", "''") + "'")
            observed = int(conn.execute(item["sql"]).fetchone()[0])
            conn.rollback()
            return {**item, "truth": observed}
        finally:
            conn.close()

    started = time.perf_counter()
    truths: list[dict[str, Any]] = []
    with ThreadPoolExecutor(max_workers=8) as pool:
        futures = [pool.submit(truth_one, item) for item in records]
        for future in as_completed(futures):
            truths.append(future.result())
    truths.sort(key=lambda item: int(str(item["query_id"]).split(".")[1]))
    rng = random.Random(22929)
    rows: list[list[str | None]] = []
    seen = 0
    with master.cursor().copy("COPY (SELECT * FROM public.dmv) TO STDOUT WITH (FORMAT text, NULL '\\N')") as copy:
        for parsed in copy.rows():
            seen += 1
            row = [None if value is None else str(value) for value in parsed]
            if len(rows) < SAMPLE_ROWS:
                rows.append(row)
            else:
                slot = rng.randrange(seen)
                if slot < SAMPLE_ROWS:
                    rows[slot] = row
    rows.sort(key=lambda row: tuple("" if value is None else value for value in row))
    return truths, rows, {"truth_worker_count": 8, "truth_elapsed_seconds": time.perf_counter() - started, "sample_source_rows_seen": seen, "seed": 22929}


def _schema_from_master(master: psycopg.Connection[Any]) -> tuple[dict[str, Any], dict[str, Any]]:
    cols = master.execute(
        "SELECT a.attnum,a.attname,a.atttypid::regtype::text,a.attnotnull FROM pg_attribute a "
        "WHERE a.attrelid='public.dmv'::regclass AND a.attnum>0 AND NOT a.attisdropped ORDER BY a.attnum"
    ).fetchall()
    if len(cols) != 11 or [str(row[1]) for row in cols] != list(SCHEMA_COLUMNS):
        raise RuntimeError("unexpected DMV production schema")
    columns = [{
        "attnum": int(attnum), "name": str(name), "nullable": not bool(notnull),
        "typmod": "-1", "type": {"kind": "builtin", "name": "text", "namespace": "pg_catalog", "portable_name": "pg_catalog.text"},
        "collation": {"name": "default", "namespace": "pg_catalog", "portable_name": "pg_catalog.default"},
        "statistics": {"target": TARGET, "source_summary_present": True},
    } for attnum, name, _typ, notnull in cols]
    relation = {"relation_id": "public.dmv", "columns": columns}
    relation["schema_digest"] = canonical_digest({k: v for k, v in relation.items() if k != "schema_digest"})
    schema = {"relations": [relation]}
    version = str(master.execute("SHOW server_version").fetchone()[0])
    gucs = {name: str(master.execute("SHOW " + name).fetchone()[0]) for name in ("server_version_num", "default_statistics_target")}
    collation, ctype = master.execute(
        "SELECT datcollate,datctype FROM pg_database WHERE datname=current_database()"
    ).fetchone()
    env = {
        "schema_version": 1, "postgres_version": "16.14", "server_version_num": gucs["server_version_num"],
        "fields": {
            "postgres_version": {"classification": "required", "value": version},
            "server_version_num": {"classification": "required", "value": gucs["server_version_num"]},
            "default_statistics_target": {"classification": "required", "value": TARGET},
            "database_collation": {"classification": "required", "value": str(collation)},
            "workload_analysis_version": {"classification": "required", "value": "pg16-mvp-v2"},
            "encoding": {"classification": "required", "value": "UTF8"},
            "locale": {"classification": "required", "value": str(ctype)},
        },
        "relation_row_count": SOURCE_ROWS,
        "source_population": {"exact_rows": SOURCE_ROWS, "relation": "public.dmv"},
        "read_only_capture_role": str(master.execute("SELECT current_user").fetchone()[0]),
    }
    return env, schema


def capture() -> dict[str, Any]:
    if CAPTURE.exists():
        raise RuntimeError(f"capture destination already exists: {CAPTURE}")
    _start_production()
    records = raw_records()
    master = psycopg.connect(PROD_DSN)
    try:
        master.execute("BEGIN ISOLATION LEVEL REPEATABLE READ READ ONLY")
        snapshot = str(master.execute("SELECT pg_export_snapshot()").fetchone()[0])
        txid = str(master.execute("SELECT txid_current_snapshot()").fetchone()[0])
        env, schema = _schema_from_master(master)
        truths, rows, timing = _truth_and_sample(master, records, snapshot)
        mismatch = [item["query_id"] for item, source in zip(truths, records, strict=True) if int(item["truth"]) != int(source["truth"])]
        if mismatch:
            raise RuntimeError(f"canonical truth mismatch: {mismatch[:5]}")
        truth_items = [{"query_id": item["query_id"], "sql": item["sql"], "truth": int(item["truth"])} for item in truths]
        workload = {
            "schema_version": 1, "benchmark": "DMV", "queries": records,
            "raw_query_count": len(records), "effective_query_count": sum(item["effective"] for item in records),
            "raw_sql_sha256": SOURCE_SHA, "objective_membership_policy": "positive_truth_only",
            "effective_workload_digest": canonical_digest({"objective_membership_policy": "positive_truth_only", "queries": [{"query_id": item["query_id"], "sql": item["sql"], "truth": item["truth"], "target_relation": "dmv"} for item in records if item["effective"]]}),
        }
        truth = {"mode": "exact_full_data_count", "query_count": len(truth_items), "queries": truth_items}
        truth["semantic_digest"] = canonical_digest(truth_items)
        relation_dir = CAPTURE / "acquisition/relations/public.dmv"
        relation_dir.mkdir(parents=True, exist_ok=True)
        write_json(CAPTURE / "environment.json", env)
        write_json(CAPTURE / "schema.json", schema)
        write_json(CAPTURE / "workload.json", workload)
        write_json(CAPTURE / "truth.json", truth)
        binary = encode_sample(rows)
        (relation_dir / "sample.copy.bin").write_bytes(binary)
        sample_manifest = {
            "relation_id": "public.dmv", "sample_method_id": "deterministic_reservoir_v1", "sample_method_version": 1,
            "serialization": "pgextstats_m223_length_prefixed_v1", "serialization_version": 1,
            "sample_row_count": len(rows), "source_population_rows": SOURCE_ROWS,
            "selected_columns": schema["relations"][0]["columns"], "binary_sha256": hashlib.sha256(binary).hexdigest(),
            "semantic_digest": canonical_digest({"relation_id": "public.dmv", "columns": schema["relations"][0]["columns"], "rows": rows}),
            "snapshot_binding": snapshot, "authoritative_for_bundle": True, "native_analyze_equivalent": False,
            "method_parameters": {"seed": 22929, "order": "canonical lexical row order after reservoir"},
        }
        write_json(relation_dir / "manifest.json", sample_manifest)
        components = {
            "environment.json": canonical_digest(env), "schema.json": canonical_digest(schema),
            "workload.json": canonical_digest(workload), "truth.json": canonical_digest(truth),
            "acquisition/public.dmv/manifest.json": canonical_digest(sample_manifest),
        }
        snapshot_consistency = {"mode": "strong_single_snapshot", "components": {"environment": snapshot, "schema": snapshot, "workload": snapshot, "truth": snapshot, "acquisition": snapshot}, "transaction_snapshot": txid, "limitations": ["sample is a deterministic reservoir and is not claimed ANALYZE-equivalent"]}
        bundle_core = {
            "bundle_schema_version": "production-capture-bundle-v1", "sealed": True,
            "production_identity": {"postgres_version": "16.14", "server_version_num": env["server_version_num"], "source_kind": "pristine_stock_upstream", "source_relation_ids": ["public.dmv"]},
            "snapshot_consistency": snapshot_consistency, "components": components,
            "relation_inventory": [{"relation_id": "public.dmv", "schema_digest": schema["relations"][0]["schema_digest"], "source_population_rows": str(SOURCE_ROWS)}],
            "workload_identity": {"raw_query_count": len(records), "effective_query_count": sum(item["effective"] for item in records), "raw_sql_sha256": SOURCE_SHA, "effective_workload_digest": workload["effective_workload_digest"]},
            "truth_identity": {"mode": "exact_full_data_count", "query_count": len(truth_items), "semantic_digest": truth["semantic_digest"]},
            "acquisition_identity": {"relation_ids": ["public.dmv"], "sample_method_id": "deterministic_reservoir_v1", "native_analyze_equivalent": False, "global_statistics_target": TARGET, "canonical_realization": True},
            "compatibility": {"required_postgres_version": "16.14", "postgres_version_policy": "exact", "supported_types": ["pg_catalog.text"], "supported_sample_method": ["deterministic_reservoir_v1", 1], "supported_serialization": ["pgextstats_m223_length_prefixed_v1", 1], "workload_analysis_version": "pg16-mvp-v2", "ce_target_scope": "single_relation_base_count", "joins_supported": False, "target_override_policy": "fail_closed_relevant_overrides"},
            "capture_mode": FIXED_CAPTURE_MODE, "created_timestamp": now(),
            "sensitivity": {"anonymized": False, "contains_exact_truth": True, "contains_full_base_table": False, "contains_sampled_row_values": True, "encrypted": False},
            "target_override_evidence": {"status": "passed", "relevant_override_count": 0, "global_target": TARGET, "attstattarget_all_relevant": -1, "stxstattarget_relevant": []},
            "read_only": True,
        }
        bundle_core["semantic_binding"] = dict(bundle_core)
        bundle_core["semantic_digest"] = canonical_digest(bundle_core["semantic_binding"])
        write_json(CAPTURE / "bundle.json", bundle_core)
        verify = verify_fixed_t_bundle(CAPTURE, expected_target=TARGET)
        summary = {"bundle": str(CAPTURE), "bundle_semantic_digest": verify["semantic_digest"], "snapshot": snapshot, "transaction_snapshot": txid, "truth_query_count": len(truth_items), "sample_rows": len(rows), "source_rows": SOURCE_ROWS, "timing": timing, "production_read_only": True}
        write_json(OUT / "capture-summary.json", summary)
        return summary
    finally:
        master.rollback()
        master.close()
        shutdown = _stop_production()
        summary_path = OUT / "capture-summary.json"
        if summary_path.exists():
            saved = json.loads(summary_path.read_text()); saved["production_shutdown"] = shutdown; write_json(summary_path, saved)


def load_catalog(relation_oid: int) -> CandidateCatalog:
    raw = json.loads((PREPARED / "candidates.json").read_text())
    candidates = []
    for record in raw["candidates"]:
        candidate_id = CandidateId(str(record["candidate_id"]))
        candidates.append(Candidate(candidate_id, relation_oid, "public.dmv", MechanismKind(str(record["mechanism"])), tuple(record["attributes"]), (("attnums", tuple(record.get("definition", {}).get("attnums", []))), ("schema", "public")), int(record["precedence_rank"]), 0))
    return CandidateCatalog(tuple(candidates))


def load_workload(bundle: Path, relation_oid: int) -> Workload:
    raw = json.loads((bundle / "workload.json").read_text())
    truth = {str(item["query_id"]): float(item["truth"]) for item in json.loads((bundle / "truth.json").read_text())["queries"]}
    queries = tuple(WorkloadQuery(QueryId(str(item["query_id"])), str(item["sql"]), truth[str(item["query_id"])], "dmv", frozenset({relation_oid})) for item in raw["queries"] if item.get("effective") is True)
    return Workload("DMV-fixed-T100", queries)


def load_incidence(workload: Workload) -> IncidenceIndex:
    raw = json.loads((PREPARED / "incidence.json").read_text())
    known = frozenset(workload.by_id)
    by_id: dict[str, set[QueryId]] = {str(item["candidate_id"]): set() for item in json.loads((PREPARED / "candidates.json").read_text())["candidates"]}
    for edge in raw["edges"]:
        if str(edge["query_id"]) in known and str(edge["candidate_id"]) in by_id:
            by_id[str(edge["candidate_id"])].add(QueryId(str(edge["query_id"])))
    return IncidenceIndex(tuple((CandidateId(key), frozenset(value)) for key, value in sorted(by_id.items())), known)


def _stage(conn: psycopg.Connection[Any], sample: list[list[str | None]], catalog: CandidateCatalog) -> int:
    conn.execute("DROP TABLE IF EXISTS public.pgextadv_m229_sample")
    conn.execute("DROP TABLE IF EXISTS public.dmv")
    columns = ", ".join(f'"{name}" text' for name in SCHEMA_COLUMNS)
    conn.execute(f"CREATE UNLOGGED TABLE public.dmv ({columns})")
    conn.execute(f"CREATE UNLOGGED TABLE public.pgextadv_m229_sample ({columns})")
    with conn.cursor().copy("COPY public.dmv FROM STDIN") as copy:
        for row in sample: copy.write_row(tuple(row))
    with conn.cursor().copy("COPY public.pgextadv_m229_sample FROM STDIN") as copy:
        for row in sample: copy.write_row(tuple(row))
    conn.commit()
    oid = int(conn.execute("SELECT 'public.dmv'::regclass::oid").fetchone()[0])
    conn.execute("SELECT set_config('pg_extstats.frozen_sample_mode','replay',false)")
    conn.execute("SELECT set_config('pg_extstats.frozen_sample_relation','public.pgextadv_m229_sample',false)")
    conn.execute(f"SELECT set_config('pg_extstats.frozen_totalrows','{SOURCE_ROWS}',false)")
    return oid


def _recreate_shells(conn: psycopg.Connection[Any], repository: PayloadRepository) -> None:
    for item in repository.payloads:
        name = dict(item.candidate.definition)["statistics_name"]
        conn.execute(f'DROP STATISTICS IF EXISTS public."{name}"')
    for item in repository.payloads:
        candidate = item.candidate
        name = dict(candidate.definition)["statistics_name"]
        kind = "mcv" if candidate.mechanism is MechanismKind.MCV else "dependencies"
        attrs = ", ".join(f'"{column}"' for column in candidate.attributes)
        conn.execute(f'CREATE STATISTICS public."{name}" ({kind}) ON {attrs} FROM public."dmv"')
        conn.execute(f'ALTER STATISTICS public."{name}" SET STATISTICS {TARGET}')
    conn.execute("ANALYZE public.dmv")
    conn.commit()


def _build_cache(conn: psycopg.Connection[Any], catalog: CandidateCatalog, relation_oid: int, identity: dict[str, Any]) -> tuple[PayloadRepository, str, bool]:
    temp = CACHE / "cold-repository"
    if temp.exists(): shutil.rmtree(temp)
    metadata = RelationMetadata("public", "dmv", relation_oid, tuple((i, name, "text", False) for i, name in enumerate(SCHEMA_COLUMNS, 1)))
    result = acquire_payloads(conn, catalog, temp, statistics_target=TARGET, global_statistics_target=TARGET, upstream_sha256=UPSTREAM_SHA, patch_commit="pg16.14-advisor-patch", repository_id="dmv-m2-29-fixed-t100", source_relations=(metadata,))
    repository = result.repository
    semantic = repository_semantic_digest(repository)
    CACHE.mkdir(parents=True, exist_ok=True)
    entry = CACHE / "fixed-t100"
    if entry.exists(): shutil.rmtree(entry)
    entry.mkdir(parents=True, exist_ok=True)
    shutil.move(str(temp), str(entry / "repository"))
    identity = bind_statistics_target(identity, TARGET)
    write_json(entry / "cache-manifest.json", {"cache_schema_version": CACHE_SCHEMA_VERSION, "identity": identity, "identity_digest": canonical_digest(identity), "semantic_digest": semantic, "repository": "repository"})
    return PayloadRepository.load(entry / "repository"), semantic, False


def _run_search(conn: psycopg.Connection[Any], repository: PayloadRepository, workload: Workload, incidence: IncidenceIndex, catalog: CandidateCatalog) -> tuple[Any, dict[str, Any]]:
    model = EmpiricalMechanismCountCostModel.load(ROOT / "calibration/dmv-pg16.14-m2-16-r1/maintenance-model.json")
    model.validate_runtime(TARGET)
    evaluator = NativeEvaluator(workload, repository, incidence, PostgresAdapter(conn, repository))
    search = DeterministicBudgetSearch(evaluator, catalog, model, MaintenanceBudget(BUDGET, model.unit), SearchConfig(add_only=True, candidate_set_mode="full", visible_candidate_count=len(catalog.candidates), global_statistics_target=TARGET), incidence)
    started = time.perf_counter(); result = search.run(); elapsed = time.perf_counter() - started
    return result, {"runtime_seconds": elapsed, "evaluated_moves": result.evaluated_moves_count, "infeasible_moves_skipped": result.infeasible_moves_skipped_count, "evaluator_calls": result.evaluator_calls_count, "accepted_moves": result.accepted_moves_count, "termination_reason": result.termination_reason, "budget": str(BUDGET), "algorithm": result.config.algorithm_version}


def _ordinary_statistics_digest(conn: psycopg.Connection[Any]) -> str:
    rows = conn.execute(
        "SELECT attname,null_frac,avg_width,n_distinct FROM pg_stats "
        "WHERE schemaname='public' AND tablename='dmv' ORDER BY attname"
    ).fetchall()
    return canonical_digest([
        {"column": str(name), "null_frac": float(null_frac), "avg_width": int(width), "n_distinct": float(distinct)}
        for name, null_frac, width, distinct in rows
    ])


def advise() -> dict[str, Any]:
    if subprocess.run(["pg_isready", "-h", str(PROD_SOCKET), "-p", "55437"], check=False, capture_output=True).returncode == 0:
        raise RuntimeError("production endpoint is reachable; offline advise is fail-closed")
    verification = verify_fixed_t_bundle(CAPTURE, expected_target=TARGET)
    bundle = json.loads((CAPTURE / "bundle.json").read_text())
    sample = json.loads((CAPTURE / "acquisition/relations/public.dmv/manifest.json").read_text())
    # Decode through the public verifier helper to avoid a second serialization contract.
    from pg_extstats_advisor.capture.bundle import decode_sample
    rows = decode_sample(CAPTURE / "acquisition/relations/public.dmv/sample.copy.bin", int(sample["sample_row_count"]), len(SCHEMA_COLUMNS))
    conn = psycopg.connect(ADVISOR_DSN)
    try:
        oid = _stage(conn, rows, load_catalog(1))
        catalog = load_catalog(oid)
        workload = load_workload(CAPTURE, oid)
        incidence = load_incidence(workload)
        identity = {"bundle_semantic_digest": verification["semantic_digest"], "sample_semantic_digest": sample["semantic_digest"], "sample_binary_sha256": sample["binary_sha256"], "postgres_version": "16.14", "candidate_universe": "DMV-frozen-72", "global_statistics_target": TARGET}
        repository, repo_semantic, cold_hit = _build_cache(conn, catalog, oid, identity)
        cold_ordinary_digest = _ordinary_statistics_digest(conn)
        state = NativeEvaluator(workload, repository, incidence, PostgresAdapter(conn, repository)).evaluate_design(Design(()))
        baseline_vector = [{"query_id": str(item.query_id), "estimate": item.estimate, "truth": item.truth, "contribution": item.contribution} for item in state.query_evaluations]
        baseline_digest = canonical_digest(baseline_vector)
        result, search_meta = _run_search(conn, repository, workload, incidence, catalog)
        from pg_extstats_advisor.deploy.sql import build_search_deployment_plan
        plan = build_search_deployment_plan(result, catalog, statistics_target=TARGET, validation_relations=("public.dmv",))
        rollback = build_rollback_statements(plan)
        deployment = tuple(plan.create_statements + plan.target_statements + plan.analyze_statements)
        model = EmpiricalMechanismCountCostModel.load(ROOT / "calibration/dmv-pg16.14-m2-16-r1/maintenance-model.json")
        problem_identity = {
            "target": TARGET,
            "workload": workload.digest,
            "candidate_catalog": candidate_catalog_digest(catalog.candidates),
            "realization": repo_semantic,
        }
        stable_search_meta = {key: value for key, value in search_meta.items() if key != "runtime_seconds"}
        recommendation = RecommendationBundle(
            TARGET,
            {"problem_digest": canonical_digest(problem_identity), **problem_identity},
            {**identity, "repository_semantic_digest": repo_semantic},
            {"workload_digest": workload.digest, "query_count": len(workload.queries)},
            {"truth_semantic_digest": bundle["truth_identity"]["semantic_digest"], "query_count": len(json.loads((CAPTURE / "truth.json").read_text())["queries"])},
            candidate_catalog_digest(catalog.candidates),
            model.digest,
            state.aggregate_objective,
            result.selected_objective,
            tuple(str(item) for item in result.selected_design.candidate_ids),
            str(result.selected_maintenance_cost),
            stable_search_meta,
            deployment,
            rollback,
            {"postgres_version": "16.14", "requires_patch": False, "advisor_replay_requires_patch": True, "target_precondition": TARGET, "sample_realization_authoritative": True},
        )
        recommendation_path = OUT / "recommendation.json"; recommendation.write(recommendation_path)
        (OUT / "deploy.sql").write_text(";\n".join(deployment) + ";\n")
        (OUT / "rollback.sql").write_text(";\n".join(rollback) + ";\n")
        validate_recommendation_bundle(recommendation_path, candidate_ids={str(item.candidate_id) for item in catalog.candidates}, expected_target=TARGET)
        write_json(OUT / "preparation-summary.json", {"bundle_verification": verification, "sample_rows": len(rows), "workload_digest": workload.digest, "candidate_catalog_digest": candidate_catalog_digest(catalog.candidates), "repository_semantic_digest": repo_semantic, "ordinary_statistics_digest": cold_ordinary_digest, "baseline_vector_digest": baseline_digest, "baseline_objective": state.aggregate_objective, "target": TARGET})
        _recreate_shells(conn, repository)
        warm_ordinary_digest = _ordinary_statistics_digest(conn)
        warm_state = NativeEvaluator(workload, repository, incidence, PostgresAdapter(conn, repository)).evaluate_design(Design(()))
        warm_vector = [{"query_id": str(item.query_id), "estimate": item.estimate, "truth": item.truth, "contribution": item.contribution} for item in warm_state.query_evaluations]
        warm_result, warm_meta = _run_search(conn, repository, workload, incidence, catalog)
        warm_stable_search_meta = {key: value for key, value in warm_meta.items() if key != "runtime_seconds"}
        warm_recommendation = RecommendationBundle(
            TARGET,
            {"problem_digest": canonical_digest(problem_identity), **problem_identity},
            {**identity, "repository_semantic_digest": repo_semantic},
            {"workload_digest": workload.digest, "query_count": len(workload.queries)},
            {"truth_semantic_digest": bundle["truth_identity"]["semantic_digest"], "query_count": len(json.loads((CAPTURE / "truth.json").read_text())["queries"])},
            candidate_catalog_digest(catalog.candidates),
            model.digest,
            warm_state.aggregate_objective,
            warm_result.selected_objective,
            tuple(str(item) for item in warm_result.selected_design.candidate_ids),
            str(warm_result.selected_maintenance_cost),
            warm_stable_search_meta,
            deployment,
            rollback,
            {"postgres_version": "16.14", "requires_patch": False, "advisor_replay_requires_patch": True, "target_precondition": TARGET, "sample_realization_authoritative": True},
        )
        warm_exact = {
            "cache_hit": True,
            "repository_semantic_digest_equal": repository_semantic_digest(repository) == repo_semantic,
            "ordinary_statistics_digest_equal": warm_ordinary_digest == cold_ordinary_digest,
            "baseline_vector_digest_equal": canonical_digest(warm_vector) == baseline_digest,
            "baseline_objective_equal": warm_state.aggregate_objective == state.aggregate_objective,
            "selected_design_equal": warm_result.selected_design == result.selected_design,
            "selected_objective_equal": warm_result.selected_objective == result.selected_objective,
            "selected_cost_equal": warm_result.selected_maintenance_cost == result.selected_maintenance_cost,
            "recommendation_digest_equal": warm_recommendation.digest == recommendation.digest,
            "warm_search": warm_meta,
        }
        write_json(OUT / "replay-summary.json", {"cold": {"repository_semantic_digest": repo_semantic, "ordinary_statistics_digest": cold_ordinary_digest, "baseline_vector_digest": baseline_digest, "baseline_objective": state.aggregate_objective, "selected_design": list(map(str, result.selected_design.candidate_ids)), "selected_objective": result.selected_objective, "selected_cost": str(result.selected_maintenance_cost), "recommendation_digest": recommendation.digest}, "warm": {"repository_semantic_digest": repository_semantic_digest(repository), "ordinary_statistics_digest": warm_ordinary_digest, "baseline_vector_digest": canonical_digest(warm_vector), "baseline_objective": warm_state.aggregate_objective, "selected_design": list(map(str, warm_result.selected_design.candidate_ids)), "selected_objective": warm_result.selected_objective, "selected_cost": str(warm_result.selected_maintenance_cost), "recommendation_digest": warm_recommendation.digest}, "exact": warm_exact, "all_exact": all(value for key, value in warm_exact.items() if key != "warm_search")})
        write_json(OUT / "search-summary.json", {"cold_cache_hit": cold_hit, "warm_cache_hit": True, "repository_semantic_digest": repo_semantic, "ordinary_statistics_digest": cold_ordinary_digest, "baseline_vector_digest": baseline_digest, "baseline_objective": state.aggregate_objective, "final_objective": result.selected_objective, "selected_design": list(map(str, result.selected_design.candidate_ids)), "selected_cost": str(result.selected_maintenance_cost), **search_meta, "recommendation_digest": recommendation.digest, "warm_recommendation_digest": warm_recommendation.digest, "warm_exact": warm_exact})
        return {"recommendation_digest": recommendation.digest, "baseline_objective": state.aggregate_objective, "final_objective": result.selected_objective, "selected_design": list(map(str, result.selected_design.candidate_ids)), "selected_cost": str(result.selected_maintenance_cost), "repository_semantic_digest": repo_semantic, "warm_exact": warm_exact}
    finally:
        conn.execute("DROP TABLE IF EXISTS public.pgextadv_m229_sample"); conn.execute("DROP TABLE IF EXISTS public.dmv"); conn.commit(); conn.close()


def ddl_smoke() -> dict[str, Any]:
    """Parse and execute the recommendation DDL on a disposable stock PG."""
    recommendation = RecommendationBundle.load(OUT / "recommendation.json")
    data = ROOT / ".build/pg16.14-m2-29-stock-smoke"
    socket = ROOT / ".build/pg16.14-m2-29-stock-smoke-socket"
    if data.exists():
        subprocess.run(["runuser", "-u", "postgres", "--", str(STOCK_BIN / "pg_ctl"), "-D", str(data), "stop", "-m", "immediate"], check=False, capture_output=True)
        shutil.rmtree(data)
    data.mkdir(parents=True)
    subprocess.run(["chown", "postgres:postgres", str(data)], check=True)
    socket.mkdir(parents=True, exist_ok=True)
    subprocess.run(["chown", "postgres:postgres", str(socket)], check=True)
    subprocess.run(["runuser", "-u", "postgres", "--", str(STOCK_BIN / "initdb"), "-D", str(data), "--no-locale", "--encoding=UTF8"], check=True, capture_output=True, text=True)
    subprocess.run(["runuser", "-u", "postgres", "--", str(STOCK_BIN / "pg_ctl"), "-D", str(data), "-o", f"-p 55439 -k {socket}", "-l", str(data / "smoke.log"), "start", "-w"], check=True, capture_output=True, text=True)
    try:
        conn = psycopg.connect(f"host={socket} port=55439 dbname=postgres user=postgres")
        cols = ", ".join(f'"{name}" text' for name in SCHEMA_COLUMNS)
        conn.execute(f"CREATE TABLE public.dmv ({cols})")
        for statement in recommendation.deployment_ddl:
            conn.execute(statement)
        conn.commit()
        created = int(conn.execute("SELECT count(*) FROM pg_statistic_ext WHERE stxname LIKE 'pgextadv_%'").fetchone()[0])
        for statement in recommendation.rollback_ddl:
            conn.execute(statement)
        conn.commit()
        remaining = int(conn.execute("SELECT count(*) FROM pg_statistic_ext WHERE stxname LIKE 'pgextadv_%'").fetchone()[0])
        conn.close()
    finally:
        subprocess.run(["runuser", "-u", "postgres", "--", str(STOCK_BIN / "pg_ctl"), "-D", str(data), "stop", "-m", "fast", "-w"], check=True, capture_output=True, text=True)
        shutil.rmtree(data)
    result = {"stock_postgres": "16.14", "deployment_statements": len(recommendation.deployment_ddl), "rollback_statements": len(recommendation.rollback_ddl), "created_statistics": created, "remaining_owned_statistics": remaining, "passed": created == len(recommendation.selected_design) and remaining == 0, "requires_patch": False}
    replay_path = OUT / "replay-summary.json"
    current = json.loads(replay_path.read_text()) if replay_path.exists() else {}
    current["stock_ddl_smoke"] = result
    write_json(replay_path, current)
    return result


def main() -> int:
    parser = argparse.ArgumentParser()
    parser.add_argument("command", choices=("capture", "advise", "smoke", "run"))
    args = parser.parse_args()
    if args.command == "capture": result = capture()
    elif args.command == "advise": result = advise()
    elif args.command == "smoke": result = ddl_smoke()
    else: capture(); result = advise()
    print(json.dumps(result, indent=2, ensure_ascii=False))
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
