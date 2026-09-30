"""Read-only, fail-closed deployment compatibility preflight."""

from __future__ import annotations

import json
import re
from pathlib import Path
from typing import Any

import psycopg

from pg_extstats_advisor.capture.manifest import canonical_digest
from pg_extstats_advisor.deploy.bundle import RecommendationBundle, validate_recommendation_bundle
from pg_extstats_advisor.errors import AdvisorCLIError, ExitCode

SUPPORTED_POSTGRES_VERSION = "16.14"
PREFLIGHT_SCHEMA_VERSION = 1


def _check(name: str, status: str, expected: Any = None, observed: Any = None, message: str = "") -> dict[str, Any]:
    value: dict[str, Any] = {"name": name, "status": status}
    if expected is not None:
        value["expected"] = expected
    if observed is not None:
        value["observed"] = observed
    if message:
        value["message"] = message
    return value


def _version(value: str) -> str:
    return str(value).split()[0]


def _portable_type(type_name: str) -> dict[str, str]:
    text = str(type_name).strip('"')
    if "." not in text:
        text = f"pg_catalog.{text}"
    namespace, name = text.rsplit(".", 1)
    return {"kind": "builtin", "name": name, "namespace": namespace, "portable_name": text}


def _portable_collation(namespace: str | None, name: str | None) -> dict[str, str]:
    if not name or name == "default":
        return {"name": "default", "namespace": "pg_catalog", "portable_name": "pg_catalog.default"}
    namespace = namespace or "pg_catalog"
    return {"name": str(name), "namespace": str(namespace), "portable_name": f"{namespace}.{name}"}


def _parse_keys(value: Any) -> tuple[int, ...]:
    if value is None:
        return ()
    if isinstance(value, (list, tuple)):
        return tuple(int(item) for item in value)
    return tuple(int(item) for item in re.findall(r"\d+", str(value)))


def _parse_kinds(value: Any) -> set[str]:
    if isinstance(value, (list, tuple)):
        return {str(item).strip("{}\" ") for item in value}
    return set(re.findall(r"[a-z]", str(value)))


def _permission_query(connection: psycopg.Connection[Any], relation_id: str) -> tuple[dict[str, Any], bool]:
    schema, _name = relation_id.split(".", 1)
    row = connection.execute(
        "SELECT current_user, rolsuper, "
        "has_schema_privilege(current_user,%s,'USAGE'), "
        "has_table_privilege(current_user,%s,'SELECT') "
        "FROM pg_roles WHERE rolname=current_user",
        (schema, relation_id),
    ).fetchone()
    if row is None:
        return _check("permissions", "FAIL", message="current role is not visible"), False
    observed = {
        "role": str(row[0]),
        "superuser": bool(row[1]),
        "schema_usage": bool(row[2]),
        "relation_select": bool(row[3]),
    }
    valid = not any((observed["superuser"], not observed["schema_usage"], not observed["relation_select"]))
    return _check(
        "permissions",
        "PASS" if valid else "FAIL",
        expected={"superuser": False, "schema_usage": True, "relation_select": True},
        observed=observed,
        message="read-only validation role requirements" if valid else "role lacks required read-only visibility",
    ), valid


def _schema_columns(connection: psycopg.Connection[Any], relation_oid: int) -> list[dict[str, Any]]:
    rows = connection.execute(
        "SELECT a.attnum,a.attname,a.atttypid::regtype::text,a.atttypmod,"
        "coalesce(n.nspname,''),coalesce(c.collname,''),a.attnotnull,a.attstattarget "
        "FROM pg_attribute a LEFT JOIN pg_collation c ON c.oid=a.attcollation "
        "LEFT JOIN pg_namespace n ON n.oid=c.collnamespace "
        "WHERE a.attrelid=%s AND a.attnum>0 AND NOT a.attisdropped ORDER BY a.attnum",
        (relation_oid,),
    ).fetchall()
    return [
        {
            "attnum": int(row[0]),
            "name": str(row[1]),
            "nullable": not bool(row[6]),
            "typmod": str(row[3]),
            "type": _portable_type(str(row[2])),
            "collation": _portable_collation(str(row[4]) or None, str(row[5]) or None),
            "attstattarget": int(row[7]),
        }
        for row in rows
    ]


