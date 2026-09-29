from pg_extstats_advisor.capture.metrics import marginal_metrics, summarize_objectives


def test_metrics_are_explicit_and_zero_safe() -> None:
    summary = summarize_objectives([10.0, 12.0, 14.0])
    assert summary["stddev_definition"] == "sample"
    assert summary["mean"] == 12.0
    assert summarize_objectives([0.0])["normalized_range"] is None
    assert marginal_metrics({"mean": 0.0, "normalized_range": None}, {"mean": 1.0, "normalized_range": 0.0}, 3.0)["quality_gain"] is None
