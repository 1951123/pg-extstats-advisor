from __future__ import annotations

import random
from dataclasses import dataclass
from itertools import combinations

import pytest

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
from pg_extstats_advisor.search.deterministic import (
    DeterministicBudgetSearch,
    optimistic_objective_lower_bound,
)
from pg_extstats_advisor.search.model import SearchConfig

Q = QueryId("q")
Q2 = QueryId("q2")


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


@dataclass
class TwoQueryLandscapeEvaluator:
    objectives: dict[tuple[str, ...], tuple[float, float]]
    catalog: CandidateCatalog
    incidence: IncidenceIndex
    full_calls: int = 0
    move_calls: int = 0

    def _state(self, design: Design) -> EvaluationState:
        first, second = self.objectives[tuple(design.candidate_ids)]
        evaluations = (
            QueryEvaluation(Q, first, 1, first, "fixture"),
            QueryEvaluation(Q2, second, 1, second, "fixture"),
        )
        affected = tuple(sorted(self.incidence.known_queries))
        return EvaluationState(
            design,
            evaluations,
            first + second,
            "repository",
            "workload",
            "16.14",
            "fixture",
            affected,
            (),
        )

    def evaluate_design(self, design: Design) -> EvaluationState:
        self.full_calls += 1
        return self._state(design)

    def evaluate_move(
        self, current_design: Design, move: Move, current_state: EvaluationState
    ) -> EvaluationState:
        del current_state
        self.move_calls += 1
        return self._state(self.catalog.apply_move(current_design, move))


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


def test_qerror_lower_bound_floor_and_unknown_query_guard() -> None:
    catalog, objectives = fixture()
    evaluator = CatalogLandscapeEvaluator(objectives, catalog)
    state = evaluator.evaluate_design(Design(()))
    assert optimistic_objective_lower_bound(state, (Q,)) == 1.0
    with pytest.raises(ValueError, match="unknown queries"):
        optimistic_objective_lower_bound(state, (QueryId("missing"),))
    invalid = EvaluationState(
        state.design,
        (QueryEvaluation(Q, 0.5, 1, 0.5, "fixture"),),
        0.5,
        state.repository_digest,
        state.workload_digest,
        state.postgres_version,
        state.evaluator_provenance,
        state.affected_query_ids,
        state.reused_query_ids,
    )
    with pytest.raises(ValueError, match="contribution"):
        optimistic_objective_lower_bound(invalid, (Q,))


def test_bound_pruning_no_strict_improvement_and_incumbent() -> None:
    candidates = tuple(
        Candidate(CandidateId(name), 10, "r", MechanismKind.MCV, ("a", "b"), (), rank, 100 + rank)
        for rank, name in enumerate("ABC")
    )
    catalog = CandidateCatalog(candidates)
    objectives = {
        (): (1.0, 1.0),
        ("A",): (1.0, 1.0),
        ("B",): (1.0, 1.0),
        ("C",): (1.0, 1.0),
    }
    incidence = IncidenceIndex(
        (
            (CandidateId("A"), frozenset({Q})),
            (CandidateId("B"), frozenset({Q2})),
            (CandidateId("C"), frozenset({Q, Q2})),
        ),
        frozenset({Q, Q2}),
    )
    evaluator = TwoQueryLandscapeEvaluator(objectives, catalog, incidence)
    model = PresetMaintenanceCostModel(0, 1, 0, 1)
    no_improvement = DeterministicBudgetSearch(
        evaluator,
        catalog,
        model,
        MaintenanceBudget(3, model.unit),
        SearchConfig(record_pruned_moves=True),
    ).run()
    assert no_improvement.final_design == Design(())
    assert no_improvement.bound_pruned_no_improvement_count >= 3
    assert no_improvement.bound_pruned_incumbent_count == 0
    assert no_improvement.evaluated_moves_count == 0
    reasons = {record.rejection_reason for record in no_improvement.trajectory}
    assert "bound-no-strict-improvement" in reasons

    incumbent_objectives = {
        (): (5.0, 5.0),
        ("A",): (2.0, 2.0),
        ("B",): (3.0, 5.0),
        ("C",): (4.0, 5.0),
    }
    incumbent_evaluator = TwoQueryLandscapeEvaluator(
        incumbent_objectives, catalog, incidence
    )
    result = DeterministicBudgetSearch(
        incumbent_evaluator,
        catalog,
        model,
        MaintenanceBudget(2, model.unit),
        SearchConfig(record_pruned_moves=True),
    ).run()
    assert result.final_design == Design((CandidateId("A"),))
    assert result.selected_objective == 4.0
    assert result.bound_pruned_incumbent_count >= 1
    reasons = {record.rejection_reason for record in result.trajectory}
    assert "bound-cannot-beat-incumbent" in reasons


def test_bound_equality_is_not_pruned_and_cost_tie_is_preserved() -> None:
    candidates = tuple(
        Candidate(CandidateId(name), 10, "r", MechanismKind.MCV, ("a", "b"), (), rank, 100 + rank)
        for rank, name in enumerate("AB")
    )
    catalog = CandidateCatalog(candidates)
    objectives = {(): 4.0, ("A",): 2.0, ("B",): 2.0, ("A", "B"): 1.0}
    incidence = IncidenceIndex(
        (
            (CandidateId("A"), frozenset({Q})),
            (CandidateId("B"), frozenset({Q})),
        ),
        frozenset({Q}),
    )
    evaluator = CatalogLandscapeEvaluator(objectives, catalog)
    evaluator.incidence = incidence
    model = PresetMaintenanceCostModel(0, 1, 0, 1)
    result = DeterministicBudgetSearch(
        evaluator,
        catalog,
        model,
        MaintenanceBudget(2, model.unit),
        SearchConfig(record_pruned_moves=True),
    ).run()
    assert result.final_design == Design((CandidateId("A"),))
    assert result.bound_pruned_incumbent_count == 0
    assert result.evaluated_moves_count >= 2


