"""Run the compact M2.14 screened recommendation lifecycle smoke.

The smoke deliberately uses the frozen M2.13 result and only validates nested
accepted-order prefixes. It does not run search, acquisition, or singleton
profiling.
"""

from __future__ import annotations

import hashlib
import json
import math
import statistics
import sys
import time
from pathlib import Path
from typing import Any

import psycopg

from pg_extstats_advisor.deploy.physical import PhysicalDeployer
from pg_extstats_advisor.deploy.sql import build_deployment_plan
from pg_extstats_advisor.evaluator.native import NativeEvaluator
from pg_extstats_advisor.models import CandidateId
from pg_extstats_advisor.orchestration import (
    _digest,
    load_maintenance_model,
    load_prepared_run,
    load_search_result,
)
from pg_extstats_advisor.postgres.adapter import PostgresAdapter
from pg_extstats_advisor.screening import load_artifact, validate_candidate_set
from pg_extstats_advisor.validate.deployment import (
    build_validation_result,
    collect_fresh_payload_fingerprints,
    evaluate_physical,
    frozen_payload_fingerprints,
)
from pg_extstats_advisor.validate.model import ValidationProvenance

ROOT = Path(__file__).resolve().parents[1]
FORMAL_ROOT = ROOT / "experiments/census-m2-13-formal-dev-workflow"
RUN_ROOT = FORMAL_ROOT / "run"
OUTPUT_ROOT = ROOT / "experiments/census-m2-14-screened-validation-smoke"
BUILD_PROVENANCE = ROOT / "experiments/environment/postgresql-16.14-build.json"
PREFIXES = (1, 5, 10)


def _write(path: Path, value: Any) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    temporary = path.with_suffix(path.suffix + ".tmp")
    temporary.write_text(json.dumps(value, sort_keys=True, indent=2) + "\n")
    temporary.replace(path)


def _digest_file(path: Path) -> str:
    return hashlib.sha256(path.read_bytes()).hexdigest()


def _percentile(values: list[float], probability: float) -> float:
    ordered = sorted(values)
    if not ordered:
        return 0.0
    if len(ordered) == 1:
        return ordered[0]
    position = probability * (len(ordered) - 1)
    lower, upper = math.floor(position), math.ceil(position)
    if lower == upper:
        return ordered[lower]
    return ordered[lower] + (ordered[upper] - ordered[lower]) * (position - lower)


def _stats_count(connection: Any, names: tuple[str, ...]) -> int:
    if not names:
        return 0
    row = connection.execute(
        "SELECT count(*) FROM pg_statistic_ext e "
        "JOIN pg_namespace n ON n.oid=e.stxnamespace "
        "WHERE n.nspname='public' AND e.stxname = ANY(%s)",
        (list(names),),
    ).fetchone()
    return int(row[0])


def _accepted_ids(result: Any) -> list[str]:
    return [str(record.move.add) for record in result.trajectory if record.accepted]