def _schema_match(expected: dict[str, Any], observed_columns: list[dict[str, Any]], relkind: str, relation_id: str) -> tuple[bool, dict[str, Any]]:
    expected_columns = [
        {key: column[key] for key in ("attnum", "name", "nullable", "typmod", "type", "collation")}
        for column in expected["columns"]
    ]
    observed = [
        {key: column[key] for key in ("attnum", "name", "nullable", "typmod", "type", "collation")}
        for column in observed_columns
    ]
    kind_ok = relkind in set(map(str, expected["relation_kind"]))
    identity = {"relation_id": relation_id, "relation_kind": list(map(str, expected["relation_kind"])), "columns": expected_columns}
    digest_ok = expected.get("schema_digest") == canonical_digest(identity)
    return bool(kind_ok and digest_ok and expected_columns == observed), {
        "relation_id": relation_id,
        "relation_kind": relkind,
        "columns": observed,
        "expected_schema_digest_valid": digest_ok,
    }


def _read_extstats(connection: psycopg.Connection[Any], relation_oid: int) -> list[dict[str, Any]]:
    rows = connection.execute(
        "SELECT e.stxname,e.stxkind::text,e.stxkeys::text,e.stxstattarget "
        "FROM pg_statistic_ext e WHERE e.stxrelid=%s ORDER BY e.stxname",
        (relation_oid,),
    ).fetchall()
    return [
        {"name": str(row[0]), "kinds": sorted(_parse_kinds(row[1])), "keys": list(_parse_keys(row[2])), "target": int(row[3])}
        for row in rows
    ]


def _validate_recommendation(path: Path) -> RecommendationBundle:
    try:
        raw = json.loads(Path(path).read_text())
        validate_recommendation_bundle(path, expected_target=int(raw["evaluated_statistics_target"]), require_product_profile=True)
        bundle = RecommendationBundle.load(path)
    except (OSError, json.JSONDecodeError, KeyError, TypeError, ValueError) as error:
        raise AdvisorCLIError(f"invalid recommendation: {error}", ExitCode.CORRUPT_ARTIFACT) from error
    if bundle.schema_binding is None:
        raise AdvisorCLIError("recommendation is missing capture-time schema binding", ExitCode.CORRUPT_ARTIFACT)
    if not bundle.selected_objects:
        raise AdvisorCLIError("recommendation is missing selected-object metadata", ExitCode.CORRUPT_ARTIFACT)
    if any(str(item.get("relation")) != str(bundle.schema_binding["relation_id"]) for item in bundle.selected_objects):
        raise AdvisorCLIError("selected-object relation does not match schema binding", ExitCode.CORRUPT_ARTIFACT)
    return bundle