def test_pruned_and_exhaustive_full_search_are_identical_with_conservative_incidence() -> None:
    catalog, objectives = fixture()
    incidence = IncidenceIndex(
        tuple((candidate.candidate_id, frozenset({Q})) for candidate in catalog.candidates),
        frozenset({Q}),
    )
    exhaustive_evaluator = CatalogLandscapeEvaluator(objectives, catalog)
    pruned_evaluator = CatalogLandscapeEvaluator(objectives, catalog)
    exhaustive_evaluator.incidence = incidence
    pruned_evaluator.incidence = incidence
    model = PresetMaintenanceCostModel(0, 1, 0, 1)
    exhaustive = DeterministicBudgetSearch(
        exhaustive_evaluator,
        catalog,
        model,
        MaintenanceBudget(6, model.unit),
        SearchConfig(exact_bound_pruning=False),
    ).run()
    pruned = DeterministicBudgetSearch(
        pruned_evaluator,
        catalog,
        model,
        MaintenanceBudget(6, model.unit),
        SearchConfig(exact_bound_pruning=True),
    ).run()
    assert pruned.selected_design == exhaustive.selected_design
    assert pruned.selected_objective == exhaustive.selected_objective
    assert pruned.selected_maintenance_cost == exhaustive.selected_maintenance_cost
    assert accepted_signature(pruned) == accepted_signature(exhaustive)
    assert pruned.termination_reason == exhaustive.termination_reason


def test_fixed_seed_randomized_differential_properties() -> None:
    rng = random.Random(20260928)
    model = PresetMaintenanceCostModel(0, 1, 0, 1)
    for _ in range(40):
        names = "ABCDE"
        candidates = tuple(
            Candidate(
                CandidateId(name),
                10,
                "r",
                MechanismKind.MCV,
                ("a", "b"),
                (),
                rank,
                100 + rank,
            )
            for rank, name in enumerate(names)
        )
        catalog = CandidateCatalog(candidates)
        objectives = {(): rng.randint(1, 20)}
        for width in range(1, len(names) + 1):
            for subset in combinations(names, width):
                objectives[subset] = rng.randint(1, 20)
        incidence = IncidenceIndex(
            tuple((candidate.candidate_id, frozenset({Q})) for candidate in candidates),
            frozenset({Q}),
        )
        exhaustive_evaluator = CatalogLandscapeEvaluator(objectives, catalog)
        pruned_evaluator = CatalogLandscapeEvaluator(objectives, catalog)
        exhaustive_evaluator.incidence = incidence
        pruned_evaluator.incidence = incidence
        exhaustive = DeterministicBudgetSearch(
            exhaustive_evaluator,
            catalog,
            model,
            MaintenanceBudget(6, model.unit),
            SearchConfig(exact_bound_pruning=False),
        ).run()
        pruned = DeterministicBudgetSearch(
            pruned_evaluator,
            catalog,
            model,
            MaintenanceBudget(6, model.unit),
            SearchConfig(exact_bound_pruning=True),
        ).run()
        assert pruned.selected_design == exhaustive.selected_design
        assert pruned.selected_objective == exhaustive.selected_objective
        assert accepted_signature(pruned) == accepted_signature(exhaustive)


def test_incremental_move_cost_matches_additive_model() -> None:
    catalog, _ = fixture()
    model = PresetMaintenanceCostModel(0, 1, 0, 1)
    evaluator = CatalogLandscapeEvaluator({(): 1.0}, catalog)
    search = DeterministicBudgetSearch(
        evaluator, catalog, model, MaintenanceBudget(10, model.unit)
    )
    design = catalog.normalize_design({CandidateId("A"), CandidateId("C")})
    current_cost = model.estimate_design(design, catalog)
    for move in (
        Move.add_candidate(CandidateId("B")),
        Move.drop_candidate(CandidateId("A")),
        Move.swap(CandidateId("A"), CandidateId("B")),
    ):
        after = catalog.apply_move(design, move)
        assert search._cost_after_move(current_cost, move) == model.estimate_design(after, catalog)


def test_swap_bound_affected_set_is_endpoint_union() -> None:
    catalog, _ = fixture()
    incidence = IncidenceIndex(
        (
            (CandidateId("A"), frozenset({Q})),
            (CandidateId("B"), frozenset({Q2})),
            (CandidateId("C"), frozenset()),
            (CandidateId("D"), frozenset({Q, Q2})),
        ),
        frozenset({Q, Q2}),
    )
    model = PresetMaintenanceCostModel(0, 1, 0, 1)
    search = DeterministicBudgetSearch(
        CatalogLandscapeEvaluator({(): 1.0}, catalog),
        catalog,
        model,
        MaintenanceBudget(10, model.unit),
        incidence=incidence,
    )
    assert search._affected(Move.swap(CandidateId("A"), CandidateId("B"))) == frozenset(
        {Q, Q2}
    )
