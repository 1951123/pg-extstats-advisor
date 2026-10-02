"""Run the authoritative Census top-100 search with durable checkpoints.

This driver deliberately reuses the frozen deterministic search implementation.
Its only additional responsibility is durable, restartable orchestration.  All
payloads and generated results are written below ``PGEXTADV_BENCHMARK_DATA``.
"""
from __future__ import annotations

import hashlib
import json
import os
import statistics
import sys
import time
from pathlib import Path
from typing import Any

HERE = Path(__file__).resolve().parent
if str(HERE) not in sys.path:
    sys.path.insert(0, str(HERE))
from census_advisor_top512_authoritative import (  # noqa: E402
    BENCHMARK,
    DB_NAME,
    INCIDENCE_DIGEST,
    MODEL_DIGEST,
    MODEL_PATH,
    ORACLE_DIGEST,
    ORACLE_ID,
    PG_BASE,
    PG_SOURCE,
    REPOSITORY_DIGEST,
    RELATION,
    ROOT,
    WORKLOAD_DIGEST,
    build_incidence,
    digest,
    load_inputs,
    make_bridge,
    move_dict,
    state_vector,
    estimate_vector_digest,
)

from pgextstats_benchmarks.comparison import ComparisonReport
from pgextstats_benchmarks.comparison_storage import allocate_comparison_report_dir, write_comparison_report
from pgextstats_benchmarks.evaluation import ArtifactEvaluator, truth_digest
from pgextstats_benchmarks.evaluation_storage import allocate_evaluation_artifact_dir, write_evaluation_artifact
from pgextstats_benchmarks.experiment import ExperimentRunArtifact
from pgextstats_benchmarks.experiment_storage import allocate_experiment_artifact_dir, write_experiment_artifact
from pgextstats_benchmarks.postgres.configuration_provider import PostgreSQLStatisticsConfigurationProvider
from pgextstats_benchmarks.postgres.connection import PostgresConnection
from pgextstats_benchmarks.postgres.estimate_provider import PostgreSQLEstimateProvider, extract_plan_rows
from pgextstats_benchmarks.postgres.instance import PostgresInstance
from pgextstats_benchmarks.statistics_configuration import StatisticsConfiguration
from pgextstats_benchmarks.statistics_storage import load_repository_artifact
from pgextstats_benchmarks.workload_executor import load_workload_artifact
from pgextstats_benchmarks.truth import TruthArtifact

from pg_extstats_advisor.candidates.model import CandidateCatalog
from pg_extstats_advisor.cost.empirical import EmpiricalMechanismCountCostModel
from pg_extstats_advisor.cost.model import MaintenanceBudget
from pg_extstats_advisor.evaluator.native import NativeEvaluator
from pg_extstats_advisor.models import CandidateId, Design, Move
from pg_extstats_advisor.search.deterministic import DeterministicBudgetSearch
from pg_extstats_advisor.search.model import SearchConfig
from pg_extstats_advisor.models import EvaluationState


K = 100
SHORTLIST_ID = "census-advisor-top100-shortlist-v1"
SEARCH_ID = "census-advisor-top100-search-v1"
SAMPLE_ID = "census-sample-v1"
TOP512_SEARCH_ID = "census-advisor-top512-search-v1"
TOP512_ADVISOR_ID = "census-advisor-top512-advisor-v1"
TOP512_COMPARISON_ID = "census-advisor-top512-comparison-v1"
TOP512_EXPERIMENT_ID = "census-advisor-top512-experiment-v1"
TOP512_SELECTED_COUNT = 212
TOP512_ADVISOR_OBJECTIVE = 1815.385930653145
ORACLE_BASELINE_OBJECTIVE = 5862.679217342954
ORACLE_BASELINE_VECTOR = "c292f85e640b79a8250448a83a042b0363a460731913e560adcb43db802e4c0c"
RANKING_PATH = ROOT / "census/artifacts/census-singleton-oracle-authoritative-v2/ranking.json"
ORACLE_BASELINE_PATH = ROOT / "census/artifacts/census-singleton-oracle-authoritative-v2/baseline.json"


