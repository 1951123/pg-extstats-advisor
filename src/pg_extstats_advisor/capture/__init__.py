"""Prototype production-capture contract primitives."""

from pg_extstats_advisor.capture.bundle import (
    BundleCompatibilityError,
    BundleVerificationError,
    check_advisor_compatibility,
    verify_production_capture_bundle,
)
from pg_extstats_advisor.capture.manifest import (
    canonical_digest,
    seal_manifest,
    verify_manifest,
)

__all__ = [
    "BundleCompatibilityError",
    "BundleVerificationError",
    "canonical_digest",
    "check_advisor_compatibility",
    "seal_manifest",
    "verify_manifest",
    "verify_production_capture_bundle",
]
