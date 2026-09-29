"""Production Capture Bundle v1 contract, sealing, verification, and gating.

The bundle is a directory contract.  It contains metadata, workload, exact
truth, and acquisition samples; it never contains PostgreSQL extstats payloads
or a full production relation.
"""

from __future__ import annotations

import hashlib
import json
import re
from collections.abc import Mapping
from pathlib import Path
from typing import Any

from pg_extstats_advisor.capture.manifest import canonical_digest
from pg_extstats_advisor.statistics import (
    DEFAULT_GLOBAL_STATISTICS_TARGET,
    validate_global_statistics_target,
)

BUNDLE_SCHEMA_VERSION = "production-capture-bundle-v1"
SNAPSHOT_MODES = {"strong_single_snapshot", "best_effort_multi_snapshot"}
SUPPORTED_VERSION = "16.14"
SUPPORTED_SAMPLE_METHOD = ("deterministic_reservoir_v1", 1)
SUPPORTED_SERIALIZATION = ("pgextstats_m223_length_prefixed_v1", 1)
SAMPLE_MAGIC = b"PGEXTSTATS-M223-SAMPLE\0"
RELATION_ID = re.compile(r"^[a-z_][a-z0-9_]*\.[a-z_][a-z0-9_]*$")


class BundleVerificationError(ValueError):
    """Raised for every malformed, incomplete, or tampered bundle."""


class BundleCompatibilityError(ValueError):
    """Raised when an advisor cannot consume a verified bundle."""


def _json(path: Path) -> Any:
    try:
        return json.loads(path.read_text(encoding="utf-8"))
    except (OSError, json.JSONDecodeError) as error:
        raise BundleVerificationError(f"cannot read JSON component: {path}") from error


def _require(mapping: Mapping[str, Any], key: str, context: str) -> Any:
    if key not in mapping:
        raise BundleVerificationError(f"missing required field {context}.{key}")
    return mapping[key]


def _sha256(path: Path) -> str:
    try:
        return hashlib.sha256(path.read_bytes()).hexdigest()
    except OSError as error:
        raise BundleVerificationError(f"missing binary component: {path}") from error


def _component_digests(root: Path, relation_ids: list[str]) -> dict[str, str]:
    names = ["environment.json", "schema.json", "workload.json", "truth.json"]
    values = {name: canonical_digest(_json(root / name)) for name in names}
    for relation_id in relation_ids:
        manifest = root / "acquisition" / "relations" / relation_id / "manifest.json"
        values[f"acquisition/{relation_id}/manifest.json"] = canonical_digest(_json(manifest))
    return values


def _relation_record(schema: Mapping[str, Any], relation_id: str) -> Mapping[str, Any]:
    for relation in schema.get("relations", []):
        if isinstance(relation, Mapping) and relation.get("relation_id") == relation_id:
            return relation
    raise BundleVerificationError(f"unknown relation reference: {relation_id}")


def _effective_global_statistics_target(environment: Mapping[str, Any]) -> int:
    """Read the one effective target recorded by a v1 capture.

    Older v1 fixtures did not expose the GUC field.  They are interpreted as
    PostgreSQL's default for backwards compatibility; new captures should
    always record ``default_statistics_target`` explicitly.
    """

    fields = environment.get("fields", {})
    raw = fields.get("default_statistics_target", {}).get(
        "value", DEFAULT_GLOBAL_STATISTICS_TARGET
    )
    try:
        return validate_global_statistics_target(int(raw))
    except (TypeError, ValueError) as error:
        raise BundleVerificationError("invalid global statistics target") from error


def _decode_sample(path: Path, row_count: int, column_count: int) -> list[list[str | None]]:
    data = path.read_bytes()
    if not data.startswith(SAMPLE_MAGIC):
        raise BundleVerificationError("unsupported sample serialization magic")
    offset = len(SAMPLE_MAGIC)
    rows: list[list[str | None]] = []
    for _ in range(row_count):
        row: list[str | None] = []
        for _ in range(column_count):
            if offset + 4 > len(data):
                raise BundleVerificationError("truncated sample binary")
            length = int.from_bytes(data[offset : offset + 4], "big")
            offset += 4
            if offset + length > len(data):
                raise BundleVerificationError("truncated sample value")
            raw = data[offset : offset + length]
            offset += length
            row.append(None if length == 0xFFFFFFFF else raw.decode("utf-8"))
        rows.append(row)
    if offset != len(data):
        raise BundleVerificationError("sample binary has trailing bytes")
    return rows


