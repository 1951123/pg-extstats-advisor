"""Frozen aggregate mechanism-count maintenance model."""

from __future__ import annotations

import hashlib
import json
from dataclasses import dataclass
from decimal import Decimal
from pathlib import Path
from typing import Any

from pg_extstats_advisor.cost.model import MaintenanceCostModel, validated_decimal
from pg_extstats_advisor.models import Candidate, MechanismKind

MODEL_TYPE = "empirical-mechanism-count-v1"
MODEL_UNIT = "milliseconds-per-analyze"


def artifact_digest(value: dict[str, Any]) -> str:
    canonical = {key: item for key, item in value.items() if key != "digest"}
    return hashlib.sha256(
        json.dumps(canonical, sort_keys=True, separators=(",", ":")).encode()
    ).hexdigest()


@dataclass(frozen=True, slots=True)
class EmpiricalMechanismCountCostModel(MaintenanceCostModel):
    mcv_ms_per_object: Decimal
    fd_ms_per_object: Decimal
    statistics_target: int
    candidate_arity: int
    artifact: tuple[tuple[str, Any], ...]
    _digest: str

    def __post_init__(self) -> None:
        object.__setattr__(
            self,
            "mcv_ms_per_object",
            validated_decimal(self.mcv_ms_per_object, "mcv_ms_per_object"),
        )
        object.__setattr__(
            self,
            "fd_ms_per_object",
            validated_decimal(self.fd_ms_per_object, "fd_ms_per_object"),
        )
        if not 0 <= self.statistics_target <= 10000:
            raise ValueError("statistics_target must be between 0 and 10000")
        if self.candidate_arity != 2:
            raise ValueError("empirical model candidate_arity must equal 2")

    @property
    def unit(self) -> str:
        return MODEL_UNIT

    @property
    def digest(self) -> str:
        return self._digest

    def validate_runtime(self, statistics_target: int) -> None:
        if statistics_target != self.statistics_target:
            raise ValueError(
                f"statistics target {statistics_target} does not match empirical model target "
                f"{self.statistics_target}"
            )

    def estimate_candidate(self, candidate: Candidate) -> Decimal:
        if len(candidate.attributes) != self.candidate_arity:
            raise ValueError("empirical model only supports arity-2 candidates")
        if candidate.mechanism is MechanismKind.MCV:
            return self.mcv_ms_per_object
        if candidate.mechanism is MechanismKind.FD:
            return self.fd_ms_per_object
        raise ValueError(f"unsupported mechanism: {candidate.mechanism}")

    @classmethod
    def from_artifact(cls, raw: dict[str, Any]) -> EmpiricalMechanismCountCostModel:
        if raw.get("format_version") != 1 or raw.get("model_type") != MODEL_TYPE:
            raise ValueError("unsupported empirical maintenance model artifact")
        if raw.get("status", "accepted") != "accepted":
            raise ValueError("maintenance model artifact is not accepted")
        if raw.get("unit") != MODEL_UNIT:
            raise ValueError("empirical maintenance model unit must be milliseconds-per-analyze")
        expected = artifact_digest(raw)
        if raw.get("digest") != expected:
            raise ValueError("maintenance model digest mismatch")
        parameters = raw["parameters"]
        return cls(
            Decimal(str(parameters["mcv_ms_per_object"])),
            Decimal(str(parameters["fd_ms_per_object"])),
            int(raw["statistics_target"]),
            int(raw["candidate_arity"]),
            tuple(sorted(raw.items())),
            expected,
        )

    @classmethod
    def load(cls, path: Path) -> EmpiricalMechanismCountCostModel:
        return cls.from_artifact(json.loads(path.read_text()))
