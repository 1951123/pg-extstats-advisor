"""Read-only post-deployment and rollback verification for recommendations."""

from __future__ import annotations

import json
import os
import tempfile
from datetime import UTC, datetime
from pathlib import Path
from typing import Any

import psycopg

from pg_extstats_advisor.deploy.bundle import RecommendationBundle
from pg_extstats_advisor.deploy.preflight import (
    SUPPORTED_POSTGRES_VERSION,
    _check,
    _permission_query,
    _read_extstats,
    _schema_columns,
    _schema_match,
    _text,
    _validate_recommendation,
    _version,
)
from pg_extstats_advisor.errors import AdvisorCLIError, ExitCode

VERIFICATION_REPORT_VERSION = 1
ADVISOR_STATISTICS_PREFIX = "pgextadv_"


def _connect(dsn: str) -> psycopg.Connection[Any]:
    try:
        return psycopg.connect(dsn)
    except Exception as error:
        raise AdvisorCLIError(
            f"cannot connect to production for read-only verification: {error}",
            ExitCode.PERMISSION,
        ) from error


def _relation(connection: psycopg.Connection[Any], relation_id: str) -> tuple[int, str] | None:
    return connection.execute(
        "SELECT c.oid,c.relkind FROM pg_class c JOIN pg_namespace n ON n.oid=c.relnamespace "
        "WHERE n.nspname=%s AND c.relname=%s",
        tuple(relation_id.split(".", 1)),
    ).fetchone()


def _data_rows(connection: psycopg.Connection[Any], statistics_oid: int) -> list[dict[str, Any]]:
    rows = connection.execute(
        "SELECT d.stxdinherit,d.stxdmcv IS NOT NULL,d.stxddependencies IS NOT NULL "
        "FROM pg_statistic_ext_data d WHERE d.stxoid=%s ORDER BY d.stxdinherit",
        (statistics_oid,),
    ).fetchall()
    return [
        {
            "inherit": bool(row[0]),
            "row_present": True,
            "mcv_payload_present": bool(row[1]),
            "dependencies_payload_present": bool(row[2]),
        }
        for row in rows
    ]


def _requested_kind(item: dict[str, Any]) -> str:
    mechanism = str(item.get("mechanism", ""))
    if mechanism == "mcv":
        return "m"
    if mechanism == "fd":
        return "f"
    return "?"


def _expected_attnums(item: dict[str, Any], observed_columns: list[dict[str, Any]]) -> tuple[int, ...]:
    by_name = {str(column["name"]): int(column["attnum"]) for column in observed_columns}
    return tuple(sorted(by_name.get(str(name), -1) for name in item.get("attributes", [])))


def write_verification_report(path: Path, report: dict[str, Any]) -> None:
    path = Path(path)
    path.parent.mkdir(parents=True, exist_ok=True)
    temporary = Path(tempfile.mkdtemp(prefix=f".{path.name}.", dir=path.parent))
    try:
        (temporary / path.name).write_text(json.dumps(report, sort_keys=True, indent=2) + "\n")
        os.replace(temporary / path.name, path)
    finally:
        if temporary.exists():
            temporary.rmdir()


def _base_report(
    report_type: str,
    bundle: RecommendationBundle,
    observed_version: str,
    observed_target: int | None,
    relation_id: str,
    checks: list[dict[str, Any]],
    objects: list[dict[str, Any]],
    warnings: list[str],
    failures: list[str],
    *,
    info: dict[str, Any] | None = None,
) -> dict[str, Any]:
    status = "PASS" if not failures else "FAIL"
    return {
        "report_type": report_type,
        "report_version": VERIFICATION_REPORT_VERSION,
        "verification_timestamp": datetime.now(UTC).isoformat(),
        "status": status,
        "recommendation_digest": bundle.digest,
        "capture_bundle_digest": bundle.capture_bundle_digest,
        "expected_pg_version": SUPPORTED_POSTGRES_VERSION,
        "observed_pg_version": observed_version,
        "expected_target": bundle.evaluated_statistics_target,
        "observed_target": observed_target,
        "relation": relation_id,
        "relation_schema_match": next(
            (item["status"] == "PASS" for item in checks if item["name"] == "schema"),
            False,
        ),
        "selected_object_count": len(bundle.selected_objects),
        "definitions_found": sum(1 for item in objects if item["definition_found"]),
        "definitions_missing": sum(1 for item in objects if not item["definition_found"]),
        "definitions_mismatched": sum(
            1 for item in objects if item["definition_found"] and not item["definition_match"]
        ),
        "data_materialized_count": sum(1 for item in objects if item["data_materialized"]),
        "data_missing_count": sum(1 for item in objects if not item["data_materialized"]),
        "checks": checks,
        "objects": sorted(objects, key=lambda item: item["expected_name"]),
        "warnings": sorted(set(warnings)),
        "failures": sorted(set(failures)),
        "informational": info or {},
        "read_only": True,
    }