def write_json(path: Path, value: Any) -> None:
    """Atomically write one JSON checkpoint without deleting arbitrary files."""
    path.parent.mkdir(parents=True, exist_ok=True)
    temporary = path.with_name(path.name + ".tmp")
    temporary.write_text(json.dumps(value, sort_keys=True, indent=2, allow_nan=False, default=str) + "\n", encoding="utf-8")
    temporary.replace(path)


def append_jsonl(path: Path, rows: list[dict[str, Any]]) -> None:
    if not rows:
        return
    with path.open("a", encoding="utf-8") as handle:
        for row in rows:
            handle.write(json.dumps(row, sort_keys=True, allow_nan=False, default=str) + "\n")
        handle.flush()
        os.fsync(handle.fileno())


def runtime_provenance() -> dict[str, Any]:
    """Collect runtime identity and capability using read-only operations."""
    connection = PostgresConnection(host="localhost", port=55437, user="wqts", database="postgres")
    try:
        version = str(connection.server_version())
        port = int(connection.execute("SHOW port")[0][0])
        data_directory = str(connection.execute("SHOW data_directory")[0][0])
        capability = connection.execute(
            "SELECT current_setting('pgextadv.analyze_sample_export', true), "
            "current_setting('pgextadv.analyze_sample_import', true)"
        )[0]
    finally:
        connection.close()
    if port != 55437 or "PostgreSQL 16.14" not in version:
        raise RuntimeError(f"unexpected PostgreSQL runtime: port={port}, version={version}")
    # Empty string is the supported reset value; presence (rather than a
    # non-empty path) is the capability signal.
    if not all(isinstance(item, str) for item in capability):
        raise RuntimeError("authoritative pgextadv sample-cache capability is unavailable")
    binary = None
    pgdata = None
    for proc in Path("/proc").iterdir():
        if not proc.name.isdigit():
            continue
        try:
            command = (proc / "cmdline").read_bytes().replace(b"\0", b" ").decode(errors="ignore").strip()
            if "postgres" not in Path(os.readlink(proc / "exe")).name or " -D " not in f" {command} ":
                continue
            if "-p 55437" not in command and "-p55437" not in command:
                continue
            binary = str(Path(os.readlink(proc / "exe")).resolve())
            parts = command.split()
            if "-D" in parts:
                pgdata = str(Path(parts[parts.index("-D") + 1]).resolve())
            break
        except (FileNotFoundError, PermissionError, IndexError, OSError):
            continue
    if binary is None or pgdata is None:
        raise RuntimeError("could not resolve authoritative postgres process")
    binary_sha = hashlib.sha256(Path(binary).read_bytes()).hexdigest()
    if not binary.endswith("postgresql-install-pgextadv-authoritative-16.14/bin/postgres"):
        raise RuntimeError(f"unexpected runtime binary: {binary}")
    runtime_build_commit = "6d7f5c9cd6cf1b0f73e84a4bacc45a31d1cb0cd6"
    source_tree = Path("/home/wqts/projects/postgresql-src-pgextadv")
    if (source_tree / ".git").exists():
        import subprocess
        runtime_build_commit = subprocess.run(["git", "-C", str(source_tree), "rev-parse", "HEAD"], check=True, capture_output=True, text=True).stdout.strip()
    return {
        "repository": PG_SOURCE,
        "source_commit": "7e992ab6438fef2f8eb98c7a9ed30c9f1c816ce7",
        "runtime_build_commit": runtime_build_commit,
        "upstream_base_commit": PG_BASE,
        "server_version": version,
        "binary_sha256": binary_sha,
        "binary_path": binary,
        "data_directory": pgdata,
        "port": port,
        "sample_cache_capability": {"export_guc": capability[0], "import_guc": capability[1]},
    }


