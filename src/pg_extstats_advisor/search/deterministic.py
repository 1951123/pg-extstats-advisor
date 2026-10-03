"""Frozen contextual-greedy plus ADD/DROP/SWAP best-improvement search."""

from __future__ import annotations

import math
import time
from collections.abc import Iterable, Iterator
from dataclasses import dataclass, replace
from decimal import Decimal
from typing import Any

from pg_extstats_advisor.candidates.model import CandidateCatalog
from pg_extstats_advisor.cost.model import MaintenanceBudget, MaintenanceCostModel
from pg_extstats_advisor.incidence.index import IncidenceIndex
from pg_extstats_advisor.models import CandidateId, Design, EvaluationState, Move, MoveKind
from pg_extstats_advisor.optimization.budget import (
    OptimizationBudget,
    OptimizationBudgetExhausted,
    OptimizationStatus,
)
from pg_extstats_advisor.search.model import (
    DesignEvaluator,
    MoveRecord,
    OptimizationSearchOutcome,
    SearchConfig,
    SearchResult,
    candidate_catalog_digest,
)


def _planner_calls(evaluator: DesignEvaluator) -> int:
    adapter = getattr(evaluator, "adapter", None)
    return int(getattr(adapter, "planner_calls_total", getattr(adapter, "explain_calls", 0)))


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
        self._optimization_budget: OptimizationBudget | None = None
        self._optimization_status = OptimizationStatus.LOCAL_OPTIMUM
        self._optimization_stop: OptimizationBudgetExhausted | None = None
        self._phase_started: dict[str, float] = {}
        self._phase_elapsed: dict[str, float] = {}
        self._baseline_planner_calls = 0
        self._singleton_planner_calls = 0
        self._greedy_planner_calls = 0
        self._last_accepted_state: EvaluationState | None = None
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
        self._optimization_status = OptimizationStatus.LOCAL_OPTIMUM
        self._optimization_stop = None
        self._phase_started = {}
        self._phase_elapsed = {}
        self._baseline_planner_calls = 0
        self._singleton_planner_calls = 0
        self._greedy_planner_calls = 0
        self._last_accepted_state = None

    def _bind_budget(self, budget: OptimizationBudget | None, phase: str) -> None:
        self._optimization_budget = budget
        binder = getattr(self.evaluator, "bind_optimization_budget", None)
        if binder is not None:
            binder(budget, phase)

    def _check_budget(self, phase: str) -> None:
        if self._optimization_budget is not None:
            self._optimization_budget.check(phase)

    def _start_phase(self, phase: str) -> None:
        self._phase_started[phase] = time.perf_counter()

    def _finish_phase(self, phase: str) -> None:
        started = self._phase_started.pop(phase, None)
        if started is not None:
            self._phase_elapsed[phase] = self._phase_elapsed.get(phase, 0.0) + (
                time.perf_counter() - started
            )

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
        self._check_budget("greedy" if phase == "greedy-add" else phase)
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
        self._check_budget("greedy" if phase == "greedy-add" else phase)
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
        self._check_budget("greedy" if phase == "greedy-add" else phase)
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
        metrics: dict[str, dict[str, Any]] | None = None,
    ) -> _EvaluatedMove | None:
        best: _EvaluatedMove | None = None
        best_index: int | None = None
        for move in moves:
            self._check_budget("greedy" if phase == "greedy-add" else phase)
            move_type = move.kind.value
            metric = metrics.get(move_type) if metrics is not None else None
            if metric is not None:
                metric["conceptual_moves"] += 1
                metric_started = time.perf_counter()
                before_skipped = self._skipped
                before_no_improvement = self._bound_pruned_no_improvement
                before_incumbent = self._bound_pruned_incumbent
                before_evaluated = self._evaluated
            evaluated = self._consider(phase, current, move, current_cost, best)
            if metric is not None:
                metric["infeasible"] += self._skipped - before_skipped
                metric["no_improvement_bound_pruned"] += (
                    self._bound_pruned_no_improvement - before_no_improvement
                )
                metric["incumbent_bound_pruned"] += (
                    self._bound_pruned_incumbent - before_incumbent
                )
                metric["native_evaluated"] += self._evaluated - before_evaluated
                metric["elapsed_seconds"] += time.perf_counter() - metric_started
                if evaluated is not None:
                    metric["planner_calls"] += len(evaluated.state.affected_query_ids)
                    metric["best_objective"] = min(
                        metric["best_objective"], evaluated.state.aggregate_objective
                    )
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
        self._check_budget("greedy" if phase == "greedy-add" else phase)
        if best is None or best.state.aggregate_objective >= current.aggregate_objective:
            return None
        assert best_index is not None
        self._trajectory[best_index] = replace(
            self._trajectory[best_index], accepted=True, rejection_reason=None
        )
        self._accepted += 1
        return best

    def evaluate_one_local_round(
        self,
        current: EvaluationState,
        current_cost: Decimal | None = None,
    ) -> tuple[EvaluationState, Decimal, _EvaluatedMove | None, dict[str, dict[str, Any]]]:
        """Evaluate exactly one complete ADD/DROP/SWAP local neighborhood.

        The method deliberately does not reset state, apply a second round, or
        perform any other phase. Callers supply the starting state and may
        inspect the optional winner before deciding what to do next.
        """

        before_cost = (
            self.cost_model.estimate_design(current.design, self.catalog)
            if current_cost is None
            else current_cost
        )
        metrics = {
            move_type: {
                "conceptual_moves": 0,
                "infeasible": 0,
                "no_improvement_bound_pruned": 0,
                "incumbent_bound_pruned": 0,
                "native_evaluated": 0,
                "planner_calls": 0,
                "elapsed_seconds": 0.0,
                "best_objective": math.inf,
            }
            for move_type in (MoveKind.ADD.value, MoveKind.DROP.value, MoveKind.SWAP.value)
        }
        selected = self._ordered_ids(True, current.design)
        unselected = self._ordered_ids(False, current.design)
        winner = self._finish_streaming_round(
            "local", current, before_cost, self._iter_local_moves(selected, unselected), metrics
        )
        after = winner.state if winner is not None else current
        after_cost = winner.cost if winner is not None else before_cost
        return after, after_cost, winner, metrics

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
            self._last_accepted_state = current
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
            self._last_accepted_state = current
            current_cost = accepted.cost

    def _make_result(
        self,
        initial: Design,
        current: EvaluationState,
        *,
        status: OptimizationStatus,
        budget: OptimizationBudget | None = None,
        baseline_planner_calls: int = 0,
        singleton_planner_calls: int = 0,
        greedy_planner_calls: int = 0,
    ) -> SearchResult:
        cost = self.cost_model.estimate_design(current.design, self.catalog)
        catalog_digest = candidate_catalog_digest(self.catalog.candidates)
        stop = self._optimization_stop
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
            termination_reason=(
                "budget-exhausted"
                if status in {
                    OptimizationStatus.BUDGET_EXHAUSTED_DURING_GREEDY,
                    OptimizationStatus.BUDGET_EXHAUSTED_DURING_LOCAL,
                }
                else "add-local-optimum"
                if self.config.add_only
                else "one-move-local-optimum"
            ),
            config=self.config,
            workload_digest=current.workload_digest,
            repository_digest=current.repository_digest,
            candidate_catalog_digest=catalog_digest,
            total_neighbor_moves_considered=self._considered,
            bound_pruned_no_improvement_count=self._bound_pruned_no_improvement,
            bound_pruned_incumbent_count=self._bound_pruned_incumbent,
            optimization_status=status,
            optimization_budget_seconds=budget.limit_seconds if budget is not None else None,
            optimization_elapsed_seconds=budget.elapsed_seconds if budget is not None else None,
            optimization_budget_exhausted=budget.exhausted if budget is not None else False,
            optimization_stop_phase=stop.phase if stop is not None else None,
            optimization_stop_reason=str(stop) if stop is not None else None,
            planner_calls_completed=(
                baseline_planner_calls + singleton_planner_calls + greedy_planner_calls
            ),
            baseline_planner_calls=baseline_planner_calls,
            singleton_planner_calls=singleton_planner_calls,
            greedy_planner_calls=greedy_planner_calls,
            phase_elapsed_seconds=tuple(sorted(self._phase_elapsed.items())),
        )

    def run(self) -> SearchResult:
        """Run the historical unbounded search API.

        New deadline-aware callers must use :meth:`run_bounded`; retaining this
        method keeps existing maintenance-budget experiments source-compatible.
        """

        self._reset_run_state()
        self._optimization_budget = None
        self._bind_budget(None, "baseline")
        initial = Design(())
        self._calls += 1
        current = self.evaluator.evaluate_design(initial)
        self._last_accepted_state = current
        current = self._greedy(current)
        if not self.config.add_only:
            current = self._local(current)
        return self._make_result(initial, current, status=OptimizationStatus.LOCAL_OPTIMUM)

    def run_bounded(
        self,
        budget: OptimizationBudget,
        *,
        initial_state: EvaluationState | None = None,
        baseline_planner_calls: int = 0,
        singleton_planner_calls: int = 0,
    ) -> OptimizationSearchOutcome:
        """Run search under a monotonic deadline.

        ``initial_state`` is used by a complete singleton-profile workflow: the
        caller starts the same budget before baseline, profiles singletons, then
        passes the last accepted state here.  This avoids charging setup or
        re-running baseline while preserving one deadline for the whole advisor
        invocation.
        """

        self._reset_run_state()
        budget.start()
        self._optimization_budget = budget
        planner_before = _planner_calls(self.evaluator)
        initial = Design(())
        current: EvaluationState | None = initial_state
        if current is None:
            self._start_phase("baseline")
            self._bind_budget(budget, "baseline")
            try:
                budget.check("baseline")
                self._calls += 1
                current = self.evaluator.evaluate_design(initial)
                budget.check("baseline")
            except OptimizationBudgetExhausted as stop:
                self._optimization_stop = stop
                self._optimization_status = OptimizationStatus.BUDGET_EXHAUSTED_DURING_BASELINE
                self._finish_phase("baseline")
                calls = _planner_calls(self.evaluator) - planner_before
                return OptimizationSearchOutcome(
                    self._optimization_status,
                    None,
                    budget.limit_seconds,
                    budget.elapsed_seconds,
                    True,
                    stop.phase,
                    str(stop),
                    calls,
                    calls,
                    singleton_planner_calls,
                    0,
                    tuple(sorted(self._phase_elapsed.items())),
                )
            self._finish_phase("baseline")
            baseline_planner_calls = _planner_calls(self.evaluator) - planner_before
        else:
            self._calls += 0
        assert current is not None
        self._last_accepted_state = current
        self._start_phase("greedy")
        self._bind_budget(budget, "greedy")
        status = OptimizationStatus.LOCAL_OPTIMUM
        try:
            budget.check("greedy")
            current = self._greedy(current)
            if not self.config.add_only:
                self._start_phase("local")
                self._bind_budget(budget, "local")
                current = self._local(current)
        except OptimizationBudgetExhausted as stop:
            self._optimization_stop = stop
            status = (
                OptimizationStatus.BUDGET_EXHAUSTED_DURING_LOCAL
                if stop.phase == "local"
                else OptimizationStatus.BUDGET_EXHAUSTED_DURING_GREEDY
            )
            self._optimization_status = status
            current = self._last_accepted_state or current
        finally:
            self._finish_phase("greedy")
            self._finish_phase("local")
        self._optimization_status = status
        planner_after = _planner_calls(self.evaluator)
        greedy_calls = max(
            0,
            planner_after - planner_before
            if initial_state is not None
            else planner_after - planner_before - baseline_planner_calls,
        )
        result = self._make_result(
            initial,
            current,
            status=status,
            budget=budget,
            baseline_planner_calls=baseline_planner_calls,
            singleton_planner_calls=singleton_planner_calls,
            greedy_planner_calls=greedy_calls,
        )
        return OptimizationSearchOutcome(
            status,
            result,
            budget.limit_seconds,
            budget.elapsed_seconds,
            budget.exhausted,
            result.optimization_stop_phase,
            result.optimization_stop_reason,
            result.planner_calls_completed,
            result.baseline_planner_calls,
            result.singleton_planner_calls,
            result.greedy_planner_calls,
            result.phase_elapsed_seconds,
        )
