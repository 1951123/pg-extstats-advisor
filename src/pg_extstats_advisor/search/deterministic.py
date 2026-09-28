"""Frozen contextual-greedy plus ADD/DROP/SWAP best-improvement search."""

from __future__ import annotations

import math
from collections.abc import Iterable, Iterator
from dataclasses import dataclass, replace
from decimal import Decimal
from typing import Any

from pg_extstats_advisor.candidates.model import CandidateCatalog
from pg_extstats_advisor.cost.model import MaintenanceBudget, MaintenanceCostModel
from pg_extstats_advisor.incidence.index import IncidenceIndex
from pg_extstats_advisor.models import CandidateId, Design, EvaluationState, Move, MoveKind
from pg_extstats_advisor.search.model import (
    DesignEvaluator,
    MoveRecord,
    SearchConfig,
    SearchResult,
    candidate_catalog_digest,
)


@dataclass(frozen=True, slots=True)
class _EvaluatedMove:
    move: Move
    state: EvaluationState
    cost: Decimal
    rank_key: tuple[Any, ...]


def optimistic_objective_lower_bound(
    current_state: EvaluationState, affected_query_ids: Iterable[Any]
) -> float:
    """Return the q-error lower bound for a conservative affected-query superset.

    The current objective is the exact contribution of unaffected queries plus
    the current contribution of affected queries.  Since every supported
    q-error contribution is at least one, an affected query can contribute no
    less than one after a counterfactual move.
    """

    evaluations = current_state.by_query()
    affected = frozenset(affected_query_ids)
    unknown = affected - evaluations.keys()
    if unknown:
        raise ValueError(f"lower-bound affected set contains unknown queries: {sorted(unknown)}")
    if any(item.contribution < 1.0 for item in evaluations.values()):
        raise ValueError("exact q-error pruning requires every contribution to be >= 1")
    affected_sum = math.fsum(evaluations[item].contribution for item in affected)
    return current_state.aggregate_objective - affected_sum + len(affected)


