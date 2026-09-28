from __future__ import annotations

import csv
from pathlib import Path
from types import SimpleNamespace

import pytest

from pg_extstats_advisor.candidates.model import CandidateCatalog
from pg_extstats_advisor.models import Candidate, CandidateId, MechanismKind, QueryId, WorkloadQuery
from pg_extstats_advisor.payloads.repository import NativePayloadState
from pg_extstats_advisor.screening import (
    build_candidate_set,
    build_singleton_profile_from_csv,
    build_singleton_profile_from_rows,
    load_artifact,
    write_artifact,
)
from pg_extstats_advisor.workload.model import Workload


class FakeCostModel:
    digest = "cost-model-test"
    unit = "test-unit"

    def estimate_candidate(self, candidate: Candidate) -> float:
        return 1.0 if candidate.mechanism is MechanismKind.MCV else 2.0


def prepared_fixture() -> tuple[SimpleNamespace, FakeCostModel]:
    candidates = tuple(
        Candidate(
            CandidateId(f"cand-{index}"),
            42,
            "public.t",
            MechanismKind.MCV if index != 1 else MechanismKind.FD,
            (f"c{index}", f"d{index}"),
            (("statistics_name", f"s{index}"),),
            index,
            index + 1,
        )
        for index in range(3)
    )
    catalog = CandidateCatalog(candidates)
    workload = Workload(
        "screen-test",
        (WorkloadQuery(QueryId("q"), "SELECT 1", 1.0, "public.t", frozenset({42})),),
    )
    repository = SimpleNamespace(
        digest="repository-test",
        postgres_version="16.14",
        patch_commit="patch-test",
        by_candidate={
            candidate.candidate_id: SimpleNamespace(state=NativePayloadState.PRESENT)
            for candidate in candidates
        },
    )
    incidence = SimpleNamespace(
        by_candidate={candidate.candidate_id: frozenset({QueryId("q")}) for candidate in candidates}
    )
    prepared = SimpleNamespace(
        catalog=catalog,
        candidate_catalog_digest="raw-catalog-test",
        workload=workload,
        repository=repository,
        incidence=incidence,
        incidence_digest="incidence-test",
    )
    return prepared, FakeCostModel()


def test_import_profile_and_screen_candidate_set_are_deterministic(tmp_path: Path) -> None:
    prepared, model = prepared_fixture()
    source = tmp_path / "singleton.csv"
    with source.open("w", newline="") as handle:
        writer = csv.DictWriter(
            handle,
            fieldnames=[
                "candidate_id",
                "precedence_rank",
                "mechanism",
                "realization_state",
                "maintenance_cost_numeric",
                "singleton_objective",
                "singleton_improvement",
            ],
        )
        writer.writeheader()
        writer.writerows(
            {
                "candidate_id": f"cand-{index}",
                "precedence_rank": index,
                "mechanism": "fd" if index == 1 else "mcv",
                "realization_state": "PRESENT",
                "maintenance_cost_numeric": 2.0 if index == 1 else 1.0,
                "singleton_objective": objective,
                "singleton_improvement": 10.0 - objective,
            }
            for index, objective in ((0, 9.0), (1, 8.0), (2, 9.5))
        )

    profile = build_singleton_profile_from_csv(source, prepared, model)
    assert profile["evaluator_provenance"]["mode"] == "imported-frozen-profile"
    assert [row["candidate_id"] for row in profile["candidates"]] == [
        "cand-1",
        "cand-0",
        "cand-2",
    ]
    screened = build_candidate_set(profile, prepared, model, top_fraction=0.5)
    assert screened["retained_count"] == 2
    assert screened["candidate_ids"] == ["cand-1", "cand-0"]
    path = tmp_path / "candidate-set.json"
    digest = write_artifact(path, screened)
    assert load_artifact(path)["digest"] == digest
    assert screened["experimental_role"] == "development"

    assert build_candidate_set(profile, prepared, model, top_fraction=1.0)[
        "retained_count"
    ] == 3
    assert build_candidate_set(profile, prepared, model, top_fraction=0.34)[
        "retained_count"
    ] == 2
    with pytest.raises(ValueError, match="top_fraction"):
        build_candidate_set(profile, prepared, model, top_fraction=0.0)


def test_screening_tie_breaks_by_precedence() -> None:
    prepared, model = prepared_fixture()
    profile = build_singleton_profile_from_rows(
        [
            {
                "candidate_id": f"cand-{index}",
                "precedence_rank": index,
                "mechanism": "fd" if index == 1 else "mcv",
                "realization_state": "PRESENT",
                "maintenance_cost_numeric": 2.0 if index == 1 else 1.0,
                "singleton_objective": 9.0,
                "singleton_improvement": 1.0,
            }
            for index in range(3)
        ],
        prepared,
        model,
        10.0,
        evaluator_provenance={"mode": "test", "native_singleton_evaluations": 0},
    )
    screened = build_candidate_set(profile, prepared, model, top_fraction=1.0)
    assert screened["candidate_ids"] == ["cand-0", "cand-2", "cand-1"]


def test_screening_rejects_lineage_mismatch() -> None:
    prepared, model = prepared_fixture()
    with pytest.raises(ValueError, match="singleton profile digest"):
        build_candidate_set(
            {
                "artifact_type": "singleton-profile",
                "format_version": 1,
                "workload_digest": "wrong",
            },
            prepared,
            model,
            top_fraction=0.5,
        )


def test_unpriced_singleton_profile_has_no_cost_tiebreak_and_cannot_screen() -> None:
    prepared, _model = prepared_fixture()
    profile = build_singleton_profile_from_rows(
        [
            {
                "candidate_id": f"cand-{index}",
                "precedence_rank": index,
                "mechanism": "fd" if index == 1 else "mcv",
                "realization_state": "PRESENT",
                "maintenance_cost_numeric": None,
                "singleton_objective": 9.0,
                "singleton_improvement": 1.0,
            }
            for index in range(3)
        ],
        prepared,
        None,
        10.0,
        evaluator_provenance={"mode": "test-unpriced", "native_singleton_evaluations": 0},
    )
    assert profile["maintenance_cost_status"] == "unavailable_for_DMV"
    assert profile["ranking_semantics"] == [
        "descending singleton improvement",
        "ascending candidate precedence",
        "ascending candidate ID",
    ]
    with pytest.raises(ValueError, match="maintenance cost model"):
        build_candidate_set(profile, prepared, None, top_fraction=0.5)
