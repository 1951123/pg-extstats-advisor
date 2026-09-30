"""Dataset-agnostic fixed-T production capture API.

This is deliberately a small one-relation product boundary.  It owns the
transaction and permission checks so callers cannot accidentally turn capture
into an ANALYZE or write operation.
"""

from __future__ import annotations

import hashlib
import json
import os
import random
import tempfile
from dataclasses import dataclass
from datetime import UTC, datetime
from pathlib import Path
from typing import Any

import psycopg

from pg_extstats_advisor.capture.bundle import canonical_digest, encode_sample
from pg_extstats_advisor.capture.fixed import FIXED_CAPTURE_MODE, verify_fixed_t_bundle
from pg_extstats_advisor.deploy.sql import qualified_relation_name
from pg_extstats_advisor.errors import AdvisorCLIError, ExitCode
from pg_extstats_advisor.statistics import validate_global_statistics_target


def _text(value: Any) -> str:
    """Normalize text-like catalog values from text or binary psycopg modes."""
    return value.decode() if isinstance(value, (bytes, bytearray)) else str(value)


@dataclass(frozen=True, slots=True)
class CaptureConfig:
    dsn: str
    relation: str
    workload_path: Path
    output_path: Path
    statistics_target: int = 100
    sample_rows: int = 30_000
    source_sha256: str | None = None

    def __post_init__(self) -> None:
        validate_global_statistics_target(self.statistics_target)
        if self.sample_rows <= 0:
            raise ValueError("sample_rows must be positive")


def _write(path: Path, value: Any) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text(json.dumps(value, sort_keys=True, indent=2, ensure_ascii=False) + "\n")


def _records(path: Path, relation_id: str) -> list[dict[str, Any]]:
    try:
        raw = json.loads(path.read_text())
    except (OSError, json.JSONDecodeError) as error:
        raise AdvisorCLIError(f"cannot read workload input: {path}", ExitCode.USAGE) from error
    rows = raw.get("queries") or raw.get("effective_queries")
    if not isinstance(rows, list) or not rows:
        raise AdvisorCLIError("workload input must contain a non-empty queries list", ExitCode.USAGE)
    result = []
    seen: set[str] = set()
    for item in rows:
        query_id = str(item.get("query_id", ""))
        if not query_id or query_id in seen:
            raise AdvisorCLIError("workload query IDs must be unique", ExitCode.USAGE)
        seen.add(query_id)
        target = str(item.get("target_relation_id", item.get("target_relation", relation_id)))
        if target.split(".")[-1] != relation_id.split(".")[-1]:
            raise AdvisorCLIError("multi-relation workload is outside the supported scope", ExitCode.COMPATIBILITY)
        result.append({
            "query_id": query_id,
            "sql": str(item["sql"]),
            "truth": float(item.get("truth", 0)),
            "target_relation_id": relation_id,
            "effective": bool(item.get("effective", float(item.get("truth", 0)) > 0)),
        })
    return result


def _relation_schema(connection: psycopg.Connection[Any], relation: str, target: int) -> tuple[dict[str, Any], int]:
    row = connection.execute(
        "SELECT n.nspname,c.relname,c.oid,c.relkind FROM pg_class c JOIN pg_namespace n "
        "ON n.oid=c.relnamespace WHERE c.oid=to_regclass(%s) AND c.relkind IN ('r','p')",
        (relation,),
    ).fetchone()
    if row is None:
        raise AdvisorCLIError(f"relation does not exist: {relation}", ExitCode.USAGE)
    columns = connection.execute(
        "SELECT a.attnum,a.attname,a.atttypid::regtype::text,a.attnotnull "
        "FROM pg_attribute a WHERE a.attrelid=%s AND a.attnum>0 AND NOT a.attisdropped ORDER BY a.attnum",
        (row[2],),
    ).fetchall()
    if not columns or any(_text(item[2]) != "text" for item in columns):
        raise AdvisorCLIError("current product scope supports one relation of text columns", ExitCode.COMPATIBILITY)
    portable_columns = [
        {
            "attnum": int(attnum), "name": _text(name), "nullable": not bool(notnull), "typmod": "-1",
            "type": {"kind": "builtin", "name": "text", "namespace": "pg_catalog", "portable_name": "pg_catalog.text"},
            "collation": {"name": "default", "namespace": "pg_catalog", "portable_name": "pg_catalog.default"},
            "statistics": {"target": target, "source_summary_present": False},
        }
        for attnum, name, _typ, notnull in columns
    ]
    relation_record: dict[str, Any] = {
        "relation_id": f"{_text(row[0])}.{_text(row[1])}",
        "relation_kind": _text(row[3]),
        "columns": portable_columns,
    }
    relation_record["schema_digest"] = canonical_digest(
        {key: value for key, value in relation_record.items() if key != "schema_digest"}
    )
    return relation_record, int(row[2])


