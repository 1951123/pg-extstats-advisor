from __future__ import annotations

import json
from dataclasses import replace

import pytest

from pg_extstats_advisor.candidates.model import CandidateCatalog
from pg_extstats_advisor.deploy.model import DeploymentResult
from pg_extstats_advisor.deploy.sql import (
    build_deployment_plan,
    quote_identifier,
    statistics_name,
)
from pg_extstats_advisor.models import (
    Candidate,
    CandidateId,
    Design,
    EvaluationState,
    MechanismKind,
    QueryEvaluation,
    QueryId,
)
from pg_extstats_advisor.validate.deployment import build_validation_result
from pg_extstats_advisor.validate.model import PayloadFingerprint, ValidationProvenance
from pg_extstats_advisor.validate.report import render_validation_report


def catalog() -> CandidateCatalog:
    return CandidateCatalog(
        (
            Candidate(
                CandidateId("mcv/a"),
                1,
                "Odd Schema.Odd Table",
                MechanismKind.MCV,
                ("a", 'b"x'),
                (),
                0,
                10,
            ),
            Candidate(CandidateId("fd b"), 1, "public.t", MechanismKind.FD, ("b", "c"), (), 1, 11),
        )
    )


def state(design: Design, estimate: float, objective: float) -> EvaluationState:
    return EvaluationState(
        design,
        (QueryEvaluation(QueryId("q"), estimate, 10, objective, "test"),),
        objective,
        "repo",
        "workload",
        "16.14",
        "test",
        (QueryId("q"),),
        (),
    )


def test_sql_generation_is_safe_ordered_and_deterministic() -> None:
    items = catalog()
    design = items.normalize_design({CandidateId("fd b"), CandidateId("mcv/a")})
    first = build_deployment_plan(
        design,
        items,
        repository_digest="repo",
        workload_digest="workload",
        statistics_target=100,
    )
    second = build_deployment_plan(
        design,
        items,
        repository_digest="repo",
        workload_digest="workload",
        statistics_target=100,
    )
    assert first == second
    assert first.ordered_candidates == items.candidates
    assert '(mcv) ON "a", "b""x"' in first.create_statements[0]
    assert 'FROM "Odd Schema"."Odd Table"' in first.create_statements[0]
    assert "(dependencies)" in first.create_statements[1]
    assert statistics_name("mcv/a") == statistics_name("mcv/a")
    assert len(statistics_name("x" * 200)) <= 63
    assert quote_identifier('a"b') == '"a""b"'


def test_empty_design_still_analyzes_explicit_validation_relation() -> None:
    plan = build_deployment_plan(
        Design(()),
        catalog(),
        repository_digest="repo",
        workload_digest="workload",
        validation_relations=("public.t",),
    )
    assert plan.create_statements == ()
    assert plan.analyze_statements == ('ANALYZE "public"."t"',)


def test_validation_comparison_payloads_serialization_and_lineage() -> None:
    items = catalog()
    design = items.normalize_design({CandidateId("mcv/a")})
    plan = build_deployment_plan(
        design, items, repository_digest="repo", workload_digest="workload"
    )
    deployment = DeploymentResult(
        plan, (), plan.analyze_statements, "disposable", "16.14", "a", "b", True
    )
    frozen_fingerprint = PayloadFingerprint("mcv/a", "mcv", "old", "a" * 64, 4, 10, 1, "relation")
    fresh_fingerprint = replace(
        frozen_fingerprint, definition_identity="new", payload_sha256="b" * 64, catalog_oid=20
    )
    provenance = ValidationProvenance(
        "commit",
        "upstream",
        "patch",
        "workload",
        "repo",
        None,
        None,
        None,
        None,
        plan.sql_digest,
        "disposable",
        None,
        "16.14",
    )
    result = build_validation_result(
        frozen=state(design, 8, 1.25),
        fresh=state(design, 12, 1.2),
        frozen_fingerprints=(frozen_fingerprint,),
        fresh_fingerprints=(fresh_fingerprint,),
        deployment=deployment,
        provenance=provenance,
    )
    comparison = result.per_query[0]
    assert comparison.absolute_estimate_difference == 4
    assert comparison.relative_estimate_difference == 0.5
    assert result.aggregate.absolute_objective_drift == pytest.approx(-0.05)
    assert result.payload_comparisons[0].same_realization is False
    assert json.loads(render_validation_report(result))["provenance"]["repository_digest"] == "repo"

    with pytest.raises(ValueError, match="repository lineage"):
        build_validation_result(
            frozen=state(design, 8, 1.25),
            fresh=state(design, 12, 1.2),
            frozen_fingerprints=(frozen_fingerprint,),
            fresh_fingerprints=(fresh_fingerprint,),
            deployment=deployment,
            provenance=replace(provenance, repository_digest="wrong"),
        )
