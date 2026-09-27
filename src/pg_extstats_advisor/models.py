"""Immutable core records for the M0 evaluator."""

from __future__ import annotations

from dataclasses import dataclass
from enum import Enum
from typing import Any, NewType

QueryId = NewType("QueryId", str)
CandidateId = NewType("CandidateId", str)


class MechanismKind(str, Enum):
    MCV = "mcv"
    FD = "fd"

    @property
    def postgres_code(self) -> str:
        return "m" if self is MechanismKind.MCV else "f"


@dataclass(frozen=True, slots=True)
class Candidate:
    candidate_id: CandidateId
    relation_oid: int
    relation_name: str
    mechanism: MechanismKind
    attributes: tuple[str, ...]
    definition: tuple[tuple[str, Any], ...]
    precedence_rank: int
    backend_oid: int

    def __post_init__(self) -> None:
        if not self.candidate_id or self.relation_oid <= 0 or self.backend_oid <= 0:
            raise ValueError("candidate identity and OIDs must be valid")
        if not self.relation_name or len(self.attributes) < 2:
            raise ValueError("candidate requires a relation and at least two attributes")
        if self.precedence_rank < 0:
            raise ValueError("precedence rank must be non-negative")


@dataclass(frozen=True, slots=True)
class Design:
    candidate_ids: tuple[CandidateId, ...]

    def __post_init__(self) -> None:
        if len(set(self.candidate_ids)) != len(self.candidate_ids):
            raise ValueError("design contains duplicate candidate IDs")

    def __contains__(self, candidate_id: CandidateId) -> bool:
        return candidate_id in self.candidate_ids


class MoveKind(str, Enum):
    ADD = "add"
    DROP = "drop"
    SWAP = "swap"


@dataclass(frozen=True, slots=True)
class Move:
    kind: MoveKind
    add: CandidateId | None = None
    drop: CandidateId | None = None

    @classmethod
    def add_candidate(cls, candidate_id: CandidateId) -> Move:
        return cls(MoveKind.ADD, add=candidate_id)

    @classmethod
    def drop_candidate(cls, candidate_id: CandidateId) -> Move:
        return cls(MoveKind.DROP, drop=candidate_id)

    @classmethod
    def swap(cls, removed: CandidateId, added: CandidateId) -> Move:
        return cls(MoveKind.SWAP, add=added, drop=removed)

    def __post_init__(self) -> None:
        expected = {
            MoveKind.ADD: (True, False),
            MoveKind.DROP: (False, True),
            MoveKind.SWAP: (True, True),
        }[self.kind]
        if (self.add is not None, self.drop is not None) != expected:
            raise ValueError(f"invalid endpoints for {self.kind.value} move")


@dataclass(frozen=True, slots=True)
class WorkloadQuery:
    query_id: QueryId
    sql: str
    truth: float
    target_relation: str
    relation_oids: frozenset[int]
    label: str | None = None

    def __post_init__(self) -> None:
        if not self.query_id or not self.sql or not self.target_relation:
            raise ValueError("query identity, SQL, and target relation are required")
        if self.truth <= 0:
            raise ValueError("objective queries require positive truth")


@dataclass(frozen=True, slots=True)
class QueryEvaluation:
    query_id: QueryId
    estimate: float
    truth: float
    contribution: float
    provenance: str


@dataclass(frozen=True, slots=True)
class EvaluationState:
    design: Design
    query_evaluations: tuple[QueryEvaluation, ...]
    aggregate_objective: float
    repository_digest: str
    workload_digest: str
    postgres_version: str
    evaluator_provenance: str
    affected_query_ids: tuple[QueryId, ...]
    reused_query_ids: tuple[QueryId, ...]

    def by_query(self) -> dict[QueryId, QueryEvaluation]:
        return {evaluation.query_id: evaluation for evaluation in self.query_evaluations}
