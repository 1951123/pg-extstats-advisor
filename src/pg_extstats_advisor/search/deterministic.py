"""Frozen contextual-greedy plus ADD/DROP/SWAP best-improvement search."""

from __future__ import annotations

from dataclasses import dataclass
from decimal import Decimal
from typing import Any

from pg_extstats_advisor.candidates.model import CandidateCatalog
from pg_extstats_advisor.cost.model import MaintenanceBudget, MaintenanceCostModel
from pg_extstats_advisor.models import CandidateId, Design, EvaluationState, Move
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


class DeterministicBudgetSearch:
    def __init__(
        self,
        evaluator: DesignEvaluator,
        catalog: CandidateCatalog,
        cost_model: MaintenanceCostModel,
        budget: MaintenanceBudget,
        config: SearchConfig | None = None,
    ) -> None:
        if budget.unit != cost_model.unit:
            raise ValueError("budget and cost-model units differ")
        self.evaluator = evaluator
        self.catalog = catalog
        self.cost_model = cost_model
        self.budget = budget
        self.config = config or SearchConfig()
        self._reset_run_state()

    def _reset_run_state(self) -> None:
        self._trajectory: list[MoveRecord] = []
        self._evaluated = 0
        self._skipped = 0
        self._calls = 0
        self._accepted = 0

    def _ordered_ids(self, selected: bool, design: Design) -> list[CandidateId]:
        membership = set(design.candidate_ids)
        values = [
            candidate
            for candidate in self.catalog.candidates
            if (candidate.candidate_id in membership) is selected
        ]
        values.sort(key=lambda item: (item.precedence_rank, item.candidate_id))
        return [item.candidate_id for item in values]

    def _evaluate(
        self, current: EvaluationState, move: Move, counterfactual: Design
    ) -> EvaluationState:
        self._calls += 1
        self._evaluated += 1
        if self.config.full_reference:
            return self.evaluator.evaluate_design(counterfactual)
        return self.evaluator.evaluate_move(current.design, move, current)

    def _consider(self, phase: str, current: EvaluationState, move: Move) -> _EvaluatedMove | None:
        counterfactual = self.catalog.apply_move(current.design, move)
        before_cost = self.cost_model.estimate_design(current.design, self.catalog)
        after_cost = self.cost_model.estimate_design(counterfactual, self.catalog)
        if not self.cost_model.is_feasible(counterfactual, self.catalog, self.budget):
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
        state = self._evaluate(current, move, counterfactual)
        if move.add is not None and move.drop is not None:
            outgoing = self.catalog.by_id[move.drop]
            incoming = self.catalog.by_id[move.add]
            rank_key = (
                2,
                outgoing.precedence_rank,
                outgoing.candidate_id,
                incoming.precedence_rank,
                incoming.candidate_id,
            )
        elif move.add is not None:
            candidate = self.catalog.by_id[move.add]
            rank_key = (0, candidate.precedence_rank, candidate.candidate_id)
        else:
            assert move.drop is not None
            candidate = self.catalog.by_id[move.drop]
            rank_key = (1, candidate.precedence_rank, candidate.candidate_id)
        return _EvaluatedMove(
            move,
            state,
            after_cost,
            rank_key,
        )

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
        self, phase: str, current: EvaluationState, options: list[_EvaluatedMove]
    ) -> EvaluationState | None:
        best = self._best(options)
        before_cost = self.cost_model.estimate_design(current.design, self.catalog)
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
                    before_cost,
                    option.cost,
                    accepted,
                    None if accepted else "not-best-strict-improvement",
                    len(option.state.affected_query_ids),
                )
            )
        if best is None or best.state.aggregate_objective >= current.aggregate_objective:
            return None
        self._accepted += 1
        return best.state

    def _greedy(self, current: EvaluationState) -> EvaluationState:
        while True:
            options = []
            for candidate_id in self._ordered_ids(False, current.design):
                evaluated = self._consider("greedy-add", current, Move.add_candidate(candidate_id))
                if evaluated is not None:
                    options.append(evaluated)
            accepted = self._finish_round("greedy-add", current, options)
            if accepted is None:
                return current
            current = accepted

    def _local(self, current: EvaluationState) -> EvaluationState:
        while True:
            selected = self._ordered_ids(True, current.design)
            unselected = self._ordered_ids(False, current.design)
            moves = [Move.add_candidate(item) for item in unselected]
            moves.extend(Move.drop_candidate(item) for item in selected)
            moves.extend(
                Move.swap(outgoing, incoming) for outgoing in selected for incoming in unselected
            )
            options = []
            for move in moves:
                evaluated = self._consider("local", current, move)
                if evaluated is not None:
                    options.append(evaluated)
            accepted = self._finish_round("local", current, options)
            if accepted is None:
                return current
            current = accepted

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
        )
