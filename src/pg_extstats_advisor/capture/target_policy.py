"""Versioned global statistics-target policy for production captures.

M2.27a deliberately treats the target as a database/advisor-run-wide input.
It is not a candidate, relation-local setting, or inner-search variable.
"""

from __future__ import annotations

import hashlib
from collections.abc import Iterable
from dataclasses import dataclass
from typing import Any

from pg_extstats_advisor.capture.manifest import canonical_digest


class TargetPolicyError(ValueError):
    """Raised when a target grid or override evidence is unsafe to use."""


@dataclass(frozen=True)
class TargetGridPolicy:
    allowed_targets: tuple[int, ...] = (100, 300, 1000)
    realization_ids: tuple[str, ...] = ("A", "B", "C")
    canonical_realization_id: str = "A"
    replicas_per_target: int = 3
    scope: str = "database/advisor_run"
    sample_capacity_multiplier: int = 300
    native_analyze_equivalent: bool = False

    def __post_init__(self) -> None:
        targets = tuple(int(value) for value in self.allowed_targets)
        labels = tuple(str(value) for value in self.realization_ids)
        if not targets or any(value <= 0 or value > 10000 for value in targets):
            raise TargetPolicyError("allowed statistics targets must be in 1..10000")
        if targets != tuple(sorted(set(targets))):
            raise TargetPolicyError("allowed statistics targets must be sorted and unique")
        if labels != tuple(dict.fromkeys(labels)) or not labels:
            raise TargetPolicyError("realization IDs must be non-empty and unique")
        if self.canonical_realization_id not in labels:
            raise TargetPolicyError("canonical realization must be in realization IDs")
        if self.replicas_per_target != len(labels):
            raise TargetPolicyError("replicas_per_target must match realization IDs")
        if self.sample_capacity_multiplier <= 0:
            raise TargetPolicyError("sample capacity multiplier must be positive")
        if self.native_analyze_equivalent:
            raise TargetPolicyError("M2.27a reservoir capture cannot claim native equivalence")

    def as_dict(self) -> dict[str, Any]:
        return {
            "allowed_targets": list(self.allowed_targets),
            "realization_ids": list(self.realization_ids),
            "canonical_realization_id": self.canonical_realization_id,
            "replicas_per_target": self.replicas_per_target,
            "scope": self.scope,
            "sample_capacity_policy": {
                "kind": "target_times_multiplier",
                "multiplier": self.sample_capacity_multiplier,
                "native_analyze_equivalent": self.native_analyze_equivalent,
            },
        }

    @property
    def digest(self) -> str:
        return canonical_digest(self.as_dict())

    def expected_cells(self) -> tuple[tuple[int, str], ...]:
        return tuple((target, label) for target in self.allowed_targets for label in self.realization_ids)

    def seed(self, target: int, realization: str) -> int:
        if target not in self.allowed_targets or realization not in self.realization_ids:
            raise TargetPolicyError("target/realization is outside the frozen grid")
        raw = hashlib.sha256(f"dmv-m2-27a-v2|{target}|{realization}".encode()).digest()
        return int.from_bytes(raw[:8], "big")


@dataclass(frozen=True)
class TargetOverride:
    kind: str
    relation: str
    name: str
    value: int

    def as_dict(self) -> dict[str, Any]:
        return {"kind": self.kind, "relation": self.relation, "name": self.name, "value": self.value}


def inspect_override_rows(
    column_rows: Iterable[tuple[str, str, str, int]],
    extstat_rows: Iterable[tuple[str, str, str, int]],
    relevant_relations: set[str],
    relevant_columns: dict[str, set[str]] | None = None,
) -> list[TargetOverride]:
    """Return only explicit non-default overrides in the relevant scope.

    PostgreSQL uses ``-1`` for the inherited/default target.  Unrelated
    relations are intentionally ignored; relevant explicit values fail closed.
    """
    relevant_columns = relevant_columns or {}
    found: list[TargetOverride] = []
    for schema, relation, column, value in column_rows:
        relation_id = f"{schema}.{relation}"
        if relation_id not in relevant_relations:
            continue
        allowed = relevant_columns.get(relation_id)
        if allowed is not None and column not in allowed:
            continue
        if int(value) >= 0:
            found.append(TargetOverride("column", relation_id, column, int(value)))
    for schema, relation, object_name, value in extstat_rows:
        relation_id = f"{schema}.{relation}"
        if relation_id in relevant_relations and int(value) >= 0:
            found.append(TargetOverride("extended_statistics", relation_id, object_name, int(value)))
    return found


def validate_override_rows(*args: Any, **kwargs: Any) -> None:
    overrides = inspect_override_rows(*args, **kwargs)
    if overrides:
        detail = "; ".join(
            f"{item.kind} {item.relation}.{item.name}={item.value}" for item in overrides
        )
        raise TargetPolicyError(f"explicit statistics-target override(s) in scope: {detail}")