def verify_production_capture_bundle(path: Path) -> dict[str, Any]:
    """Verify a sealed v1 directory and return its normalized inventory.

    All checks fail closed.  In particular, unknown versions, duplicate IDs,
    inconsistent relation references, unsupported sample methods, and every
    digest mismatch are rejected rather than downgraded to warnings.
    """
    root = Path(path)
    bundle = _json(root / "bundle.json")
    if _require(bundle, "bundle_schema_version", "bundle") != BUNDLE_SCHEMA_VERSION:
        raise BundleVerificationError("unsupported bundle schema version")
    if _require(bundle, "sealed", "bundle") is not True:
        raise BundleVerificationError("bundle is not sealed")
    for key in (
        "semantic_digest", "production_identity", "snapshot_consistency", "components",
        "relation_inventory", "workload_identity", "truth_identity",
        "acquisition_identity", "compatibility", "capture_mode", "created_timestamp",
        "sensitivity",
    ):
        _require(bundle, key, "bundle")
    schema = _json(root / "schema.json")
    environment = _json(root / "environment.json")
    workload = _json(root / "workload.json")
    truth = _json(root / "truth.json")
    relations = schema.get("relations")
    if not isinstance(relations, list) or not relations:
        raise BundleVerificationError("schema.relations must be non-empty")
    relation_ids = [str(item.get("relation_id", "")) for item in relations]
    if len(set(relation_ids)) != len(relation_ids):
        raise BundleVerificationError("duplicate relation id")
    if any(not RELATION_ID.fullmatch(item) for item in relation_ids):
        raise BundleVerificationError("malformed relation id")
    inventory_ids = [str(item.get("relation_id", "")) for item in bundle["relation_inventory"]]
    if sorted(inventory_ids) != sorted(relation_ids):
        raise BundleVerificationError("relation inventory/schema mismatch")
    component_digests = _component_digests(root, relation_ids)
    if bundle["components"] != component_digests:
        raise BundleVerificationError("component semantic digest mismatch")
    semantic_binding = dict(bundle.get("semantic_binding", {}))
    if canonical_digest(semantic_binding) != bundle["semantic_digest"]:
        raise BundleVerificationError("root semantic digest mismatch")
    snapshot = bundle["snapshot_consistency"]
    mode = _require(snapshot, "mode", "snapshot_consistency")
    if mode not in SNAPSHOT_MODES:
        raise BundleVerificationError("unsupported snapshot consistency mode")
    if not isinstance(_require(snapshot, "components", "snapshot_consistency"), Mapping):
        raise BundleVerificationError("snapshot component binding must be an object")
    version = str(_require(environment, "postgres_version", "environment"))
    if not re.fullmatch(r"\d+\.\d+", version):
        raise BundleVerificationError("malformed PostgreSQL version")
    target = _effective_global_statistics_target(environment)
    query_items = workload.get("queries")
    if not isinstance(query_items, list):
        raise BundleVerificationError("workload.queries must be a list")
    query_ids = [str(item.get("query_id", "")) for item in query_items]
    if not query_ids or len(set(query_ids)) != len(query_ids):
        raise BundleVerificationError("workload query IDs must be unique")
    if any(not item.get("target_relation_id") for item in query_items):
        raise BundleVerificationError("workload query has no target relation")
    if any(str(item["target_relation_id"]) not in relation_ids for item in query_items):
        raise BundleVerificationError("workload references unknown relation")
    effective = {item["query_id"] for item in query_items if item.get("effective") is True}
    truth_items = truth.get("queries")
    if not isinstance(truth_items, list):
        raise BundleVerificationError("truth.queries must be a list")
    truth_ids = [str(item.get("query_id", "")) for item in truth_items]
    if len(set(truth_ids)) != len(truth_ids):
        raise BundleVerificationError("truth query IDs must be unique")
    if truth.get("semantic_digest") != canonical_digest(truth_items):
        raise BundleVerificationError("truth semantic digest mismatch")
    truth_by_id = {item["query_id"]: item for item in truth_items}
    if not effective.issubset(truth_by_id):
        raise BundleVerificationError("truth is missing an effective query")
    for relation_id in relation_ids:
        relation = _relation_record(schema, relation_id)
        columns = relation.get("columns")
        if not isinstance(columns, list) or not columns:
            raise BundleVerificationError(f"relation has no columns: {relation_id}")
        portable_relation = {key: value for key, value in relation.items() if key not in {"provenance", "schema_digest"}}
        if relation.get("schema_digest") != canonical_digest(portable_relation):
            raise BundleVerificationError("relation schema digest mismatch")
        sample_dir = root / "acquisition" / "relations" / relation_id
        sample_manifest = _json(sample_dir / "manifest.json")
        if sample_manifest.get("relation_id") != relation_id:
            raise BundleVerificationError("sample relation identity mismatch")
        if sample_manifest.get("sample_method_id") != SUPPORTED_SAMPLE_METHOD[0] or int(sample_manifest.get("sample_method_version", -1)) != SUPPORTED_SAMPLE_METHOD[1]:
            raise BundleVerificationError("unsupported sample method")
        if sample_manifest.get("serialization") != SUPPORTED_SERIALIZATION[0] or int(sample_manifest.get("serialization_version", -1)) != SUPPORTED_SERIALIZATION[1]:
            raise BundleVerificationError("unsupported sample serialization")
        row_count = int(_require(sample_manifest, "sample_row_count", "sample manifest"))
        source_rows = int(_require(sample_manifest, "source_population_rows", "sample manifest"))
        if row_count <= 0 or source_rows <= row_count:
            raise BundleVerificationError("incoherent sample population metadata")
        if sample_manifest.get("selected_columns") != columns:
            raise BundleVerificationError("sample schema does not match relation schema")
        binary = sample_dir / "sample.copy.bin"
        if _sha256(binary) != sample_manifest.get("binary_sha256"):
            raise BundleVerificationError("sample binary digest mismatch")
        rows = _decode_sample(binary, row_count, len(columns))
        if canonical_digest({"relation_id": relation_id, "columns": columns, "rows": rows}) != sample_manifest.get("semantic_digest"):
            raise BundleVerificationError("sample semantic digest mismatch")
        if sample_manifest.get("authoritative_for_bundle") is not True:
            raise BundleVerificationError("sample authority declaration missing")
        if sample_manifest.get("native_analyze_equivalent") is not False:
            raise BundleVerificationError("sample fidelity claim is not explicit")
    if bundle.get("sensitivity", {}).get("contains_full_base_table") is not False:
        raise BundleVerificationError("bundle must declare that it excludes the full base table")
    return {
        "bundle_schema_version": BUNDLE_SCHEMA_VERSION,
        "semantic_digest": bundle["semantic_digest"],
        "relation_ids": relation_ids,
        "query_count": len(query_items),
        "effective_query_count": len(effective),
        "production_version": version,
        "snapshot_mode": mode,
        "global_statistics_target": target,
    }


