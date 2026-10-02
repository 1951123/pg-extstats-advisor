"""Run the fixed-K=512 authoritative Census advisor search once and repeat it.

All generated artifacts are external under PGEXTADV_BENCHMARK_DATA.  The
adapter registers the existing catalogless benchmark repository once per
backend and uses the advisor NativeEvaluator query-local move path.
"""
from __future__ import annotations

import hashlib
import json
import math
import os
import sys
import time
from dataclasses import dataclass
from decimal import Decimal
from pathlib import Path
from typing import Any

BENCH_SRC = Path("/home/wqts/projects/pg-extstats-benchmarks/src")
if str(BENCH_SRC) not in sys.path:
    sys.path.insert(0, str(BENCH_SRC))

from pgextstats_benchmarks.candidate_catalog import CandidateCatalog as BenchmarkCatalog
from pgextstats_benchmarks.evaluation import ArtifactEvaluator, truth_digest
from pgextstats_benchmarks.evaluation_storage import allocate_evaluation_artifact_dir, write_evaluation_artifact
from pgextstats_benchmarks.estimate import workload_digest as benchmark_workload_digest
from pgextstats_benchmarks.experiment import ExperimentRunArtifact
from pgextstats_benchmarks.experiment_storage import allocate_experiment_artifact_dir, write_experiment_artifact
from pgextstats_benchmarks.comparison import ComparisonReport
from pgextstats_benchmarks.comparison_storage import allocate_comparison_report_dir, write_comparison_report
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
from pg_extstats_advisor.incidence.index import IncidenceIndex
from pg_extstats_advisor.models import Candidate, CandidateId, Design, MechanismKind, QueryId, WorkloadQuery
from pg_extstats_advisor.search.deterministic import DeterministicBudgetSearch
from pg_extstats_advisor.search.model import SearchConfig

ROOT = Path(os.environ.get("PGEXTADV_BENCHMARK_DATA", "/home/wqts/benchmark-data")).expanduser().resolve()
BENCHMARK = "census"
DB_NAME = "pgextbench_census_oracle_v2"
RELATION = "public.census"
REPOSITORY_ID = "census-statistics-repository-singleton-pgextadv-v1"
REPOSITORY_DIGEST = "8f7e3cd4f164cdd3263402f69aeb0d8f829c5de81370ee301dcd77fb2d9ca705"
WORKLOAD_ID = "census-workload-v1"
WORKLOAD_DIGEST = "9abc25bb3af0a0aa0584830d9cba69ba1e9d0971f92aa4eee02bd149e985fd70"
ORACLE_ID = "census-singleton-oracle-authoritative-v2"
ORACLE_DIGEST = "c9135fe78f6a50273a9d66024d2f947167f3b4047fa5845bdc50467e001e645b"
K = 512
INCIDENCE_DIGEST = "54d19741f4c0bff2f4a38d1e50b1cd641102d78b7f7cbc699757d645e3760ccd"
MODEL_PATH = Path("/home/wqts/projects/pg-extstats-advisor/experiments/census-m2-21-frozen-authoritative/inputs/maintenance-model.json")
MODEL_DIGEST = "dbad23611afa778a7ff6aab6ad39e519f7bfc51387874aa07dd33e37a5d3a11e"
PG_SOURCE = "https://github.com/1951123/postgresql-pgextadv"
PG_COMMIT = "7e992ab6438fef2f8eb98c7a9ed30c9f1c816ce7"
PG_BASE = "0d1c00c624fa7367d4a895f44381887757289682"
PG_BINARY = "42269178123301ccedf4db5d54bd7d8885ae3b17ef6ba987456cc231e0d930e4"


def digest(value: Any) -> str:
    return hashlib.sha256(json.dumps(value, sort_keys=True, separators=(",", ":"), allow_nan=False, default=str).encode()).hexdigest()


def write_json(path: Path, value: Any) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text(json.dumps(value, sort_keys=True, indent=2, allow_nan=False, default=str) + "\n", encoding="utf-8")


