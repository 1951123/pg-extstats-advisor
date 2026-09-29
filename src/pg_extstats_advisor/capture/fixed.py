"""Validation boundary for the core one-target production capture.

Bundle v1 already has the required portable components and one acquisition
sample.  This module adds the stricter fixed-T/same-snapshot contract without
changing the historical v1 or experimental v2 schemas.
"""

from __future__ import annotations

import json
from pathlib import Path
from typing import Any

from pg_extstats_advisor.capture.bundle import verify_production_capture_bundle
from pg_extstats_advisor.statistics import validate_global_statistics_target

FIXED_CAPTURE_MODE = "fixed_t_same_snapshot"


class FixedCaptureVerificationError(ValueError):
    """Raised when a bundle cannot be used as a fixed-T core input."""


def verify_fixed_t_bundle(path: Path, *, expected_target: int = 100) -> dict[str, Any]:
    """Verify one sealed fixed-T bundle and return its portable identities."""

    root = Path(path)
    try:
        verification = verify_production_capture_bundle(root)
        bundle = json.loads((root / "bundle.json").read_text())
        environment = json.loads((root / "environment.json").read_text())
        acquisition = dict(bundle["acquisition_identity"])
        compatibility = dict(bundle["compatibility"])
    except (OSError, KeyError, TypeError, ValueError, json.JSONDecodeError) as error:
        raise FixedCaptureVerificationError("malformed fixed-T bundle") from error

    target = validate_global_statistics_target(
        int(verification["global_statistics_target"]), field="bundle global_statistics_target"
    )
    expected = validate_global_statistics_target(expected_target, field="expected_target")
    if target != expected:
        raise FixedCaptureVerificationError(
            f"fixed-T target mismatch: bundle={target}, expected={expected}"
        )
    if bundle.get("capture_mode") != FIXED_CAPTURE_MODE:
        raise FixedCaptureVerificationError("bundle is not a fixed-T capture")
    if bundle.get("read_only") is not True:
        raise FixedCaptureVerificationError("capture is not marked read-only")
    if bundle["snapshot_consistency"].get("mode") != "strong_single_snapshot":
        raise FixedCaptureVerificationError("fixed-T core capture requires one strong snapshot")
    if acquisition.get("global_statistics_target") != target:
        raise FixedCaptureVerificationError("acquisition target is not bound to bundle target")
    if acquisition.get("canonical_realization") is not True:
        raise FixedCaptureVerificationError("canonical acquisition realization is not declared")
    if compatibility.get("target_override_policy") != "fail_closed_relevant_overrides":
        raise FixedCaptureVerificationError("target-override policy is not fail-closed")
    overrides = bundle.get("target_override_evidence", {})
    if overrides.get("status") != "passed" or overrides.get("relevant_override_count") != 0:
        raise FixedCaptureVerificationError("target override evidence is not clean")
    if int(environment.get("relation_row_count", 0)) <= 0:
        raise FixedCaptureVerificationError("source population row count is missing")
    return {
        **verification,
        "global_statistics_target": target,
        "capture_mode": FIXED_CAPTURE_MODE,
        "canonical_realization": True,
        "target_override_status": "passed",
    }


__all__ = ["FIXED_CAPTURE_MODE", "FixedCaptureVerificationError", "verify_fixed_t_bundle"]