def check_advisor_compatibility(path: Path, advisor_environment: Mapping[str, Any]) -> dict[str, Any]:
    """Apply the strict v1 exact-version/type/collation/workload gate."""
    verification = verify_production_capture_bundle(path)
    bundle = _json(Path(path) / "bundle.json")
    environment = _json(Path(path) / "environment.json")
    required_version = str(environment["postgres_version"])
    advisor_version = str(advisor_environment.get("postgres_version", ""))
    if advisor_version != required_version or advisor_version != SUPPORTED_VERSION:
        raise BundleCompatibilityError("v1 requires exact PostgreSQL 16.14")
    bundle_target = int(verification["global_statistics_target"])
    requested_target = advisor_environment.get(
        "global_statistics_target", advisor_environment.get("statistics_target", bundle_target)
    )
    try:
        requested_target = validate_global_statistics_target(int(requested_target))
    except (TypeError, ValueError) as error:
        raise BundleCompatibilityError("invalid advisor global statistics target") from error
    if requested_target != bundle_target:
        raise BundleCompatibilityError(
            f"global statistics target mismatch: bundle={bundle_target}, advisor={requested_target}"
        )
    supported_types = set(advisor_environment.get("supported_types", ["pg_catalog.text"]))
    schema = _json(Path(path) / "schema.json")
    for relation in schema["relations"]:
        for column in relation["columns"]:
            if column["type"]["portable_name"] not in supported_types:
                raise BundleCompatibilityError("unsupported PostgreSQL data type")
    bundle_collation = environment["fields"]["database_collation"]["value"]
    if advisor_environment.get("database_collation", bundle_collation) != bundle_collation:
        raise BundleCompatibilityError("database collation mismatch")
    if advisor_environment.get("sample_serialization", SUPPORTED_SERIALIZATION[0]) != SUPPORTED_SERIALIZATION[0]:
        raise BundleCompatibilityError("unsupported sample serialization")
    if advisor_environment.get("sample_method", SUPPORTED_SAMPLE_METHOD[0]) != SUPPORTED_SAMPLE_METHOD[0]:
        raise BundleCompatibilityError("unsupported sampling method")
    if advisor_environment.get("workload_analysis_version", environment["fields"]["workload_analysis_version"]["value"]) != environment["fields"]["workload_analysis_version"]["value"]:
        raise BundleCompatibilityError("workload analysis version mismatch")
    if bundle["compatibility"].get("ce_target_scope") != "single_relation_base_count":
        raise BundleCompatibilityError("unsupported CE target scope")
    return {
        "compatible": True,
        "production_version": required_version,
        "advisor_version": advisor_version,
        "global_statistics_target": bundle_target,
        **verification,
    }


def encode_sample(rows: list[list[str | None]]) -> bytes:
    data = bytearray(SAMPLE_MAGIC)
    for row in rows:
        for value in row:
            if value is None:
                data.extend((0xFFFFFFFF).to_bytes(4, "big"))
                continue
            raw = str(value).encode("utf-8")
            data.extend(len(raw).to_bytes(4, "big"))
            data.extend(raw)
    return bytes(data)


def decode_sample(path: Path, row_count: int, column_count: int) -> list[list[str | None]]:
    return _decode_sample(path, row_count, column_count)