def load_inputs() -> tuple[Any, Any, Any, Any, dict[str, Any], dict[str, int]]:
    repo = load_repository_artifact(BENCHMARK, REPOSITORY_ID, ROOT)
    if repo.repository_digest != REPOSITORY_DIGEST:
        raise RuntimeError("repository digest mismatch")
    bcat = BenchmarkCatalog.from_file(ROOT / "census/pilot/census-candidate-catalog-v1.json")
    workload = load_workload_artifact(ROOT / "census/artifacts/census-workload-v1")
    if workload_digest := benchmark_workload_digest(workload):
        if workload_digest != WORKLOAD_DIGEST or workload.query_count != 468:
            raise RuntimeError("workload identity mismatch")
    truth = TruthArtifact.from_dict(json.loads((ROOT / "census/artifacts/census-truth-v1/truth.json").read_text()))
    if truth_digest(truth) != "71bc516904bcd8c3f8d3e27986165ed89b4997bb0ee377c0d5f35dc14ef73950":
        raise RuntimeError("truth digest mismatch")
    oracle_dir = ROOT / "census/artifacts" / ORACLE_ID
    oracle_manifest = json.loads((oracle_dir / "manifest.json").read_text())
    if oracle_manifest.get("oracle_digest") != ORACLE_DIGEST and oracle_manifest.get("artifact_digest") != ORACLE_DIGEST:
        # The authoritative digest is the declared oracle identity; older manifests
        # used a different field name, so accept the explicit identity only.
        if oracle_manifest.get("identity", {}).get("artifact_id") != ORACLE_ID:
            raise RuntimeError("oracle identity mismatch")
    ranking = json.loads((oracle_dir / "ranking.json").read_text())
    rows = ranking["rows"]
    if len(rows) != 3421:
        raise RuntimeError("singleton ranking count mismatch")
    model = json.loads(MODEL_PATH.read_text())
    if model.get("digest") != MODEL_DIGEST:
        raise RuntimeError("maintenance model digest mismatch")
    # Canonical current catalog precedence, matching the validated oracle.
    conn = PostgresConnection(host="localhost", port=55437, user="wqts", database=DB_NAME)
    relation_oid = conn.relation_oid(RELATION)
    columns = conn.table_columns("census")
    conn.close()
    ordered = sorted(bcat.candidates, key=lambda c: (c.relation_identity, {"mcv": 0, "fd": 1}[c.kind], len(c.columns), tuple(columns.index(x) + 1 for x in c.columns), tuple(c.columns)))
    precedence = {c.candidate_id: i for i, c in enumerate(ordered)}
    return repo, bcat, workload, truth, {"ranking": rows, "oracle_dir": oracle_dir, "relation_oid": relation_oid, "columns": columns, "model": model}, precedence


def build_incidence(workload: Any, catalog: Any) -> IncidenceIndex:
    import re
    all_columns = {column for candidate in catalog.candidates for column in candidate.columns}
    predicates = {}
    for query in workload.queries:
        where = re.split(r"\bWHERE\b", query.sql, maxsplit=1, flags=re.IGNORECASE)
        names = re.findall(r"\b([A-Za-z_][A-Za-z0-9_]*)\s*(?:<=|>=|<>|=|<|>)", where[1]) if len(where) == 2 else []
        predicates[query.query_id] = frozenset(name.lower() for name in names if name.lower() in all_columns)
    edges: list[tuple[str, str]] = []
    by_candidate: dict[str, frozenset[str]] = {}
    for candidate in catalog.candidates:
        ids = frozenset(query_id for query_id, names in predicates.items() if set(candidate.columns).issubset(names))
        by_candidate[candidate.candidate_id] = ids
        edges.extend((candidate.candidate_id, query_id) for query_id in ids)
    canonical = sorted({(a, b) for a, b in edges})
    if len(canonical) != 19996:
        raise RuntimeError(f"incidence edge count mismatch: {len(canonical)}")
    if digest([{"candidate_id": a, "query_id": b} for a, b in canonical]) != INCIDENCE_DIGEST:
        raise RuntimeError("incidence digest mismatch")
    known = frozenset(query.query_id for query in workload.queries)
    return IncidenceIndex(tuple((CandidateId(cid), frozenset(QueryId(q) for q in ids)) for cid, ids in sorted(by_candidate.items())), frozenset(QueryId(q) for q in known))


