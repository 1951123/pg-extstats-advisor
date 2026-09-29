#!/usr/bin/env python3
"""M2.23 prototype: capture DMV from a pristine stock PostgreSQL server.

The tool intentionally has two phases.  ``capture`` talks only to the stock
production simulator and writes an ignored, sealed artifact.  ``reconstruct``
reads that artifact and talks only to a separate patched advisor cluster.  No
production connection, CSV, or full relation is used by the latter phase.
This is a prototype boundary test, not the production capture CLI.
"""

from __future__ import annotations

import argparse
import hashlib
import json
import random
import shutil
import time
from concurrent.futures import ThreadPoolExecutor, as_completed
from datetime import UTC, datetime
from pathlib import Path
from typing import Any

import psycopg

from pg_extstats_advisor.evaluator.native import NativeEvaluator
from pg_extstats_advisor.models import Design
from pg_extstats_advisor.orchestration import load_prepared_run
from pg_extstats_advisor.postgres.adapter import PostgresAdapter
from pg_extstats_advisor.prepare.acquisition import acquire_payloads
from pg_extstats_advisor.prepare.workload import RelationMetadata

ROOT = Path(__file__).resolve().parents[1]
CAPTURE = ROOT / ".build/production-captures/dmv-m2-23-v1"
DATASET = ROOT / "experiments/environment/dmv-dataset.json"
PREPARED_WORKLOAD = ROOT / "experiments/dmv-m2-15-singletons/prepared-run/workload.json"
RAW_SQL = Path("/root/projects/extended-stats-optim-v2/benchmarks/DMV/queries/dmv.sql")
SOURCE_CSV = Path("/root/projects/extended-stats-optim-v2/benchmarks/DMV/data/original.csv")
STOCK = ROOT / ".build/postgresql-16.14-stock-install/bin"
ADVISOR = ROOT / ".build/postgresql-16.14-install/bin"
PROD_DSN = "host=/root/projects/pg-extstats-advisor/.build/pg16.14-production-sim-socket port=55437 dbname=pgextadv_m2_23_production user=pgextadv_m223_capture"
ADVISOR_DSN = "host=/root/projects/pg-extstats-advisor/.build/pg16.14-advisor-socket port=55438 dbname=postgres user=postgres"
EXPECTED = {
    "source_sha256": "ae310972b7ac08629d135a1da7c580e3fe603bfc2e665e1969005bd481d4605a",
    "workload_sha256": "1953dc96e00c8caeeb9d363070853d27a6548ce43ab14772d8e7637af9c2cd22",
    "effective_workload_digest": "e790933cfc4f0f42d92807170b76cec080621c5b463dab83e23474a0a51151d8",
    "rows": 11591877,
    "sample_rows": 30000,
}
SCHEMA = [
    {"attnum": i, "name": name, "type": "text", "not_null": False}
    for i, name in enumerate(
        (
            "record_type", "registration_class", "state", "county", "body_type",
            "fuel_type", "reg_valid_date", "color", "scofflaw_indicator",
            "suspension_indicator", "revocation_indicator",
        ), 1
    )
]


def digest(value: Any) -> str:
    return hashlib.sha256(json.dumps(value, sort_keys=True, separators=(",", ":"), default=str).encode()).hexdigest()


def semantic_file_digest(path: Path) -> str:
    def scrub(value: Any, key: str = "") -> Any:
        if isinstance(value, dict):
            return {
                str(name): scrub(item, str(name))
                for name, item in value.items()
                if str(name) not in {"capture_timestamp", "timestamp"}
            }
        if isinstance(value, list):
            return [scrub(item, key) for item in value]
        if isinstance(value, str) and value.startswith("/") and ("path" in key or key in {"raw_source", "stock_binary"}):
            return "<LOCAL_PATH>"
        return value
    value = scrub(json.loads(path.read_text()))
    return digest(value)


def now() -> str:
    return datetime.now(UTC).isoformat()