def _compact_validation(
    validation: Any,
    *,
    prefix: int,
    accepted_order_ids: list[str],
    expected_objective: float,
    deployment_elapsed: float,
    cleanup_success: bool,
    residual_stats_count: int,
    provenance: dict[str, Any],
) -> dict[str, Any]:
    frozen = validation.frozen_hypothetical_state
    fresh = validation.fresh_physical_state
    deltas = [item.fresh_q_error - item.frozen_q_error for item in validation.per_query]
    improved = sum(item < 0 for item in deltas)
    unchanged = sum(item == 0 for item in deltas)
    worsened = sum(item > 0 for item in deltas)
    return {
        "format_version": 1,
        "artifact_type": "m2-14-prefix-validation-smoke",
        "experimental_role": "development-smoke",
        "prefix_size": prefix,
        "accepted_order_candidate_ids": accepted_order_ids,
        "selected_candidate_ids": list(validation.selected_design.candidate_ids),
        "selected_mechanisms": [
            item.mechanism.value for item in validation.deployment.plan.ordered_candidates
        ],
        "ddl": {
            "create_count": len(validation.deployment.plan.create_statements),
            "target_count": len(validation.deployment.plan.target_statements),
            "analyze_count": len(validation.deployment.plan.analyze_statements),
            "sql_digest": validation.deployment.plan.sql_digest,
            "create_statements": list(validation.deployment.plan.create_statements),
            "target_statements": list(validation.deployment.plan.target_statements),
            "analyze_statements": list(validation.deployment.plan.analyze_statements),
        },
        "workload_query_count": len(validation.per_query),
        "a_hypothetical_objective": frozen.aggregate_objective,
        "a_expected_objective_from_m2_13_trajectory": expected_objective,
        "a_exact_match": frozen.aggregate_objective == expected_objective,
        "b_same_realization_control": None,
        "b_objective": None,
        "b_exact_match": None,
        "c_fresh_physical_objective": fresh.aggregate_objective,
        "a_vs_c_absolute_difference": validation.aggregate.absolute_objective_drift,
        "a_vs_c_relative_difference": validation.aggregate.relative_objective_drift,
        "a_vs_c_improved_query_count": improved,
        "a_vs_c_unchanged_query_count": unchanged,
        "a_vs_c_worsened_query_count": worsened,
        "a_vs_c_max_absolute_qerror_delta": max((abs(item) for item in deltas), default=0.0),
        "a_vs_c_median_qerror_delta": statistics.median(deltas) if deltas else 0.0,
        "a_vs_c_p90_qerror_delta": _percentile(deltas, 0.9),
        "per_query_changed_count": sum(item.estimate_changed for item in validation.per_query),
        "frozen_payload_count": len(validation.frozen_payload_fingerprints),
        "fresh_payload_count": len(validation.fresh_payload_fingerprints),
        "fresh_payload_states": {
            state: sum(item.state == state for item in validation.fresh_payload_fingerprints)
            for state in ("PRESENT", "ABSENT_NATIVE")
        },
        "analyze_elapsed_seconds": validation.deployment.analyze_elapsed_seconds,
        "deployment_elapsed_seconds": deployment_elapsed,
        "validation_elapsed_seconds": None,
        "cleanup_success": cleanup_success,
        "residual_smoke_statistics_count": residual_stats_count,
        "provenance": provenance,
    }


