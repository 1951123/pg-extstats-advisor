from pg_extstats_advisor.analysis.interaction import (
    classify_contextual,
    gain_recall,
    interaction_gain,
    percentile_bucket,
)


def test_contextual_classification_and_interaction_gain() -> None:
    assert classify_contextual(1.0) == "positive"
    assert classify_contextual(0.0) == "zero"
    assert classify_contextual(-1.0) == "negative"
    assert interaction_gain(4.0, 1.5) == 2.5


def test_percentile_bucket_boundaries() -> None:
    assert percentile_bucket(1, 100) == "top_1pct"
    assert percentile_bucket(5, 100) == "1_5pct"
    assert percentile_bucket(40, 100) == "20_40pct"
    assert percentile_bucket(50, 100) == "40_60pct"
    assert percentile_bucket(100, 100) == "bottom_20pct"


def test_gain_recall_uses_observed_accepted_denominator() -> None:
    accepted = [("a", 2.0), ("b", 1.0)]
    assert gain_recall({"a"}, accepted) == 2 / 3
    assert gain_recall(set(), accepted) == 0.0
