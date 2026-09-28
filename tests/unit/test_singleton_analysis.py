from pg_extstats_advisor.analysis.singleton import (
    classify_improvement,
    deterministic_order,
    linear_quantile,
    percentile_from_rank,
    recall_at,
)


def test_classification_uses_strict_float_ordering() -> None:
    assert classify_improvement(3.0, 2.0) == "positive"
    assert classify_improvement(3.0, 3.0) == "zero"
    assert classify_improvement(3.0, 4.0) == "negative"


def test_linear_quantile_and_top_oriented_percentile() -> None:
    assert linear_quantile([1.0, 3.0, 7.0], 0.5) == 3.0
    assert linear_quantile([1.0, 3.0, 7.0], 0.25) == 2.0
    assert percentile_from_rank(1, 10) == 100.0
    assert percentile_from_rank(10, 10) == 10.0


def test_deterministic_order_and_recall() -> None:
    rows = [
        {"candidate_id": "b", "singleton_improvement": 2, "maintenance_cost_numeric": 1,
         "precedence_rank": 1},
        {"candidate_id": "a", "singleton_improvement": 2, "maintenance_cost_numeric": 1,
         "precedence_rank": 0},
        {"candidate_id": "c", "singleton_improvement": 1, "maintenance_cost_numeric": 1,
         "precedence_rank": 2},
    ]
    assert [row["candidate_id"] for row in deterministic_order(rows, "singleton_improvement")] == [
        "a", "b", "c"
    ]
    assert recall_at([1, 5, 10], 5) == 2
