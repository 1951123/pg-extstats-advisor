"""Fail-closed verifier for the M2.27a production-capture Bundle v2.

This is intentionally a new contract.  The historical v1 verifier remains
untouched and continues to accept only v1 directories.
"""

from __future__ import annotations

import json
from collections.abc import Mapping
from pathlib import Path
from typing import Any

from pg_extstats_advisor.capture.manifest import canonical_digest
from pg_extstats_advisor.capture.target_policy import TargetGridPolicy, TargetPolicyError

BUNDLE_V2_SCHEMA_VERSION = "production-capture-bundle-v2"


class BundleV2VerificationError(ValueError):
    """Raised when a v2 bundle is incomplete or identity-inconsistent."""


def _load(path: Path) -> Any:
    try:
        return json.loads(path.read_text(encoding="utf-8"))
    except (OSError, json.JSONDecodeError) as exc:
        raise BundleV2VerificationError(f"cannot read {path}") from exc


def _required(value: Mapping[str, Any], key: str, context: str) -> Any:
    if key not in value:
        raise BundleV2VerificationError(f"missing {context}.{key}")
    return value[key]


def verify_bundle_v2(path: Path) -> dict[str, Any]:
    root = Path(path)
    bundle = _load(root / "bundle.json")
    if _required(bundle, "bundle_schema_version", "bundle") != BUNDLE_V2_SCHEMA_VERSION:
        raise BundleV2VerificationError("unsupported bundle schema version")
    if _required(bundle, "sealed", "bundle") is not True:
        raise BundleV2VerificationError("bundle is not sealed")
    binding = dict(bundle)
    expected_digest = binding.pop("semantic_digest", None)
    if expected_digest != canonical_digest(binding):
        raise BundleV2VerificationError("bundle semantic digest mismatch")
    for component in ("environment.json", "schema.json", "workload.json", "truth.json", "permissions.json"):
        if not (root / component).is_file():
            raise BundleV2VerificationError(f"missing required component: {component}")
    policy_raw = _load(root / "target-policy.json")
    try:
        policy = TargetGridPolicy(
            allowed_targets=tuple(policy_raw["allowed_targets"]),
            realization_ids=tuple(policy_raw["realization_ids"]),
            canonical_realization_id=policy_raw["canonical_realization_id"],
            replicas_per_target=int(policy_raw["replicas_per_target"]),
            scope=policy_raw["scope"],
            sample_capacity_multiplier=int(policy_raw["sample_capacity_policy"]["multiplier"]),
            native_analyze_equivalent=bool(policy_raw["sample_capacity_policy"]["native_analyze_equivalent"]),
        )
    except (KeyError, TypeError, ValueError, TargetPolicyError) as exc:
        raise BundleV2VerificationError("invalid target policy") from exc
    if policy.as_dict() != policy_raw:
        raise BundleV2VerificationError("target policy is not canonical")
    snapshot = _load(root / "snapshot-provenance.json")
    if snapshot.get("mode") != "strong_single_snapshot":
        raise BundleV2VerificationError("v2 requires strong_single_snapshot")
    snapshot_identity = _required(snapshot, "snapshot_identity", "snapshot-provenance")
    component_refs = _required(snapshot, "components", "snapshot-provenance")
    if not isinstance(component_refs, Mapping) or set(component_refs) != {"metadata", "truth", "reservoir_grid"}:
        raise BundleV2VerificationError("snapshot component references are incomplete")
    if any(ref != snapshot_identity for ref in component_refs.values()):
        raise BundleV2VerificationError("snapshot component reference mismatch")
    if _required(snapshot, "coordinator", "snapshot-provenance").get("committed_after_workers") is not True:
        raise BundleV2VerificationError("coordinator lifetime evidence is incomplete")
    acquisitions = _load(root / "acquisition.json")
    cells = acquisitions.get("cells")
    if not isinstance(cells, list) or len(cells) != len(policy.expected_cells()):
        raise BundleV2VerificationError("target grid is incomplete")
    seen: set[tuple[int, str]] = set()
    artifact_ids: set[str] = set()
    for item in cells:
        try:
            identity = (int(item["statistics_target"]), str(item["realization_id"]))
            if identity not in policy.expected_cells() or identity in seen:
                raise BundleV2VerificationError("duplicate or unexpected target cell")
            seen.add(identity)
            if item.get("snapshot_identity") != snapshot_identity:
                raise BundleV2VerificationError("snapshot mismatch in acquisition cell")
            if item.get("native_analyze_equivalent") is not False:
                raise BundleV2VerificationError("reservoir cell claims native equivalence")
            if item.get("acquisition_method") != "deterministic_reservoir_v1":
                raise BundleV2VerificationError("unsupported acquisition method")
            if not item.get("artifact_cache_relative") or int(item["sample_row_count"]) <= 0:
                raise BundleV2VerificationError("acquisition artifact is incomplete")
            if int(item["source_population_rows"]) <= int(item["sample_row_count"]):
                raise BundleV2VerificationError("incoherent sample population metadata")
            if not item.get("semantic_sha256") or not item.get("binary_sha256"):
                raise BundleV2VerificationError("acquisition artifact digests are missing")
            artifact_id = str(item["artifact_identity"])
            if artifact_id in artifact_ids:
                raise BundleV2VerificationError("A/B/C artifacts are aliased")
            artifact_ids.add(artifact_id)
            expected_seed = policy.seed(*identity)
            if int(item["seed"]) != expected_seed:
                raise BundleV2VerificationError("target-aware seed mismatch")
        except KeyError as exc:
            raise BundleV2VerificationError(f"incomplete acquisition cell: {exc}") from exc
    if set(seen) != set(policy.expected_cells()):
        raise BundleV2VerificationError("target grid cell set mismatch")
    if bundle.get("snapshot_consistency_digest") != canonical_digest(snapshot):
        raise BundleV2VerificationError("snapshot consistency digest mismatch")
    if bundle.get("target_policy_digest") != policy.digest:
        raise BundleV2VerificationError("target policy digest mismatch")
    return {
        "bundle_schema_version": BUNDLE_V2_SCHEMA_VERSION,
        "target_policy_digest": policy.digest,
        "snapshot_identity": snapshot_identity,
        "target_cells": len(cells),
        "native_analyze_equivalent": False,
    }


def write_bundle_root(root: Path, policy: TargetGridPolicy, snapshot: Mapping[str, Any], acquisition: Mapping[str, Any], **extra: Any) -> dict[str, Any]:
    """Write a compact sealed root after all components have succeeded."""
    root.mkdir(parents=True, exist_ok=True)
    (root / "target-policy.json").write_text(json.dumps(policy.as_dict(), sort_keys=True, indent=2) + "\n")
    (root / "snapshot-provenance.json").write_text(json.dumps(snapshot, sort_keys=True, indent=2) + "\n")
    (root / "acquisition.json").write_text(json.dumps(acquisition, sort_keys=True, indent=2) + "\n")
    if extra:
        for name, value in extra.items():
            (root / f"{name}.json").write_text(json.dumps(value, sort_keys=True, indent=2) + "\n")
    manifest = {
        "bundle_schema_version": BUNDLE_V2_SCHEMA_VERSION,
        "sealed": True,
        "target_policy_digest": policy.digest,
        "snapshot_consistency_digest": canonical_digest(snapshot),
        "semantic_digest_algorithm": "sha256-canonical-json-v1",
    }
    manifest["semantic_digest"] = canonical_digest(manifest)
    (root / "bundle.json").write_text(json.dumps(manifest, sort_keys=True, indent=2) + "\n")
    verify_bundle_v2(root)
    return manifest
