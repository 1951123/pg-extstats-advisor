"""Prototype production-capture contract primitives."""

from pg_extstats_advisor.capture.bundle import (
    BundleCompatibilityError,
    BundleVerificationError,
    check_advisor_compatibility,
    verify_production_capture_bundle,
)
from pg_extstats_advisor.capture.bundle_v2 import (
    BUNDLE_V2_SCHEMA_VERSION,
    BundleV2VerificationError,
    verify_bundle_v2,
)
from pg_extstats_advisor.capture.fixed import (
    FIXED_CAPTURE_MODE,
    FixedCaptureVerificationError,
    verify_fixed_t_bundle,
)
from pg_extstats_advisor.capture.manifest import (
    canonical_digest,
    seal_manifest,
    verify_manifest,
)
from pg_extstats_advisor.capture.target_policy import (
    TargetGridPolicy,
    TargetOverride,
    TargetPolicyError,
    inspect_override_rows,
    validate_override_rows,
)

__all__ = [
    "BUNDLE_V2_SCHEMA_VERSION",
    "FIXED_CAPTURE_MODE",
    "BundleCompatibilityError",
    "BundleV2VerificationError",
    "BundleVerificationError",
    "FixedCaptureVerificationError",
    "TargetGridPolicy",
    "TargetOverride",
    "TargetPolicyError",
    "canonical_digest",
    "check_advisor_compatibility",
    "inspect_override_rows",
    "seal_manifest",
    "validate_override_rows",
    "verify_bundle_v2",
    "verify_fixed_t_bundle",
    "verify_manifest",
    "verify_production_capture_bundle",
]