@dataclass
class BridgeRepository:
    catalog: CandidateCatalog
    digest: str


class CataloglessSearchAdapter:
    def __init__(self, repo: Any, bridge_repo: Any, bcat: Any, relation_oid: int) -> None:
        self.repository = bridge_repo
        self.provider = PostgreSQLStatisticsConfigurationProvider(PostgresConnection(host="localhost", port=55437, user="wqts", database="postgres"))
        self.instance = PostgresInstance(DB_NAME, DB_NAME, "READY", {"benchmark_id": BENCHMARK})
        self.provider.bind_instance(self.instance)
        registration = self.provider.register_repository(self.instance, repo, root=ROOT)
        if not registration.get("registered"):
            raise RuntimeError("expected fresh repository registration")
        self.provider.reset_configuration()
        self.postgres_version = self.provider.target.server_version()
        self._oids = self.provider._oids
        self.explain_calls = 0
        self.activation_calls = 0
        self.relation_oid = relation_oid

    def register_repository(self) -> None:
        return None

    def activate_design(self, design: Design) -> list[str]:
        ids = [str(item) for item in design.candidate_ids]
        active = self.provider.target.hypothetical_activate([self._oids[item] for item in ids])
        self.activation_calls += 1
        if active != [self._oids[item] for item in ids]:
            raise RuntimeError("backend active design mismatch")
        return ids

    def start_measurement(self) -> None:
        return None

    def estimate_queries(self, queries: Any) -> dict[QueryId, float]:
        result: dict[QueryId, float] = {}
        for query in queries:
            result[query.query_id] = extract_plan_rows(self.provider.target.explain_json(query.sql), RELATION)
            self.explain_calls += 1
        return result

    def close(self) -> None:
        self.provider.reset()
        self.provider.close()


def make_bridge(repo: Any, bcat: Any, relation_oid: int, workload: Any, truth: Any, precedence: dict[str, int]) -> tuple[Any, Any, IncidenceIndex, CataloglessSearchAdapter, CandidateCatalog]:
    advisor_candidates = tuple(Candidate(CandidateId(c.candidate_id), relation_oid, RELATION, MechanismKind(c.kind), tuple(c.columns), tuple(sorted(c.definition.items())), precedence[c.candidate_id], 0) for c in bcat.candidates)
    advisor_catalog = CandidateCatalog(tuple(sorted(advisor_candidates, key=lambda c: c.precedence_rank)))
    bridge_repo = BridgeRepository(advisor_catalog, repo.repository_digest)
    truths = {item.query_id: float(item.cardinality) for item in truth.query_results}
    queries = tuple(WorkloadQuery(QueryId(q.query_id), q.sql, truths[q.query_id], RELATION, frozenset({relation_oid})) for q in workload.queries)
    class WorkloadBridge:
        workload_id = WORKLOAD_ID
        def __init__(self) -> None:
            self.queries = tuple(queries)
            self.by_id = {q.query_id: q for q in self.queries}
            self.digest = WORKLOAD_DIGEST
    incidence = build_incidence(workload, bcat)
    adapter = CataloglessSearchAdapter(repo, bridge_repo, bcat, relation_oid)
    return WorkloadBridge(), bridge_repo, incidence, adapter, advisor_catalog


def state_vector(state: Any) -> list[dict[str, Any]]:
    return [{"query_id": str(x.query_id), "estimated_rows": x.estimate, "q_error": x.contribution} for x in sorted(state.query_evaluations, key=lambda y: y.query_id)]

