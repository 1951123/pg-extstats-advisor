from __future__ import annotations

import json
from pathlib import Path

from pg_extstats_advisor.deploy.sql import build_deployment_plan
from pg_extstats_advisor.models import CandidateId
from pg_extstats_advisor.orchestration import (
    load_maintenance_model,
    load_prepared_run,
    load_search_result,
)
from pg_extstats_advisor.screening import load_artifact, validate_candidate_set

ROOT = Path(__file__).parents[2]
M13 = ROOT / "experiments/census-m2-13-formal-dev-workflow"
M14 = ROOT / "experiments/census-m2-14-screened-validation-smoke"


def test_screened_recommendation_resolves_and_generates_prefix_ddl() -> None:
    run = M13 / "run"
    prepared = load_prepared_run(run)
    model = load_maintenance_model(run)
    visible = validate_candidate_set(
        load_artifact(M13 / "screened-candidate-set.json"), prepared, model
    )
    recommendation = json.loads((M13 / "recommendation.json").read_text())
    assert set(recommendation["selected_design"]) <= set(prepared.catalog.by_id)
    assert recommendation["candidate_set_mode"] == "screened"
    assert recommendation["experimental_role"] == "development"
    for item in recommendation["selected_candidates"]:
        candidate = prepared.catalog.by_id[CandidateId(item["candidate_id"])]
        assert item["mechanism"] == candidate.mechanism.value
        assert item["relation"] == candidate.relation_name
        assert tuple(item["columns"]) == candidate.attributes
        assert item["precedence_rank"] == candidate.precedence_rank

    result = load_search_result(run)
    accepted = [str(record.move.add) for record in result.trajectory if record.accepted]
    for prefix in (1, 5, 10):
        design = visible.normalize_design({CandidateId(item) for item in accepted[:prefix]})
        plan = build_deployment_plan(
            design,
            visible,
            repository_digest=prepared.repository.digest,
            workload_digest=prepared.workload.digest,
            statistics_target=100,
            cost_model_digest=model.digest,
            validation_relations=("public.climate",),
        )
        assert len(plan.create_statements) == prefix
        assert {item.candidate_id for item in plan.ordered_candidates} == set(accepted[:prefix])
        assert len(plan.statistics_names) == len(set(plan.statistics_names))
        assert all("CREATE STATISTICS" in statement for statement in plan.create_statements)


def test_m2_14_a_recomputation_and_cleanup_artifacts_are_consistent() -> None:
    result = load_search_result(M13 / "run")
    accepted = [record for record in result.trajectory if record.accepted]
    for prefix in (1, 5, 10):
        artifact = json.loads(
            (M14 / f"prefix-{prefix}" / "validation-result.json").read_text()
        )
        assert artifact["experimental_role"] == "development-smoke"
        assert artifact["workload_query_count"] == 468
        assert artifact["a_exact_match"] is True
        assert artifact["a_hypothetical_objective"] == accepted[prefix - 1].after_objective
        cleanup = json.loads(
            (M14 / f"prefix-{prefix}" / "cleanup-result.json").read_text()
        )
        assert cleanup["success"] is True
        assert cleanup["residual_statistics_count"] == 0


def test_m2_14_artifacts_are_compact_and_no_trajectory_is_persisted() -> None:
    sizes = json.loads((M14 / "artifact-size-summary.json").read_text())
    assert sizes["trajectory_persisted"] is False
    assert sizes["largest_new_artifact_bytes"] < 100_000
    assert not (M14 / "trajectory.json").exists()