def freeze_shortlist(ranking: list[dict[str, Any]], repo: Any) -> tuple[list[dict[str, Any]], str, Path]:
    ordered = sorted(ranking, key=lambda row: (-float(row["singleton_improvement"]), float(row["maintenance_cost"]), int(row["precedence_rank"]), row["candidate_id"]))
    present = {state.candidate_id for state in repo.candidate_states if state.state == "PRESENT"}
    rows = []
    for rank, row in enumerate(ordered[:K], start=1):
        if row["candidate_id"] not in present:
            raise RuntimeError(f"top-{K} shortlist contains non-PRESENT candidate")
        rows.append({"shortlist_rank": rank, "candidate_id": row["candidate_id"], "singleton_precedence_rank": int(row["precedence_rank"]), "kind": row["kind"], "columns": list(row["columns"]), "singleton_objective": float(row["objective"]), "singleton_improvement": float(row["singleton_improvement"]), "maintenance_cost": float(row["maintenance_cost"]), "state": "PRESENT"})
    shortlist_digest = digest({"format": "census-advisor-top100-shortlist-v1", "k": K, "rows": rows})
    path = ROOT / "census/artifacts" / SHORTLIST_ID
    manifest = {"artifact_type": SHORTLIST_ID, "format_version": 1, "benchmark_id": BENCHMARK, "k": K, "source_oracle_artifact_id": ORACLE_ID, "source_oracle_digest": ORACLE_DIGEST, "source_ranking_sha256": hashlib.sha256(RANKING_PATH.read_bytes()).hexdigest(), "candidate_catalog_digest": "f51a380c3e1710196e405d746cc3b405caacf4e19be1eda5831d914d205e36c1", "repository_digest": repo.repository_digest, "rationale": "The top-100 cutoff was chosen solely for runtime tractability.", "shortlist_digest": shortlist_digest, "rows": rows}
    if path.exists():
        existing = json.loads((path / "manifest.json").read_text(encoding="utf-8"))
        if existing.get("shortlist_digest") != shortlist_digest:
            raise FileExistsError(f"existing shortlist differs; refusing overwrite: {path}")
    else:
        path.mkdir(parents=True)
        write_json(path / "manifest.json", manifest)
    return rows, shortlist_digest, path


def state_to_dict(state: EvaluationState) -> dict[str, Any]:
    return {"design": [str(item) for item in state.design.candidate_ids], "aggregate_objective": state.aggregate_objective, "repository_digest": state.repository_digest, "workload_digest": state.workload_digest, "postgres_version": state.postgres_version, "evaluator_provenance": state.evaluator_provenance, "affected_query_ids": [str(item) for item in state.affected_query_ids], "reused_query_ids": [str(item) for item in state.reused_query_ids], "query_evaluations": [{"query_id": str(item.query_id), "estimate": item.estimate, "truth": item.truth, "contribution": item.contribution, "provenance": item.provenance} for item in state.query_evaluations]}


def checkpoint(engine: DeterministicBudgetSearch, current: EvaluationState, current_cost: Any, run_dir: Path, iteration: int, shortlist_digest: str, adapter: Any, baseline_explain: int, started: float, status: str = "RUNNING", termination_reason: str | None = None) -> None:
    trajectory_count = 0
    trajectory_path = run_dir / "trajectory.jsonl"
    if trajectory_path.exists():
        trajectory_count = sum(1 for _ in trajectory_path.open(encoding="utf-8"))
    evaluated_records = [item for item in engine._trajectory if item.after_objective is not None]
    cache_misses = sum(int(item.affected_query_count or 0) for item in evaluated_records)
    cache_hits = sum(max(0, 468 - int(item.affected_query_count or 0)) for item in evaluated_records)
    counters = {"iterations": iteration, "add_moves_considered": engine._considered, "add_moves_evaluated": engine._evaluated, "moves_bound_pruned": engine._bound_pruned_no_improvement + engine._bound_pruned_incumbent, "bound_pruned_no_improvement": engine._bound_pruned_no_improvement, "bound_pruned_incumbent": engine._bound_pruned_incumbent, "explain_calls": adapter.explain_calls, "cache_hits": cache_hits, "cache_misses": cache_misses, "accepted": engine._accepted, "current_config_size": len(current.design.candidate_ids), "evaluator_calls": engine._calls, "infeasible_moves_skipped": engine._skipped, "unexplained_explain_residual": adapter.explain_calls - (baseline_explain + cache_misses), "elapsed_seconds": time.perf_counter() - started}
    write_json(run_dir / "counters.json", counters)
    write_json(run_dir / "search-state.json", {"status": status, "run_id": run_dir.name, "iteration": iteration, "shortlist_digest": shortlist_digest, "current_design": [str(item) for item in current.design.candidate_ids], "current_cost": str(current_cost), "current_state": state_to_dict(current), "trajectory_records": trajectory_count, "termination_reason": termination_reason})