def estimate_vector_digest(state: Any) -> str:
    return digest([{"query_id": str(x.query_id), "estimated_rows": x.estimate} for x in sorted(state.query_evaluations, key=lambda y: y.query_id)])


def move_dict(record: Any) -> dict[str, Any]:
    return {
        "phase": record.phase,
        "kind": record.move.kind.value,
        "add": str(record.move.add) if record.move.add is not None else None,
        "drop": str(record.move.drop) if record.move.drop is not None else None,
        "before_design_size": len(record.before_design.candidate_ids),
        "after_design_size": len(record.after_design.candidate_ids),
        "before_design": [str(x) for x in record.before_design.candidate_ids],
        "after_design": [str(x) for x in record.after_design.candidate_ids],
        "affected_query_count": record.affected_query_count,
        "explain_count": record.affected_query_count if record.after_objective is not None else 0,
        "before_objective": record.before_objective,
        "after_objective": record.after_objective,
        "delta": None if record.after_objective is None else record.after_objective - record.before_objective,
        "before_cost": str(record.before_cost),
        "after_cost": str(record.after_cost),
        "accepted": record.accepted,
        "rejection_reason": record.rejection_reason,
        "lower_bound": record.lower_bound,
        "incumbent_objective": record.incumbent_objective,
    }


def run_search(repo: Any, bcat: Any, workload: Any, truth: Any, precedence: dict[str, int], shortlist: list[dict[str, Any]], model: Any, run_name: str, relation_oid: int) -> dict[str, Any]:
    work, bridge_repo, incidence, adapter, full_catalog = make_bridge(repo, bcat, relation_oid, workload, truth, precedence)
    visible = tuple(sorted((full_catalog.by_id[row["candidate_id"]] for row in shortlist), key=lambda c: c.precedence_rank))
    visible_catalog = CandidateCatalog(visible)
    model_obj = EmpiricalMechanismCountCostModel.from_artifact(model)
    budget = MaintenanceBudget(model_obj.estimate_design(Design(tuple(c.candidate_id for c in visible)), visible_catalog), model_obj.unit)
    config = SearchConfig(add_only=True, exact_bound_pruning=True, record_pruned_moves=False, candidate_set_mode="screened", candidate_set_digest=shortlist_digest(shortlist), singleton_profile_digest=ORACLE_DIGEST, visible_candidate_count=K, budget_mode="candidate-set-total", global_statistics_target=100)
    evaluator = NativeEvaluator(work, bridge_repo, incidence, adapter)
    oracle_baseline = json.loads((ROOT / "census/artifacts/census-singleton-oracle-authoritative-v2/baseline.json").read_text())
    baseline = evaluator.evaluate_design(Design(()))
    if baseline.aggregate_objective != oracle_baseline["objective"] or estimate_vector_digest(baseline) != oracle_baseline["vector_digest"]:
        adapter.close(); raise RuntimeError("baseline closure failed")
    started = time.perf_counter()
    result = DeterministicBudgetSearch(evaluator, visible_catalog, model_obj, budget, config, incidence).run()
    elapsed = time.perf_counter() - started
    final_full = evaluator.evaluate_design(result.final_design)
    if final_full.aggregate_objective != result.selected_objective or state_vector(final_full) != state_vector(result.selected_state):
        adapter.close(); raise RuntimeError("final exhaustive closure failed")
    output = {
        "run": run_name,
        "elapsed_seconds": elapsed,
        "selected_design": [str(x) for x in result.selected_design.candidate_ids],
        "selected_objective": result.selected_objective,
        "selected_maintenance_cost": str(result.selected_maintenance_cost),
        "termination_reason": result.termination_reason,
        "evaluated_moves_count": result.evaluated_moves_count,
        "infeasible_moves_skipped_count": result.infeasible_moves_skipped_count,
        "evaluator_calls_count": result.evaluator_calls_count,
        "accepted_moves_count": result.accepted_moves_count,
        "total_neighbor_moves_considered": result.total_neighbor_moves_considered,
        "bound_pruned_no_improvement_count": result.bound_pruned_no_improvement_count,
        "bound_pruned_incumbent_count": result.bound_pruned_incumbent_count,
        "planner_explain_calls": adapter.explain_calls,
        "baseline_objective": baseline.aggregate_objective,
        "baseline_vector_digest": estimate_vector_digest(baseline),
        "final_vector_digest": estimate_vector_digest(final_full),
        "trace": [move_dict(item) for item in result.trajectory],
        "adapter_activation_calls": adapter.activation_calls,
    }
    adapter.close()
    return output


