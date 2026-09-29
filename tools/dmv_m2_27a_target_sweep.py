#!/usr/bin/env python3
"""M2.27a: same-snapshot DMV target-grid characterization.

The command has two deliberately separate phases. ``capture`` uses only the
stock production simulator and writes Bundle v2; ``sweep`` uses only the
ignored capture cache and the patched advisor cluster, with ordinary
statistics and no extended-statistics objects.  It never runs a budget search.
"""

from __future__ import annotations

import argparse
import hashlib
import json
import math
import random
import shutil
import statistics
import sys
import time
from concurrent.futures import ThreadPoolExecutor, as_completed
from pathlib import Path
from typing import Any

import psycopg

ROOT = Path(__file__).resolve().parents[1]
if str(ROOT / "src") not in sys.path:
    sys.path.insert(0, str(ROOT / "src"))

from pg_extstats_advisor.capture.bundle import decode_sample, encode_sample
from pg_extstats_advisor.capture.bundle_v2 import verify_bundle_v2, write_bundle_root
from pg_extstats_advisor.capture.manifest import canonical_digest
from pg_extstats_advisor.capture.metrics import marginal_metrics, summarize_objectives
from pg_extstats_advisor.capture.target_policy import TargetGridPolicy, inspect_override_rows
from pg_extstats_advisor.postgres.extraction import extract_target_estimate

PROD_DSN = "host=/root/projects/pg-extstats-advisor/.build/pg16.14-production-sim-socket port=55437 dbname=pgextadv_m2_23_production user=pgextadv_m223_capture"
ADVISOR_DSN = "host=/root/projects/pg-extstats-advisor/.build/pg16.14-advisor-socket port=55438 dbname=postgres user=postgres"
RAW_SQL = Path("/root/projects/extended-stats-optim-v2/benchmarks/DMV/queries/dmv.sql")
OUT = ROOT / "experiments/dmv-m2-27a-reservoir-target-characterization"
BUNDLE = ROOT / ".build/production-captures/dmv-m2-27a-v2"
CACHE = ROOT / ".build/artifact-cache/dmv-m2-27a-v2"
SAMPLE_MAGIC = b"PGEXTSTATS-M223-SAMPLE\0"
SOURCE_ROWS = 11_591_877
SCHEMA_COLUMNS = (
    "record_type", "registration_class", "state", "county", "body_type", "fuel_type",
    "reg_valid_date", "color", "scofflaw_indicator", "suspension_indicator",
    "revocation_indicator",
)
POLICY = TargetGridPolicy()


def digest(value: Any) -> str:
    return hashlib.sha256(json.dumps(value, sort_keys=True, separators=(",", ":"), default=str).encode()).hexdigest()


def write_json(path: Path, value: Any) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text(json.dumps(value, sort_keys=True, indent=2, default=str) + "\n")


def records() -> list[dict[str, Any]]:
    result = []
    for line_no, line in enumerate(RAW_SQL.read_text(encoding="utf-8").splitlines(), 1):
        if not line.strip():
            continue
        sql, truth = line.rsplit("||", 1)
        result.append({"query_id": f"dmv.{line_no}", "sql": sql.strip(), "truth": float(truth), "target_relation": "public.dmv", "weight": 1.0})
    if len(result) != 1965:
        raise RuntimeError(f"expected 1965 DMV queries, got {len(result)}")
    return result


def conn(dsn: str) -> psycopg.Connection[Any]:
    return psycopg.connect(dsn, autocommit=False)


def snapshot_token(c: psycopg.Connection[Any]) -> tuple[str, str, str]:
    c.execute("BEGIN ISOLATION LEVEL REPEATABLE READ READ ONLY")
    token = str(c.execute("SELECT pg_export_snapshot()").fetchone()[0])
    tx_snapshot = str(c.execute("SELECT txid_current_snapshot()").fetchone()[0])
    version = str(c.execute("SELECT version()").fetchone()[0])
    return token, tx_snapshot, version


def imported_connection(token: str) -> psycopg.Connection[Any]:
    c = conn(PROD_DSN)
    c.execute("BEGIN ISOLATION LEVEL REPEATABLE READ READ ONLY")
    c.execute(f"SET TRANSACTION SNAPSHOT '{token}'")
    return c


def truth_worker(token: str, item: dict[str, Any]) -> dict[str, Any]:
    c = imported_connection(token)
    try:
        value = int(c.execute(item["sql"]).fetchone()[0])
        return {"query_id": item["query_id"], "sql": item["sql"], "truth": value, "declared_truth": item["truth"], "truth_match": value == int(item["truth"])}
    finally:
        c.rollback(); c.close()