def _verify_object(
    item: dict[str, Any],
    existing: dict[str, Any] | None,
    data: list[dict[str, Any]],
    relation_oid: int,
    observed_columns: list[dict[str, Any]],
    expected_target: int,
) -> dict[str, Any]:
    expected_name = str(item.get("statistics_name", ""))
    expected_kind = _requested_kind(item)
    expected_attributes = [str(value) for value in item.get("attributes", [])]
    expected_keys = _expected_attnums(item, observed_columns)
    definition_found = existing is not None
    definition_match = bool(
        existing
        and existing["relation_oid"] == relation_oid
        and expected_kind in set(existing["kinds"])
        and tuple(existing["keys"]) == expected_keys
    )
    target_compatible = bool(existing and existing["target"] in (-1, expected_target))
    data_row = next((row for row in data if not row["inherit"]), data[0] if data else None)
    data_materialized = bool(definition_match and data_row is not None)
    payload_key = "mcv_payload_present" if expected_kind == "m" else "dependencies_payload_present"
    payload_present = bool(data_row and data_row.get(payload_key))
    if not definition_found:
        status = "DEFINITION_MISSING"
    elif not definition_match or not target_compatible:
        status = "DEFINITION_MISMATCH"
    elif data_row is None:
        status = "DEFINITION_PRESENT_DATA_ABSENT"
    else:
        status = "DEFINITION_PRESENT_DATA_PRESENT"
    return {
        "candidate_id": str(item.get("candidate_id", "")),
        "expected_name": expected_name,
        "relation": str(item.get("relation", "")),
        "columns": expected_attributes,
        "kind": str(item.get("mechanism", "")),
        "definition_found": definition_found,
        "definition_match": definition_match,
        "data_materialized": data_materialized,
        "data_row_present": data_row is not None,
        "payload_present": payload_present,
        "payload_status": "PRESENT" if payload_present else ("ABSENT_NATIVE" if data_row else "NO_DATA_ROW"),
        "target_compatible": target_compatible,
        "observed_target": existing["target"] if existing else None,
        "status": status,
    }


def _run_connection_checks(
    connection: psycopg.Connection[Any], bundle: RecommendationBundle, *, full_schema: bool
) -> tuple[list[dict[str, Any]], int | None, str, int | None, list[dict[str, Any]], list[dict[str, Any]], list[str]]:
    relation_id = str(bundle.schema_binding["relation_id"])
    checks: list[dict[str, Any]] = []
    failures: list[str] = []
    observed_version = _version(connection.execute("SHOW server_version").fetchone()[0])
    checks.append(_check("postgres_version", "PASS" if observed_version == SUPPORTED_POSTGRES_VERSION else "FAIL", SUPPORTED_POSTGRES_VERSION, observed_version))
    if observed_version != SUPPORTED_POSTGRES_VERSION:
        failures.append("postgres_version")
    observed_target = int(connection.execute("SHOW default_statistics_target").fetchone()[0])
    checks.append(_check("statistics_target", "PASS" if observed_target == bundle.evaluated_statistics_target else "FAIL", bundle.evaluated_statistics_target, observed_target))
    if observed_target != bundle.evaluated_statistics_target:
        failures.append("statistics_target")
    permission_check, permissions_ok = _permission_query(connection, relation_id)
    checks.append(permission_check)
    if not permissions_ok:
        failures.append("permissions")
    relation = _relation(connection, relation_id)
    if relation is None:
        checks.append(_check("relation", "FAIL", relation_id, None, "relation does not exist"))
        failures.append("relation")
        return checks, None, observed_version, observed_target, [], failures, [
            "relation does not exist; no selected definitions can be verified"
        ]
    relation_oid, relkind = int(relation[0]), _text(relation[1])
    checks.append(_check("relation", "PASS", relation_id, relation_id, f"relkind={relkind}"))
    observed_columns = _schema_columns(connection, relation_oid)
    if full_schema:
        schema_ok, observed_schema = _schema_match(bundle.schema_binding, observed_columns, relkind, relation_id)
        checks.append(_check("schema", "PASS" if schema_ok else "FAIL", bundle.schema_binding, observed_schema))
        if not schema_ok:
            failures.append("schema")
    else:
        checks.append(_check("schema", "PASS", relation_id, relation_id, "relation identity retained; full capture schema is not required for rollback"))
    bad_column_targets = [column["name"] for column in observed_columns if column["attstattarget"] != -1]
    checks.append(_check("column_target_overrides", "PASS" if not bad_column_targets else "FAIL", [], bad_column_targets, "inherited/default target semantics"))
    if bad_column_targets:
        failures.append("column_target_overrides")
    return checks, relation_oid, observed_version, observed_target, observed_columns, failures, []


