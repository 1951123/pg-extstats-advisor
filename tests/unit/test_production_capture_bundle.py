import hashlib
import json
from pathlib import Path

import pytest

from pg_extstats_advisor.capture.bundle import (
    BUNDLE_SCHEMA_VERSION,
    BundleCompatibilityError,
    BundleVerificationError,
    canonical_digest,
    check_advisor_compatibility,
    encode_sample,
    verify_production_capture_bundle,
)


def _write(path: Path, value: object) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text(json.dumps(value, sort_keys=True, indent=2) + "\n")


def _make_bundle(root: Path) -> Path:
    columns = [{"attnum": 1, "name": "a", "type": {"namespace": "pg_catalog", "name": "text", "portable_name": "pg_catalog.text", "kind": "builtin"}, "typmod": "-1", "collation": {"namespace": "pg_catalog", "name": "default", "portable_name": "pg_catalog.default"}, "nullable": True, "statistics": {"target": 100}}]
    relation = {"relation_id": "public.t", "schema": "public", "name": "t", "kind": "ordinary_table", "persistence": "u", "columns": columns, "source_population_rows": "2"}
    relation["schema_digest"] = canonical_digest({key: value for key, value in relation.items() if key != "schema_digest"})
    schema = {"schema_version": 1, "relations": [relation]}
    environment = {"schema_version": 1, "postgres_version": "16.14", "server_version_num": "160014", "fields": {"database_collation": {"value": "C", "classification": "required"}, "workload_analysis_version": {"value": "pg16-mvp-v2", "classification": "required"}}, "extensions": {"value": [], "classification": "informational"}}
    workload = {"schema_version": 1, "queries": [{"query_id": "q.1", "sql": "SELECT COUNT(*) FROM t", "effective": True, "weight": 1.0, "target_relation_id": "public.t", "ce_target_id": "q.1", "parsed_support": {"status": "supported", "analysis_version": "pg16-mvp-v2"}}], "effective_workload_digest": "fixture-effective", "raw_sql_sha256": "a" * 64}
    truth_queries = [{"query_id": "q.1", "ce_target_id": "q.1", "exact_cardinality": 1, "source_relation_id": "public.t", "quality": "authoritative_exact", "method": "exact_full_data_count", "snapshot_binding": {"snapshot": "s"}}]
    truth = {"schema_version": 1, "mode": "exact_full_data_count", "queries": truth_queries, "semantic_digest": canonical_digest(truth_queries)}
    sample_dir = root / "acquisition/relations/public.t"
    sample_dir.mkdir(parents=True, exist_ok=True)
    binary = encode_sample([["x"]])
    (sample_dir / "sample.copy.bin").write_bytes(binary)
    sample = {"schema_version": 1, "relation_id": "public.t", "sample_method_id": "deterministic_reservoir_v1", "sample_method_version": 1, "sample_row_count": 1, "source_population_rows": "2", "statistics_population_rows": "2", "selected_columns": columns, "serialization": "pgextstats_m223_length_prefixed_v1", "serialization_version": 1, "semantic_digest": canonical_digest({"relation_id": "public.t", "columns": columns, "rows": [["x"]]}), "binary_sha256": hashlib.sha256(binary).hexdigest(), "authoritative_for_bundle": True, "native_analyze_equivalent": False}
    _write(sample_dir / "manifest.json", sample)
    _write(root / "schema.json", schema); _write(root / "environment.json", environment); _write(root / "workload.json", workload); _write(root / "truth.json", truth)
    components = {"environment.json": canonical_digest(environment), "schema.json": canonical_digest(schema), "workload.json": canonical_digest(workload), "truth.json": canonical_digest(truth), "acquisition/public.t/manifest.json": canonical_digest(sample)}
    binding = {"bundle_schema_version": BUNDLE_SCHEMA_VERSION, "production_identity": {"postgres_version": "16.14"}, "snapshot_consistency": {"mode": "best_effort_multi_snapshot", "components": {}}, "components": components, "relation_inventory": [{"relation_id": "public.t", "schema_digest": relation["schema_digest"], "source_population_rows": "2"}], "workload_identity": {}, "truth_identity": {}, "acquisition_identity": {}, "compatibility": {"ce_target_scope": "single_relation_base_count"}, "capture_mode": "fixture", "sensitivity": {"contains_full_base_table": False}}
    _write(root / "bundle.json", {**binding, "sealed": True, "semantic_digest": canonical_digest(binding), "semantic_binding": binding, "created_timestamp": "2026-01-01T00:00:00Z"})
    return root


def test_valid_bundle_is_accepted(tmp_path: Path) -> None:
    result = verify_production_capture_bundle(_make_bundle(tmp_path))
    assert result["bundle_schema_version"] == BUNDLE_SCHEMA_VERSION
    assert check_advisor_compatibility(tmp_path, {"postgres_version": "16.14"})["compatible"]


@pytest.mark.parametrize("field", ["sealed", "semantic_digest", "components", "relation_inventory", "compatibility"])
def test_missing_required_top_level_field_is_rejected(tmp_path: Path, field: str) -> None:
    _make_bundle(tmp_path)
    bundle_path = tmp_path / "bundle.json"
    value = json.loads(bundle_path.read_text()); value.pop(field); bundle_path.write_text(json.dumps(value))
    with pytest.raises(BundleVerificationError):
        verify_production_capture_bundle(tmp_path)


def test_unknown_version_and_root_tamper_are_rejected(tmp_path: Path) -> None:
    _make_bundle(tmp_path); p = tmp_path / "bundle.json"; value = json.loads(p.read_text()); value["bundle_schema_version"] = "production-capture-bundle-v2"; p.write_text(json.dumps(value))
    with pytest.raises(BundleVerificationError): verify_production_capture_bundle(tmp_path)
    _make_bundle(tmp_path); value = json.loads(p.read_text()); value["semantic_digest"] = "0" * 64; p.write_text(json.dumps(value))
    with pytest.raises(BundleVerificationError): verify_production_capture_bundle(tmp_path)


def test_binary_tamper_and_truth_tamper_are_rejected(tmp_path: Path) -> None:
    _make_bundle(tmp_path); sample = tmp_path / "acquisition/relations/public.t/sample.copy.bin"; sample.write_bytes(sample.read_bytes() + b"x")
    with pytest.raises(BundleVerificationError): verify_production_capture_bundle(tmp_path)
    _make_bundle(tmp_path); truth = tmp_path / "truth.json"; value = json.loads(truth.read_text()); value["queries"][0]["exact_cardinality"] = 2; truth.write_text(json.dumps(value))
    with pytest.raises(BundleVerificationError): verify_production_capture_bundle(tmp_path)


@pytest.mark.parametrize("environment", [{"postgres_version": "17.0"}, {"postgres_version": "16.14", "database_collation": "en_US.UTF-8"}, {"postgres_version": "16.14", "sample_method": "native_analyze"}, {"postgres_version": "16.14", "sample_serialization": "postgres_binary_copy"}, {"postgres_version": "16.14", "workload_analysis_version": "other"}])
def test_incompatible_advisor_environment_is_rejected(tmp_path: Path, environment: dict[str, str]) -> None:
    _make_bundle(tmp_path)
    with pytest.raises(BundleCompatibilityError): check_advisor_compatibility(tmp_path, environment)
