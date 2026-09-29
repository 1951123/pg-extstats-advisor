"""Compact, sealed recommendation bundle for the fixed-T core workflow."""

from __future__ import annotations

import json
from dataclasses import dataclass
from pathlib import Path
from typing import Any

from pg_extstats_advisor.capture.manifest import canonical_digest
from pg_extstats_advisor.statistics import validate_global_statistics_target

RECOMMENDATION_BUNDLE_VERSION = 1


def selected_design_digest(candidate_ids: tuple[str, ...] | list[str]) -> str:
    return canonical_digest(list(candidate_ids))


@dataclass(frozen=True, slots=True)
class RecommendationBundle:
    evaluated_statistics_target: int
    problem_provenance: dict[str, Any]
    acquisition_identity: dict[str, Any]
    workload_identity: dict[str, Any]
    truth_identity: dict[str, Any]
    candidate_catalog_digest: str
    maintenance_model_digest: str
    baseline_objective: float
    final_objective: float
    selected_design: tuple[str, ...]
    selected_maintenance_cost: str
    search_metadata: dict[str, Any]
    deployment_ddl: tuple[str, ...]
    rollback_ddl: tuple[str, ...]
    compatibility: dict[str, Any]
    selected_design_digest: str | None = None

    def __post_init__(self) -> None:
        target = validate_global_statistics_target(self.evaluated_statistics_target)
        object.__setattr__(self, "evaluated_statistics_target", target)
        if not self.candidate_catalog_digest or not self.maintenance_model_digest:
            raise ValueError("recommendation bundle lineage digests are required")
        if len(set(self.selected_design)) != len(self.selected_design):
            raise ValueError("recommendation selected design contains duplicates")
        expected = selected_design_digest(self.selected_design)
        if self.selected_design_digest not in (None, expected):
            raise ValueError("recommendation selected design digest mismatch")
        object.__setattr__(self, "selected_design_digest", expected)
        if not self.deployment_ddl:
            raise ValueError("recommendation deployment DDL is required")
        if any("ALTER DATABASE" in statement.upper() for statement in self.deployment_ddl):
            raise ValueError("recommendation must not mutate database target configuration")
        if len(set(self.deployment_ddl)) != len(self.deployment_ddl):
            raise ValueError("recommendation deployment DDL contains duplicates")
        if len(set(self.rollback_ddl)) != len(self.rollback_ddl):
            raise ValueError("recommendation rollback DDL contains duplicates")

    @property
    def digest(self) -> str:
        return canonical_digest(self.as_dict(include_digest=False))

    def as_dict(self, *, include_digest: bool = True) -> dict[str, Any]:
        value: dict[str, Any] = {
            "format_version": RECOMMENDATION_BUNDLE_VERSION,
            "bundle_type": "fixed-t-recommendation-v1",
            "evaluated_statistics_target": self.evaluated_statistics_target,
            "statistics_target_role": "evaluated_external_configuration",
            "problem_provenance": self.problem_provenance,
            "acquisition_identity": self.acquisition_identity,
            "workload_identity": self.workload_identity,
            "truth_identity": self.truth_identity,
            "candidate_catalog_digest": self.candidate_catalog_digest,
            "maintenance_model_digest": self.maintenance_model_digest,
            "baseline_objective": self.baseline_objective,
            "final_objective": self.final_objective,
            "selected_design": list(self.selected_design),
            "selected_design_digest": self.selected_design_digest,
            "selected_maintenance_cost": self.selected_maintenance_cost,
            "search_metadata": self.search_metadata,
            "deployment_ddl": list(self.deployment_ddl),
            "rollback_ddl": list(self.rollback_ddl),
            "compatibility": self.compatibility,
        }
        if include_digest:
            value["digest"] = self.digest
        return value

    def write(self, path: Path) -> str:
        path.parent.mkdir(parents=True, exist_ok=True)
        path.write_text(json.dumps(self.as_dict(), sort_keys=True, indent=2) + "\n")
        return self.digest

    @classmethod
    def load(cls, path: Path) -> RecommendationBundle:
        raw = json.loads(Path(path).read_text())
        if raw.get("format_version") != RECOMMENDATION_BUNDLE_VERSION:
            raise ValueError("unsupported recommendation bundle version")
        stored = raw.pop("digest", None)
        if stored is None or canonical_digest(raw) != stored:
            raise ValueError("recommendation bundle digest mismatch")
        raw.pop("bundle_type", None)
        return cls(
            int(raw["evaluated_statistics_target"]),
            dict(raw["problem_provenance"]),
            dict(raw["acquisition_identity"]),
            dict(raw["workload_identity"]),
            dict(raw["truth_identity"]),
            str(raw["candidate_catalog_digest"]),
            str(raw["maintenance_model_digest"]),
            float(raw["baseline_objective"]),
            float(raw["final_objective"]),
            tuple(map(str, raw["selected_design"])),
            str(raw["selected_maintenance_cost"]),
            dict(raw["search_metadata"]),
            tuple(map(str, raw["deployment_ddl"])),
            tuple(map(str, raw["rollback_ddl"])),
            dict(raw["compatibility"]),
            str(raw["selected_design_digest"]),
        )


def validate_recommendation_bundle(
    path: Path,
    *,
    candidate_ids: set[str] | None = None,
    expected_target: int | None = None,
) -> dict[str, Any]:
    bundle = RecommendationBundle.load(path)
    if expected_target is not None and bundle.evaluated_statistics_target != validate_global_statistics_target(expected_target):
        raise ValueError("recommendation target mismatch")
    if candidate_ids is not None and not set(bundle.selected_design).issubset(candidate_ids):
        raise ValueError("recommendation references unknown candidate")
    allowed_deploy_prefixes = ("CREATE STATISTICS", "ALTER STATISTICS", "ANALYZE")
    if not all(statement.upper().startswith(allowed_deploy_prefixes) for statement in bundle.deployment_ddl):
        raise ValueError("recommendation contains non-standard deployment DDL")
    if not all(statement.upper().startswith("DROP STATISTICS IF EXISTS") for statement in bundle.rollback_ddl):
        raise ValueError("recommendation contains non-standard rollback DDL")
    return bundle.as_dict()


__all__ = [
    "RECOMMENDATION_BUNDLE_VERSION",
    "RecommendationBundle",
    "selected_design_digest",
    "validate_recommendation_bundle",
]