def run_checkpointed(repo: Any, bcat: Any, workload: Any, truth: Any, precedence: dict[str, int], shortlist: list[dict[str, Any]], model: Any, relation_oid: int, shortlist_digest: str, run_dir: Path) -> dict[str, Any]:
    existing_result = run_dir / "result.json"
    existing_manifest = run_dir / "manifest.json"
    if existing_result.exists() and existing_manifest.exists():
        manifest = json.loads(existing_manifest.read_text(encoding="utf-8"))
        if manifest.get("status") == "PASS":
            return json.loads(existing_result.read_text(encoding="utf-8"))
    run_dir.mkdir(parents=True, exist_ok=True)
    work, bridge_repo, incidence, adapter, full_catalog = make_bridge(repo, bcat, relation_oid, workload, truth, precedence)
    visible = tuple(sorted((full_catalog.by_id[row["candidate_id"]] for row in shortlist), key=lambda candidate: candidate.precedence_rank))
    visible_catalog = CandidateCatalog(visible)
    model_obj = EmpiricalMechanismCountCostModel.from_artifact(model)
    budget = MaintenanceBudget(model_obj.estimate_design(Design(tuple(candidate.candidate_id for candidate in visible)), visible_catalog), model_obj.unit)
    config = SearchConfig(add_only=True, exact_bound_pruning=True, record_pruned_moves=False, candidate_set_mode="screened", candidate_set_digest=shortlist_digest, singleton_profile_digest=ORACLE_DIGEST, visible_candidate_count=K, budget_mode="candidate-set-total", global_statistics_target=100)
    evaluator = NativeEvaluator(work, bridge_repo, incidence, adapter)
    engine = DeterministicBudgetSearch(evaluator, visible_catalog, model_obj, budget, config, incidence)
    started = time.perf_counter()
    state_path = run_dir / "search-state.json"
    if state_path.exists():
        saved = json.loads(state_path.read_text(encoding="utf-8"))
        if saved.get("shortlist_digest") != shortlist_digest:
            raise RuntimeError("checkpoint shortlist identity mismatch")
        current_design = Design(tuple(CandidateId(item) for item in saved["current_design"]))
        current = evaluator.evaluate_design(current_design)
        saved_counters = json.loads((run_dir / "counters.json").read_text(encoding="utf-8"))
        engine._calls = int(saved_counters["evaluator_calls"])
        engine._evaluated = int(saved_counters["add_moves_evaluated"])
        engine._considered = int(saved_counters["add_moves_considered"])
        engine._skipped = int(saved_counters["infeasible_moves_skipped"])
        engine._accepted = int(saved_counters["accepted"])
        engine._bound_pruned_no_improvement = int(saved_counters["bound_pruned_no_improvement"])
        engine._bound_pruned_incumbent = int(saved_counters["bound_pruned_incumbent"])
        adapter.explain_calls = int(saved_counters["explain_calls"])
        iteration = int(saved["iteration"])
        baseline_explain = 468
        current_cost = model_obj.estimate_design(current.design, visible_catalog)
        baseline_objective = ORACLE_BASELINE_OBJECTIVE
    else:
        oracle_baseline = json.loads(ORACLE_BASELINE_PATH.read_text(encoding="utf-8"))
        engine._calls += 1
        current = evaluator.evaluate_design(Design(()))
        baseline_explain = adapter.explain_calls
        if current.aggregate_objective != oracle_baseline["objective"] or estimate_vector_digest(current) != oracle_baseline["vector_digest"] or current.aggregate_objective != ORACLE_BASELINE_OBJECTIVE or estimate_vector_digest(current) != ORACLE_BASELINE_VECTOR:
            adapter.close()
            raise RuntimeError("baseline closure failed")
        current_cost = model_obj.estimate_design(current.design, visible_catalog)
        baseline_objective = current.aggregate_objective
        iteration = 0
        write_json(run_dir / "manifest.json", {"status": "RUNNING", "artifact_type": SEARCH_ID, "benchmark_id": BENCHMARK, "k": K, "shortlist_digest": shortlist_digest, "source_oracle_digest": ORACLE_DIGEST, "incidence_digest": INCIDENCE_DIGEST})
        checkpoint(engine, current, current_cost, run_dir, iteration, shortlist_digest, adapter, baseline_explain, started)
    try:
        while True:
            before_records = len(engine._trajectory)
            moves = (Move.add_candidate(item) for item in engine._ordered_ids(False, current.design))
            winner = engine._finish_streaming_round("greedy-add", current, current_cost, moves)
            round_records = [move_dict(item) for item in engine._trajectory[before_records:]]
            append_jsonl(run_dir / "trajectory.jsonl", round_records)
            if winner is None:
                checkpoint(engine, current, current_cost, run_dir, iteration, shortlist_digest, adapter, baseline_explain, started, status="SEARCH_COMPLETE", termination_reason="add-local-optimum")
                break
            iteration += 1
            current, current_cost = winner.state, winner.cost
            accepted = [item for item in engine._trajectory[before_records:] if item.accepted]
            accepted_item = move_dict(accepted[0]) if accepted else None
            checkpoint(engine, current, current_cost, run_dir, iteration, shortlist_digest, adapter, baseline_explain, started)
            if accepted_item is not None:
                accepted_row = {"iteration": iteration, "objective_before": accepted_item["before_objective"], "selected_candidate": accepted_item["add"], "singleton_rank": next((row["shortlist_rank"] for row in shortlist if row["candidate_id"] == accepted_item["add"]), None), "affected_query_count": accepted_item["affected_query_count"], "objective_after": accepted_item["after_objective"], "delta": accepted_item["delta"], "config_size": len(current.design.candidate_ids), "cumulative_explain_calls": adapter.explain_calls, "elapsed_seconds": time.perf_counter() - started, "stopping_state": "CONTINUE"}
                append_jsonl(run_dir / "accepted-iterations.jsonl", [accepted_row])
                write_json(run_dir / "accepted-iteration-latest.json", accepted_row)
        final_before = adapter.explain_calls
        final_full = evaluator.evaluate_design(current.design)
        if final_full.aggregate_objective != current.aggregate_objective or state_vector(final_full) != state_vector(current):
            raise RuntimeError("final exhaustive closure failed")
        final_explain = adapter.explain_calls - final_before
        counters = json.loads((run_dir / "counters.json").read_text(encoding="utf-8"))
        counters.update({"final_exhaustive_explain_calls": final_explain, "explain_calls": adapter.explain_calls, "unexplained_explain_residual": adapter.explain_calls - (baseline_explain + counters["cache_misses"] + final_explain)})
        write_json(run_dir / "counters.json", counters)
        result = {"status": "PASS", "selected_design": [str(item) for item in current.design.candidate_ids], "selected_objective": current.aggregate_objective, "selected_maintenance_cost": str(current_cost), "termination_reason": "add-local-optimum", "evaluated_moves_count": engine._evaluated, "infeasible_moves_skipped_count": engine._skipped, "evaluator_calls_count": engine._calls, "accepted_moves_count": engine._accepted, "total_neighbor_moves_considered": engine._considered, "bound_pruned_no_improvement_count": engine._bound_pruned_no_improvement, "bound_pruned_incumbent_count": engine._bound_pruned_incumbent, "planner_explain_calls": adapter.explain_calls, "baseline_objective": baseline_objective, "baseline_vector_digest": ORACLE_BASELINE_VECTOR, "final_vector_digest": estimate_vector_digest(final_full), "elapsed_seconds": time.perf_counter() - started, "trajectory_path": str(run_dir / "trajectory.jsonl"), "counters_path": str(run_dir / "counters.json"), "selected_count": len(current.design.candidate_ids)}
        write_json(run_dir / "result.json", result)
        write_json(run_dir / "manifest.json", {"status": "PASS", "artifact_type": SEARCH_ID, "benchmark_id": BENCHMARK, "k": K, "shortlist_digest": shortlist_digest, "source_oracle_digest": ORACLE_DIGEST, "incidence_digest": INCIDENCE_DIGEST, "result": result, "counters": counters})
        return result
    finally:
        adapter.close()


