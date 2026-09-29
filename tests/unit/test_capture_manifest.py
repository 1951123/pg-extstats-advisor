import pytest

from pg_extstats_advisor.capture.manifest import canonical_digest, seal_manifest, verify_manifest


def test_digest_is_deterministic_and_excludes_runtime_timestamps() -> None:
    left = {"payload": {"rows": [1, 2]}, "capture_timestamp": "2026-01-01T00:00:00Z"}
    right = {"capture_timestamp": "2027-01-01T00:00:00Z", "payload": {"rows": [1, 2]}}
    assert canonical_digest(left) == canonical_digest(right)


def test_tampered_component_and_missing_component_are_rejected() -> None:
    components = {"schema.json": {"columns": ["a"]}, "truth.json": {"q1": 1}}
    manifest = seal_manifest(components)
    verify_manifest(manifest, components)
    with pytest.raises(ValueError, match="component"):
        verify_manifest(manifest, {**components, "truth.json": {"q1": 2}})
    with pytest.raises(ValueError, match="component"):
        verify_manifest(manifest, {"schema.json": components["schema.json"]})


def test_unsealed_manifest_is_rejected() -> None:
    with pytest.raises(ValueError, match="sealed"):
        verify_manifest({"sealed": False}, {})
