"""Deterministic search records and evaluator protocol."""

from __future__ import annotations

import hashlib
import json
from dataclasses import dataclass
from decimal import Decimal
from typing import Protocol

from pg_extstats_advisor.cost.model import MaintenanceBudget
from pg_extstats_advisor.models import Candidate, Design, EvaluationState, Move
from pg_extstats_advisor.optimization.budget import OptimizationStatus
from pg_extstats_advisor.statistics import (
    DEFAULT_GLOBAL_STATISTICS_TARGET,
    validate_global_statistics_target,
)


class DesignEvaluator(Protocol):
    def evaluate_design(self, design: Design) -> EvaluationState: ...

    def evaluate_move(
        self, current_design: Design, move: Move, current_state: EvaluationState
    ) -> EvaluationState: ...


@dataclass(frozen=True, slots=True)
class SearchConfig:
    algorithm_version: str = "contextual-greedy-local-v1"
    full_reference: bool = False
    exact_bound_pruning: bool = True
    record_pruned_moves: bool = False
    add_only: bool = False
    candidate_set_mode: str = "full"
    candidate_set_digest: str | None = None
    singleton_profile_digest: str | None = None
    visible_candidate_count: int | None = None
    budget_mode: str = "absolute"
    global_statistics_target: int = DEFAULT_GLOBAL_STATISTICS_TARGET

    def __post_init__(self) -> None:
        validate_global_statistics_target(self.global_statistics_target)


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
    lower_bound: float | None = None
    incumbent_objective: float | None = None


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
    total_neighbor_moves_considered: int = 0
    bound_pruned_no_improvement_count: int = 0
    bound_pruned_incumbent_count: int = 0
    optimization_status: OptimizationStatus = OptimizationStatus.LOCAL_OPTIMUM
    optimization_budget_seconds: float | None = None
    optimization_elapsed_seconds: float | None = None
    optimization_budget_exhausted: bool = False
    optimization_stop_phase: str | None = None
    optimization_stop_reason: str | None = None
    planner_calls_completed: int = 0
    baseline_planner_calls: int = 0
    singleton_planner_calls: int = 0
    greedy_planner_calls: int = 0
    phase_elapsed_seconds: tuple[tuple[str, float], ...] = ()


@dataclass(frozen=True, slots=True)
class OptimizationSearchOutcome:
    """Bounded search outcome, including a missing state for baseline timeout."""

    status: OptimizationStatus
    search_result: SearchResult | None
    budget_seconds: float
    elapsed_seconds: float
    budget_exhausted: bool
    stop_phase: str | None
    stop_reason: str | None
    planner_calls_completed: int
    baseline_planner_calls: int
    singleton_planner_calls: int
    greedy_planner_calls: int
    phase_elapsed_seconds: tuple[tuple[str, float], ...]


def candidate_catalog_digest(candidates: list[Candidate] | tuple[Candidate, ...]) -> str:
    value = [
        {
            "candidate_id": item.candidate_id,
            "relation_oid": item.relation_oid,
            "relation_name": item.relation_name,
            "mechanism": item.mechanism.value,
            "attributes": item.attributes,
            "definition": item.definition,
            "precedence_rank": item.precedence_rank,
        }
        for item in candidates
    ]
    encoded = json.dumps(value, sort_keys=True, separators=(",", ":")).encode()
    return hashlib.sha256(encoded).hexdigest()
