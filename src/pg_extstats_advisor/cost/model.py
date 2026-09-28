"""Maintenance-cost interface and typed budget."""

from __future__ import annotations

from abc import ABC, abstractmethod
from dataclasses import dataclass
from decimal import Decimal

from pg_extstats_advisor.candidates.model import CandidateCatalog
from pg_extstats_advisor.models import Candidate, Design


def validated_decimal(value: Decimal | int | str, label: str) -> Decimal:
    result = Decimal(value)
    if not result.is_finite() or result < 0:
        raise ValueError(f"{label} must be finite and non-negative")
    return result


@dataclass(frozen=True, slots=True)
class MaintenanceBudget:
    value: Decimal
    unit: str

    def __post_init__(self) -> None:
        object.__setattr__(self, "value", validated_decimal(self.value, "budget"))
        if not self.unit:
            raise ValueError("budget unit is required")


class MaintenanceCostModel(ABC):
    @property
    @abstractmethod
    def unit(self) -> str: ...

    @property
    @abstractmethod
    def digest(self) -> str: ...

    @abstractmethod
    def estimate_candidate(self, candidate: Candidate) -> Decimal: ...

    def estimate_design(self, design: Design, catalog: CandidateCatalog) -> Decimal:
        catalog.validate_design(design)
        return sum(
            (self.estimate_candidate(catalog.by_id[item]) for item in design.candidate_ids),
            Decimal(0),
        )

    def is_feasible(
        self, design: Design, catalog: CandidateCatalog, budget: MaintenanceBudget
    ) -> bool:
        if budget.unit != self.unit:
            raise ValueError(f"budget unit {budget.unit!r} does not match model unit {self.unit!r}")
        return self.estimate_design(design, catalog) <= budget.value