def make_configuration(config_id: str, repo: Any, ids: list[str], label: str) -> StatisticsConfiguration:
    return StatisticsConfiguration(config_id, repo.artifact_id, repo.repository_digest, tuple(ids), repo.relation_identity, metadata={"producer": "census_advisor_top100_crashsafe", "label": label, "shortlist_k": K}, lineage={"repository_artifact_id": repo.artifact_id, "shortlist_artifact_id": SHORTLIST_ID})


def evaluate_final(repo: Any, workload: Any, truth: Any, shortlist: list[dict[str, Any]], search: dict[str, Any], runtime: dict[str, Any], timings: dict[str, float]) -> dict[str, Any]:
    ranked_ids = [row["candidate_id"] for row in shortlist]
    full_present = [row["candidate_id"] for row in json.loads(RANKING_PATH.read_text(encoding="utf-8"))["rows"]]
    selected = list(search["selected_design"])
    configs = [("baseline", []), ("small-fixed", ranked_ids[:8]), ("medium-fixed", ranked_ids[:32]), ("advisor-top100", selected), ("full", full_present)]
    expected_ids = [f"census-advisor-top100-evaluation-{label}-v1" for label, _ in configs]
    existing_dirs = [ROOT / "census/artifacts" / item for item in expected_ids]
    existing_experiment = ROOT / "census/artifacts/census-advisor-top100-insample-v1/manifest.json"
    existing_comparison = ROOT / "census/artifacts/census-advisor-top100-comparison-v1/manifest.json"
    if all(item.is_file() for item in [*(directory / "manifest.json" for directory in existing_dirs), existing_experiment, existing_comparison]):
        records = []
        for label, _ in configs:
            manifest = json.loads((ROOT / "census/artifacts" / f"census-advisor-top100-evaluation-{label}-v1" / "manifest.json").read_text(encoding="utf-8"))
            records.append({"configuration_id": manifest["configuration_id"], "label": label, "selected_count": len(dict(configs)[label]), "configuration_digest": manifest["configuration_digest"], "estimate_artifact_id": manifest["estimate_artifact_id"], "estimate_digest": manifest["estimate_digest"], "evaluation_artifact_id": manifest["artifact_id"], "evaluation_digest": manifest["evaluation_digest"], "objective": manifest["aggregate_metrics"].get("mean_q_error")})
        experiment_manifest = json.loads(existing_experiment.read_text(encoding="utf-8"))
        comparison_manifest = json.loads(existing_comparison.read_text(encoding="utf-8"))
        return {"configurations": records, "experiment": {"artifact_id": experiment_manifest["experiment_id"], "digest": experiment_manifest["experiment_digest"]}, "comparison": {"artifact_id": comparison_manifest["report_id"], "digest": comparison_manifest["report_digest"]}, "comparison_summaries": comparison_manifest["configuration_summaries"], "timings": timings}
    instance = PostgresInstance(DB_NAME, DB_NAME, "READY", {"benchmark_id": BENCHMARK})
    provider = PostgreSQLStatisticsConfigurationProvider(PostgresConnection(host="localhost", port=55437, user="wqts", database="postgres"))
    estimator = PostgreSQLEstimateProvider(provider)
    evaluations = []
    records = []
    truth_d = truth_digest(truth)
    started = time.perf_counter()
    try:
        for label, ids in configs:
            config_id = f"census-advisor-top100-{label}-v1"
            cfg = make_configuration(config_id, repo, ids, label)
            cfg_path = ROOT / "census/configurations" / f"{config_id}.json"
            if cfg_path.exists() and cfg_path.read_text(encoding="utf-8") != cfg.to_json():
                raise FileExistsError(f"configuration differs; refusing overwrite: {cfg_path}")
            cfg_path.parent.mkdir(parents=True, exist_ok=True)
            cfg_path.write_text(cfg.to_json(), encoding="utf-8")
            estimate_result = estimator.collect(instance, workload, repo, cfg, benchmark_id=BENCHMARK, root=ROOT, artifact_id=f"census-advisor-top100-estimate-{label}-v1")
            if estimate_result["successful_count"] != 468:
                raise RuntimeError(f"estimate failed for {label}")
            evaluation = ArtifactEvaluator().evaluate(truth, estimate_result["artifact"], truth_artifact_id="census-truth-v1", truth_digest_value=truth_d, artifact_id=f"census-advisor-top100-evaluation-{label}-v1")
            allocate_evaluation_artifact_dir(BENCHMARK, evaluation.artifact_id, ROOT)
            write_evaluation_artifact(evaluation, ROOT)
            evaluations.append(evaluation)
            records.append({"configuration_id": config_id, "label": label, "selected_count": len(ids), "configuration_digest": cfg.configuration_digest, "estimate_artifact_id": estimate_result["artifact"].artifact_id, "estimate_digest": estimate_result["artifact"].estimate_digest, "evaluation_artifact_id": evaluation.artifact_id, "evaluation_digest": evaluation.evaluation_digest, "objective": evaluation.aggregate_metrics.get("sum_q_error")})
    finally:
        timings["five_config_evaluation_seconds"] = time.perf_counter() - started
        estimator.close()
    exp = ExperimentRunArtifact.from_evaluations(experiment_id="census-advisor-top100-insample-v1", evaluations=evaluations, repositories={repo.artifact_id: repo}, baseline_label="baseline", labels={item.artifact_id: next(label for label, _ in configs if item.configuration_id == f"census-advisor-top100-{label}-v1") for item in evaluations}, metadata={"shortlist_k": K, "in_sample": True, "runtime_postgres_source": runtime, "repository_acquisition_postgres_source": dict(repo.postgres_source), "sample_producer_commit": "7e992ab6438fef2f8eb98c7a9ed30c9f1c816ce7", "advisor_commit": os.popen("git rev-parse HEAD").read().strip(), "benchmark_commit": os.popen("git -C /home/wqts/projects/pg-extstats-benchmarks rev-parse HEAD").read().strip()})
    allocate_experiment_artifact_dir(BENCHMARK, exp.experiment_id, ROOT)
    write_experiment_artifact(exp, ROOT)
    report = ComparisonReport.create(report_id="census-advisor-top100-comparison-v1", experiment=exp, evaluations=evaluations, metadata={"shortlist_k": K, "in_sample": True, "runtime_postgres_source": runtime})
    allocate_comparison_report_dir(BENCHMARK, report.report_id, ROOT)
    write_comparison_report(report, BENCHMARK, ROOT)
    return {"configurations": records, "experiment": {"artifact_id": exp.experiment_id, "digest": exp.experiment_digest}, "comparison": {"artifact_id": report.report_id, "digest": report.report_digest}, "comparison_summaries": [item.to_dict() for item in report.configuration_summaries], "timings": timings}