def main() -> int:
    if OUTPUT_ROOT.exists():
        raise FileExistsError(f"output already exists: {OUTPUT_ROOT}")
    OUTPUT_ROOT.mkdir(parents=True)

    prepared = load_prepared_run(RUN_ROOT)
    model = load_maintenance_model(RUN_ROOT)
    search_result = load_search_result(RUN_ROOT)
    candidate_set = load_artifact(FORMAL_ROOT / "screened-candidate-set.json")
    visible = validate_candidate_set(candidate_set, prepared, model)
    recommendation = json.loads((FORMAL_ROOT / "recommendation.json").read_text())
    recommendation_digest = recommendation.pop("digest")
    if _digest(recommendation) != recommendation_digest:
        raise ValueError("M2.13 recommendation digest mismatch")
    recommendation["digest"] = recommendation_digest
    raw_ids = set(prepared.catalog.by_id)
    if set(recommendation["selected_design"]) - raw_ids:
        raise ValueError("recommendation contains a candidate absent from raw catalog")
    if recommendation["candidate_set_mode"] != "screened":
        raise ValueError("recommendation is not screened")
    if recommendation["experimental_role"] != "development":
        raise ValueError("recommendation is not marked development")
    if recommendation["selected_design"] != list(search_result.selected_design.candidate_ids):
        raise ValueError("recommendation/search selected design mismatch")

    accepted = _accepted_ids(search_result)
    if len(accepted) < PREFIXES[-1]:
        raise ValueError("M2.13 accepted sequence is shorter than smoke prefixes")
    accepted_records = [record for record in search_result.trajectory if record.accepted]
    expected_by_prefix = {
        prefix: accepted_records[prefix - 1].after_objective for prefix in PREFIXES
    }

    source_search_result_digest = json.loads(
        (RUN_ROOT / "search" / "result.json").read_text()
    )["digest"]
    source_dsn = str(json.loads((RUN_ROOT / "config.json").read_text())["database"]["source_dsn"])
    acquisition_dsn = str(
        json.loads((RUN_ROOT / "config.json").read_text())["database"]["acquisition_dsn"]
    )
    build = json.loads(BUILD_PROVENANCE.read_text())
    patch_digest = _digest_file(ROOT / "pg/patches/postgresql-16.14-hypothetical-extstats.patch")
    baseline_repo_digest = prepared.repository.digest
    baseline_catalog_digest = prepared.candidate_catalog_digest
    baseline_model_digest = model.digest
    prefix_summaries: list[dict[str, Any]] = []
    lifecycle = {
        "create": 0,
        "analyze": 0,
        "drop": 0,
        "hypothetical_evaluations": 0,
        "singleton_native_evaluations": 0,
        "search_native_evaluations": 0,
    }

    with (
        psycopg.connect(acquisition_dsn) as acquisition_connection,
        psycopg.connect(source_dsn) as physical_connection,
    ):
        evaluator = NativeEvaluator(
            prepared.workload,
            prepared.repository,
            prepared.incidence,
            PostgresAdapter(acquisition_connection, prepared.repository),
        )
        lifecycle["hypothetical_evaluations"] = len(PREFIXES)
        for prefix in PREFIXES:
            prefix_root = OUTPUT_ROOT / f"prefix-{prefix}"
            prefix_root.mkdir()
            ids = accepted[:prefix]
            design = visible.normalize_design({CandidateId(item) for item in ids})
            plan = build_deployment_plan(
                design,
                visible,
                repository_digest=prepared.repository.digest,
                workload_digest=prepared.workload.digest,
                statistics_target=int(json.loads((RUN_ROOT / "prepare-summary.json").read_text())["statistics_target"]),
                cost_model_digest=model.digest,
                search_provenance=search_result.config.algorithm_version,
                validation_relations=tuple(q.target_relation for q in prepared.workload.queries),
            )
            if _stats_count(physical_connection, plan.statistics_names) != 0:
                raise RuntimeError(f"prefix {prefix} has pre-existing smoke statistics")
            a_state = evaluator.evaluate_design(design)
            deployment_started = time.perf_counter()
            deployment = PhysicalDeployer(physical_connection, "m2-14-development-smoke").deploy(plan)
            deployment_elapsed = time.perf_counter() - deployment_started
            validation_started = time.perf_counter()
            fresh = evaluate_physical(
                physical_connection, prepared.workload, design, prepared.repository.digest
            )
            fresh_fingerprints = collect_fresh_payload_fingerprints(
                physical_connection, deployment
            )
            validation_elapsed = time.perf_counter() - validation_started
            provenance = ValidationProvenance(
                "m2-14-development-smoke",
                prepared.repository.upstream_sha256,
                prepared.repository.patch_commit,
                prepared.workload.digest,
                prepared.repository.digest,
                model.digest,
                str(search_result.budget.value),
                search_result.budget.unit,
                search_result.config.algorithm_version,
                plan.sql_digest,
                "m2-14-development-smoke",
                plan.statistics_target,
                deployment.postgres_version,
            )
            validation = build_validation_result(
                frozen=a_state,
                fresh=fresh,
                frozen_fingerprints=frozen_payload_fingerprints(
                    prepared.repository, design
                ),
                fresh_fingerprints=fresh_fingerprints,
                deployment=deployment,
                provenance=provenance,
            )
            if not validation.frozen_hypothetical_state.aggregate_objective == expected_by_prefix[prefix]:
                raise RuntimeError(f"prefix {prefix} hypothetical objective mismatch")
            created_ids = [item.candidate_id for item in deployment.created_statistics]
            if set(created_ids) != set(ids):
                raise RuntimeError(f"prefix {prefix} deployment identity mismatch")
            lifecycle["create"] += len(plan.create_statements)
            lifecycle["analyze"] += len(plan.analyze_statements)
            compact_provenance = {
                "source_recommendation_digest": recommendation_digest,
                "source_search_result_digest": source_search_result_digest,
                "screened_candidate_set_digest": candidate_set["digest"],
                "screened_candidate_catalog_digest": candidate_set[
                    "screened_candidate_catalog_digest"
                ],
                "singleton_profile_digest": candidate_set["singleton_profile_digest"],
                "raw_candidate_catalog_digest": prepared.candidate_catalog_digest,
                "workload_digest": prepared.workload.digest,
                "repository_digest": prepared.repository.digest,
                "maintenance_model_digest": model.digest,
                "postgres_version": deployment.postgres_version,
                "postgres_binary_path": str(Path(build["install_prefix"]) / "bin" / "postgres"),
                "build_recipe_digest": build["build_recipe_digest"],
                "patch_sha256": patch_digest,
                "search_mode": "add-only",
                "screening_fraction": candidate_set["top_fraction"],
                "experimental_role": "development-smoke",
                "search_hot_path_analyze": False,
                "validation_fresh_analyze": True,
                "prefix_size": prefix,
            }
            cleanup_names = PhysicalDeployer(
                physical_connection, "m2-14-development-smoke"
            ).cleanup(deployment)
            lifecycle["drop"] += len(cleanup_names)
            residual = _stats_count(physical_connection, plan.statistics_names)
            if residual != 0:
                raise RuntimeError(f"prefix {prefix} cleanup left {residual} statistics")
            compact = _compact_validation(
                validation,
                prefix=prefix,
                accepted_order_ids=ids,
                expected_objective=expected_by_prefix[prefix],
                deployment_elapsed=deployment_elapsed,
                cleanup_success=True,
                residual_stats_count=residual,
                provenance=compact_provenance,
            )
            compact["validation_elapsed_seconds"] = validation_elapsed
            compact["cleanup_dropped_statistics"] = list(cleanup_names)
            _write(prefix_root / "design.json", {"design": ids, "prefix_size": prefix})
            _write(
                prefix_root / "ddl.json",
                {
                    "experimental_role": "development-smoke",
                    "candidate_ids": ids,
                    "create": list(plan.create_statements),
                    "target": list(plan.target_statements),
                    "analyze": list(plan.analyze_statements),
                    "sql_digest": plan.sql_digest,
                },
            )
            _write(prefix_root / "validation-result.json", compact)
            _write(
                prefix_root / "cleanup-result.json",
                {
                    "experimental_role": "development-smoke",
                    "dropped_statistics": list(cleanup_names),
                    "residual_statistics_count": residual,
                    "success": residual == 0,
                },
            )
            prefix_summaries.append(compact)

    if prepared.repository.digest != baseline_repo_digest:
        raise RuntimeError("frozen repository digest changed")
    if prepared.candidate_catalog_digest != baseline_catalog_digest:
        raise RuntimeError("candidate catalog digest changed")
    if model.digest != baseline_model_digest:
        raise RuntimeError("maintenance model digest changed")
    final_residual = 0
    with psycopg.connect(source_dsn) as connection:
        final_names = tuple(
            name for summary in prefix_summaries for name in summary["cleanup_dropped_statistics"]
        )
        # Exact names are checked per prefix; this final query documents the
        # smoke namespace state without assuming unrelated statistics are absent.
        final_residual = _stats_count(connection, final_names)
    search_result_digest = source_search_result_digest
    protocol = {
        "format_version": 1,
        "experiment": "M2.14-Screened-Recommendation-Deployment-Validation-Smoke",
        "status": "development-smoke-passed",
        "baseline_repo_commit": "ec6283274f7153412ff51df131164eeef132bf13",
        "source_search_result_digest": search_result_digest,
        "source_recommendation_digest": recommendation_digest,
        "screened_candidate_set_digest": candidate_set["digest"],
        "screened_candidate_catalog_digest": candidate_set[
            "screened_candidate_catalog_digest"
        ],
        "singleton_profile_digest": candidate_set["singleton_profile_digest"],
        "raw_candidate_catalog_digest": prepared.candidate_catalog_digest,
        "workload_digest": prepared.workload.digest,
        "repository_digest": prepared.repository.digest,
        "maintenance_model_digest": model.digest,
        "patch_sha256_before": patch_digest,
        "patch_sha256_after": _digest_file(
            ROOT / "pg/patches/postgresql-16.14-hypothetical-extstats.patch"
        ),
        "build_recipe_digest": build["build_recipe_digest"],
        "prefixes": list(PREFIXES),
        "full_workload_query_count": len(prepared.workload.queries),
        "lifecycle": {
            **lifecycle,
            "search_native_evaluations": 0,
            "singleton_native_evaluations": 0,
            "residual_smoke_statistics_count": final_residual,
        },
        "search_hot_path_analyze": False,
        "validation_phase_fresh_analyze": True,
        "experimental_role": "development-smoke",
        "prefix_summaries": [
            {
                "prefix_size": item["prefix_size"],
                "a_objective": item["a_hypothetical_objective"],
                "c_objective": item["c_fresh_physical_objective"],
                "a_exact_match": item["a_exact_match"],
                "a_vs_c_absolute_difference": item["a_vs_c_absolute_difference"],
                "analyze_elapsed_seconds": item["analyze_elapsed_seconds"],
                "validation_elapsed_seconds": item["validation_elapsed_seconds"],
                "cleanup_success": item["cleanup_success"],
            }
            for item in prefix_summaries
        ],
    }
    protocol["digest"] = _digest(protocol)
    _write(OUTPUT_ROOT / "protocol.json", protocol)
    summary = {
        "format_version": 1,
        "artifact_type": "m2-14-screened-validation-summary",
        "experimental_role": "development-smoke",
        "selected_recommendation_count": len(recommendation["selected_design"]),
        "recommendation_ids_resolve": True,
        "candidate_identity_preserved": True,
        "ddl_generated_from_raw_catalog": True,
        "screening_provenance_preserved": True,
        "prefixes": prefix_summaries,
        "lifecycle": protocol["lifecycle"],
        "frozen_repository_mutation_count": 0,
        "candidate_catalog_mutation_count": 0,
        "maintenance_model_mutation_count": 0,
        "protocol_digest": protocol["digest"],
    }
    _write(OUTPUT_ROOT / "summary.json", summary)
    sizes = {
        str(path.relative_to(OUTPUT_ROOT)): path.stat().st_size
        for path in sorted(OUTPUT_ROOT.rglob("*"))
        if path.is_file()
    }
    _write(
        OUTPUT_ROOT / "artifact-size-summary.json",
        {
            "format_version": 1,
            "artifact_type": "m2-14-artifact-size-audit",
            "experimental_role": "development-smoke",
            "files": sizes,
            "largest_new_artifact_bytes": max(sizes.values()),
            "largest_new_artifact": max(sizes, key=sizes.get),
            "trajectory_persisted": False,
        },
    )
    report_lines = [
        "# M2.14 screened recommendation/deployment/validation smoke",
        "",
        "Development-only lifecycle smoke; not an M2.7 authoritative validation.",
        "",
        f"- Full Census workload: {len(prepared.workload.queries)} queries.",
        f"- Recommendation selected: {len(recommendation['selected_design'])} candidates.",
        f"- Prefixes: {', '.join(map(str, PREFIXES))} in accepted-order nesting.",
        f"- Singleton native evaluations: {lifecycle['singleton_native_evaluations']}.",
        f"- Search native evaluations: {lifecycle['search_native_evaluations']}.",
        f"- CREATE/ANALYZE/DROP totals: {lifecycle['create']}/{lifecycle['analyze']}/{lifecycle['drop']}.",
        f"- Residual smoke statistics: {final_residual}.",
        "- Search hot path performed no ANALYZE; fresh ANALYZE occurred only during validation.",
        "- B same-realization control was not run; the smoke reports A/C as permitted by the protocol.",
        "",
        "Per-prefix details are in `prefix-{1,5,10}/validation-result.json` and `summary.json`.",
    ]
    (OUTPUT_ROOT / "report.md").write_text("\n".join(report_lines) + "\n")
    print(json.dumps({"output": str(OUTPUT_ROOT), "protocol_digest": protocol["digest"]}))
    return 0


if __name__ == "__main__":
    try:
        raise SystemExit(main())
    except Exception as error:
        print(f"error: {error}", file=sys.stderr)
        raise