def raw_records() -> list[dict[str, Any]]:
    if hashlib.sha256(RAW_SQL.read_bytes()).hexdigest() != EXPECTED["workload_sha256"]:
        raise RuntimeError("DMV workload checksum mismatch")
    records = []
    for i, line in enumerate(RAW_SQL.read_text().splitlines(), 1):
        if not line.strip():
            continue
        try:
            sql, truth = line.rsplit("||", 1)
            records.append({"query_id": f"dmv.{i}", "sql": sql.strip(), "truth": float(truth), "target_relation": "public.dmv", "weight": 1.0})
        except ValueError as exc:
            raise RuntimeError(f"cannot parse DMV line {i}") from exc
    if len(records) != 1965:
        raise RuntimeError(f"unexpected raw workload size: {len(records)}")
    return records


def effective_digest(records: list[dict[str, Any]]) -> str:
    return digest({"objective_membership_policy": "positive_truth_only", "queries": [
        {"query_id": r["query_id"], "sql": r["sql"], "truth": r["truth"], "target_relation": r["target_relation"]}
        for r in records if r["truth"] > 0
    ]})


def connect(dsn: str) -> psycopg.Connection:
    return psycopg.connect(dsn, autocommit=False)


def count_truths(records: list[dict[str, Any]]) -> tuple[list[dict[str, Any]], dict[str, Any]]:
    """Execute exact COUNT queries on one exported REPEATABLE READ snapshot.

    Workers import the same exported snapshot, so concurrency changes elapsed
    time but not the source state.  Every query is still the canonical COUNT
    SQL; no truth is inferred from an acquisition sample.
    """
    master = connect(PROD_DSN)
    master.execute("BEGIN ISOLATION LEVEL REPEATABLE READ READ ONLY")
    snapshot = str(master.execute("SELECT pg_export_snapshot()").fetchone()[0])
    tx_snapshot = str(master.execute("SELECT txid_current_snapshot()").fetchone()[0])

    def one(item: dict[str, Any]) -> dict[str, Any]:
        conn = connect(PROD_DSN)
        try:
            conn.execute("BEGIN ISOLATION LEVEL REPEATABLE READ READ ONLY")
            conn.execute(f"SET TRANSACTION SNAPSHOT '{snapshot}'")
            observed = int(conn.execute(item["sql"]).fetchone()[0])
            conn.rollback()
            return {**item, "observed_truth": observed, "truth_match": observed == int(item["truth"])}
        finally:
            conn.close()

    started = time.perf_counter()
    results: list[dict[str, Any]] = []
    with ThreadPoolExecutor(max_workers=8) as pool:
        futures = [pool.submit(one, item) for item in records]
        for future in as_completed(futures):
            results.append(future.result())
    master.rollback()
    master.close()
    results.sort(key=lambda row: int(str(row["query_id"]).split(".")[1]))
    mismatch = [r["query_id"] for r in results if not r["truth_match"]]
    return results, {"snapshot": snapshot, "txid_snapshot": tx_snapshot, "elapsed_seconds": time.perf_counter() - started, "mismatch_ids": mismatch, "worker_count": 8}


def capture_sample() -> tuple[list[list[str | None]], dict[str, Any]]:
    conn = connect(PROD_DSN)
    conn.execute("BEGIN ISOLATION LEVEL REPEATABLE READ READ ONLY")
    snapshot = str(conn.execute("SELECT pg_export_snapshot()").fetchone()[0])
    # Client-side reservoir over a stock SELECT stream.  This is intentionally
    # not described as PostgreSQL ANALYZE-equivalent sampling.
    rng = random.Random(22323)
    rows: list[list[str | None]] = []
    seen = 0
    started = time.perf_counter()
    # Text COPY is safe here because canonical DMV values contain neither tabs
    # nor newlines; psycopg decodes complete rows without CSV chunk framing.
    with conn.cursor().copy("COPY (SELECT * FROM public.dmv) TO STDOUT WITH (FORMAT text, NULL '\\N')") as copy:
        for parsed in copy.rows():
            seen += 1
            row = [None if value is None else str(value) for value in parsed]
            if len(rows) < EXPECTED["sample_rows"]:
                rows.append(row)
            else:
                slot = rng.randrange(seen)
                if slot < EXPECTED["sample_rows"]:
                    rows[slot] = row
    conn.rollback()
    conn.close()
    rows.sort(key=lambda row: tuple("" if x is None else x for x in row))
    return rows, {"method": "stock COPY SELECT stream + deterministic client reservoir", "seed": 22323, "source_row_count": seen, "snapshot": snapshot, "elapsed_seconds": time.perf_counter() - started, "order": "lexicographically canonicalized after reservoir"}