def reservoir_worker(token: str) -> tuple[int, dict[str, list[list[str | None]]], int, dict[str, Any]]:
    """One imported worker scans once and maintains nine independent reservoirs."""
    c = imported_connection(token)
    capacities = {(target, realization): 300 * target for target, realization in POLICY.expected_cells()}
    rngs = {cell: random.Random(POLICY.seed(*cell)) for cell in POLICY.expected_cells()}
    reservoirs: dict[tuple[int, str], list[list[str | None]]] = {cell: [] for cell in POLICY.expected_cells()}
    seen = 0
    started = time.perf_counter()
    try:
        with c.cursor().copy("COPY (SELECT * FROM public.dmv) TO STDOUT WITH (FORMAT text, NULL '\\N')") as copy:
            for parsed in copy.rows():
                seen += 1
                row = [None if value is None else str(value) for value in parsed]
                for cell, capacity in capacities.items():
                    sample = reservoirs[cell]
                    if len(sample) < capacity:
                        sample.append(row)
                    else:
                        slot = rngs[cell].randrange(seen)
                        if slot < capacity:
                            sample[slot] = row
        for sample in reservoirs.values():
            sample.sort(key=lambda row: tuple("" if value is None else value for value in row))
        return seen, {f"{target}-{realization}": sample for (target, realization), sample in reservoirs.items()}, {"worker": "reservoir-grid", "snapshot_imported": True, "elapsed_seconds": time.perf_counter() - started}
    finally:
        c.rollback(); c.close()


def permission_evidence() -> dict[str, Any]:
    c = conn(PROD_DSN)
    try:
        row = c.execute("SELECT current_user, current_database(), current_setting('default_transaction_read_only'), has_table_privilege(current_user, 'public.dmv', 'SELECT'), has_table_privilege(current_user, 'public.dmv', 'INSERT'), has_table_privilege(current_user, 'public.dmv', 'TRUNCATE'), has_schema_privilege(current_user, 'public', 'CREATE'), pg_has_role(current_user, 'pg_read_all_data', 'member')").fetchone()
        return {"role": str(row[0]), "database": str(row[1]), "default_transaction_read_only": str(row[2]), "select_allowed": bool(row[3]), "insert_allowed": bool(row[4]), "truncate_allowed": bool(row[5]), "schema_create_allowed": bool(row[6]), "pg_read_all_data_member": bool(row[7]), "denied_probe_policy": "read-only transaction plus has_* privilege checks; no mutation was accepted or committed", "grant": "GRANT SELECT ON TABLE public.dmv TO pgextadv_m223_capture"}
    finally:
        c.rollback(); c.close()


