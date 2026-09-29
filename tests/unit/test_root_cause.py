import pytest

from pg_extstats_advisor.analysis.root_cause import (
    adjacent_target_gains,
    frequency_differences,
    jaccard,
    mcv_membership,
    normalize_ndistinct,
    parse_predicates,
    quantiles,
    top_contributors,
    trimmed_aggregate,
)


def test_parse_predicates_preserves_in_and_conjunctions() -> None:
    relation, predicates, structure = parse_predicates(
        "SELECT COUNT(*) FROM DMV WHERE State IN ('NY','NJ') AND Body_Type = 'SUBN'"
    )
    assert relation == "dmv"
    assert structure == "AND"
    assert predicates[0].column == "state"
    assert predicates[0].operator == "IN"
    assert predicates[0].constants == ("NY", "NJ")
    assert predicates[1].constants == ("SUBN",)


def test_membership_and_negative_ndistinct_normalization() -> None:
    stat = {
        "mcv_values": ["NY", "NJ"],
        "mcv_frequencies": [0.7, 0.1],
        "n_distinct": -0.25,
        "histogram_bounds": [],
    }
    assert mcv_membership(stat, "NY")["selectivity_path"] == "MCV_EXACT"
    assert mcv_membership(stat, "CA")["selectivity_path"] == "MCV_MISS_NDISTINCT_FALLBACK"
    assert normalize_ndistinct(-0.25, 1000) == 250.0


def test_semantic_diffs_and_quantiles_are_deterministic() -> None:
    left = {"mcv_values": ["A", "B"], "mcv_frequencies": [0.2, 0.3]}
    right = {"mcv_values": ["B", "C"], "mcv_frequencies": [0.4, 0.1]}
    assert jaccard(left["mcv_values"], right["mcv_values"]) == 1 / 3
    assert frequency_differences(left, right) == pytest.approx([0.1])
    assert quantiles([1, 2, 3, 4])["p50"] == 2


def test_frozen_top_contributors_and_trimmed_target_differential() -> None:
    rows = [{"query_id": "q2", "qerror_reduction": 1}, {"query_id": "q1", "qerror_reduction": 3}]
    assert [row["query_id"] for row in top_contributors(rows)] == ["q1", "q2"]
    vector = [{"query_id": "q1", "q_error": 10}, {"query_id": "q2", "q_error": 2}]
    assert trimmed_aggregate(vector, {"q1"}) == 2
    assert adjacent_target_gains({100: 10, 300: 8, 1000: 7}) == [0.2, 0.125]
