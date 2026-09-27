"""Candidate catalog, fixed precedence, and move application."""

from dataclasses import dataclass

from pg_extstats_advisor.models import Candidate, CandidateId, Design, Move, MoveKind


@dataclass(frozen=True, slots=True)
class CandidateCatalog:
    candidates: tuple[Candidate, ...]

    def __post_init__(self) -> None:
        ids = [candidate.candidate_id for candidate in self.candidates]
        ranks = [candidate.precedence_rank for candidate in self.candidates]
        if len(ids) != len(set(ids)) or len(ranks) != len(set(ranks)):
            raise ValueError("candidate IDs and precedence ranks must be unique")

    @property
    def by_id(self) -> dict[CandidateId, Candidate]:
        return {candidate.candidate_id: candidate for candidate in self.candidates}

    def normalize_design(self, candidate_ids: set[CandidateId]) -> Design:
        unknown = candidate_ids - self.by_id.keys()
        if unknown:
            raise ValueError(f"unknown candidates: {sorted(unknown)}")
        ordered = sorted(candidate_ids, key=lambda item: self.by_id[item].precedence_rank)
        return Design(tuple(ordered))

    def validate_design(self, design: Design) -> None:
        if self.normalize_design(set(design.candidate_ids)) != design:
            raise ValueError("design is not in fixed precedence order")

    def apply_move(self, design: Design, move: Move) -> Design:
        self.validate_design(design)
        selected = set(design.candidate_ids)
        if move.kind is MoveKind.ADD:
            assert move.add is not None
            if move.add in selected:
                raise ValueError("ADD candidate is already selected")
            selected.add(move.add)
        elif move.kind is MoveKind.DROP:
            assert move.drop is not None
            if move.drop not in selected:
                raise ValueError("DROP candidate is not selected")
            selected.remove(move.drop)
        else:
            assert move.add is not None and move.drop is not None
            if move.drop not in selected or move.add in selected or move.add == move.drop:
                raise ValueError("invalid SWAP endpoints")
            selected.remove(move.drop)
            selected.add(move.add)
        return self.normalize_design(selected)