def capture() -> None:
    if (BUNDLE / "bundle.json").exists():
        raise RuntimeError("sealed v2 bundle already exists; refusing overwrite")
    if not RAW_SQL.exists():
        raise RuntimeError(f"missing authoritative workload: {RAW_SQL}")
    shutil.rmtree(BUNDLE, ignore_errors=True)
    shutil.rmtree(CACHE, ignore_errors=True)
    started = time.perf_counter()
    query_records = records()
    effective = [item for item in query_records if item["truth"] > 0]
    coordinator = conn(PROD_DSN)
    capture_succeeded = False
    token, tx_snapshot, version = snapshot_token(coordinator)
    try:
        relation = coordinator.execute("SELECT c.oid,n.nspname,c.relname,c.reltuples,c.relpages FROM pg_class c JOIN pg_namespace n ON n.oid=c.relnamespace WHERE c.oid='public.dmv'::regclass").fetchone()
        if relation is None:
            raise RuntimeError("public.dmv is missing")
        row_count = int(coordinator.execute("SELECT count(*) FROM public.dmv").fetchone()[0])
        if row_count != SOURCE_ROWS:
            raise RuntimeError(f"source row count changed: {row_count}")
        columns = coordinator.execute("SELECT attnum,attname,atttypid::regtype::text,attstattarget FROM pg_attribute WHERE attrelid='public.dmv'::regclass AND attnum>0 AND NOT attisdropped ORDER BY attnum").fetchall()
        extrows = coordinator.execute("SELECT n.nspname,c.relname,e.stxname,e.stxstattarget FROM pg_statistic_ext e JOIN pg_class c ON c.oid=e.stxrelid JOIN pg_namespace n ON n.oid=c.relnamespace WHERE e.stxrelid='public.dmv'::regclass").fetchall()
        column_rows = [("public", "dmv", str(item[1]), int(item[3])) for item in columns]
        overrides = [item.as_dict() for item in inspect_override_rows(column_rows, extrows, {"public.dmv"}, {"public.dmv": set(SCHEMA_COLUMNS)})]
        if overrides:
            raise RuntimeError(f"relevant extended-statistics target override(s): {overrides}")
        truth_rows: list[dict[str, Any]] = []
        with ThreadPoolExecutor(max_workers=8) as pool:
            futures = [pool.submit(truth_worker, token, item) for item in query_records]
            for future in as_completed(futures):
                truth_rows.append(future.result())
        truth_rows.sort(key=lambda item: item["query_id"])
        if not all(item["truth_match"] for item in truth_rows):
            raise RuntimeError("authoritative truth mismatch in imported snapshot")
        seen, samples, reservoir_meta = reservoir_worker(token)
        if seen != SOURCE_ROWS:
            raise RuntimeError(f"reservoir stream row count changed: {seen}")
        snapshot_identity = canonical_digest({"database": "pgextadv_m2_23_production", "relation": "public.dmv", "txid_snapshot": tx_snapshot, "server_version": version, "source_rows": seen})
        snapshot = {"mode": "strong_single_snapshot", "snapshot_identity": snapshot_identity, "exported_snapshot_token_sha256": hashlib.sha256(token.encode()).hexdigest(), "txid_snapshot": tx_snapshot, "coordinator": {"isolation": "repeatable_read", "read_only": True, "exported": True, "committed_after_workers": False, "worker_count": 9}, "components": {"metadata": snapshot_identity, "truth": snapshot_identity, "reservoir_grid": snapshot_identity}}
        cells = []
        for target, realization in POLICY.expected_cells():
            name = f"{target}-{realization}"
            sample = samples[name]
            binary = encode_sample(sample)
            artifact = CACHE / f"target-{target}" / realization
            artifact.mkdir(parents=True, exist_ok=True)
            binary_path = artifact / "sample.copy.bin"
            binary_path.write_bytes(binary)
            semantic = canonical_digest({"columns": list(SCHEMA_COLUMNS), "rows": sample})
            artifact_identity = canonical_digest({"target": target, "realization": realization, "seed": POLICY.seed(target, realization), "semantic": semantic, "binary": hashlib.sha256(binary).hexdigest()})
            write_json(artifact / "manifest.json", {"statistics_target": target, "realization_id": realization, "canonical": realization == POLICY.canonical_realization_id, "seed": POLICY.seed(target, realization), "sample_row_count": len(sample), "source_population_rows": seen, "semantic_sha256": semantic, "binary_sha256": hashlib.sha256(binary).hexdigest(), "artifact_identity": artifact_identity, "native_analyze_equivalent": False})
            cells.append({"statistics_target": target, "realization_id": realization, "canonical": realization == POLICY.canonical_realization_id, "seed": POLICY.seed(target, realization), "sample_row_count": len(sample), "source_population_rows": seen, "semantic_sha256": semantic, "binary_sha256": hashlib.sha256(binary).hexdigest(), "artifact_identity": artifact_identity, "artifact_cache_relative": str(artifact.relative_to(ROOT / ".build/artifact-cache")), "snapshot_identity": snapshot_identity, "acquisition_method": "deterministic_reservoir_v1", "native_analyze_equivalent": False})
        acquisition = {"target_policy_digest": POLICY.digest, "cells": cells, "same_snapshot": True, "native_analyze_equivalent": False, "source_population_rows": seen, "reservoir_worker": reservoir_meta}
        environment = {"benchmark": "DMV", "postgres_version": version, "server_version_num": str(coordinator.execute("SHOW server_version_num").fetchone()[0]), "stock_binary": str(ROOT / ".build/postgresql-16.14-stock-install/bin/postgres"), "upstream_tarball_sha256": "f6d077142737920858ce958ccdb75c6ee137a63b5b0853c70693d401ac7e3471", "build_input_digest": "94941ca606a390ad4a0c540158aa49b199820b21603bfd626acaa12948836c25", "database": "pgextadv_m2_23_production", "source_relation": "public.dmv", "source_population_rows": seen, "capture_role": "pgextadv_m223_capture", "native_analyze_equivalent": False}
        schema = {"relation": "public.dmv", "oid_provenance": int(relation[0]), "columns": [{"attnum": int(item[0]), "name": str(item[1]), "type": str(item[2]), "attstattarget": int(item[3])} for item in columns], "column_order": list(SCHEMA_COLUMNS), "target_override_scan": {"scope": ["public.dmv"], "column_overrides": [], "extended_statistics_overrides": [], "status": "pass_fail_closed"}}
        workload = {"raw_query_count": len(query_records), "effective_query_count": len(effective), "excluded_query_ids": [item["query_id"] for item in query_records if item["truth"] <= 0], "objective_membership_policy": "positive_truth_only", "source": str(RAW_SQL), "source_sha256": hashlib.sha256(RAW_SQL.read_bytes()).hexdigest(), "queries": effective, "effective_workload_digest": digest(effective)}
        truth = {"method": "exact COUNT on imported repeatable-read snapshot", "source_population_rows": seen, "queries": truth_rows, "digest": digest(truth_rows), "snapshot_identity": snapshot_identity}
        permission = permission_evidence()
        # Coordinator is deliberately kept open until every component has been written/validated.
        snapshot["coordinator"]["committed_after_workers"] = True
        staging = BUNDLE.with_name(BUNDLE.name + ".partial")
        shutil.rmtree(staging, ignore_errors=True)
        write_bundle_root(staging, POLICY, snapshot, acquisition, environment=environment, schema=schema, workload=workload, truth=truth, permissions=permission)
        staging.rename(BUNDLE)
        summary = {"milestone": "M2.27a", "status": "capture_complete", "bundle_schema_version": "production-capture-bundle-v2", "bundle_path": str(BUNDLE), "source_population_rows": seen, "raw_query_count": len(query_records), "effective_query_count": len(effective), "target_grid": list(POLICY.allowed_targets), "realizations": list(POLICY.realization_ids), "same_snapshot": True, "native_analyze_equivalent": False, "override_policy": "fail_closed_relevant_scope", "search_started": False, "extstats_created": False, "elapsed_seconds": time.perf_counter() - started}
        write_json(OUT / "capture-summary.json", summary)
        write_json(OUT / "permission-evidence.json", permission)
        write_json(OUT / "target-policy.json", POLICY.as_dict())
        write_json(OUT / "snapshot-provenance.json", snapshot)
        write_json(OUT / "bundle-verification.json", verify_bundle_v2(BUNDLE))
        capture_succeeded = True
        print(json.dumps(summary, indent=2))
    finally:
        if capture_succeeded:
            coordinator.commit()
        else:
            coordinator.rollback()
        coordinator.close()