def compare_top512(search: dict[str, Any], final: dict[str, Any], shortlist: list[dict[str, Any]]) -> dict[str, Any]:
    top512_dir = ROOT / "census/artifacts" / TOP512_SEARCH_ID
    top512_manifest = json.loads((top512_dir / "manifest.json").read_text(encoding="utf-8"))
    top512 = {"selected_design": top512_manifest["selected_design"], "selected_objective": top512_manifest.get("final_objective")}
    top512_evaluation = json.loads((ROOT / "census/artifacts" / TOP512_ADVISOR_ID.replace("advisor-v1", "evaluation-advisor-v1") / "manifest.json").read_text(encoding="utf-8"))
    top100_ids = set(search["selected_design"])
    top512_ids = set(top512["selected_design"])
    selected_ranks = [row["shortlist_rank"] for row in shortlist if row["candidate_id"] in top100_ids]
    return {"top100_selected_count": len(top100_ids), "top512_selected_count": len(top512_ids), "selected_overlap_count": len(top100_ids & top512_ids), "top100_objective": search["selected_objective"], "top512_objective": top512["selected_objective"], "top100_mean_q_error": next((item["qerror_metrics"].get("mean_q_error") for item in final["comparison_summaries"] if item["configuration_id"] == "census-advisor-top100-advisor-top100-v1"), None), "top512_mean_q_error": top512_evaluation["aggregate_metrics"].get("mean_q_error"), "top100_rank_min": min(selected_ranks) if selected_ranks else None, "top100_rank_median": statistics.median(selected_ranks) if selected_ranks else None, "top100_rank_max": max(selected_ranks) if selected_ranks else None, "rank100_selected": 100 in selected_ranks, "search_depth_boundary": max(selected_ranks) if selected_ranks else None, "top512_advisor_objective_reference": TOP512_ADVISOR_OBJECTIVE, "top512_comparison_artifact": TOP512_COMPARISON_ID, "top512_experiment_artifact": TOP512_EXPERIMENT_ID, "top512_advisor_selected_count": TOP512_SELECTED_COUNT, "top100_evaluation_artifact": final["comparison"]["artifact_id"]}


