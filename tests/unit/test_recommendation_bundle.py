from __future__ import annotations

import json
from pathlib import Path

import pytest

from pg_extstats_advisor.candidates.model import CandidateCatalog
from pg_extstats_advisor.deploy.bundle import (
    RecommendationBundle,
    validate_recommendation_bundle,
)
from pg_extstats_advisor.deploy.sql import build_deployment_plan, build_rollback_statements
from pg_extstats_advisor.models import Candidate, CandidateId, Design, MechanismKind


def _catalog() -> CandidateCatalog:
    return CandidateCatalog(
        (
            Candidate(CandidateId("m"), 1, "public.t", MechanismKind.MCV, ("a", "b"), (), 0, 10),
            Candidate(CandidateId("f"), 1, "public.t", MechanismKind.FD, ("b", "c"), (), 1, 11),
        )
    )


def _bundle() -> RecommendationBundle:
    catalog = _catalog()
    plan = build_deployment_plan(
        Design((CandidateId("m"),)),
        catalog,
        repository_digest="repo",
        workload_digest="workload",
        statistics_target=100,
        cost_model_digest="model",
    )
    return RecommendationBundle(
        100,
        {"problem_digest": "problem"},
        {"realization_digest": "sample"},
        {"digest": "workload"},
        {"digest": "truth"},
        "catalog",
        "model",
        20.0,
        10.0,
        ("m",),
        "1.25",
        {"termination_reason": "add-local-optimum"},
        plan.create_statements + plan.target_statements + plan.analyze_statements,
        build_rollback_statements(plan),
        {"postgres_version": "16.14", "requires_patch": False},
    )


def test_recommendation_round_trip_and_validation(tmp_path: Path) -> None:
    path = tmp_path / "recommendation.json"
    bundle = _bundle()
    digest = bundle.write(path)
    assert RecommendationBundle.load(path).digest == digest
    result = validate_recommendation_bundle(path, candidate_ids={"m", "f"}, expected_target=100)
    assert result["evaluated_statistics_target"] == 100
    assert result["statistics_target_role"] == "evaluated_external_configuration"
    assert result["rollback_ddl"] == list(build_rollback_statements(
        build_deployment_plan(
            Design((CandidateId("m"),)),
            _catalog(),
            repository_digest="repo",
            workload_digest="workload",
            statistics_target=100,
            cost_model_digest="model",
        )
    ))


def test_recommendation_corruption_and_target_mutation_fail_closed(tmp_path: Path) -> None:
    path = tmp_path / "recommendation.json"
    _bundle().write(path)
    raw = json.loads(path.read_text())
    raw["evaluated_statistics_target"] = 300
    path.write_text(json.dumps(raw))
    with pytest.raises(ValueError, match="digest"):
        RecommendationBundle.load(path)


def test_recommendation_never_emits_database_target_mutation() -> None:
    with pytest.raises(ValueError, match="database target"):
        RecommendationBundle(
            100,
            {},
            {},
            {},
            {},
            "catalog",
            "model",
            1.0,
            1.0,
            (),
            "0",
            {},
            ("ALTER DATABASE x SET default_statistics_target = 300",),
            (),
            {},
        )
