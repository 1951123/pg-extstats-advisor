"""Versioned calibration configuration."""

from __future__ import annotations

import hashlib
import json
import os
import re
from dataclasses import dataclass
from pathlib import Path
from typing import Any

from pg_extstats_advisor.statistics import validate_global_statistics_target


@dataclass(frozen=True, slots=True)
class CalibrationGates:
    max_cv: float = 0.10
    min_r_squared: float = 0.95
    max_heldout_relative_error: float = 0.15
    max_same_count_subset_cv: float = 0.10


@dataclass(frozen=True, slots=True)
class CalibrationConfig:
    schema_version: int
    dsn: str
    relation: str
    columns: tuple[str, ...]
    statistics_target: int
    repetitions: int
    seed: int
    output_path: Path
    count_levels: tuple[int, ...]
    gates: CalibrationGates
    subsets_per_count: int = 1
    require_postgres_version: str | None = None
    build_provenance_path: Path | None = None
    expected_build_recipe_digest: str | None = None
    postgres_binary_path: Path | None = None
    environment_description: str | None = None
    candidate_catalog_path: Path | None = None
    benchmark: str | None = None
    dataset_provenance_path: Path | None = None
    stability_repetitions: int | None = None

    @property
    def digest(self) -> str:
        return hashlib.sha256(self.canonical_json().encode()).hexdigest()

    def canonical_json(self) -> str:
        dsn = re.sub(r"(?i)(password\s*=\s*)[^\s]+", r"\1<redacted>", self.dsn)
        dsn = re.sub(r"(?i)(://[^:/@]+:)[^@]+@", r"\1<redacted>@", dsn)
        return json.dumps(
            {
                "schema_version": self.schema_version,
                "database": {"calibration_dsn": dsn},
                "relation": self.relation,
                "columns": self.columns,
                "statistics_target": self.statistics_target,
                "repetitions": self.repetitions,
                "seed": self.seed,
                "output_path": str(self.output_path),
                "count_levels": self.count_levels,
                "gates": {
                    "max_cv": self.gates.max_cv,
                    "min_r_squared": self.gates.min_r_squared,
                    "max_heldout_relative_error": self.gates.max_heldout_relative_error,
                    "max_same_count_subset_cv": self.gates.max_same_count_subset_cv,
                },
                "subsets_per_count": self.subsets_per_count,
                "require_postgres_version": self.require_postgres_version,
                "build_provenance_path": (
                    str(self.build_provenance_path) if self.build_provenance_path else None
                ),
                "expected_build_recipe_digest": self.expected_build_recipe_digest,
                "postgres_binary_path": (
                    str(self.postgres_binary_path) if self.postgres_binary_path else None
                ),
                "environment_description": self.environment_description,
                "candidate_catalog_path": (
                    str(self.candidate_catalog_path) if self.candidate_catalog_path else None
                ),
                "benchmark": self.benchmark,
                "dataset_provenance_path": (
                    str(self.dataset_provenance_path) if self.dataset_provenance_path else None
                ),
                "stability_repetitions": self.stability_repetitions,
            },
            sort_keys=True,
            separators=(",", ":"),
        )

    @classmethod
    def load(cls, path: Path) -> CalibrationConfig:
        raw: dict[str, Any] = json.loads(path.read_text())
        if raw.get("schema_version") != 1:
            raise ValueError("unsupported calibration config schema_version")
        database = raw["database"]
        env_name = database.get("calibration_dsn_env")
        if env_name:
            try:
                dsn = os.environ[str(env_name)]
            except KeyError as error:
                raise ValueError(f"missing calibration DSN environment variable: {env_name}") from error
        else:
            dsn = str(database["calibration_dsn"])
        target = int(raw["statistics_target"])
        validate_global_statistics_target(target, field="statistics_target")
        repetitions = int(raw.get("repetitions", 9))
        if repetitions < 2:
            raise ValueError("calibration requires at least two measured repetitions")
        columns = tuple(map(str, raw.get("columns", ())))
        if columns and (len(columns) < 3 or len(columns) != len(set(columns))):
            raise ValueError("columns must contain at least three unique names")
        levels = tuple(sorted(set(map(int, raw.get("count_levels", ())))))
        if levels and (levels[0] <= 0 or len(levels) < 2):
            raise ValueError("count_levels must contain at least two positive levels")
        gates = raw.get("gates", {})
        parsed_gates = CalibrationGates(
            float(gates.get("max_cv", 0.10)),
            float(gates.get("min_r_squared", 0.95)),
            float(gates.get("max_heldout_relative_error", 0.15)),
            float(gates.get("max_same_count_subset_cv", 0.10)),
        )
        if any(value <= 0 for value in (
            parsed_gates.max_cv,
            parsed_gates.min_r_squared,
            parsed_gates.max_heldout_relative_error,
            parsed_gates.max_same_count_subset_cv,
        )):
            raise ValueError("calibration gate thresholds must be positive")
        subsets = int(raw.get("subsets_per_count", 1))
        if subsets < 1:
            raise ValueError("subsets_per_count must be positive")
        build_path = raw.get("build_provenance_path")
        binary_path = raw.get("postgres_binary_path")
        catalog_path = raw.get("candidate_catalog_path")
        dataset_path = raw.get("dataset_provenance_path")
        stability_repetitions = raw.get("stability_repetitions")
        if stability_repetitions is not None and int(stability_repetitions) < 2:
            raise ValueError("stability_repetitions must be at least two")
        return cls(
            1,
            dsn,
            str(raw["relation"]),
            columns,
            target,
            repetitions,
            int(raw.get("seed", 20260928)),
            Path(raw["output_path"]),
            levels,
            parsed_gates,
            subsets,
            raw.get("require_postgres_version"),
            Path(build_path) if build_path else None,
            raw.get("expected_build_recipe_digest"),
            Path(binary_path) if binary_path else None,
            raw.get("environment_description"),
            Path(catalog_path) if catalog_path else None,
            raw.get("benchmark"),
            Path(dataset_path) if dataset_path else None,
            int(stability_repetitions) if stability_repetitions is not None else None,
        )
