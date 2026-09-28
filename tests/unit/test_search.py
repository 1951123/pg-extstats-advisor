from __future__ import annotations

from dataclasses import dataclass

from pg_extstats_advisor.candidates.model import CandidateCatalog
from pg_extstats_advisor.cost.model import MaintenanceBudget
from pg_extstats_advisor.cost.preset import PresetMaintenanceCostModel
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
from pg_extstats_advisor.search.deterministic import DeterministicBudgetSearch
from pg_extstats_advisor.search.model import SearchConfig

Q = QueryId("q")


@dataclass
class LandscapeEvaluator:
    objectives: dict[tuple[str, ...], float]
    full_calls: int = 0
    move_calls: int = 0
    planner_calls: int = 0

    def _state(self, design: Design, affected: tuple[QueryId, ...]) -> EvaluationState:
        objective = self.objectives[tuple(design.candidate_ids)]
        evaluation = QueryEvaluation(Q, objective, 1, objective, "fixture")
        return EvaluationState(
            design,
            (evaluation,),
            objective,
            "repository",
            "workload",
            "16.14",
            "fixture",
            affected,
            () if affected else (Q,),
        )

    def evaluate_design(self, design: Design) -> EvaluationState:
        self.full_calls += 1
        self.planner_calls += 1
        return self._state(design, (Q,))

    def evaluate_move(
        self, current_design: Design, move: Move, current_state: EvaluationState
    ) -> EvaluationState:
        del current_design, move, current_state
        raise AssertionError("bound evaluator must provide catalog-aware move application")


class CatalogLandscapeEvaluator(LandscapeEvaluator):
    def __init__(self, objectives: dict[tuple[str, ...], float], catalog: CandidateCatalog):
        super().__init__(objectives)
        self.catalog = catalog

    def evaluate_move(
        self, current_design: Design, move: Move, current_state: EvaluationState
    ) -> EvaluationState:
        assert current_state.design == current_design
        self.move_calls += 1
        self.planner_calls += 1
        return self._state(self.catalog.apply_move(current_design, move), (Q,))


def fixture() -> tuple[CandidateCatalog, dict[tuple[str, ...], float]]:
    candidates = tuple(
        Candidate(CandidateId(name), 10, "r", MechanismKind.MCV, ("a", "b"), (), rank, 100 + rank)
        for rank, name in enumerate("ABCD")
    )
    objectives = {
        (): 100,
        ("A",): 60,
        ("B",): 70,
        ("C",): 80,
        ("D",): 90,
        ("A", "B"): 50,
        ("A", "C"): 55,
        ("A", "D"): 58,
        ("B", "C"): 40,
        ("B", "D"): 65,
        ("C", "D"): 30,
        ("A", "B", "C"): 45,
        ("A", "B", "D"): 49,
        ("A", "C", "D"): 32,
        ("B", "C", "D"): 35,
        ("A", "B", "C", "D"): 44,
    }
    return CandidateCatalog(candidates), objectives


def run(budget: int, full_reference: bool = False):
    catalog, objectives = fixture()
    evaluator = CatalogLandscapeEvaluator(objectives, catalog)
    model = PresetMaintenanceCostModel(0, 1, 0, 1)
    search = DeterministicBudgetSearch(
        evaluator,
        catalog,
        model,
        MaintenanceBudget(budget, model.unit),
        SearchConfig(full_reference=full_reference),
    )
    return search.run(), evaluator


def accepted_signature(result) -> tuple:
    return tuple(
        (record.move, record.after_design, record.after_objective)
        for record in result.trajectory
        if record.accepted
    )


def test_zero_intermediate_loose_budgets_and_precheck() -> None:
    zero, zero_evaluator = run(0)
    assert zero.final_design == Design(())
    assert zero.selected_maintenance_cost == 0
    assert zero_evaluator.move_calls == 0
    assert zero.infeasible_moves_skipped_count > 0

    intermediate, intermediate_evaluator = run(4)
    assert intermediate.selected_maintenance_cost <= 4
    assert intermediate.infeasible_moves_skipped_count > 0
    assert intermediate_evaluator.move_calls == intermediate.evaluated_moves_count
    assert "swap" in [
        record.move.kind.value for record in intermediate.trajectory if record.accepted
    ]

    loose, _ = run(10)
    assert loose.final_design != Design(tuple(CandidateId(item) for item in "ABCD"))
    assert loose.selected_objective == 30


def test_local_drop_swap_contextual_and_repeatable() -> None:
    first, _ = run(6)
    second, _ = run(6)
    assert first == second
    accepted = [record.move.kind.value for record in first.trajectory if record.accepted]
    assert "drop" in accepted
    assert first.final_design == Design((CandidateId("C"), CandidateId("D")))


def test_same_search_instance_can_run_twice_without_accumulating_state() -> None:
    catalog, objectives = fixture()
    evaluator = CatalogLandscapeEvaluator(objectives, catalog)
    model = PresetMaintenanceCostModel(0, 1, 0, 1)
    search = DeterministicBudgetSearch(
        evaluator,
        catalog,
        model,
        MaintenanceBudget(6, model.unit),
    )

    first = search.run()
    first_fixture_calls = evaluator.full_calls + evaluator.move_calls
    second = search.run()

    assert second.trajectory == first.trajectory
    assert second.selected_design == first.selected_design
    assert second.selected_objective == first.selected_objective
    assert second.selected_maintenance_cost == first.selected_maintenance_cost
    assert second.evaluated_moves_count == first.evaluated_moves_count
    assert second.infeasible_moves_skipped_count == first.infeasible_moves_skipped_count
    assert second.evaluator_calls_count == first.evaluator_calls_count
    assert second.accepted_moves_count == first.accepted_moves_count
    assert second.termination_reason == first.termination_reason
    assert evaluator.full_calls + evaluator.move_calls == 2 * first_fixture_calls


def test_query_local_and_full_reference_choose_same_trajectory() -> None:
    local, _ = run(6, full_reference=False)
    reference, _ = run(6, full_reference=True)
    assert accepted_signature(local) == accepted_signature(reference)
    assert local.final_design == reference.final_design
    assert local.selected_objective == reference.selected_objective