def verify_deployment(recommendation_path: Path, production_dsn: str) -> dict[str, Any]:
    """Verify deployed definitions and ANALYZE materialization with read-only SQL."""

    bundle = _validate_recommendation(recommendation_path)
    relation_id = str(bundle.schema_binding["relation_id"])
    checks: list[dict[str, Any]] = [_check("recommendation_integrity", "PASS", message="validated before production connection")]
    connection = _connect(production_dsn)
    failures: list[str] = []
    warnings: list[str] = ["production data may have changed; row/value drift is not a verification failure"]
    try:
        connection.execute("BEGIN READ ONLY")
        checks, relation_oid, observed_version, observed_target, observed_columns, base_failures, base_warnings = _run_connection_checks(connection, bundle, full_schema=True)
        checks.insert(0, _check("recommendation_integrity", "PASS", message="validated before production connection"))
        failures.extend(base_failures)
        warnings.extend(base_warnings)
        objects: list[dict[str, Any]] = []
        extstats = _read_extstats(connection, relation_oid) if relation_oid is not None else []
        by_name = {item["name"]: item for item in extstats}
        expected_names = {str(item.get("statistics_name")) for item in bundle.selected_objects}
        unexpected = sorted(
            item["name"] for item in extstats
            if item["name"].startswith(ADVISOR_STATISTICS_PREFIX) and item["name"] not in expected_names
        )
        checks.append(_check("unexpected_advisor_owned_objects", "PASS" if not unexpected else "FAIL", [], unexpected, "advisor namespace objects outside this recommendation"))
        if unexpected:
            failures.append("unexpected_advisor_owned_objects")
        attnums = {str(column["name"]): int(column["attnum"]) for column in observed_columns}
        equivalent: list[dict[str, str]] = []
        for selected in bundle.selected_objects:
            selected_name = str(selected.get("statistics_name"))
            expected_kind = _requested_kind(selected)
            expected_keys = {attnums.get(str(value), -1) for value in selected.get("attributes", [])}
            if -1 in expected_keys:
                continue
            for existing in extstats:
                if existing["name"] in expected_names or existing["name"] in {item["existing"] for item in equivalent}:
                    continue
                if expected_kind in set(existing["kinds"]) and set(existing["keys"]) == expected_keys:
                    equivalent.append({"recommendation": selected_name, "existing": existing["name"]})
        checks.append(_check("equivalent_extstats", "WARN" if equivalent else "PASS", [], equivalent, "post-deployment equivalents are informational"))
        if equivalent:
            warnings.append("equivalent extstats appeared under another name after deployment")
        for selected in bundle.selected_objects:
            existing = by_name.get(str(selected.get("statistics_name")))
            data = _data_rows(connection, existing["oid"]) if existing else []
            detail = _verify_object(selected, existing, data, relation_oid or 0, observed_columns, bundle.evaluated_statistics_target)
            objects.append(detail)
            if detail["status"] == "DEFINITION_MISSING":
                failures.append(f"missing:{detail['expected_name']}")
            elif detail["status"] == "DEFINITION_MISMATCH":
                failures.append(f"mismatch:{detail['expected_name']}")
            elif not detail["data_materialized"]:
                failures.append(f"data_missing:{detail['expected_name']}")
            if detail["data_row_present"] and not detail["payload_present"]:
                warnings.append(f"{detail['expected_name']}: requested {detail['kind']} payload is NULL under native PG16.14 semantics")
        checks.append(_check("statistics_definitions", "PASS" if not any(item["status"] in {"DEFINITION_MISSING", "DEFINITION_MISMATCH"} for item in objects) else "FAIL", len(objects), sum(int(item["definition_match"]) for item in objects)))
        checks.append(_check("materialized_data", "PASS" if all(item["data_materialized"] for item in objects) else "FAIL", len(objects), sum(int(item["data_materialized"]) for item in objects), "data row in pg_statistic_ext_data; payload NULL is reported separately"))
        if any(not item["data_materialized"] for item in objects):
            failures.append("materialized_data")
        reltuples = connection.execute("SELECT reltuples FROM pg_class WHERE oid=%s", (relation_oid,)).fetchone()[0] if relation_oid is not None else None
        connection.rollback()
    except psycopg.Error as error:
        try:
            connection.rollback()
        finally:
            connection.close()
        code = ExitCode.PERMISSION if getattr(error, "sqlstate", None) == "42501" else ExitCode.EXECUTION
        raise AdvisorCLIError(f"read-only deployment verification failed: {error}", code) from error
    finally:
        connection.close()
    report = _base_report("deployment-verification", bundle, observed_version, observed_target, relation_id, checks, objects, warnings, failures, info={"reltuples": float(reltuples) if reltuples is not None else None})
    report["definition_with_no_analyze_guidance"] = "statistics definition exists but no materialized statistics data found; run ANALYZE on the relation and verify again"
    return {**report, "_exit_code": int(ExitCode.SUCCESS if report["status"] == "PASS" else (ExitCode.PERMISSION if "permissions" in failures else ExitCode.COMPATIBILITY))}


