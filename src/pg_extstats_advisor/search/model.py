"""Deterministic search records and evaluator protocol."""

from __future__ import annotations

import hashlib
import json
from dataclasses import dataclass
from decimal import Decimal
from typing import Protocol

from pg_extstats_advisor.cost.model import MaintenanceBudget
from pg_extstats_advisor.models import Design, EvaluationState, Move


class DesignEvaluator(Protocol):
    def evaluate_design(self, design: Design) -> EvaluationState: ...

    def evaluate_move(
        self, current_design: Design, move: Move, current_state: EvaluationState
    ) -> EvaluationState: ...


@dataclass(frozen=True, slots=True)
class SearchConfig:
    algorithm_version: str = "contextual-greedy-local-v1"
    full_reference: bool = False


@dataclass(frozen=True, slots=True)
class MoveRecord:
    phase: str
    move: Move
    before_design: Design
    after_design: Design
    before_objective: float
    after_objective: float | None
    before_cost: Decimal
    after_cost: Decimal
    accepted: bool
    rejection_reason: str | None
    affected_query_count: int | None


@dataclass(frozen=True, slots=True)
class SearchResult:
    selected_state: EvaluationState
    selected_design: Design
    selected_objective: float
    selected_maintenance_cost: Decimal
    budget: MaintenanceBudget
    cost_model_digest: str
    initial_design: Design
    final_design: Design
    trajectory: tuple[MoveRecord, ...]
    evaluated_moves_count: int
    infeasible_moves_skipped_count: int
    evaluator_calls_count: int
    accepted_moves_count: int
    termination_reason: str
    config: SearchConfig
    workload_digest: str
    repository_digest: str
    candidate_catalog_digest: str


def candidate_catalog_digest(catalog_items: list[tuple[str, int]]) -> str:
    encoded = json.dumps(catalog_items, separators=(",", ":")).encode()
    return hashlib.sha256(encoded).hexdigest()