def capture_metadata() -> tuple[dict[str, Any], dict[str, Any]]:
    conn = connect(PROD_DSN)
    conn.execute("BEGIN ISOLATION LEVEL REPEATABLE READ READ ONLY")
    snapshot = str(conn.execute("SELECT pg_export_snapshot()").fetchone()[0])
    version = str(conn.execute("SELECT version()").fetchone()[0])
    row = conn.execute("SELECT c.oid,n.nspname,c.relname,c.relpersistence,c.reltuples,c.relpages FROM pg_class c JOIN pg_namespace n ON n.oid=c.relnamespace WHERE c.oid='public.dmv'::regclass").fetchone()
    cols = conn.execute("SELECT attnum,attname,atttypid::regtype::text,atttypmod,attcollation::regcollation::text,attnotnull FROM pg_attribute WHERE attrelid='public.dmv'::regclass AND attnum>0 AND NOT attisdropped ORDER BY attnum").fetchall()
    stats = conn.execute("SELECT attname,null_frac,avg_width,n_distinct,most_common_vals FROM pg_stats WHERE schemaname='public' AND tablename='dmv' ORDER BY attname").fetchall()
    gucs = {name: str(conn.execute("SHOW " + name).fetchone()[0]) for name in ("server_version_num", "default_statistics_target", "jit", "max_parallel_workers_per_gather", "work_mem", "random_page_cost", "effective_cache_size")}
    conn.rollback(); conn.close()
    schema = {"relation": {"schema": "public", "name": "dmv", "oid": int(row[0]), "persistence": row[3], "reltuples": float(row[4]), "relpages": int(row[5])}, "columns": [dict(zip(("attnum", "name", "type", "typmod", "collation", "not_null"), c, strict=True)) for c in cols]}
    env = {"postgres_version": version, "server_version_num": gucs.pop("server_version_num"), "stock_binary": str(STOCK / "postgres"), "stock_binary_sha256": hashlib.sha256((STOCK / "postgres").read_bytes()).hexdigest(), "source_tarball_sha256": "f6d077142737920858ce958ccdb75c6ee137a63b5b0853c70693d401ac7e3471", "database": "pgextadv_m2_23_production", "relation_row_count": EXPECTED["rows"], "snapshot": snapshot, "gucs": gucs, "stats_summary": [dict(zip(("column", "nullfrac", "avg_width", "distinct", "most_common_vals"), s, strict=True)) for s in stats]}
    return env, schema


def write_json(path: Path, value: Any) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text(json.dumps(value, sort_keys=True, indent=2, ensure_ascii=False) + "\n")


def seal() -> dict[str, Any]:
    components = {name: semantic_file_digest(CAPTURE / name) for name in ("environment.json", "schema.json", "workload.json", "truth.json", "acquisition/sample.json")}
    binary_path = CAPTURE / "acquisition/sample.rows.bin"
    binary_sha = hashlib.sha256(binary_path.read_bytes()).hexdigest()
    semantic = {"capture_schema_version": 1, "benchmark": "DMV", "production_pg_major": "16", "source_relation": "public.dmv", "components": components, "acquisition_binary_sha256": binary_sha}
    root = digest(semantic)
    manifest = {"capture_schema_version": 1, "benchmark": "DMV", "sealed": True, "semantic_digest_algorithm": "sha256-canonical-json-v1", "semantic_digest": root, "source": {"database": "pgextadv_m2_23_production", "relation": "public.dmv", "source_csv_sha256": EXPECTED["source_sha256"]}, "consistency": {"status": "best-effort/multi-snapshot", "reason": "truth workers and sample used separate exported repeatable-read snapshots; each operation is internally consistent"}, "components": components, "acquisition_binary_sha256": binary_sha, "capture_timestamp": now(), "capture_mode": "prototype_stock_read_only", "capture_role": "pgextadv_m223_capture", "read_only": True, "semantic_binding": semantic}
    write_json(CAPTURE / "manifest.json", manifest)
    write_json(CAPTURE / "capture-manifest.json", manifest)
    return manifest