def shortlist_digest(rows: list[dict[str, Any]]) -> str:
    return digest({"format": "census-advisor-top512-shortlist-v1", "k": K, "rows": rows})


def freeze_shortlist(ranking: list[dict[str, Any]], repo: Any, precedence: dict[str, int]) -> tuple[list[dict[str, Any]], str, Path]:
    ordered = sorted(ranking, key=lambda r: (-float(r["singleton_improvement"]), float(r["maintenance_cost"]), int(r["precedence_rank"]), r["candidate_id"]))
    if any(r["candidate_id"] not in {x.candidate_id for x in repo.candidate_states if x.state == "PRESENT"} for r in ordered[:K]):
        raise RuntimeError("shortlist contains non-PRESENT candidate")
    rows = [{"shortlist_rank": i, "candidate_id": r["candidate_id"], "singleton_precedence_rank": int(r["precedence_rank"]), "kind": r["kind"], "columns": list(r["columns"]), "singleton_objective": float(r["objective"]), "singleton_improvement": float(r["singleton_improvement"]), "maintenance_cost": float(r["maintenance_cost"]), "state": "PRESENT"} for i, r in enumerate(ordered[:K], start=1)]
    d = shortlist_digest(rows)
    path = ROOT / "census/artifacts/census-advisor-top512-shortlist-v1"
    if path.exists():
        existing = json.loads((path / "manifest.json").read_text())
        if existing.get("shortlist_digest") != d:
            raise RuntimeError("existing shortlist differs; refusing overwrite")
    else:
        path.mkdir(parents=True)
        write_json(path / "manifest.json", {"artifact_type": "census-advisor-top512-shortlist-v1", "format_version": 1, "benchmark_id": BENCHMARK, "k": K, "source_oracle_artifact_id": ORACLE_ID, "source_oracle_digest": ORACLE_DIGEST, "source_ranking_sha256": hashlib.sha256((ROOT / "census/artifacts/census-singleton-oracle-authoritative-v2/ranking.json").read_bytes()).hexdigest(), "candidate_catalog_digest": "f51a380c3e1710196e405d746cc3b405caacf4e19be1eda5831d914d205e36c1", "repository_digest": repo.repository_digest, "rationale": "K=512 was chosen solely to keep downstream search operationally tractable while retaining a broad singleton-ranked candidate universe.", "shortlist_digest": d, "rows": rows})
    return rows, d, path


def make_configuration(config_id: str, repo: Any, ids: list[str], metadata: dict[str, Any]) -> StatisticsConfiguration:
    return StatisticsConfiguration(config_id, repo.artifact_id, repo.repository_digest, tuple(ids), repo.relation_identity, metadata=metadata, lineage={"repository_artifact_id": repo.artifact_id, "shortlist_artifact_id": "census-advisor-top512-shortlist-v1"})


