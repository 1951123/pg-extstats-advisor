"""Small, dependency-free semantic sealing primitives for capture artifacts."""

from __future__ import annotations

import hashlib
import json
from collections.abc import Mapping
from typing import Any


def _without_runtime_timestamps(value: Any, key: str = "") -> Any:
    if isinstance(value, Mapping):
        return {
            str(name): _without_runtime_timestamps(item, str(name))
            for name, item in value.items()
            if str(name) not in {"timestamp", "capture_timestamp"}
        }
    if isinstance(value, list):
        return [_without_runtime_timestamps(item, key) for item in value]
    if isinstance(value, str) and value.startswith("/") and ("path" in key or key in {"raw_source", "stock_binary"}):
        return "<LOCAL_PATH>"
    return value


def canonical_digest(value: Any) -> str:
    payload = json.dumps(
        _without_runtime_timestamps(value),
        sort_keys=True,
        separators=(",", ":"),
        ensure_ascii=False,
        default=str,
    ).encode()
    return hashlib.sha256(payload).hexdigest()


def seal_manifest(
    components: Mapping[str, Any], *, capture_schema_version: int = 1, benchmark: str = "unspecified"
) -> dict[str, Any]:
    component_digests = {
        name: canonical_digest(value) for name, value in sorted(components.items())
    }
    semantic_binding = {
        "capture_schema_version": capture_schema_version,
        "benchmark": benchmark,
        "components": component_digests,
    }
    return {
        "capture_schema_version": capture_schema_version,
        "benchmark": benchmark,
        "sealed": True,
        "semantic_digest_algorithm": "sha256-canonical-json-v1",
        "components": component_digests,
        "semantic_binding": semantic_binding,
        "semantic_digest": canonical_digest(semantic_binding),
    }


def verify_manifest(manifest: Mapping[str, Any], components: Mapping[str, Any]) -> None:
    if manifest.get("sealed") is not True:
        raise ValueError("capture is not sealed")
    expected = {
        name: canonical_digest(value) for name, value in sorted(components.items())
    }
    if dict(manifest.get("components", {})) != expected:
        raise ValueError("capture component digest mismatch")
    binding = manifest.get("semantic_binding")
    if not isinstance(binding, Mapping) or canonical_digest(binding) != manifest.get("semantic_digest"):
        raise ValueError("capture root digest mismatch")