def ordinary_summary(c: psycopg.Connection[Any]) -> dict[str, Any]:
    rows = c.execute("SELECT a.attnum,a.attname,s.stanullfrac,s.stawidth,s.stadistinct,s.stakind1,s.stakind2,s.stakind3,s.stakind4,s.stakind5,cardinality(s.stavalues1),cardinality(s.stavalues2),cardinality(s.stavalues3),cardinality(s.stavalues4),cardinality(s.stavalues5),cardinality(s.stanumbers1),cardinality(s.stanumbers2),cardinality(s.stanumbers3),cardinality(s.stanumbers4),cardinality(s.stanumbers5),s.stavalues1::text,s.stavalues2::text,s.stavalues3::text,s.stavalues4::text,s.stavalues5::text,s.stanumbers1::text,s.stanumbers2::text,s.stanumbers3::text,s.stanumbers4::text,s.stanumbers5::text FROM pg_attribute a LEFT JOIN pg_statistic s ON s.starelid=a.attrelid AND s.staattnum=a.attnum AND s.stainherit=false WHERE a.attrelid='public.dmv'::regclass AND a.attnum>0 AND NOT a.attisdropped ORDER BY a.attnum").fetchall()
    return {"digest": digest([list(row) for row in rows]), "rows": [list(row[:20]) for row in rows]}


def qerror(estimate: float, truth: float) -> float | None:
    return None if estimate <= 0 or truth <= 0 else max(estimate / truth, truth / estimate)


