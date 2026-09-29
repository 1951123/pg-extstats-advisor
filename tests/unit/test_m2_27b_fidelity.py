from pg_extstats_advisor.capture.fidelity import (
    adjacent_quality,
    adjacent_stability,
    gate,
    knee_structure,
    normalize_mean,
    per_query_diagnostics,
    target_summary,
)


def test_aggregate_mean_normalization_binds_workload_size() -> None:
    assert normalize_mean(45.22179440722701, 1963) == 88770.38242138662


def test_target_summary_and_marginals_are_explicit() -> None:
    left = target_summary([10.0, 12.0, 14.0])
    right = target_summary([8.0, 9.0, 10.0])
    assert left["stddev_definition"] == "sample"
    assert adjacent_quality(left, right) == 0.25
    assert adjacent_stability(left, right) == 1 / 3
    assert adjacent_stability(target_summary([0.0]), right) is None


def test_knee_and_gate_fixtures() -> None:
    assert knee_structure([0.2, 0.01], [0.8, -0.2])["pattern"] == "A"
    assert knee_structure([-0.01, 0.4], [0.7, 0.8])["pattern"] == "B"
    native = {"quality_ordering": [1000, 300, 100], "quality_gain_ordering": ["first", "second"], "normalized_range_trend": ["improve", "worsen"], "cv_trend": ["improve", "worsen"], "knee": {"pattern": "A"}}
    reservoir = {"quality_ordering": [1000, 100, 300], "quality_gain_ordering": ["second", "first"], "normalized_range_trend": ["improve", "improve"], "cv_trend": ["improve", "improve"], "knee": {"pattern": "B"}}
    result = gate(native, reservoir)
    assert result["target_selection_fidelity_qualified"] is False
    assert len(result["failed_conditions"]) == 5


def test_canonical_per_query_diagnostic_is_unpaired_descriptive() -> None:
    native = [{"query_id": "q1", "q_error": 2.0}, {"query_id": "q2", "q_error": 4.0}]
    reservoir = [{"query_id": "q1", "q_error": 3.0}, {"query_id": "q2", "q_error": 2.0}]
    result = per_query_diagnostics(native, reservoir)
    assert result["log_base"] == "natural"
    assert result["count"] == 2