def verify_rollback(recommendation_path: Path, production_dsn: str) -> dict[str, Any]:
    """Verify selected advisor-owned definitions are absent, without mutation."""

    bundle = _validate_recommendation(recommendation_path)
    relation_id = str(bundle.schema_binding["relation_id"])
    checks: list[dict[str, Any]] = [_check("recommendation_integrity", "PASS", message="validated before production connection")]
    connection = _connect(production_dsn)
    failures: list[str] = []
    warnings: list[str] = ["rollback does not restore a prior ANALYZE sample; it only removes this recommendation's definitions"]
    try:
        connection.execute("BEGIN READ ONLY")
        checks, relation_oid, observed_version, observed_target, _observed_columns, base_failures, base_warnings = _run_connection_checks(connection, bundle, full_schema=False)
        checks.insert(0, _check("recommendation_integrity", "PASS", message="validated before production connection"))
        failures.extend(base_failures)
        warnings.extend(base_warnings)
        extstats = _read_extstats(connection, relation_oid) if relation_oid is not None else []
        by_name = {item["name"]: item for item in extstats}
        objects = []
        for selected in bundle.selected_objects:
            name = str(selected.get("statistics_name"))
            present = name in by_name
            detail = {
                "candidate_id": str(selected.get("candidate_id", "")),
                "expected_name": name,
                "relation": str(selected.get("relation", relation_id)),
                "columns": [str(value) for value in selected.get("attributes", [])],
                "kind": str(selected.get("mechanism", "")),
                "definition_found": present,
                "definition_match": not present,
                "data_materialized": False,
                "data_row_present": False,
                "payload_present": False,
                "payload_status": "ABSENT_AFTER_ROLLBACK",
                "target_compatible": True,
                "observed_target": by_name[name]["target"] if present else None,
                "status": "REMAINING_OBJECT" if present else "ABSENT",
            }
            objects.append(detail)
            if present:
                failures.append(f"remaining:{name}")
        checks.append(_check("selected_definitions_absent", "PASS" if not failures or not any(item["status"] == "REMAINING_OBJECT" for item in objects) else "FAIL", 0, sum(int(item["status"] == "REMAINING_OBJECT") for item in objects)))
        connection.rollback()
    except psycopg.Error as error:
        try:
            connection.rollback()
        finally:
            connection.close()
        code = ExitCode.PERMISSION if getattr(error, "sqlstate", None) == "42501" else ExitCode.EXECUTION
        raise AdvisorCLIError(f"read-only rollback verification failed: {error}", code) from error
    finally:
        connection.close()
    report = _base_report("rollback-verification", bundle, observed_version, observed_target, relation_id, checks, objects, warnings, failures)
    report["remaining_object_count"] = sum(int(item["status"] == "REMAINING_OBJECT") for item in objects)
    return {**report, "_exit_code": int(ExitCode.SUCCESS if report["status"] == "PASS" else (ExitCode.PERMISSION if "permissions" in failures else ExitCode.COMPATIBILITY))}


__all__ = [
    "VERIFICATION_REPORT_VERSION",
    "verify_deployment",
    "verify_rollback",
    "write_verification_report",
]
