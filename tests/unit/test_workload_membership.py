from __future__ import annotations

import json
from pathlib import Path

import pytest

from pg_extstats_advisor.prepare.workload import ingest_workload


class _Result:
    def __init__(self, rows):
        self.rows = rows

    def fetchone(self):
        return self.rows[0] if self.rows else None

    def fetchall(self):
        return self.rows


class _Connection:
    def execute(self, sql, params=()):
        if "FROM pg_class" in sql:
            return _Result([("public", "t", 42)])
        if "FROM pg_attribute" in sql:
            return _Result([(1, "a", "integer", False), (2, "b", "integer", False)])
        raise AssertionError(sql)


def _workload(path: Path, truths: list[float]) -> None:
    path.write_text(
        json.dumps(
            {
                "schema_version": 1,
                "workload_id": "w",
                "queries": [
                    {
                        "query_id": f"q{i}",
                        "sql": "SELECT * FROM public.t WHERE a=1 AND b=1",
                        "truth": truth,
                        "target_relation": "public.t",
                    }
                    for i, truth in enumerate(truths)
                ],
            }
        )
    )


def test_positive_truth_only_filters_explicitly_and_preserves_order(tmp_path: Path) -> None:
    path = tmp_path / "workload.json"
    _workload(path, [3, 0, 2])
    result = ingest_workload(path, _Connection(), objective_membership_policy="positive_truth_only")
    assert [query.query_id for query in result.workload.queries] == ["q0", "q2"]
    assert result.raw_query_count == 3
    assert result.excluded_query_ids == ("q1",)
    assert result.objective_membership_policy == "positive_truth_only"
    assert result.raw_workload_digest != result.effective_workload_digest


def test_default_policy_rejects_zero_truth(tmp_path: Path) -> None:
    path = tmp_path / "workload.json"
    _workload(path, [1, 0])
    with pytest.raises(ValueError, match="positive_truth_only"):
        ingest_workload(path, _Connection())


def test_negative_truth_fails_closed_even_when_filtering(tmp_path: Path) -> None:
    path = tmp_path / "workload.json"
    _workload(path, [1, -1])
    with pytest.raises(ValueError, match="negative truth"):
        ingest_workload(path, _Connection(), objective_membership_policy="positive_truth_only")