def verify_capture(path: Path = CAPTURE) -> dict[str, Any]:
    manifest = json.loads((path / "manifest.json").read_text())
    if not manifest.get("sealed"):
        raise RuntimeError("capture is not sealed")
    components = {name: semantic_file_digest(path / name) for name in ("environment.json", "schema.json", "workload.json", "truth.json", "acquisition/sample.json")}
    if components != manifest["components"]:
        raise RuntimeError("capture component digest mismatch")
    binary_path = path / "acquisition/sample.rows.bin"
    if not binary_path.exists() or hashlib.sha256(binary_path.read_bytes()).hexdigest() != manifest.get("acquisition_binary_sha256"):
        raise RuntimeError("capture binary sample digest mismatch")
    semantic = dict(manifest["semantic_binding"])
    if digest(semantic) != manifest["semantic_digest"]:
        raise RuntimeError("capture root digest mismatch")
    workload = json.loads((path / "workload.json").read_text())
    truth = json.loads((path / "truth.json").read_text())
    sample = json.loads((path / "acquisition/sample.json").read_text())
    if {r["query_id"] for r in workload["effective_queries"]} != {r["query_id"] for r in truth["queries"] if r["truth"] > 0}:
        raise RuntimeError("workload/truth membership mismatch")
    if len(sample["rows"]) != EXPECTED["sample_rows"] or len(sample["rows"][0]) != 11:
        raise RuntimeError("sample/schema mismatch")
    if not str(json.loads((path / "environment.json").read_text())["server_version_num"]).startswith("16"):
        raise RuntimeError("unsupported PostgreSQL major")
    return {"sealed": True, "semantic_digest": manifest["semantic_digest"], "component_digests": components}


def capture() -> None:
    started = time.perf_counter()
    records = raw_records()
    if effective_digest(records) != EXPECTED["effective_workload_digest"]:
        raise RuntimeError("effective workload digest mismatch")
    env, schema = capture_metadata()
    write_json(CAPTURE / "environment.json", env)
    write_json(CAPTURE / "schema.json", schema)
    effective = [r for r in records if r["truth"] > 0]
    workload = {"schema_version": 1, "benchmark": "DMV", "raw_source": str(RAW_SQL), "raw_source_sha256": EXPECTED["workload_sha256"], "raw_query_count": len(records), "effective_query_count": len(effective), "excluded_query_ids": [r["query_id"] for r in records if r["truth"] == 0], "effective_workload_digest": EXPECTED["effective_workload_digest"], "effective_queries": effective}
    write_json(CAPTURE / "workload.json", workload)
    truth_meta_path = CAPTURE / "truth.json"
    if truth_meta_path.exists():
        saved = json.loads(truth_meta_path.read_text())
        truth_meta = dict(saved["snapshot"])
    else:
        truth_rows, truth_meta = count_truths(records)
        if truth_meta["mismatch_ids"]:
            raise RuntimeError(f"stock truth mismatch: {truth_meta['mismatch_ids']}")
        write_json(truth_meta_path, {"provenance": "full production relation exact COUNT", "relation": "public.dmv", "row_count": EXPECTED["rows"], "method": "canonical SQL SELECT COUNT(*)", "snapshot": truth_meta, "queries": [{"query_id": r["query_id"], "truth": r["observed_truth"], "sql": r["sql"]} for r in truth_rows]})
    rows, sample_meta = capture_sample()
    sample_bytes = json.dumps({"schema": SCHEMA, "rows": rows}, sort_keys=True, separators=(",", ":"), ensure_ascii=False).encode()
    binary = bytearray(b"PGEXTSTATS-M223-SAMPLE\0")
    for row in rows:
        for value in row:
            encoded = b"" if value is None else str(value).encode("utf-8")
            binary.extend(len(encoded).to_bytes(4, "big", signed=False))
            binary.extend(encoded)
    (CAPTURE / "acquisition/sample.rows.bin").write_bytes(binary)
    sample = {"schema": SCHEMA, "rows": rows, "row_count": len(rows), "source_relation_row_count": EXPECTED["rows"], "method": sample_meta["method"], "seed": sample_meta["seed"], "sample_metadata": sample_meta, "semantic_digest": digest({"schema": SCHEMA, "rows": rows}), "binary_digest": hashlib.sha256(binary).hexdigest(), "binary_serialization": "PGEXTSTATS-M223-SAMPLE length-prefixed UTF-8 values", "capture_timestamp": now()}
    write_json(CAPTURE / "acquisition/sample.json", sample)
    (CAPTURE / "acquisition/sample.canonical.json").write_bytes(sample_bytes)
    manifest = seal()
    verification = verify_capture()
    write_json(CAPTURE / "capture-metrics.json", {"total_elapsed_seconds": time.perf_counter() - started, "truth_elapsed_seconds": truth_meta["elapsed_seconds"], "sample_elapsed_seconds": sample_meta["elapsed_seconds"], "artifact_bytes": sum(p.stat().st_size for p in CAPTURE.rglob("*") if p.is_file()), "verification": verification})
    print(json.dumps({"manifest": manifest, "verification": verification}, indent=2))


