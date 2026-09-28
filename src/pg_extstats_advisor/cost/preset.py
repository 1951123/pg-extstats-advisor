"""Serializable development-only additive maintenance-cost model."""

from __future__ import annotations

import hashlib
import json
from dataclasses import dataclass
from decimal import Decimal

from pg_extstats_advisor.cost.model import MaintenanceCostModel, validated_decimal
from pg_extstats_advisor.models import Candidate, MechanismKind


@dataclass(frozen=True, slots=True)
class CostModelProvenance:
    model_type: str
    model_version: str
    unit: str
    parameters: tuple[tuple[str, str], ...]
    fitting_provenance: str
    feature_schema: tuple[str, ...]

    @property
    def digest(self) -> str:
        encoded = json.dumps(
            {
                "model_type": self.model_type,
                "model_version": self.model_version,
                "unit": self.unit,
                "parameters": dict(self.parameters),
                "fitting_provenance": self.fitting_provenance,
                "feature_schema": self.feature_schema,
            },
            sort_keys=True,
            separators=(",", ":"),
        ).encode()
        return hashlib.sha256(encoded).hexdigest()


@dataclass(frozen=True, slots=True)
class PresetMaintenanceCostModel(MaintenanceCostModel):
    base_mcv: Decimal
    per_column_mcv: Decimal
    base_fd: Decimal
    per_column_fd: Decimal
    _unit: str = "maintenance-cost-unit"
    model_version: str = "preset-additive-v1"
    fitting_provenance: str = "preset-development"

    def __post_init__(self) -> None:
        for field in ("base_mcv", "per_column_mcv", "base_fd", "per_column_fd"):
            object.__setattr__(self, field, validated_decimal(getattr(self, field), field))
        if not self._unit:
            raise ValueError("cost-model unit is required")

    @property
    def unit(self) -> str:
        return self._unit

    @property
    def provenance(self) -> CostModelProvenance:
        return CostModelProvenance(
            model_type="preset-additive",
            model_version=self.model_version,
            unit=self.unit,
            parameters=tuple(
                sorted(
                    {
                        "base_mcv": str(self.base_mcv),
                        "per_column_mcv": str(self.per_column_mcv),
                        "base_fd": str(self.base_fd),
                        "per_column_fd": str(self.per_column_fd),
                    }.items()
                )
            ),
            fitting_provenance=self.fitting_provenance,
            feature_schema=("mechanism", "arity"),
        )

    @property
    def digest(self) -> str:
        return self.provenance.digest

    def estimate_candidate(self, candidate: Candidate) -> Decimal:
        arity = Decimal(len(candidate.attributes))
        if candidate.mechanism is MechanismKind.MCV:
            return self.base_mcv + self.per_column_mcv * arity
        return self.base_fd + self.per_column_fd * arity