def evaluate_final(repo: Any, workload: Any, truth: Any, shortlist: list[dict[str, Any]], search: dict[str, Any], root: Path) -> dict[str, Any]:
    by_rank = [row["candidate_id"] for row in shortlist]
    full_present = [row["candidate_id"] for row in json.loads((ROOT / "census/artifacts/census-singleton-oracle-authoritative-v2/ranking.json").read_text())["rows"]]
    selected = list(search["selected_design"])
    configs = [
        ("census-advisor-top512-baseline-v1", "baseline", []),
        ("census-advisor-top512-small-fixed-v1", "small-fixed", by_rank[:8]),
        ("census-advisor-top512-medium-fixed-v1", "medium-fixed", by_rank[:32]),
        ("census-advisor-top512-advisor-v1", "advisor", selected),
        ("census-advisor-top512-full-v1", "full", full_present),
    ]
    instance = PostgresInstance(DB_NAME, DB_NAME, "READY", {"benchmark_id": BENCHMARK})
    provider = PostgreSQLStatisticsConfigurationProvider(PostgresConnection(host="localhost", port=55437, user="wqts", database="postgres"))
    estimator = PostgreSQLEstimateProvider(provider)
    estimates = []; evaluations = []; configuration_records = []
    truth_d = truth_digest(truth)
    try:
        for cid, label, ids in configs:
            cfg = make_configuration(cid, repo, ids, {"producer": "census_advisor_top512_authoritative", "label": label, "shortlist_k": K, "source_oracle_digest": ORACLE_DIGEST})
            cfg_path = root / "census/configurations" / f"{cid}.json"; cfg_path.parent.mkdir(parents=True, exist_ok=True); cfg_path.write_text(cfg.to_json(), encoding="utf-8")
            configuration_records.append({"configuration_id": cid, "label": label, "configuration_digest": cfg.configuration_digest, "selected_count": len(ids), "path": str(cfg_path)})
            est_id = f"census-advisor-top512-estimate-{label}-v1"
            result = estimator.collect(instance, workload, repo, cfg, benchmark_id=BENCHMARK, root=root, artifact_id=est_id)
            if result["successful_count"] != 468: raise RuntimeError(f"estimate failed for {label}")
            estimate = result["artifact"]; estimates.append(estimate)
            evaluation_label = label
            evaluation = ArtifactEvaluator().evaluate(truth, estimate, truth_artifact_id="census-truth-v1", truth_digest_value=truth_d, artifact_id=f"census-advisor-top512-evaluation-{label}-v1")
            allocate_evaluation_artifact_dir(BENCHMARK, evaluation.artifact_id, root); write_evaluation_artifact(evaluation, root); evaluations.append(evaluation)
    finally:
        estimator.close()
    exp = ExperimentRunArtifact.from_evaluations(experiment_id="census-advisor-top512-experiment-v1", evaluations=evaluations, repositories={repo.artifact_id: repo}, baseline_label="baseline", labels={e.artifact_id: label for e, (_, label, _) in zip(evaluations, configs)}, metadata={"in_sample_statement": "The same 468-query Census workload is used for singleton ranking, advisor search, and evaluation. This experiment measures in-sample optimization behavior; it does not measure held-out generalization.", "shortlist_k": K, "source_oracle_digest": ORACLE_DIGEST})
    allocate_experiment_artifact_dir(BENCHMARK, exp.experiment_id, root); write_experiment_artifact(exp, root)
    report = ComparisonReport.create(report_id="census-advisor-top512-comparison-v1", experiment=exp, evaluations=evaluations, metadata={"shortlist_k": K, "in_sample": True})
    allocate_comparison_report_dir(BENCHMARK, report.report_id, root); write_comparison_report(report, BENCHMARK, root)
    return {"configurations": configuration_records, "estimates": [{"artifact_id": e.artifact_id, "digest": e.estimate_digest} for e in estimates], "evaluations": [{"artifact_id": e.artifact_id, "digest": e.evaluation_digest, "metrics": dict(e.aggregate_metrics), "label": next(label for cid, label, ids in configs if cid == e.configuration_id)} for e in evaluations], "experiment": {"artifact_id": exp.experiment_id, "digest": exp.experiment_digest}, "comparison": {"artifact_id": report.report_id, "digest": report.report_digest}, "comparison_summaries": [x.to_dict() for x in report.configuration_summaries]}


