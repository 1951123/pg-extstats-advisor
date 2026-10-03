from __future__ import annotations

from dataclasses import dataclass

from pg_extstats_advisor.candidates.model import CandidateCatalog
from pg_extstats_advisor.cost.model import MaintenanceBudget
from pg_extstats_advisor.cost.preset import PresetMaintenanceCostModel
from pg_extstats_advisor.incidence.index import IncidenceIndex
from pg_extstats_advisor.models import (
    Candidate,
    CandidateId,
    Design,
    EvaluationState,
    MechanismKind,
    Move,
    QueryEvaluation,
    QueryId,
)
from pg_extstats_advisor.optimization.budget import OptimizationBudget, OptimizationStatus
from pg_extstats_advisor.screening import profile_singletons_native
from pg_extstats_advisor.search.deterministic import DeterministicBudgetSearch
from pg_extstats_advisor.search.model import SearchConfig


class FakeClock:
    def __init__(self) -> None:
        self.value = 0.0

    def __call__(self) -> float:
        return self.value

    def advance(self, seconds: float = 1.0) -> None:
        self.value += seconds


def _catalog(names: str = "ABC") -> CandidateCatalog:
    return CandidateCatalog(
        tuple(
            Candidate(
                CandidateId(name),
                1,
                "public.t",
                MechanismKind.MCV,
                ("a", "b"),
                (),
                rank,
                100 + rank,
            )
            for rank, name in enumerate(names)
        )
    )


@dataclass
class FakeEvaluator:
    catalog: CandidateCatalog
    clock: FakeClock
    per_query_steps: int = 1
    planner_calls_total: int = 0
    budget: OptimizationBudget | None = None
    phase: str = "baseline"

    def __post_init__(self) -> None:
        self.incidence = IncidenceIndex(
            tuple((candidate.candidate_id, frozenset({QueryId("q")})) for candidate in self.catalog.candidates),
            frozenset({QueryId("q")}),
        )

    def bind_optimization_budget(self, budget: OptimizationBudget | None, phase: str) -> None:
        self.budget, self.phase = budget, phase

    def _tick(self) -> None:
        for _ in range(self.per_query_steps):
            if self.budget is not None:
                self.budget.check(self.phase)
            self.clock.advance()
            self.planner_calls_total += 1
            if self.budget is not None:
                self.budget.check(self.phase)

    def _state(self, design: Design) -> EvaluationState:
        # A strictly improving design is useful for testing accepted-state
        # preservation without requiring PostgreSQL.
        objective = 10.0 - len(design.candidate_ids)
        evaluation = QueryEvaluation(QueryId("q"), objective, 1.0, objective, "fake")
        return EvaluationState(
            design,
            (evaluation,),
            objective,
            "repo",
            "workload",
            "16.14",
            "fake",
            (QueryId("q"),),
            (),
        )

    def evaluate_design(self, design: Design) -> EvaluationState:
        self._tick()
        return self._state(design)

    def evaluate_move(self, current_design: Design, move: Move, current_state: EvaluationState) -> EvaluationState:
        del current_state
        self._tick()
        return self._state(self.catalog.apply_move(current_design, move))


def _search(evaluator: FakeEvaluator, budget: OptimizationBudget, *, initial: EvaluationState | None = None):
    model = PresetMaintenanceCostModel(0, 1, 0, 1)
    search = DeterministicBudgetSearch(
        evaluator,
        evaluator.catalog,
        model,
        MaintenanceBudget(99, model.unit),
        config=SearchConfig(add_only=True, exact_bound_pruning=False),
    )
    return search.run_bounded(budget, initial_state=initial)


def test_budget_sufficient_and_monotonic_clock_are_deterministic() -> None:
    clock = FakeClock()
    budget = OptimizationBudget(100, clock=clock)
    budget.start()
    assert budget.start_monotonic == 0.0
    clock.advance(3.5)
    assert budget.elapsed_seconds == 3.5
    assert budget.remaining_seconds == 96.5
    evaluator = FakeEvaluator(_catalog("AB"), clock)
    baseline = evaluator.evaluate_design(Design(()))
    profile = profile_singletons_native(evaluator, list(evaluator.catalog.candidates), baseline, budget=budget)
    assert profile["singleton_profile_complete"] is True
    assert profile["status"] == OptimizationStatus.COMPLETED.value


def test_sufficient_budget_reaches_greedy_local_optimum() -> None:
    clock = FakeClock()
    evaluator = FakeEvaluator(_catalog("AB"), clock)
    baseline = evaluator._state(Design(()))
    outcome = _search(evaluator, OptimizationBudget(100, clock=clock), initial=baseline)
    assert outcome.status is OptimizationStatus.LOCAL_OPTIMUM
    assert outcome.search_result is not None
    assert outcome.search_result.selected_design == Design((CandidateId("A"), CandidateId("B")))


def test_expire_during_baseline_is_structured() -> None:
    clock = FakeClock()
    evaluator = FakeEvaluator(_catalog("A"), clock)
    outcome = _search(evaluator, OptimizationBudget(0, clock=clock))
    assert outcome.status is OptimizationStatus.BUDGET_EXHAUSTED_DURING_BASELINE
    assert outcome.search_result is None


def test_expire_mid_singleton_preserves_partial_profile_without_precedence() -> None:
    clock = FakeClock()
    evaluator = FakeEvaluator(_catalog("ABC"), clock)
    baseline = evaluator.evaluate_design(Design(()))
    budget = OptimizationBudget(1.5, clock=clock)
    budget.start()
    profile = profile_singletons_native(evaluator, list(evaluator.catalog.candidates), baseline, budget=budget)
    assert profile["status"] == OptimizationStatus.BUDGET_EXHAUSTED_DURING_SINGLETON.value
    assert profile["completed_singleton_candidates"] == 1
    assert profile["remaining_singleton_candidates"] == 2
    assert "precedence" not in profile


def test_expire_between_contextual_candidates_returns_last_accepted_state() -> None:
    clock = FakeClock()
    evaluator = FakeEvaluator(_catalog("ABC"), clock)
    baseline = evaluator.evaluate_design(Design(()))
    # Three ADD evaluations complete the first round and accept A; the first
    # move of the next round reaches the deadline before it can commit.
    budget = OptimizationBudget(4.5, clock=clock)
    outcome = _search(evaluator, budget, initial=baseline)
    assert outcome.status is OptimizationStatus.BUDGET_EXHAUSTED_DURING_GREEDY
    assert outcome.search_result is not None
    assert outcome.search_result.selected_design == Design((CandidateId("A"),))
    assert outcome.search_result.accepted_moves_count == 1


def test_expire_mid_contextual_workload_cannot_accept_move() -> None:
    clock = FakeClock()
    evaluator = FakeEvaluator(_catalog("A"), clock, per_query_steps=3)
    baseline = evaluator._state(Design(()))
    budget = OptimizationBudget(1.5, clock=clock)
    outcome = _search(evaluator, budget, initial=baseline)
    assert outcome.status is OptimizationStatus.BUDGET_EXHAUSTED_DURING_GREEDY
    assert outcome.search_result is not None
    assert outcome.search_result.selected_design == Design(())
    assert outcome.search_result.accepted_moves_count == 0