def evaluate_connection(c: psycopg.Connection[Any], queries: list[dict[str, Any]]) -> dict[str, Any]:
    values = []
    for item in queries:
        plan = c.execute(f"EXPLAIN (FORMAT JSON) {item['sql']}").fetchone()[0]
        estimate = extract_target_estimate(plan, "dmv")
        values.append({"query_id": item["query_id"], "estimate": estimate, "truth": item["truth"], "q_error": qerror(estimate, item["truth"])})
    mean_qerror = statistics.fmean(item["q_error"] for item in values if item["q_error"] is not None)
    aggregate_objective = math.fsum(item["q_error"] for item in values if item["q_error"] is not None)
    # ``objective`` is retained for raw-v1 compatibility; it is the
    # per-query mean, not the project's authoritative aggregate objective.
    return {"objective": mean_qerror, "mean_qerror": mean_qerror, "aggregate_objective": aggregate_objective, "estimate_vector_digest": digest(values), "q_error_vector_digest": digest([item["q_error"] for item in values]), "query_count": len(values)}


def fresh_replay(cell: dict[str, Any], queries: list[dict[str, Any]]) -> dict[str, Any]:
    """Replay one canonical cell on a fresh backend for the exactness gate."""
    target = int(cell["statistics_target"])
    sample_path = ROOT / ".build/artifact-cache" / cell["artifact_cache_relative"] / "sample.copy.bin"
    rows = decode_sample(sample_path, int(cell["sample_row_count"]), len(SCHEMA_COLUMNS))
    fresh = conn(ADVISOR_DSN)
    try:
        fresh.execute("DROP TABLE IF EXISTS public.dmv")
        fresh.execute("CREATE UNLOGGED TABLE public.dmv (" + ",".join(f'"{name}" text' for name in SCHEMA_COLUMNS) + ")")
        with fresh.cursor().copy("COPY public.dmv FROM STDIN") as copy:
            for row in rows:
                copy.write_row(tuple(row))
        fresh.execute("SELECT set_config('default_statistics_target', %s, false)", (str(target),))
        fresh.execute("SELECT set_config('pg_extstats.frozen_sample_mode', 'replay', false)")
        fresh.execute("SELECT set_config('pg_extstats.frozen_sample_relation', 'public.dmv', false)")
        fresh.execute("SELECT set_config('pg_extstats.frozen_totalrows', %s, false)", (str(SOURCE_ROWS),))
        fresh.execute("ANALYZE public.dmv")
        stats = ordinary_summary(fresh)
        physical = evaluate_connection(fresh, queries)
        ext_count = int(fresh.execute("SELECT count(*) FROM pg_statistic_ext WHERE stxrelid='public.dmv'::regclass").fetchone()[0])
        fresh.commit()
        return {"statistics_target": target, "realization_id": cell["realization_id"], "ordinary_statistics_digest": stats["digest"], "estimate_vector_digest": physical["estimate_vector_digest"], "q_error_vector_digest": physical["q_error_vector_digest"], "objective": physical["objective"], "extstats_count": ext_count, "fresh_backend": True}
    finally:
        fresh.close()


