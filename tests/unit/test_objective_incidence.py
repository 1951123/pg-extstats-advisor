import pytest

from pg_extstats_advisor.incidence.index import IncidenceIndex
from pg_extstats_advisor.models import CandidateId, Move, QueryEvaluation, QueryId
from pg_extstats_advisor.objective.qerror import aggregate_objective, q_error


def test_q_error_and_deterministic_aggregation() -> None:
    assert q_error(20, 10) == 2
    assert q_error(5, 10) == 2
    assert q_error(0, 10) == 10
    with pytest.raises(ValueError, match="positive truth"):
        q_error(1, 0)
    evaluations = (
        QueryEvaluation(QueryId("q2"), 5, 10, 2, "test"),
        QueryEvaluation(QueryId("q1"), 20, 10, 2, "test"),
    )
    assert aggregate_objective(evaluations) == 4


def test_incidence_add_drop_swap_and_validation() -> None:
    q1, q2, q3 = QueryId("q1"), QueryId("q2"), QueryId("q3")
    s1, s2 = CandidateId("s1"), CandidateId("s2")
    index = IncidenceIndex(((s1, frozenset({q1, q2})), (s2, frozenset({q2}))), frozenset({q1, q2, q3}))
    assert index.affected(Move.add_candidate(s1)) == {q1, q2}
    assert index.affected(Move.drop_candidate(s2)) == {q2}
    assert index.affected(Move.swap(s1, s2)) == {q1, q2}
    assert q3 not in index.affected(Move.add_candidate(s1))
    with pytest.raises(ValueError, match="unknown queries"):
        IncidenceIndex(((s1, frozenset({QueryId("missing")})),), frozenset({q1}))