class DeterministicBudgetSearch:
    def __init__(
        self,
        evaluator: DesignEvaluator,
        catalog: CandidateCatalog,
        cost_model: MaintenanceCostModel,
        budget: MaintenanceBudget,
        config: SearchConfig | None = None,
        incidence: IncidenceIndex | None = None,
    ) -> None:
        if budget.unit != cost_model.unit:
            raise ValueError("budget and cost-model units differ")
        self.evaluator = evaluator
        self.catalog = catalog
        self.cost_model = cost_model
        self.budget = budget
        self.config = config or SearchConfig()
        self.incidence = incidence or getattr(evaluator, "incidence", None)
        self._catalog_by_id = self.catalog.by_id
        self._incidence_by_candidate = (
            self.incidence.by_candidate if self.incidence is not None else {}
        )
        self._candidate_costs = {
            candidate.candidate_id: self.cost_model.estimate_candidate(candidate)
            for candidate in self.catalog.candidates
        }
        self._reset_run_state()

    def _reset_run_state(self) -> None:
        self._trajectory: list[MoveRecord] = []
        self._evaluated = 0
        self._skipped = 0
        self._calls = 0
        self._accepted = 0
        self._considered = 0
        self._bound_pruned_no_improvement = 0
        self._bound_pruned_incumbent = 0

    def _ordered_ids(self, selected: bool, design: Design) -> list[CandidateId]:
        membership = set(design.candidate_ids)
        values = [
            candidate
            for candidate in self.catalog.candidates
            if (candidate.candidate_id in membership) is selected
        ]
        values.sort(key=lambda item: (item.precedence_rank, item.candidate_id))
        return [item.candidate_id for item in values]

    def _iter_local_moves(
        self, selected: list[CandidateId], unselected: list[CandidateId]
    ) -> Iterator[Move]:
        for candidate_id in unselected:
            yield Move.add_candidate(candidate_id)
        for candidate_id in selected:
            yield Move.drop_candidate(candidate_id)
        for outgoing in selected:
            for incoming in unselected:
                yield Move.swap(outgoing, incoming)

    def _rank_key(self, move: Move) -> tuple[Any, ...]:
        if move.add is not None and move.drop is not None:
            outgoing = self._catalog_by_id[move.drop]
            incoming = self._catalog_by_id[move.add]
            return (
                2,
                outgoing.precedence_rank,
                outgoing.candidate_id,
                incoming.precedence_rank,
                incoming.candidate_id,
            )
        if move.add is not None:
            candidate = self._catalog_by_id[move.add]
            return (0, candidate.precedence_rank, candidate.candidate_id)
        assert move.drop is not None
        candidate = self._catalog_by_id[move.drop]
        return (1, candidate.precedence_rank, candidate.candidate_id)

    def _cost_after_move(self, current_cost: Decimal, move: Move) -> Decimal:
        if move.kind is MoveKind.ADD:
            assert move.add is not None
            return current_cost + self._candidate_costs[move.add]
        if move.kind is MoveKind.DROP:
            assert move.drop is not None
            return current_cost - self._candidate_costs[move.drop]
        assert move.add is not None and move.drop is not None
        return (
            current_cost
            - self._candidate_costs[move.drop]
            + self._candidate_costs[move.add]
        )

    def _evaluate(
        self, current: EvaluationState, move: Move, counterfactual: Design
    ) -> EvaluationState:
        self._calls += 1
        self._evaluated += 1
        if self.config.full_reference:
            return self.evaluator.evaluate_design(counterfactual)
        return self.evaluator.evaluate_move(current.design, move, current)

    def _append_pruned_record(
        self,
        phase: str,
        current: EvaluationState,
        move: Move,
        counterfactual: Design,
        before_cost: Decimal,
        after_cost: Decimal,
        affected: frozenset[Any],
        reason: str,
        lower_bound: float,
        incumbent_objective: float | None,
    ) -> None:
        if not self.config.record_pruned_moves:
            return
        self._trajectory.append(
            MoveRecord(
                phase,
                move,
                current.design,
                counterfactual,
                current.aggregate_objective,
                None,
                before_cost,
                after_cost,
                False,
                reason,
                len(affected),
                lower_bound,
                incumbent_objective,
            )
        )

    def _consider(
        self,
        phase: str,
        current: EvaluationState,
        move: Move,
        current_cost: Decimal,
        incumbent: _EvaluatedMove | None = None,
    ) -> _EvaluatedMove | None:
        self._considered += 1
        counterfactual = self.catalog.apply_move(current.design, move)
        before_cost = current_cost
        after_cost = self._cost_after_move(current_cost, move)
        if after_cost > self.budget.value:
            self._skipped += 1
            self._trajectory.append(
                MoveRecord(
                    phase,
                    move,
                    current.design,
                    counterfactual,
                    current.aggregate_objective,
                    None,
                    before_cost,
                    after_cost,
                    False,
                    "infeasible-budget",
                    None,
                )
            )
            return None
        affected = (
            self._affected(move)
            if self.config.exact_bound_pruning and self.incidence is not None
            else frozenset()
        )
        if self.config.exact_bound_pruning and self.incidence is not None:
            lower_bound = optimistic_objective_lower_bound(current, affected)
            if incumbent is None and lower_bound >= current.aggregate_objective:
                self._bound_pruned_no_improvement += 1
                self._append_pruned_record(
                    phase,
                    current,
                    move,
                    counterfactual,
                    before_cost,
                    after_cost,
                    affected,
                    "bound-no-strict-improvement",
                    lower_bound,
                    None,
                )
                return None
            if incumbent is not None and lower_bound > incumbent.state.aggregate_objective:
                self._bound_pruned_incumbent += 1
                self._append_pruned_record(
                    phase,
                    current,
                    move,
                    counterfactual,
                    before_cost,
                    after_cost,
                    affected,
                    "bound-cannot-beat-incumbent",
                    lower_bound,
                    incumbent.state.aggregate_objective,
                )
                return None
        state = self._evaluate(current, move, counterfactual)
        return _EvaluatedMove(
            move,
            state,
            after_cost,
            self._rank_key(move),
        )

    def _affected(self, move: Move) -> frozenset[Any]:
        endpoints: list[CandidateId] = []
        if move.kind in (MoveKind.ADD, MoveKind.SWAP):
            assert move.add is not None
            endpoints.append(move.add)
        if move.kind in (MoveKind.DROP, MoveKind.SWAP):
            assert move.drop is not None
            endpoints.append(move.drop)
        try:
            return frozenset().union(
                *(self._incidence_by_candidate[item] for item in endpoints)
            )
        except KeyError as error:
            raise ValueError(f"missing incidence for candidate {error.args[0]}") from error

    @staticmethod
    def _best(options: list[_EvaluatedMove]) -> _EvaluatedMove | None:
        if not options:
            return None
        return min(
            options,
            key=lambda item: (
                item.state.aggregate_objective,
                item.cost,
                item.rank_key,
            ),
        )

    def _finish_round(
        self,
        phase: str,
        current: EvaluationState,
        current_cost: Decimal,
        options: list[_EvaluatedMove],
    ) -> _EvaluatedMove | None:
        best = self._best(options)
        for option in options:
            accepted = (
                best is option and option.state.aggregate_objective < current.aggregate_objective
            )
            self._trajectory.append(
                MoveRecord(
                    phase,
                    option.move,
                    current.design,
                    option.state.design,
                    current.aggregate_objective,
                    option.state.aggregate_objective,
                    current_cost,
                    option.cost,
                    accepted,
                    None if accepted else "not-best-strict-improvement",
                    len(option.state.affected_query_ids),
                )
            )
        if best is None or best.state.aggregate_objective >= current.aggregate_objective:
            return None
        self._accepted += 1
        return best

    def _finish_streaming_round(
        self,
        phase: str,
        current: EvaluationState,
        current_cost: Decimal,
        moves: Iterable[Move],
    ) -> _EvaluatedMove | None:
        best: _EvaluatedMove | None = None
        best_index: int | None = None
        for move in moves:
            evaluated = self._consider(phase, current, move, current_cost, best)
            if evaluated is None:
                continue
            index = len(self._trajectory)
            self._trajectory.append(
                MoveRecord(
                    phase,
                    evaluated.move,
                    current.design,
                    evaluated.state.design,
                    current.aggregate_objective,
                    evaluated.state.aggregate_objective,
                    current_cost,
                    evaluated.cost,
                    False,
                    "not-best-strict-improvement",
                    len(evaluated.state.affected_query_ids),
                )
            )
            if best is None or (
                evaluated.state.aggregate_objective,
                evaluated.cost,
                evaluated.rank_key,
            ) < (best.state.aggregate_objective, best.cost, best.rank_key):
                best = evaluated
                best_index = index
        if best is None or best.state.aggregate_objective >= current.aggregate_objective:
            return None
        assert best_index is not None
        self._trajectory[best_index] = replace(
            self._trajectory[best_index], accepted=True, rejection_reason=None
        )
        self._accepted += 1
        return best

    def _greedy(self, current: EvaluationState) -> EvaluationState:
        current_cost = self.cost_model.estimate_design(current.design, self.catalog)
        while True:
            moves = (
                Move.add_candidate(candidate_id)
                for candidate_id in self._ordered_ids(False, current.design)
            )
            if self.config.exact_bound_pruning and self.incidence is not None:
                accepted = self._finish_streaming_round(
                    "greedy-add", current, current_cost, moves
                )
            else:
                options = [
                    evaluated
                    for move in moves
                    if (evaluated := self._consider("greedy-add", current, move, current_cost))
                    is not None
                ]
                accepted = self._finish_round(
                    "greedy-add", current, current_cost, options
                )
            if accepted is None:
                return current
            current = accepted.state
            current_cost = accepted.cost

    def _local(self, current: EvaluationState) -> EvaluationState:
        current_cost = self.cost_model.estimate_design(current.design, self.catalog)
        while True:
            selected = self._ordered_ids(True, current.design)
            unselected = self._ordered_ids(False, current.design)
            moves = self._iter_local_moves(selected, unselected)
            if self.config.exact_bound_pruning and self.incidence is not None:
                accepted = self._finish_streaming_round("local", current, current_cost, moves)
            else:
                options = [
                    evaluated
                    for move in moves
                    if (evaluated := self._consider("local", current, move, current_cost))
                    is not None
                ]
                accepted = self._finish_round("local", current, current_cost, options)
            if accepted is None:
                return current
            current = accepted.state
            current_cost = accepted.cost

    def run(self) -> SearchResult:
        self._reset_run_state()
        initial = Design(())
        self._calls += 1
        current = self.evaluator.evaluate_design(initial)
        current = self._greedy(current)
        current = self._local(current)
        cost = self.cost_model.estimate_design(current.design, self.catalog)
        catalog_digest = candidate_catalog_digest(self.catalog.candidates)
        return SearchResult(
            selected_state=current,
            selected_design=current.design,
            selected_objective=current.aggregate_objective,
            selected_maintenance_cost=cost,
            budget=self.budget,
            cost_model_digest=self.cost_model.digest,
            initial_design=initial,
            final_design=current.design,
            trajectory=tuple(self._trajectory),
            evaluated_moves_count=self._evaluated,
            infeasible_moves_skipped_count=self._skipped,
            evaluator_calls_count=self._calls,
            accepted_moves_count=self._accepted,
            termination_reason="one-move-local-optimum",
            config=self.config,
            workload_digest=current.workload_digest,
            repository_digest=current.repository_digest,
            candidate_catalog_digest=catalog_digest,
            total_neighbor_moves_considered=self._considered,
            bound_pruned_no_improvement_count=self._bound_pruned_no_improvement,
            bound_pruned_incumbent_count=self._bound_pruned_incumbent,
        )
