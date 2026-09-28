"""Versioned MVP preparation configuration."""

from __future__ import annotations

import hashlib
import json
from dataclasses import dataclass
from pathlib import Path
from typing import Any


@dataclass(frozen=True, slots=True)
class ExplicitCandidate:
    relation: str
    mechanism: str
    columns: tuple[str, ...]


@dataclass(frozen=True, slots=True)
class PreparationConfig:
    schema_version: int
    source_dsn: str
    acquisition_dsn: str
    workload_path: Path
    output_path: Path
    mechanisms: tuple[str, ...]
    max_candidate_arity: int
    max_candidates_per_relation: int | None
    explicit_candidates: tuple[ExplicitCandidate, ...]
    statistics_target: int
    maintenance: tuple[tuple[str, Any], ...]

    @property
    def digest(self) -> str:
        return hashlib.sha256(self.canonical_json().encode()).hexdigest()

    def canonical_json(self) -> str:
        value = {
            "schema_version": self.schema_version,
            "database": {"source_dsn": self.source_dsn, "acquisition_dsn": self.acquisition_dsn},
            "workload": {"path": str(self.workload_path)},
            "candidates": {
                "mechanisms": self.mechanisms,
                "max_candidate_arity": self.max_candidate_arity,
                "max_candidates_per_relation": self.max_candidates_per_relation,
                "explicit": [
                    {
                        "relation": item.relation,
                        "mechanism": item.mechanism,
                        "columns": item.columns,
                    }
                    for item in self.explicit_candidates
                ],
            },
            "acquisition": {
                "statistics_target": self.statistics_target,
                "output_path": str(self.output_path),
            },
            "maintenance": dict(self.maintenance),
        }
        return json.dumps(value, sort_keys=True, separators=(",", ":"))

    @classmethod
    def load(cls, path: Path) -> PreparationConfig:
        raw = json.loads(path.read_text())
        if raw.get("schema_version") != 1:
            raise ValueError("unsupported preparation config schema_version")
        required = {"database", "workload", "candidates", "acquisition", "maintenance"}
        if required - raw.keys():
            raise ValueError(f"missing config sections: {sorted(required - raw.keys())}")
        database, candidates, acquisition = raw["database"], raw["candidates"], raw["acquisition"]
        mechanisms = tuple(candidates["mechanisms"])
        if not mechanisms or set(mechanisms) - {"mcv", "fd"}:
            raise ValueError("mechanisms must be a non-empty subset of mcv/fd")
        max_arity = int(candidates["max_candidate_arity"])
        if max_arity < 2:
            raise ValueError("max_candidate_arity must be at least 2")
        cap = candidates.get("max_candidates_per_relation")
        if cap is not None and int(cap) <= 0:
            raise ValueError("max_candidates_per_relation must be positive")
        target = int(acquisition["statistics_target"])
        if not 0 <= target <= 10000:
            raise ValueError("statistics_target must be between 0 and 10000")
        explicit = tuple(
            ExplicitCandidate(str(item["relation"]), str(item["mechanism"]), tuple(item["columns"]))
            for item in candidates.get("explicit", [])
        )
        return cls(
            1,
            str(database["source_dsn"]),
            str(database["acquisition_dsn"]),
            Path(raw["workload"]["path"]),
            Path(acquisition["output_path"]),
            mechanisms,
            max_arity,
            int(cap) if cap is not None else None,
            explicit,
            target,
            tuple(sorted(raw["maintenance"].items())),
        )
