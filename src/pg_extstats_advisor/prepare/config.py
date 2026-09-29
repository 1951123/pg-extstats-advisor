"""Versioned MVP preparation configuration."""

from __future__ import annotations

import hashlib
import json
import os
import re
from dataclasses import dataclass
from pathlib import Path
from typing import Any

from pg_extstats_advisor.statistics import (
    DEFAULT_GLOBAL_STATISTICS_TARGET,
    validate_global_statistics_target,
)


@dataclass(frozen=True, slots=True)
class StatisticsTargetConfig:
    """One advisor-level target shared by ordinary and extended statistics."""

    global_statistics_target: int = DEFAULT_GLOBAL_STATISTICS_TARGET

    def __post_init__(self) -> None:
        validate_global_statistics_target(self.global_statistics_target)


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
    objective_membership_policy: str = "require_all_positive"

    def __post_init__(self) -> None:
        validate_global_statistics_target(self.statistics_target, field="statistics_target")

    @property
    def global_statistics_target(self) -> int:
        """Compatibility name for the single target used by the MVP."""

        return StatisticsTargetConfig(self.statistics_target).global_statistics_target

    @property
    def evaluated_statistics_target(self) -> int:
        """The externally supplied target evaluated by this run.

        This is a reporting alias, not a second decision variable.
        """

        return self.global_statistics_target

    @property
    def digest(self) -> str:
        return hashlib.sha256(self.canonical_json().encode()).hexdigest()

    def canonical_json(self) -> str:
        def sanitize(dsn: str) -> str:
            value = re.sub(r"(?i)(password\s*=\s*)[^\s]+", r"\1<redacted>", dsn)
            return re.sub(r"(?i)(://[^:/@]+:)[^@]+@", r"\1<redacted>@", value)

        value = {
            "schema_version": self.schema_version,
            "database": {
                "source_dsn": sanitize(self.source_dsn),
                "acquisition_dsn": sanitize(self.acquisition_dsn),
            },
            "workload": {
                "path": str(self.workload_path),
                **(
                    {"objective_membership_policy": self.objective_membership_policy}
                    if self.objective_membership_policy != "require_all_positive"
                    else {}
                ),
            },
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
                "global_statistics_target": self.global_statistics_target,
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

        def dsn(name: str) -> str:
            env_name = database.get(f"{name}_env")
            if env_name:
                try:
                    return os.environ[str(env_name)]
                except KeyError as error:
                    raise ValueError(f"missing DSN environment variable: {env_name}") from error
            if name not in database:
                raise ValueError(f"missing database.{name} or database.{name}_env")
            return str(database[name])

        mechanisms = tuple(candidates["mechanisms"])
        if not mechanisms or set(mechanisms) - {"mcv", "fd"}:
            raise ValueError("mechanisms must be a non-empty subset of mcv/fd")
        max_arity = int(candidates["max_candidate_arity"])
        if max_arity != 2:
            raise ValueError("MVP max_candidate_arity must equal 2")
        cap = candidates.get("max_candidates_per_relation")
        if cap is not None and int(cap) <= 0:
            raise ValueError("max_candidates_per_relation must be positive")
        legacy_target = int(acquisition["statistics_target"]) if "statistics_target" in acquisition else None
        target = int(acquisition.get("global_statistics_target", legacy_target if legacy_target is not None else 100))
        if legacy_target is not None and legacy_target != target:
            raise ValueError("statistics_target and global_statistics_target disagree")
        validate_global_statistics_target(target, field="statistics_target")
        explicit = tuple(
            ExplicitCandidate(str(item["relation"]), str(item["mechanism"]), tuple(item["columns"]))
            for item in candidates.get("explicit", [])
        )
        for item in explicit:
            if item.mechanism not in {"mcv", "fd"}:
                raise ValueError("explicit candidate mechanism must be mcv or fd")
            if len(item.columns) != 2:
                raise ValueError("MVP explicit candidates must have exactly two columns")
        policy = str(raw["workload"].get("objective_membership_policy", "require_all_positive"))
        if policy not in {"require_all_positive", "positive_truth_only"}:
            raise ValueError(
                "workload.objective_membership_policy must be require_all_positive or "
                "positive_truth_only"
            )
        return cls(
            1,
            dsn("source_dsn"),
            dsn("acquisition_dsn"),
            Path(raw["workload"]["path"]),
            Path(acquisition["output_path"]),
            mechanisms,
            max_arity,
            int(cap) if cap is not None else None,
            explicit,
            target,
            tuple(sorted(raw["maintenance"].items())),
            policy,
        )
