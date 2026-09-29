"""Content-addressed cache for reconstructed native payload repositories.

The cache is deliberately a derived artifact.  A persisted acquisition sample
and the complete semantic build identity are the authority; cache directory
names, PostgreSQL OIDs, timestamps, and temporary paths are never identity
inputs.
"""

from __future__ import annotations

import hashlib
import json
import os
import shutil
import tempfile
from collections.abc import Callable, Mapping
from dataclasses import dataclass
from pathlib import Path
from typing import Any

from pg_extstats_advisor.payloads.repository import PayloadRepository
from pg_extstats_advisor.statistics import validate_global_statistics_target

CACHE_SCHEMA_VERSION = 1


def bind_statistics_target(
    identity: Mapping[str, Any], global_statistics_target: int
) -> dict[str, Any]:
    """Return a cache identity explicitly bound to one external target.

    ``statistics_target`` is accepted as a legacy alias, but conflicting
    aliases fail closed.  The returned identity always carries the canonical
    ``global_statistics_target`` key.
    """

    target = validate_global_statistics_target(global_statistics_target)
    value = dict(identity)
    for alias in ("statistics_target", "global_statistics_target"):
        if alias in value and int(value[alias]) != target:
            raise ValueError("cache identity statistics target mismatch")
    value["global_statistics_target"] = target
    return value


def canonical_digest(value: Any) -> str:
    return hashlib.sha256(
        json.dumps(value, sort_keys=True, separators=(",", ":"), default=str).encode()
    ).hexdigest()


def repository_semantic_digest(repository: PayloadRepository) -> str:
    """Digest only payload semantics, excluding volatile OIDs and timestamps."""

    rows = [
        {
            "candidate_id": str(item.candidate.candidate_id),
            "mechanism": item.candidate.mechanism.value,
            "attributes": list(item.candidate.attributes),
            "state": item.state.value,
            "payload_sha256": item.payload_sha256,
            "payload_size": len(item.payload) if item.payload is not None else None,
        }
        for item in repository.payloads
    ]
    return canonical_digest(sorted(rows, key=lambda row: row["candidate_id"]))


@dataclass(frozen=True, slots=True)
class CacheResolution:
    repository: PayloadRepository
    cache_path: Path
    cache_hit: bool
    semantic_digest: str
    identity_digest: str


def _summary(repository: PayloadRepository, semantic_digest: str) -> dict[str, Any]:
    states: dict[str, int] = {}
    mechanisms: dict[str, int] = {}
    for item in repository.payloads:
        states[item.state.value] = states.get(item.state.value, 0) + 1
        kind = item.candidate.mechanism.value
        mechanisms[kind] = mechanisms.get(kind, 0) + 1
    return {
        "candidate_count": len(repository.payloads),
        "realization_state_counts": states,
        "mechanism_counts": mechanisms,
        "semantic_digest": semantic_digest,
    }


def _load_valid(
    entry: Path,
    identity: Mapping[str, Any],
    expected_semantic_digest: str,
    load_repository: Callable[[Path], PayloadRepository],
) -> CacheResolution:
    manifest = json.loads((entry / "cache-manifest.json").read_text())
    if manifest.get("cache_schema_version") != CACHE_SCHEMA_VERSION:
        raise ValueError("unsupported artifact-cache schema")
    if manifest.get("identity") != dict(identity):
        raise ValueError("artifact-cache identity mismatch")
    repository = load_repository(entry / "repository")
    semantic = repository_semantic_digest(repository)
    if semantic != expected_semantic_digest or manifest.get("semantic_digest") != semantic:
        raise ValueError("artifact-cache semantic digest mismatch")
    identity_digest = canonical_digest(identity)
    if manifest.get("identity_digest") != identity_digest:
        raise ValueError("artifact-cache identity digest mismatch")
    return CacheResolution(repository, entry, True, semantic, identity_digest)


def resolve_or_build_repository(
    cache_root: Path,
    lineage_key: str,
    *,
    identity: Mapping[str, Any],
    expected_semantic_digest: str,
    build_repository: Callable[[Path], PayloadRepository | None],
    load_repository: Callable[[Path], PayloadRepository] = PayloadRepository.load,
    global_statistics_target: int | None = None,
) -> CacheResolution:
    """Resolve a validated cache entry, rebuilding it from the frozen sample.

    Invalid entries are discarded only beneath the explicitly named cache
    lineage.  A build is performed in a temporary sibling and atomically
    installed, so partial repositories cannot become hits.
    """

    if not lineage_key or "/" in lineage_key or "\\" in lineage_key or lineage_key in {".", ".."}:
        raise ValueError("lineage_key must be a simple cache entry name")
    if global_statistics_target is not None:
        identity = bind_statistics_target(identity, global_statistics_target)
    elif "statistics_target" in identity or "global_statistics_target" in identity:
        # Canonicalize legacy callers so the target is always explicit in the
        # persisted cache identity, while retaining the legacy alias in the
        # identity for backwards-readable manifests.
        raw_target = identity.get("global_statistics_target", identity.get("statistics_target"))
        identity = bind_statistics_target(identity, int(raw_target))
    cache_root.mkdir(parents=True, exist_ok=True)
    entry = cache_root / lineage_key
    try:
        return _load_valid(entry, identity, expected_semantic_digest, load_repository)
    except (OSError, ValueError, KeyError, TypeError, json.JSONDecodeError):
        if entry.exists():
            shutil.rmtree(entry)

    temporary = Path(tempfile.mkdtemp(prefix=f".{lineage_key}.", dir=cache_root))
    try:
        repository_path = temporary / "repository"
        built = build_repository(repository_path)
        repository = built if built is not None else load_repository(repository_path)
        semantic = repository_semantic_digest(repository)
        if semantic != expected_semantic_digest:
            raise ValueError(
                f"rebuilt repository semantic digest mismatch: {semantic} != {expected_semantic_digest}"
            )
        identity_digest = canonical_digest(identity)
        manifest = {
            "cache_schema_version": CACHE_SCHEMA_VERSION,
            "identity": dict(identity),
            "identity_digest": identity_digest,
            "semantic_digest": semantic,
            "repository": "repository",
            "summary": _summary(repository, semantic),
        }
        (temporary / "cache-manifest.json").write_text(
            json.dumps(manifest, sort_keys=True, indent=2) + "\n"
        )
        os.replace(temporary, entry)
    except Exception:
        shutil.rmtree(temporary, ignore_errors=True)
        raise
    return CacheResolution(repository, entry, False, semantic, identity_digest)
