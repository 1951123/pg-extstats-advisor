"""Prototype production-capture contract primitives."""

from pg_extstats_advisor.capture.manifest import (
    canonical_digest,
    seal_manifest,
    verify_manifest,
)

__all__ = ["canonical_digest", "seal_manifest", "verify_manifest"]
