from dataclasses import replace

import pytest

from pg_extstats_advisor.candidates.model import CandidateCatalog
from pg_extstats_advisor.models import (
    Candidate,
    CandidateId,
    Design,
    EvaluationState,
    MechanismKind,
    Move,
    QueryEvaluation,
    QueryId,
    WorkloadQuery,
)
from pg_extstats_advisor.workload.model import Workload


def candidate(name: str, rank: int) -> Candidate:
    return Candidate(
        CandidateId(name),
        10,
        "fixture",
        MechanismKind.MCV,
        ("a", "b"),
        (("sql", "CREATE STATISTICS"),),
        rank,
        100 + rank,
    )


def test_fixed_precedence_add_drop_swap() -> None:
    catalog = CandidateCatalog((candidate("s1", 1), candidate("s2", 2), candidate("s3", 3)))
    original = Design((CandidateId("s1"), CandidateId("s3")))
    added = catalog.apply_move(original, Move.add_candidate(CandidateId("s2")))
    assert added.candidate_ids == ("s1", "s2", "s3")
    dropped = catalog.apply_move(added, Move.drop_candidate(CandidateId("s1")))
    assert dropped.candidate_ids == ("s2", "s3")
    swapped = catalog.apply_move(dropped, Move.swap(CandidateId("s2"), CandidateId("s1")))
    assert swapped.candidate_ids == ("s1", "s3")


@pytest.mark.parametrize(
    "design,move,message",
    [
        (Design((CandidateId("s1"),)), Move.add_candidate(CandidateId("s1")), "already"),
        (Design((CandidateId("s1"),)), Move.drop_candidate(CandidateId("s2")), "not selected"),
        (
            Design((CandidateId("s1"),)),
            Move.swap(CandidateId("s2"), CandidateId("s3")),
            "invalid SWAP",
        ),
    ],
)
def test_invalid_moves(design: Design, move: Move, message: str) -> None:
    catalog = CandidateCatalog((candidate("s1", 1), candidate("s2", 2), candidate("s3", 3)))
    with pytest.raises(ValueError, match=message):
        catalog.apply_move(design, move)


def test_workload_and_state_are_identity_checked_records() -> None:
    query = WorkloadQuery(QueryId("q1"), "select 1", 1, "fixture", frozenset({10}))
    workload = Workload("fixture", (query,))
    assert len(workload.digest) == 64
    evaluation = QueryEvaluation(QueryId("q1"), 1, 1, 1, "test")
    state = EvaluationState(
        Design(()), (evaluation,), 1, "r", workload.digest, "16.14", "test", (QueryId("q1"),), ()
    )
    assert state.by_query()[QueryId("q1")] is evaluation
    assert replace(state, aggregate_objective=2).aggregate_objective == 2
    with pytest.raises(ValueError, match="positive truth"):
        replace(query, truth=0)