def sweep() -> None:
    verification = verify_bundle_v2(BUNDLE)
    acquisition = json.loads((BUNDLE / "acquisition.json").read_text())
    workload = json.loads((BUNDLE / "workload.json").read_text())["queries"]
    CACHE.mkdir(parents=True, exist_ok=True)
    # Ensure the patched advisor cluster is running before this command.
    c = conn(ADVISOR_DSN)
    try:
        results = []
        for cell in acquisition["cells"]:
            target = int(cell["statistics_target"]); realization = str(cell["realization_id"])
            sample_path = ROOT / ".build/artifact-cache" / cell["artifact_cache_relative"] / "sample.copy.bin"
            sample_rows = decode_sample(sample_path, int(cell["sample_row_count"]), len(SCHEMA_COLUMNS))
            c.execute("DROP TABLE IF EXISTS public.dmv")
            c.execute("CREATE UNLOGGED TABLE public.dmv (" + ",".join(f'"{name}" text' for name in SCHEMA_COLUMNS) + ")")
            with c.cursor().copy("COPY public.dmv FROM STDIN") as copy:
                for row in sample_rows:
                    copy.write_row(tuple(row))
            c.execute("SELECT set_config('default_statistics_target', %s, false)", (str(target),))
            c.execute("SELECT set_config('pg_extstats.frozen_sample_mode', 'replay', false)")
            c.execute("SELECT set_config('pg_extstats.frozen_sample_relation', 'public.dmv', false)")
            c.execute("SELECT set_config('pg_extstats.frozen_totalrows', %s, false)", (str(SOURCE_ROWS),))
            c.execute("ANALYZE public.dmv")
            stats = ordinary_summary(c)
            physical = evaluate_connection(c, workload)
            ext_count = int(c.execute("SELECT count(*) FROM pg_statistic_ext WHERE stxrelid='public.dmv'::regclass").fetchone()[0])
            hyp = c.execute("SELECT pg_hypothetical_extstats_active()").fetchone()[0]
            if ext_count != 0 or hyp not in (None, [], ""):
                raise RuntimeError("extstats contamination detected during ordinary-only sweep")
            result = {"statistics_target": target, "realization_id": realization, "canonical": realization == POLICY.canonical_realization_id, "sample_rows": len(sample_rows), "source_population_rows": SOURCE_ROWS, "native_analyze_equivalent": False, "snapshot_identity": cell["snapshot_identity"], "sample_semantic_digest": cell["semantic_sha256"], "ordinary_statistics_digest": stats["digest"], "ordinary_statistics": stats, "estimate_vector_digest": physical["estimate_vector_digest"], "q_error_vector_digest": physical["q_error_vector_digest"], "objective": physical["objective"], "mean_qerror": physical["mean_qerror"], "aggregate_objective": physical["aggregate_objective"], "extstats_count": ext_count, "hypothetical_active": hyp in (None, [], "")}
            results.append(result)
            c.execute("RESET pg_extstats.frozen_sample_mode")
            c.execute("RESET pg_extstats.frozen_sample_relation")
            c.execute("RESET pg_extstats.frozen_totalrows")
            c.commit()
        by_target = {}
        for target in POLICY.allowed_targets:
            target_results = [item for item in results if item["statistics_target"] == target]
            summary = summarize_objectives([float(item["objective"]) for item in target_results])
            by_target[str(target)] = {"target": target, **summary, "realizations": target_results}
        marginal = {}
        for left, right in zip(POLICY.allowed_targets, POLICY.allowed_targets[1:], strict=False):
            marginal[f"{left}->{right}"] = marginal_metrics(by_target[str(left)], by_target[str(right)], right / left)
        exact = []
        for cell in acquisition["cells"]:
            if cell["realization_id"] != POLICY.canonical_realization_id:
                continue
            replay = fresh_replay(cell, workload)
            expected = next(item for item in results if item["statistics_target"] == replay["statistics_target"] and item["realization_id"] == replay["realization_id"])
            replay["ordinary_statistics_exact"] = replay["ordinary_statistics_digest"] == expected["ordinary_statistics_digest"]
            replay["estimate_vector_exact"] = replay["estimate_vector_digest"] == expected["estimate_vector_digest"]
            replay["q_error_vector_exact"] = replay["q_error_vector_digest"] == expected["q_error_vector_digest"]
            replay["objective_exact"] = replay["objective"] == expected["objective"]
            replay["all_exact"] = all(replay[key] for key in ("ordinary_statistics_exact", "estimate_vector_exact", "q_error_vector_exact", "objective_exact"))
            exact.append(replay)
        write_json(OUT / "exact-replay.json", {"fresh_backend": True, "canonical_realization": POLICY.canonical_realization_id, "cells": exact, "all_exact": all(item["all_exact"] for item in exact)})
        out = {"milestone": "M2.27a", "status": "ordinary_only_complete", "bundle_verification": verification, "target_results": results, "target_summary": by_target, "adjacent_marginal_metrics": marginal, "selected_target": None, "experimental_only": True, "target_selection_fidelity_qualified": False, "search_started": False, "extstats_search_started": False, "extstats_contamination": False, "fixed_positive_truth_workload_count": len(workload), "stddev_definition": "sample"}
        write_json(OUT / "target-sweep.json", out)
        write_json(OUT / "marginal-metrics.json", {"adjacent": marginal, "stddev_definition": "sample", "zero_denominator_policy": "null/status, never NaN"})
        (OUT / "report.md").write_text("# M2.27a DMV reservoir target characterization\n\nBundle v2 captures targets 100, 300, and 1000 with independent deterministic A/B/C reservoirs from one exported repeatable-read snapshot. The sweep reconstructs ordinary statistics only; no extstats objects or search are involved. `native_analyze_equivalent=false` and no production target recommendation is made. M2.27b native-vs-reservoir fidelity remains deferred.\n")
        print(json.dumps({"status": out["status"], "cells": len(results), "targets": list(by_target)}, indent=2))
    finally:
        c.close()


def main() -> int:
    parser = argparse.ArgumentParser()
    parser.add_argument("command", choices=("capture", "sweep"))
    args = parser.parse_args()
    if args.command == "capture": capture()
    else: sweep()
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
