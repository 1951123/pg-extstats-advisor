"""Conservative candidate-to-query incidence."""

from dataclasses import dataclass

from pg_extstats_advisor.models import CandidateId, Move, MoveKind, QueryId


@dataclass(frozen=True, slots=True)
class IncidenceIndex:
    entries: tuple[tuple[CandidateId, frozenset[QueryId]], ...]
    known_queries: frozenset[QueryId]

    def __post_init__(self) -> None:
        candidate_ids = [candidate_id for candidate_id, _ in self.entries]
        if len(candidate_ids) != len(set(candidate_ids)):
            raise ValueError("duplicate candidate incidence entry")
        unknown = set().union(*(queries for _, queries in self.entries)) - self.known_queries
        if unknown:
            raise ValueError(f"incidence contains unknown queries: {sorted(unknown)}")

    @property
    def by_candidate(self) -> dict[CandidateId, frozenset[QueryId]]:
        return dict(self.entries)

    def affected(self, move: Move) -> frozenset[QueryId]:
        endpoints = []
        if move.kind in (MoveKind.ADD, MoveKind.SWAP):
            assert move.add is not None
            endpoints.append(move.add)
        if move.kind in (MoveKind.DROP, MoveKind.SWAP):
            assert move.drop is not None
            endpoints.append(move.drop)
        try:
            return frozenset().union(*(self.by_candidate[item] for item in endpoints))
        except KeyError as error:
            raise ValueError(f"missing incidence for candidate {error.args[0]}") from error
