from __future__ import annotations

import pytest

from pg_extstats_advisor.analysis.singleton import screen_singleton_rows, top_fraction_count


def rows() -> list[dict[str, object]]:
    return [
        {
            "candidate_id": "b",
            "singleton_improvement": "2.0",
            "maintenance_cost_numeric": "1.0",
            "precedence_rank": "1",
        },
        {
            "candidate_id": "a",
            "singleton_improvement": "2.0",
            "maintenance_cost_numeric": "1.0",
            "precedence_rank": "0",
        },
        {
            "candidate_id": "c",
            "singleton_improvement": "1.0",
            "maintenance_cost_numeric": "1.0",
            "precedence_rank": "2",
        },
    ]


def test_top_fraction_uses_ceiling_rounding() -> None:
    assert top_fraction_count(4506, 0.05) == 226
    assert top_fraction_count(3, 0.01) == 1


def test_screening_reuses_m29_deterministic_tie_order() -> None:
    selected = screen_singleton_rows(rows(), 0.5)
    assert [row["candidate_id"] for row in selected] == ["a", "b"]
    assert [row["screening_rank"] for row in selected] == [1, 2]
    assert selected[0]["singleton_percentile"] == pytest.approx(100.0)


def test_screening_rejects_invalid_fraction() -> None:
    with pytest.raises(ValueError):
        top_fraction_count(3, 0.0)