def main() -> int:
    if not ROOT.is_absolute() or ROOT == Path(ROOT.anchor): raise RuntimeError("unsafe benchmark root")
    repo, bcat, workload, truth, extra, precedence = load_inputs()
    shortlist, short_digest, short_path = freeze_shortlist(extra["ranking"], repo, precedence)
    model = extra["model"]
    first = run_search(repo, bcat, workload, truth, precedence, shortlist, model, "run-1", extra["relation_oid"])
    repeat = run_search(repo, bcat, workload, truth, precedence, shortlist, model, "run-2-fresh-backend", extra["relation_oid"])
    exact = all(first[key] == repeat[key] for key in ("selected_design", "selected_objective", "selected_maintenance_cost", "termination_reason", "accepted_moves_count", "evaluated_moves_count", "total_neighbor_moves_considered", "bound_pruned_no_improvement_count", "bound_pruned_incumbent_count", "baseline_vector_digest", "final_vector_digest"))
    if not exact: raise RuntimeError("search repeatability gate failed")
    artifact = ROOT / "census/artifacts/census-advisor-top512-search-v1"
    if artifact.exists(): raise FileExistsError(f"refusing overwrite: {artifact}")
    artifact.mkdir(parents=True)
    final = evaluate_final(repo, workload, truth, shortlist, first, ROOT)
    diagnostics = []
    oracle_rows = {r["candidate_id"]: r for r in extra["ranking"]}; best = oracle_rows[shortlist[0]["candidate_id"]]
    eval_by_label = {item["label"]: item for item in final["evaluations"]}
    estimates_data = {}
    for label in ("baseline", "advisor", "full"):
        eid = eval_by_label[label]["artifact_id"]; data=json.loads((ROOT/"census/artifacts"/eid/"evaluation.json").read_text())["query_evaluations"]; estimates_data[label]={r["query_id"]:r for r in data}
    for qid in sorted(estimates_data["baseline"]):
        b=estimates_data["baseline"][qid]; a=estimates_data["advisor"][qid]; f=estimates_data["full"][qid]
        diagnostics.append({"query_id": qid, "truth": b.get("truth_cardinality"), "baseline_q_error": b.get("q_error"), "best_singleton_q_error": next((x["q_error"] for x in best["q_errors"] if x["query_id"]==qid), None), "advisor_q_error": a.get("q_error"), "full_q_error": f.get("q_error"), "advisor_delta": None if a.get("q_error") is None or b.get("q_error") is None else a["q_error"]-b["q_error"], "full_delta": None if f.get("q_error") is None or b.get("q_error") is None else f["q_error"]-b["q_error"]})
    write_json(artifact / "shortlist.json", {"shortlist_digest": short_digest, "k": K, "rows": shortlist})
    write_json(artifact / "search-run-1.json", first); write_json(artifact / "search-run-2.json", repeat); write_json(artifact / "repeat.json", {"exact": exact, "run1_elapsed_seconds": first["elapsed_seconds"], "run2_elapsed_seconds": repeat["elapsed_seconds"]}); write_json(artifact / "final.json", final); write_json(artifact / "query-diagnostics.json", diagnostics)
    summary={"status":"PASS","benchmark":BENCHMARK,"shortlist_artifact":str(short_path),"shortlist_digest":short_digest,"k":K,"incidence_digest":INCIDENCE_DIGEST,"search":{k:first[k] for k in first if k != "trace"},"repeat_exact":exact,"final":final,"postgres_source":{"repository":PG_SOURCE,"source_commit":PG_COMMIT,"upstream_base_commit":PG_BASE,"version":"16.14","binary_sha256":PG_BINARY},"in_sample_statement":"The same 468-query Census workload is used for singleton ranking, advisor search, and evaluation. This experiment measures in-sample optimization behavior; it does not measure held-out generalization."}
    write_json(artifact / "manifest.json", summary)
    print(json.dumps({"status":"PASS","shortlist_digest":short_digest,"selected_count":len(first["selected_design"]),"selected_objective":first["selected_objective"],"repeat_exact":exact,"final":final}, indent=2, sort_keys=True))
    return 0

if __name__ == "__main__": raise SystemExit(main())
