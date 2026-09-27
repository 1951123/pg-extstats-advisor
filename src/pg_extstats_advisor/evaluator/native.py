"""Native full-design and query-local move evaluator."""

from __future__ import annotations

from dataclasses import dataclass

from pg_extstats_advisor.candidates.model import CandidateCatalog
from pg_extstats_advisor.incidence.index import IncidenceIndex
from pg_extstats_advisor.models import (
    Design,
    EvaluationState,
    Move,
    QueryEvaluation,
    QueryId,
)
from pg_extstats_advisor.objective.qerror import aggregate_objective, q_error
from pg_extstats_advisor.payloads.repository import PayloadRepository
from pg_extstats_advisor.postgres.adapter import PostgresAdapter
from pg_extstats_advisor.workload.model import Workload


@dataclass(slots=True)
class NativeEvaluator:
    workload: Workload
    repository: PayloadRepository
    incidence: IncidenceIndex
    adapter: PostgresAdapter
    provenance: str = "m0-b-native-evaluator-v1"

    def __post_init__(self) -> None:
        if self.adapter.repository.digest != self.repository.digest:
            raise ValueError("adapter repository does not match evaluator repository")
        if self.incidence.known_queries != frozenset(self.workload.by_id):
            raise ValueError("incidence/workload query universe mismatch")
        self.adapter.register_repository()

    @property
    def catalog(self) -> CandidateCatalog:
        return self.repository.catalog

    def _evaluation(self, query_id: QueryId, estimate: float) -> QueryEvaluation:
        query = self.workload.by_id[query_id]
        return QueryEvaluation(
            query_id=query_id,
            estimate=estimate,
            truth=query.truth,
            contribution=q_error(estimate, query.truth),
            provenance=f"native-explain:{self.adapter.postgres_version}",
        )

    def _state(
        self,
        design: Design,
        evaluations: dict[QueryId, QueryEvaluation],
        affected: frozenset[QueryId],
        reused: frozenset[QueryId],
    ) -> EvaluationState:
        ordered = tuple(evaluations[item] for item in sorted(evaluations))
        return EvaluationState(
            design=design,
            query_evaluations=ordered,
            aggregate_objective=aggregate_objective(ordered),
            repository_digest=self.repository.digest,
            workload_digest=self.workload.digest,
            postgres_version=self.adapter.postgres_version,
            evaluator_provenance=self.provenance,
            affected_query_ids=tuple(sorted(affected)),
            reused_query_ids=tuple(sorted(reused)),
        )

    def evaluate_design(self, design: Design) -> EvaluationState:
        self.catalog.validate_design(design)
        self.adapter.activate_design(design)
        self.adapter.start_measurement()
        queries = tuple(sorted(self.workload.queries, key=lambda item: item.query_id))
        estimates = self.adapter.estimate_queries(queries)
        evaluations = {
            query.query_id: self._evaluation(query.query_id, estimates[query.query_id])
            for query in queries
        }
        all_queries = frozenset(self.workload.by_id)
        return self._state(design, evaluations, all_queries, frozenset())

    def evaluate_move(
        self, current_design: Design, move: Move, current_state: EvaluationState
    ) -> EvaluationState:
        if current_state.design != current_design:
            raise ValueError("current state/design mismatch")
        if current_state.repository_digest != self.repository.digest:
            raise ValueError("current state/repository mismatch")
        if current_state.workload_digest != self.workload.digest:
            raise ValueError("current state/workload mismatch")
        counterfactual = self.catalog.apply_move(current_design, move)
        affected = self.incidence.affected(move)
        reused = frozenset(self.workload.by_id) - affected
        previous = current_state.by_query()
        if set(previous) != set(self.workload.by_id):
            raise ValueError("current state query universe mismatch")
        self.adapter.activate_design(counterfactual)
        self.adapter.start_measurement()
        estimates = self.adapter.estimate_queries(
            self.workload.by_id[item] for item in sorted(affected)
        )
        evaluations = dict(previous)
        for query_id, estimate in estimates.items():
            evaluations[query_id] = self._evaluation(query_id, estimate)
        return self._state(counterfactual, evaluations, affected, reused)