def run_preflight(recommendation_path: Path, production_dsn: str) -> dict[str, Any]:
    """Run only SELECT/SHOW statements against the supplied production DSN."""

    bundle = _validate_recommendation(recommendation_path)
    relation_id = str(bundle.schema_binding["relation_id"])
    checks = [_check("recommendation_integrity", "PASS", message="validated before production connection")]
    failures: list[str] = []
    exit_code = int(ExitCode.COMPATIBILITY)
    relation_oid: int | None = None
    observed_columns: list[dict[str, Any]] = []
    observed_extstats: list[dict[str, Any]] = []
    observed_version = ""
    observed_target: int | None = None
    try:
        connection = psycopg.connect(production_dsn)
    except Exception as error:
        raise AdvisorCLIError(f"cannot connect to production for read-only preflight: {error}", ExitCode.PERMISSION) from error
    try:
        connection.execute("BEGIN READ ONLY")
        observed_version = _version(str(connection.execute("SHOW server_version").fetchone()[0]))
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
            exit_code = int(ExitCode.PERMISSION)
        relation = connection.execute(
            "SELECT c.oid,c.relkind FROM pg_class c JOIN pg_namespace n ON n.oid=c.relnamespace "
            "WHERE n.nspname=%s AND c.relname=%s",
            tuple(relation_id.split(".", 1)),
        ).fetchone()
        if relation is None:
            checks.append(_check("relation", "FAIL", relation_id, None, "relation does not exist"))
            failures.append("relation")
        else:
            relation_oid = int(relation[0])
            checks.append(_check("relation", "PASS", relation_id, relation_id, f"relkind={relation[1]}"))
            observed_columns = _schema_columns(connection, relation_oid)
            schema_ok, observed_schema = _schema_match(bundle.schema_binding, observed_columns, str(relation[1]), relation_id)
            checks.append(_check("schema", "PASS" if schema_ok else "FAIL", bundle.schema_binding, observed_schema))
            if not schema_ok:
                failures.append("schema")
            bad_column_targets = [column["name"] for column in observed_columns if column["attstattarget"] != -1]
            checks.append(_check("column_target_overrides", "PASS" if not bad_column_targets else "FAIL", [], bad_column_targets, "inherited/default target semantics"))
            if bad_column_targets:
                failures.append("column_target_overrides")
            observed_extstats = _read_extstats(connection, relation_oid)
            bad_ext_targets = [item["name"] for item in observed_extstats if item["target"] != -1]
            checks.append(_check("extstats_target_overrides", "PASS" if not bad_ext_targets else "FAIL", [], bad_ext_targets, "inherited/default target semantics"))
            if bad_ext_targets:
                failures.append("extstats_target_overrides")
            selected_names = {str(item.get("statistics_name")) for item in bundle.selected_objects}
            collisions = sorted(selected_names.intersection({item["name"] for item in observed_extstats}))
            checks.append(_check("name_collisions", "PASS" if not collisions else "FAIL", [], collisions, "deterministic names are checked again before deployment"))
            if collisions:
                failures.append("name_collisions")
            attnums = {column["name"]: column["attnum"] for column in observed_columns}
            equivalents: list[dict[str, Any]] = []
            for selected in bundle.selected_objects:
                attrs = tuple(attnums.get(str(item), -1) for item in selected.get("attributes", []))
                required_kind = "m" if selected.get("mechanism") == "mcv" else "f"
                if -1 in attrs:
                    continue
                for existing in observed_extstats:
                    if existing["name"] in selected_names:
                        continue
                    if required_kind in existing["kinds"] and set(attrs) == set(existing["keys"]):
                        equivalents.append({"recommendation": selected.get("statistics_name"), "existing": existing["name"]})
            checks.append(_check("equivalent_extstats", "PASS" if not equivalents else "FAIL", [], equivalents, "automatic deduplication is unsupported"))
            if equivalents:
                failures.append("equivalent_extstats")
            reltuples = connection.execute("SELECT reltuples FROM pg_class WHERE oid=%s", (relation_oid,)).fetchone()[0]
            warnings = ["production data may have changed since capture; row/value drift is not a hard failure"]
            info = {"reltuples": float(reltuples) if reltuples is not None else None}
        if relation is None:
            warnings = ["production data may have changed since capture; row/value drift is not a hard failure"]
            info = {}
        connection.rollback()
    except psycopg.Error as error:
        try:
            connection.rollback()
        finally:
            connection.close()
        code = ExitCode.PERMISSION if getattr(error, "sqlstate", None) == "42501" else ExitCode.EXECUTION
        raise AdvisorCLIError(f"read-only preflight query failed: {error}", code) from error
    finally:
        connection.close()
    if not failures:
        exit_code = int(ExitCode.SUCCESS)
    report = {
        "report_schema_version": PREFLIGHT_SCHEMA_VERSION,
        "status": "PASS" if not failures else "FAIL",
        "recommendation_digest": bundle.digest,
        "capture_bundle_digest": bundle.capture_bundle_digest,
        "capture_timestamp": bundle.acquisition_identity.get("capture_timestamp"),
        "relation": relation_id,
        "selected_object_count": len(bundle.selected_objects),
        "evaluated_statistics_target": bundle.evaluated_statistics_target,
        "observed_statistics_target": observed_target,
        "expected_postgres_version": SUPPORTED_POSTGRES_VERSION,
        "observed_postgres_version": observed_version,
        "checks": checks,
        "failures": failures,
        "warnings": warnings,
        "informational": info,
        "read_only": True,
        "_exit_code": exit_code,
    }
    status_by_name = {check["name"]: check["status"] for check in checks}
    report["schema_match"] = status_by_name.get("schema") == "PASS"
    report["target_override_check"] = all(
        status_by_name.get(name) == "PASS"
        for name in ("column_target_overrides", "extstats_target_overrides")
    )
    report["name_collision_check"] = status_by_name.get("name_collisions") == "PASS"
    report["equivalent_extstats_check"] = status_by_name.get("equivalent_extstats") == "PASS"
    return report


__all__ = ["PREFLIGHT_SCHEMA_VERSION", "SUPPORTED_POSTGRES_VERSION", "run_preflight"]