def main() -> int:
    if not ROOT.is_absolute() or ROOT == Path(ROOT.anchor):
        raise RuntimeError("unsafe benchmark root")
    runtime = runtime_provenance()
    advisor_commit = os.popen("git rev-parse HEAD").read().strip()
    benchmark_commit = os.popen("git -C /home/wqts/projects/pg-extstats-benchmarks rev-parse HEAD").read().strip()
    repo, bcat, workload, truth, extra, precedence = load_inputs()
    shortlist_started = time.perf_counter()
    shortlist, shortlist_digest, shortlist_path = freeze_shortlist(extra["ranking"], repo)
    shortlist_seconds = time.perf_counter() - shortlist_started
    search_root = ROOT / "census/artifacts" / SEARCH_ID
    if search_root.exists() and not (search_root / "manifest.json").exists():
        raise FileExistsError(f"refusing ambiguous existing artifact directory: {search_root}")
    first = run_checkpointed(repo, bcat, workload, truth, precedence, shortlist, extra["model"], extra["relation_oid"], shortlist_digest, search_root)
    repeat_dir = search_root / "repeat"
    repeat = run_checkpointed(repo, bcat, workload, truth, precedence, shortlist, extra["model"], extra["relation_oid"], shortlist_digest, repeat_dir)
    exact = all(first[key] == repeat[key] for key in ("selected_design", "selected_objective", "selected_maintenance_cost", "termination_reason", "accepted_moves_count", "evaluated_moves_count", "total_neighbor_moves_considered", "bound_pruned_no_improvement_count", "bound_pruned_incumbent_count", "baseline_vector_digest", "final_vector_digest"))
    if not exact:
        raise RuntimeError("top-100 deterministic repeatability gate failed")
    timings = {"shortlist_seconds": shortlist_seconds, "first_search_seconds": first["elapsed_seconds"], "repeat_search_seconds": repeat["elapsed_seconds"]}
    final_started = time.perf_counter()
    final = evaluate_final(repo, workload, truth, shortlist, first, runtime, timings)
    timings["final_exhaustive_seconds"] = first.get("final_exhaustive_seconds", 0.0)
    comparison = compare_top512(first, final, shortlist)
    summary = {"status": "PASS", "benchmark": BENCHMARK, "shortlist_artifact": str(shortlist_path), "shortlist_digest": shortlist_digest, "k": K, "incidence_digest": INCIDENCE_DIGEST, "search": first, "repeat": repeat, "repeat_exact": exact, "final": final, "top512_comparison": comparison, "timings": timings, "provenance": {"repository_acquisition_postgres_commit": repo.postgres_source.get("source_commit"), "runtime_postgres": runtime, "sample_producer_commit": "7e992ab6438fef2f8eb98c7a9ed30c9f1c816ce7", "advisor_commit": advisor_commit, "benchmark_commit": benchmark_commit, "oracle_digest": ORACLE_DIGEST, "repository_artifact_id": repo.artifact_id, "repository_digest": repo.repository_digest, "workload_digest": WORKLOAD_DIGEST, "model_digest": MODEL_DIGEST}, "contract": {"search_semantics": "existing DeterministicBudgetSearch ADD-only exact-bound pruning", "evaluation": "query-local NativeEvaluator", "in_sample_statement": "The same 468-query Census workload is used for singleton ranking, advisor search, and evaluation; this is in-sample optimization behavior.", "configuration_order_observation": "StatisticsConfiguration canonicalizes selected IDs lexicographically while the frozen search design is fixed-precedence ordered. The top-100 selected set is the same, but its two orders differ; both search and persisted-configuration objectives are retained without changing search semantics."}}
    write_json(search_root / "repeat.json", {"exact": exact, "first": first, "repeat": repeat})
    write_json(search_root / "comparison-top512.json", comparison)
    write_json(search_root / "manifest.json", summary)
    print(json.dumps({"status": "PASS", "k": K, "selected_count": first["selected_count"], "selected_objective": first["selected_objective"], "repeat_exact": exact, "comparison": comparison, "timings": timings}, indent=2, sort_keys=True))
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