def _check_permissions(connection: psycopg.Connection[Any], relation: str) -> dict[str, Any]:
    schema, name = relation.split(".", 1) if "." in relation else ("public", relation)
    row = connection.execute(
        "SELECT current_user,rolsuper,has_schema_privilege(current_user,%s,'USAGE'),"
        "has_table_privilege(current_user,%s,'SELECT') FROM pg_roles WHERE rolname=current_user",
        (schema, f"{schema}.{name}"),
    ).fetchone()
    if row is None or bool(row[1]):
        raise AdvisorCLIError("capture requires a non-superuser role", ExitCode.PERMISSION)
    if not bool(row[2]):
        raise AdvisorCLIError(f"capture role lacks USAGE on schema {schema}", ExitCode.PERMISSION)
    if not bool(row[3]):
        raise AdvisorCLIError(f"capture role lacks SELECT on relation {schema}.{name}", ExitCode.PERMISSION)
    return {"role": _text(row[0]), "superuser": False, "schema_usage": True, "relation_select": True, "write_privilege_required": False, "create_privilege_required": False, "analyze_privilege_required": False}


def capture_fixed_t(config: CaptureConfig) -> dict[str, Any]:
    target = validate_global_statistics_target(config.statistics_target)
    relation = config.relation if "." in config.relation else f"public.{config.relation}"
    records = _records(config.workload_path, relation)
    if config.output_path.exists():
        raise AdvisorCLIError(f"output already exists: {config.output_path}", ExitCode.USAGE)
    config.output_path.parent.mkdir(parents=True, exist_ok=True)
    temporary = Path(tempfile.mkdtemp(prefix=f".{config.output_path.name}.", dir=config.output_path.parent))
    try:
        with psycopg.connect(config.dsn) as connection:
            connection.execute("BEGIN ISOLATION LEVEL REPEATABLE READ READ ONLY")
            snapshot = _text(connection.execute("SELECT pg_export_snapshot()").fetchone()[0])
            tx_snapshot = _text(connection.execute("SELECT txid_current_snapshot()").fetchone()[0])
            permissions = _check_permissions(connection, relation)
            relation_record, relation_oid = _relation_schema(connection, relation, target)
            effective_target = int(connection.execute("SHOW default_statistics_target").fetchone()[0])
            if effective_target != target:
                raise AdvisorCLIError(f"expected default_statistics_target={target}, observed {effective_target}", ExitCode.COMPATIBILITY)
            population_rows = int(connection.execute(f"SELECT count(*) FROM {qualified_relation_name(relation)}").fetchone()[0])
            att_overrides = connection.execute("SELECT attname,attstattarget FROM pg_attribute WHERE attrelid=%s AND attnum>0 AND NOT attisdropped ORDER BY attnum", (relation_oid,)).fetchall()
            ext_overrides = connection.execute("SELECT stxname,stxstattarget FROM pg_statistic_ext WHERE stxrelid=%s ORDER BY stxname", (relation_oid,)).fetchall()
            if any(int(value) != -1 for _, value in att_overrides) or any(int(value) != -1 for _, value in ext_overrides):
                raise AdvisorCLIError("explicit statistics target override conflicts with fixed-T capture", ExitCode.COMPATIBILITY)
            rng = random.Random(22929)
            rows: list[list[str | None]] = []
            seen = 0
            with connection.cursor().copy(f"COPY (SELECT * FROM {relation}) TO STDOUT WITH (FORMAT text, NULL '\\N')") as copy:
                for parsed in copy.rows():
                    seen += 1
                    row = [None if value is None else _text(value) for value in parsed]
                    if len(rows) < config.sample_rows:
                        rows.append(row)
                    else:
                        slot = rng.randrange(seen)
                        if slot < config.sample_rows:
                            rows[slot] = row
            rows.sort(key=lambda value: tuple("" if item is None else item for item in value))
            truth_rows = []
            for item in records:
                observed = int(connection.execute(item["sql"]).fetchone()[0])
                truth_rows.append({"query_id": item["query_id"], "sql": item["sql"], "truth": observed})
            schema = {"relations": [relation_record]}
            observed_version = _text(connection.execute("SHOW server_version").fetchone()[0]).split()[0]
            environment = {
                "schema_version": 1, "postgres_version": observed_version,
                "server_version_num": _text(connection.execute("SHOW server_version_num").fetchone()[0]),
                "fields": {"postgres_version": {"classification": "required", "value": "16.14"}, "server_version_num": {"classification": "required", "value": _text(connection.execute("SHOW server_version_num").fetchone()[0])}, "default_statistics_target": {"classification": "required", "value": target}, "database_collation": {"classification": "required", "value": _text(connection.execute("SELECT datcollate FROM pg_database WHERE datname=current_database()").fetchone()[0])}, "workload_analysis_version": {"classification": "required", "value": "pg16-mvp-v2"}},
                "relation_row_count": population_rows, "permissions": permissions,
            }
            workload = {"schema_version": 1, "queries": records, "raw_query_count": len(records), "effective_query_count": sum(int(item["effective"]) for item in records), "raw_sql_sha256": config.source_sha256 or hashlib.sha256(config.workload_path.read_bytes()).hexdigest(), "effective_workload_digest": canonical_digest({"objective_membership_policy": "positive_truth_only", "queries": [{"query_id": item["query_id"], "sql": item["sql"], "truth": item["truth"], "target_relation": relation.split(".")[-1]} for item in records if item["effective"]]})}
            truth = {"mode": "exact_full_data_count", "query_count": len(truth_rows), "queries": truth_rows}
            truth["semantic_digest"] = canonical_digest(truth_rows)
            rel_dir = temporary / "acquisition/relations" / relation
            rel_dir.mkdir(parents=True)
            binary = encode_sample(rows)
            (rel_dir / "sample.copy.bin").write_bytes(binary)
            sample_manifest = {"relation_id": relation, "sample_method_id": "deterministic_reservoir_v1", "sample_method_version": 1, "serialization": "pgextstats_m223_length_prefixed_v1", "serialization_version": 1, "sample_row_count": len(rows), "source_population_rows": population_rows, "selected_columns": relation_record["columns"], "binary_sha256": hashlib.sha256(binary).hexdigest(), "semantic_digest": canonical_digest({"relation_id": relation, "columns": relation_record["columns"], "rows": rows}), "snapshot_binding": snapshot, "authoritative_for_bundle": True, "native_analyze_equivalent": False, "method_parameters": {"seed": 22929}}
            _write(rel_dir / "manifest.json", sample_manifest)
            _write(temporary / "environment.json", environment); _write(temporary / "schema.json", schema); _write(temporary / "workload.json", workload); _write(temporary / "truth.json", truth)
            components = {"environment.json": canonical_digest(environment), "schema.json": canonical_digest(schema), "workload.json": canonical_digest(workload), "truth.json": canonical_digest(truth), f"acquisition/{relation}/manifest.json": canonical_digest(sample_manifest)}
            bundle = {"bundle_schema_version": "production-capture-bundle-v1", "profile": "fixed_t_single_snapshot", "sealed": True, "production_identity": {"postgres_version": environment["postgres_version"], "server_version_num": environment["server_version_num"], "source_relation_ids": [relation], "source_kind": "stock_postgresql"}, "snapshot_consistency": {"mode": "strong_single_snapshot", "components": {"environment": snapshot, "schema": snapshot, "workload": snapshot, "truth": snapshot, "acquisition": snapshot}, "transaction_snapshot": tx_snapshot}, "components": components, "relation_inventory": [{"relation_id": relation, "schema_digest": relation_record["schema_digest"], "source_population_rows": str(population_rows)}], "workload_identity": {"effective_workload_digest": workload["effective_workload_digest"], "effective_query_count": workload["effective_query_count"]}, "truth_identity": {"mode": "exact_full_data_count", "query_count": len(truth_rows), "semantic_digest": truth["semantic_digest"]}, "acquisition_identity": {"relation_ids": [relation], "sample_method_id": "deterministic_reservoir_v1", "native_analyze_equivalent": False, "global_statistics_target": target, "canonical_realization": True}, "compatibility": {"required_postgres_version": "16.14", "postgres_version_policy": "exact", "supported_types": ["pg_catalog.text"], "supported_sample_method": ["deterministic_reservoir_v1", 1], "supported_serialization": ["pgextstats_m223_length_prefixed_v1", 1], "workload_analysis_version": "pg16-mvp-v2", "ce_target_scope": "single_relation_base_count", "joins_supported": False, "target_override_policy": "fail_closed_relevant_overrides"}, "capture_mode": FIXED_CAPTURE_MODE, "created_timestamp": datetime.now(UTC).isoformat(), "sensitivity": {"anonymized": False, "contains_exact_truth": True, "contains_full_base_table": False, "contains_sampled_row_values": True, "encrypted": False}, "target_override_evidence": {"status": "passed", "relevant_override_count": 0, "ordinary_columns": [[_text(name), int(value)] for name, value in att_overrides], "existing_extstats": [[_text(name), int(value)] for name, value in ext_overrides]}, "read_only": True}
            bundle["semantic_binding"] = dict(bundle); bundle["semantic_digest"] = canonical_digest(bundle["semantic_binding"])
            _write(temporary / "bundle.json", bundle)
            verify_fixed_t_bundle(temporary, expected_target=target, require_supported_profile=True)
            # The bundle may be consumed by the separate non-root advisor
            # role through a shared bind mount.  Preserve private ownership
            # while granting the dedicated runtime group traversal/read access.
            for entry in temporary.rglob("*"):
                current_mode = entry.stat().st_mode & 0o777
                os.chmod(entry, current_mode | (0o070 if entry.is_file() else 0o070))
            os.chmod(temporary, 0o770)
        os.replace(temporary, config.output_path)
        return {"artifact_type": "capture", "profile": "fixed_t_single_snapshot", "semantic_digest": bundle["semantic_digest"], "target": target, "relation": relation, "query_count": len(records), "sample_rows": config.sample_rows, "source_rows": population_rows}
    except AdvisorCLIError:
        raise
    except Exception as error:
        raise AdvisorCLIError(f"capture failed: {error}", ExitCode.EXECUTION) from error
    finally:
        if temporary.exists():
            import shutil
            shutil.rmtree(temporary, ignore_errors=True)


__all__ = ["CaptureConfig", "capture_fixed_t"]