def mutation_audit() -> dict[str, Any]:
    conn = connect(PROD_DSN)
    attempts = {}
    for label, sql in {"create_table": "CREATE TABLE public.m223_forbidden(x int)", "create_statistics": "CREATE STATISTICS m223_forbidden (mcv) ON record_type FROM public.dmv", "insert": "INSERT INTO public.dmv VALUES (NULL)"}.items():
        try:
            conn.execute(sql); conn.commit(); attempts[label] = "UNEXPECTEDLY_ACCEPTED"
        except psycopg.Error as exc:
            conn.rollback(); attempts[label] = type(exc).__name__
    conn.close(); return attempts


def reconstruct() -> None:
    """Rebuild a planner/statistics state from the sealed artifact only."""
    verification = verify_capture()
    sample = json.loads((CAPTURE / "acquisition/sample.json").read_text())
    dataset = json.loads(DATASET.read_text())
    prepared = load_prepared_run(ROOT / "experiments/dmv-m2-15-singletons/prepared-run")
    out = CAPTURE / "advisor-reconstruction"
    out.mkdir(parents=True, exist_ok=True)
    conn = psycopg.connect(ADVISOR_DSN)
    try:
        conn.execute("DROP TABLE IF EXISTS public.pgextadv_m223_sample")
        conn.execute("DROP TABLE IF EXISTS public.dmv")
        conn.execute("CREATE UNLOGGED TABLE public.dmv (record_type text, registration_class text, state text, county text, body_type text, fuel_type text, reg_valid_date text, color text, scofflaw_indicator text, suspension_indicator text, revocation_indicator text)")
        conn.execute("CREATE UNLOGGED TABLE public.pgextadv_m223_sample (record_type text, registration_class text, state text, county text, body_type text, fuel_type text, reg_valid_date text, color text, scofflaw_indicator text, suspension_indicator text, revocation_indicator text)")
        with conn.cursor().copy("COPY public.pgextadv_m223_sample FROM STDIN") as copy:
            for row in sample["rows"]:
                copy.write_row(tuple(row))
        # The patched ANALYZE path needs nonzero target pages for the planner
        # to expose base-relation estimates.  This is the same 30k-row
        # sample-sized staging input, never a production-table copy.
        with conn.cursor().copy("COPY public.dmv FROM STDIN") as copy:
            for row in sample["rows"]:
                copy.write_row(tuple(row))
        conn.commit()
        relrow = conn.execute("SELECT oid FROM pg_class WHERE oid='public.dmv'::regclass").fetchone()
        metadata = RelationMetadata("public", "dmv", int(relrow[0]), tuple((int(c["attnum"]), str(c["name"]), str(c["type"]), bool(c["not_null"])) for c in dataset["ordered_column_schema"]))
        conn.execute("SELECT set_config('pg_extstats.frozen_sample_mode','replay',false)")
        conn.execute("SELECT set_config('pg_extstats.frozen_sample_relation','public.pgextadv_m223_sample',false)")
        conn.execute(f"SELECT set_config('pg_extstats.frozen_totalrows','{EXPECTED['rows']}',false)")
        repo_path = out / "repository"
        if repo_path.exists():
            shutil.rmtree(repo_path)
        result = acquire_payloads(conn, prepared.catalog, repo_path, statistics_target=100, upstream_sha256="f6d077142737920858ce958ccdb75c6ee137a63b5b0853c70693d401ac7e3471", patch_commit="prototype-stock-capture", repository_id="dmv-m2-23-prototype", source_relations=(metadata,))
        repository = result.repository
        ordinary = conn.execute("SELECT attname,null_frac,avg_width,n_distinct FROM pg_stats WHERE schemaname='public' AND tablename='dmv' ORDER BY attname").fetchall()
        ordinary_digest = digest([dict(zip(("column", "nullfrac", "avg_width", "distinct"), row, strict=True)) for row in ordinary])
        evaluator = NativeEvaluator(prepared.workload, repository, prepared.incidence, PostgresAdapter(conn, repository))
        state = evaluator.evaluate_design(Design(()))
        estimate_vector = [{"query_id": str(item.query_id), "estimate": item.estimate, "truth": item.truth, "contribution": item.contribution} for item in state.query_evaluations]
        manifest = json.loads((repo_path / "manifest.json").read_text())
        states = {}
        for item in manifest["candidates"]:
            states[item["state"]] = states.get(item["state"], 0) + 1
        advisor_rows = int(conn.execute("SELECT count(*) FROM public.dmv").fetchone()[0])
        sample_rows = int(conn.execute("SELECT count(*) FROM public.pgextadv_m223_sample").fetchone()[0])
        result_doc = {"milestone": "M2.23", "sealed_capture_digest": verification["semantic_digest"], "production_reconnected": False, "advisor_binary": str(ADVISOR / "postgres"), "advisor_binary_sha256": hashlib.sha256((ADVISOR / "postgres").read_bytes()).hexdigest(), "advisor_version": str(conn.execute("SELECT version()").fetchone()[0]), "full_relation_absence": {"advisor_public_dmv_rows": advisor_rows, "prototype_sample_staging_rows": sample_rows, "interpretation": "both relations are 30k-row disposable staging/sample inputs; no 11,591,877-row production relation"}, "original_relation_rows": EXPECTED["rows"], "frozen_totalrows": EXPECTED["rows"], "repository_digest": repository.digest, "ordinary_statistics_digest": ordinary_digest, "realization_state_counts": states, "candidate_count": len(repository.payloads), "baseline_objective": state.aggregate_objective, "estimate_vector_digest": digest(estimate_vector), "estimate_vector": estimate_vector, "repository_path": str(repo_path), "capture_input_allowlist": [str(CAPTURE)], "used_canonical_csv": False, "used_production_data_directory": False, "search_started": False}
        write_json(out / "reconstruction.json", result_doc)
        conn.execute("DROP TABLE IF EXISTS public.pgextadv_m223_sample")
        conn.execute("DROP TABLE IF EXISTS public.dmv")
        conn.commit()
    finally:
        conn.close()




def main() -> int:
    parser = argparse.ArgumentParser()
    parser.add_argument("command", choices=("capture", "verify", "mutation-audit", "reconstruct"))
    args = parser.parse_args()
    if args.command == "capture": capture()
    elif args.command == "verify": print(json.dumps(verify_capture(), indent=2))
    elif args.command == "mutation-audit": print(json.dumps(mutation_audit(), indent=2))
    else: reconstruct()
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
