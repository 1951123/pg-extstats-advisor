"""Census13 held-out RQ1/RQ2 run over the audited AreCELearnedYet workload."""
from __future__ import annotations

import gzip
import hashlib
import itertools
import json
import math
import os
import sys
from dataclasses import dataclass
from decimal import Decimal
from pathlib import Path
from typing import Any, Iterable

BENCH_SRC = Path("/home/wqts/projects/pg-extstats-benchmarks/src")
if str(BENCH_SRC) not in sys.path:
    sys.path.insert(0, str(BENCH_SRC))

import psycopg
from pgextstats_benchmarks.arecel_adapter import AreCELearnedYetAdapter, DATASET_SPECS
from pgextstats_benchmarks.candidate_catalog import CandidateCatalog as BenchmarkCatalog
from pgextstats_benchmarks.postgres.configuration_provider import PostgreSQLStatisticsConfigurationProvider
from pgextstats_benchmarks.postgres.connection import PostgresConnection
from pgextstats_benchmarks.postgres.instance import PostgresInstance
from pgextstats_benchmarks.postgres.loader import PostgreSQLLoader
from pgextstats_benchmarks.postgres.sample_provider import PostgreSQLAnalyzeSampleProvider
from pgextstats_benchmarks.postgres.statistics_provider import PostgreSQLStatisticsRepositoryProvider
from pgextstats_benchmarks.sample_storage import load_sample_manifest
from pgextstats_benchmarks.statistics_repository import StatisticsRepositoryArtifact
from pgextstats_benchmarks.statistics_storage import load_repository_artifact

from pg_extstats_advisor.candidates.model import CandidateCatalog
from pg_extstats_advisor.cost.model import MaintenanceBudget, MaintenanceCostModel
from pg_extstats_advisor.evaluator.native import NativeEvaluator
from pg_extstats_advisor.incidence.index import IncidenceIndex
from pg_extstats_advisor.models import Candidate, CandidateId, Design, EvaluationState, MechanismKind, Move, QueryEvaluation, QueryId, WorkloadQuery
from pg_extstats_advisor.search.deterministic import DeterministicBudgetSearch
from pg_extstats_advisor.search.model import SearchConfig
from pg_extstats_advisor.workload.model import Workload

ROOT = Path(os.environ.get("PGEXTADV_BENCHMARK_DATA", "/home/wqts/benchmark-data")).expanduser().resolve()
EXP = ROOT / "arecel/census13/experiments/rq1-rq2-heldout-v1"
CATALOG_PATH = ROOT / "arecel/census13/catalogs/census13-candidate-catalog-v1.json"
AUDIT_ROOT = Path("/home/wqts/benchmark-data/arecel/audit-v1")
CANONICAL = AUDIT_ROOT / "census13.canonical.jsonl.gz"
BENCHMARK_ID = "arecel-census13"
RELATION = "public.census13"
TARGET = 100
REPO_ID = "arecel-census13-statistics-repository-v1"
SAMPLE_ID = "arecel-census13-sample-v1"
DB_PORT = 55437
DB_USER = "wqts"
ARCHIVE_SHA256 = "5cd33cba7f3d7182ef497e60e7346fb2a7546941590a90a4444913a944958f79"
UPSTREAM_COMMIT = "aa52da7768023270bad884232972e0b77ec6534a"


def digest(value: Any) -> str:
    return hashlib.sha256(json.dumps(value, sort_keys=True, separators=(",", ":"), default=str, allow_nan=False).encode()).hexdigest()


def write_json(path: Path, value: Any) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text(json.dumps(value, sort_keys=True, indent=2, default=str, allow_nan=False) + "\n", encoding="utf-8")


def read_records() -> list[dict[str, Any]]:
    with gzip.open(CANONICAL, "rt", encoding="utf-8") as stream:
        return [json.loads(line) for line in stream]


def split_records(records: list[dict[str, Any]], split: str) -> list[dict[str, Any]]:
    return [item for item in records if item["split"] == split]


def positive(records: Iterable[dict[str, Any]]) -> list[dict[str, Any]]:
    return [item for item in records if int(item["source_label"]["cardinality"]) > 0]


def promote(adapter: AreCELearnedYetAdapter) -> Any:
    if not (adapter.artifacts_root / "arecel-census13-raw-v1/manifest.json").is_file():
        adapter.fetch()
    if not (adapter.artifacts_root / "arecel-census13-prepared-v1/manifest.json").is_file():
        adapter.prepare()
    if not (adapter.artifacts_root / "arecel-census13-workload-v1/manifest.json").is_file():
        adapter.normalize_workload()
    return adapter.prepared_artifact()


def collect_truth(instance: PostgresInstance, records: list[dict[str, Any]]) -> dict[str, Any]:
    rows = []
    with psycopg.connect(host="localhost", port=DB_PORT, user=DB_USER, dbname=instance.database_name, autocommit=True) as conn:
        for item in records:
            with conn.cursor() as cur:
                cur.execute(item["sql"])
                actual = int(cur.fetchone()[0])
            expected = int(item["source_label"]["cardinality"])
            if actual != expected:
                raise RuntimeError(f"truth mismatch at {item['query_id']}: source={expected}, actual={actual}")
            rows.append({"query_id": item["query_id"], "sql": item["sql"], "source_label": item["source_label"], "actual_cardinality": actual, "truth_match": True})
    split = records[0]["split"]
    return {"artifact_id": f"census13-{split}-truth-v1", "dataset": "census13", "split": split, "query_count": len(rows), "positive_query_count": sum(row["actual_cardinality"] > 0 for row in rows), "zero_query_count": sum(row["actual_cardinality"] == 0 for row in rows), "collection": "exact SELECT COUNT(*)", "rows": rows}


def cached_baseline(evaluator: NativeEvaluator) -> EvaluationState | None:
    path = EXP / "valid-baseline.json"
    if not path.is_file():
        return None
    raw = json.loads(path.read_text(encoding="utf-8"))
    if int(raw.get("query_count", -1)) != len(evaluator.workload.queries):
        return None
    values = []
    for item in raw["rows"]:
        values.append(QueryEvaluation(QueryId(item["query_id"]), float(item["estimated_rows"]), float(item["truth"]), float(item["q_error"]), f"native-explain:{evaluator.adapter.postgres_version}"))
    state = EvaluationState(Design(()), tuple(values), math.fsum(item.contribution for item in values), evaluator.repository.digest, evaluator.workload.digest, evaluator.adapter.postgres_version, "m0-b-native-evaluator-v1", tuple(item.query_id for item in values), frozenset())
    if state_digest(state) != raw.get("vector_digest") or state.aggregate_objective != raw.get("objective"):
        raise RuntimeError("cached baseline artifact integrity check failed")
    return state


def build_catalog() -> BenchmarkCatalog:
    if not CATALOG_PATH.is_file():
        columns = tuple(DATASET_SPECS["census13"]["columns"])
        candidates = []
        for kind, pair in itertools.product(("mcv", "fd"), itertools.combinations(columns, 2)):
            cid = "cand_" + hashlib.sha256(f"{RELATION}|{kind}|{','.join(pair)}".encode()).hexdigest()[:20]
            candidates.append({"candidate_id": cid, "relation_identity": RELATION, "kind": kind, "columns": list(pair), "statistics_target": TARGET, "definition": {"attnums": [columns.index(column) + 1 for column in pair], "schema": "public"}})
        candidates.sort(key=lambda item: item["candidate_id"])
        write_json(CATALOG_PATH, {"catalog_id": "census13-candidate-catalog-v1", "relation_identity": RELATION, "statistics_target": TARGET, "universe": "all unordered column pairs x {mcv,fd}; no screening", "candidates": candidates})
    catalog = BenchmarkCatalog.from_file(CATALOG_PATH)
    expected = set(itertools.combinations(DATASET_SPECS["census13"]["columns"], 2))
    if catalog.candidate_count != 156 or sum(item.kind == "mcv" for item in catalog.candidates) != 78 or sum(item.kind == "fd" for item in catalog.candidates) != 78:
        raise RuntimeError("catalog is not the complete 156-candidate universe")
    if {tuple(item.columns) for item in catalog.candidates if item.kind == "mcv"} != expected:
        raise RuntimeError("MCV catalog is not the exact unordered pair universe")
    return catalog


def diagnostics(conn: PostgresConnection) -> dict[str, Any]:
    conn.connect()
    try:
        row = conn.execute("SELECT c.oid,c.reltuples,c.relpages,s.n_live_tup,s.n_mod_since_analyze,s.last_analyze,s.last_autoanalyze,c.reloptions FROM pg_class c JOIN pg_namespace n ON n.oid=c.relnamespace LEFT JOIN pg_stat_all_tables s ON s.relid=c.oid WHERE n.nspname=%s AND c.relname=%s", ("public", "census13"))[0]
        return {"relation_oid": int(row[0]), "reltuples": float(row[1]), "relpages": int(row[2]), "n_live_tup": int(row[3]), "n_mod_since_analyze": int(row[4]), "last_analyze": str(row[5]) if row[5] else None, "last_autoanalyze": str(row[6]) if row[6] else None, "reloptions": list(row[7] or []), "ordinary_statistics_fingerprint": digest([list(x) for x in conn.ordinary_statistics(RELATION)]), "physical_extended_statistics_count": int(conn.execute("SELECT count(*) FROM pg_statistic_ext e WHERE e.stxrelid=to_regclass(%s)", (RELATION,))[0][0])}
    finally:
        conn.close()


@dataclass
class BridgeRepository:
    catalog: CandidateCatalog
    digest: str


class CataloglessAdapter:
    def __init__(self, repo: StatisticsRepositoryArtifact, instance: PostgresInstance, bridge: BridgeRepository) -> None:
        self.repository = bridge
        self.provider = PostgreSQLStatisticsConfigurationProvider(PostgresConnection(host="localhost", port=DB_PORT, user=DB_USER, database="postgres"))
        self.instance = instance
        self.provider.bind_instance(instance)
        result = self.provider.register_repository(instance, repo, root=ROOT)
        if not result.get("registered"):
            raise RuntimeError("repository was not newly registered")
        self.provider.reset_configuration()
        self.postgres_version = self.provider.target.server_version()
        self._oids = self.provider._oids
        self.explain_calls = 0
        self.activation_calls = 0

    def register_repository(self) -> None:
        return None

    def activate_design(self, design: Design) -> list[str]:
        ids = [str(item) for item in design.candidate_ids]
        active = self.provider.target.hypothetical_activate([self._oids[item] for item in ids])
        self.activation_calls += 1
        if active != [self._oids[item] for item in ids]:
            raise RuntimeError("backend active design differs from requested order")
        return ids

    def start_measurement(self) -> None:
        return None

    def estimate_queries(self, queries: Iterable[WorkloadQuery]) -> dict[QueryId, float]:
        from pgextstats_benchmarks.postgres.estimate_provider import extract_plan_rows
        result = {}
        for query in queries:
            result[query.query_id] = extract_plan_rows(self.provider.target.explain_json(query.sql), RELATION)
            self.explain_calls += 1
        return result

    def close(self) -> None:
        self.provider.reset()
        self.provider.close()


class ZeroCostModel(MaintenanceCostModel):
    @property
    def unit(self) -> str:
        return "objective-only"

    @property
    def digest(self) -> str:
        return digest({"model": "zero"})

    def estimate_candidate(self, candidate: Candidate) -> Decimal:
        return Decimal(0)


def order_for(catalog: BenchmarkCatalog, mode: str, singleton_rank: dict[str, int], columns: tuple[str, ...]) -> list[str]:
    items = list(catalog.candidates)
    if mode == "singleton":
        return [item.candidate_id for item in sorted(items, key=lambda item: (singleton_rank[item.candidate_id], item.candidate_id))]
    if mode == "reverse":
        return list(reversed(order_for(catalog, "singleton", singleton_rank, columns)))
    if mode == "advisor":
        items.sort(key=lambda item: (0 if item.kind == "mcv" else 1, len(item.columns), tuple(columns.index(column) + 1 for column in item.columns), tuple(item.columns), item.candidate_id))
        return [item.candidate_id for item in items]
    if mode == "lexical":
        return sorted(item.candidate_id for item in items)
    if mode == "raw":
        return [item.candidate_id for item in items]
    raise ValueError(mode)


def advisor_catalog(catalog: BenchmarkCatalog, relation_oid: int, order: list[str]) -> CandidateCatalog:
    by_id = {item.candidate_id: item for item in catalog.candidates}
    return CandidateCatalog(tuple(Candidate(CandidateId(item.candidate_id), relation_oid, RELATION, MechanismKind(item.kind), tuple(item.columns), tuple(sorted(item.definition.items())), rank, 0) for rank, item in enumerate(by_id[cid] for cid in order)))


def make_workload(records: list[dict[str, Any]], relation_oid: int, split: str) -> tuple[Workload, dict[str, frozenset[str]]]:
    selected = positive(split_records(records, split))
    queries = tuple(WorkloadQuery(QueryId(item["query_id"]), item["sql"], float(item["source_label"]["cardinality"]), RELATION, frozenset({relation_oid})) for item in selected)
    predicates = {item["query_id"]: frozenset(pred["column"] for pred in item["source_query"]["predicates"]) for item in selected}
    return Workload(f"arecel-census13-{split}-positive-v1", queries), predicates


def incidence(workload: Workload, predicates: dict[str, frozenset[str]], catalog: BenchmarkCatalog) -> IncidenceIndex:
    entries = []
    for item in catalog.candidates:
        affected = frozenset(QueryId(qid) for qid, cols in predicates.items() if set(item.columns).issubset(cols))
        entries.append((CandidateId(item.candidate_id), affected))
    return IncidenceIndex(tuple(entries), frozenset(query.query_id for query in workload.queries))


def make_evaluator(repo: StatisticsRepositoryArtifact, instance: PostgresInstance, catalog: BenchmarkCatalog, order: list[str], records: list[dict[str, Any]], split: str, relation_oid: int) -> tuple[NativeEvaluator, CataloglessAdapter]:
    workload, predicates = make_workload(records, relation_oid, split)
    inc = incidence(workload, predicates, catalog)
    bridge = BridgeRepository(advisor_catalog(catalog, relation_oid, order), repo.repository_digest)
    adapter = CataloglessAdapter(repo, instance, bridge)
    return NativeEvaluator(workload, bridge, inc, adapter), adapter


def state_digest(state: Any) -> str:
    return digest([{"query_id": str(item.query_id), "estimate": item.estimate, "q_error": item.contribution} for item in state.query_evaluations])


def state_rows(state: Any) -> list[dict[str, Any]]:
    return [{"query_id": str(item.query_id), "estimated_rows": item.estimate, "truth": item.truth, "q_error": item.contribution} for item in state.query_evaluations]


def exact(a: Any, b: Any, label: str) -> None:
    if a.aggregate_objective != b.aggregate_objective or state_digest(a) != state_digest(b):
        raise RuntimeError(f"exact replay mismatch: {label}")


def profile(evaluator: NativeEvaluator, catalog: BenchmarkCatalog, baseline: Any) -> list[dict[str, Any]]:
    before = baseline.by_query()
    rows = []
    for item in catalog.candidates:
        state = evaluator.evaluate_move(baseline.design, Move.add_candidate(CandidateId(item.candidate_id)), baseline)
        changed = [str(qid) for qid, value in before.items() if value.estimate != state.by_query()[qid].estimate]
        values = [value.contribution for value in state.query_evaluations]
        base_values = [value.contribution for value in baseline.query_evaluations]
        ordered = sorted(values)
        rows.append({"candidate_id": item.candidate_id, "kind": item.kind, "columns": list(item.columns), "objective": state.aggregate_objective, "singleton_improvement": baseline.aggregate_objective - state.aggregate_objective, "mean_q_error": math.fsum(values) / len(values), "median_q_error": ordered[len(ordered) // 2], "p90_q_error": ordered[int(len(ordered) * .90) - 1], "p95_q_error": ordered[int(len(ordered) * .95) - 1], "max_q_error": max(values), "improved_count": sum(x < y for x, y in zip(values, base_values)), "unchanged_count": sum(x == y for x, y in zip(values, base_values)), "worsened_count": sum(x > y for x, y in zip(values, base_values)), "changed_estimate_count": len(changed), "changed_query_ids": changed, "incidence_count": len(evaluator.incidence.by_candidate[CandidateId(item.candidate_id)]), "incidence_superset_check": set(changed).issubset({str(x) for x in evaluator.incidence.by_candidate[CandidateId(item.candidate_id)]}), "planner_calls": len(state.affected_query_ids)})
    if len(rows) != 156 or not all(row["incidence_superset_check"] for row in rows):
        raise RuntimeError("singleton incidence validation failed")
    return rows


def greedy(evaluator: NativeEvaluator, catalog: BenchmarkCatalog, baseline: Any, order: list[str], relation_oid: int, singleton_rows: list[dict[str, Any]]) -> dict[str, Any]:
    search_catalog = advisor_catalog(catalog, relation_oid, order)
    zero = ZeroCostModel()
    search = DeterministicBudgetSearch(evaluator, search_catalog, zero, MaintenanceBudget(Decimal(0), zero.unit), SearchConfig(add_only=True, exact_bound_pruning=False, record_pruned_moves=False, candidate_set_mode="full", visible_candidate_count=156, global_statistics_target=TARGET), evaluator.incidence)
    search._calls = 1
    current = baseline
    accepted_rounds = []
    cached_winner = CandidateId(singleton_rows[0]["candidate_id"])
    cached_partial = evaluator.evaluate_move(current.design, Move.add_candidate(cached_winner), current)
    cached_oracle = evaluator.evaluate_design(cached_partial.design)
    exact(cached_partial, cached_oracle, "accepted-round-1")
    current = cached_oracle
    search._calls += 2
    search._evaluated += 1
    search._considered += 156
    search._accepted += 1
    accepted_rounds.append({"round": 1, "accepted_candidate_id": str(cached_winner), "design": [str(x) for x in current.design.candidate_ids], "objective": current.aggregate_objective, "vector_digest": state_digest(current), "incremental_explain_calls": len(cached_partial.affected_query_ids), "oracle_explain_calls": len(current.query_evaluations), "exact_match": True, "singleton_profile_reused": True})
    while True:
        before_calls = evaluator.adapter.explain_calls
        selected = set(current.design.candidate_ids)
        winner = search._finish_streaming_round("greedy-add", current, Decimal(0), (Move.add_candidate(CandidateId(cid)) for cid in order if CandidateId(cid) not in selected))
        if winner is None:
            break
        oracle = evaluator.evaluate_design(winner.state.design)
        exact(winner.state, oracle, f"accepted-round-{len(accepted_rounds) + 1}")
        accepted_rounds.append({"round": len(accepted_rounds) + 1, "accepted_candidate_id": str(winner.move.add), "design": [str(x) for x in oracle.design.candidate_ids], "objective": oracle.aggregate_objective, "vector_digest": state_digest(oracle), "incremental_explain_calls": evaluator.adapter.explain_calls - before_calls, "oracle_explain_calls": len(oracle.query_evaluations), "exact_match": True, "singleton_profile_reused": False})
        current = oracle
    return {"initial_objective": baseline.aggregate_objective, "final_objective": current.aggregate_objective, "final_design": [str(x) for x in current.design.candidate_ids], "final_vector_digest": state_digest(current), "accepted_rounds": accepted_rounds, "trajectory": [{"add": str(item.move.add) if item.move.add else None, "accepted": item.accepted, "before_objective": item.before_objective, "after_objective": item.after_objective, "affected_query_count": item.affected_query_count, "rejection_reason": item.rejection_reason} for item in search._trajectory], "search_accounting": {"evaluated_moves": search._evaluated, "calls": search._calls, "accepted": search._accepted, "considered": search._considered, "infeasible": search._skipped, "bound_pruned": search._bound_pruned_no_improvement + search._bound_pruned_incumbent}, "explain_calls": evaluator.adapter.explain_calls}


def evaluate(repo: StatisticsRepositoryArtifact, instance: PostgresInstance, catalog: BenchmarkCatalog, records: list[dict[str, Any]], split: str, relation_oid: int, designs: dict[str, list[str]], order: list[str]) -> tuple[dict[str, Any], CataloglessAdapter]:
    evaluator, adapter = make_evaluator(repo, instance, catalog, order, records, split, relation_oid)
    result = {}
    try:
        for label, ids in designs.items():
            selected = set(ids)
            ordered_ids = [cid for cid in order if cid in selected]
            state = evaluator.evaluate_design(Design(tuple(CandidateId(cid) for cid in ordered_ids)))
            result[label] = {"design": ordered_ids, "objective": state.aggregate_objective, "vector_digest": state_digest(state), "query_count": len(state.query_evaluations), "rows": state_rows(state)}
        return result, adapter
    except Exception:
        adapter.close()
        raise


def main() -> None:
    EXP.mkdir(parents=True, exist_ok=True)
    records = read_records()
    splits = {split: split_records(records, split) for split in ("train", "valid", "test")}
    if [len(splits[split]) for split in ("train", "valid", "test")] != [100000, 10000, 10000]:
        raise RuntimeError("unexpected split sizes")
    adapter = AreCELearnedYetAdapter(dataset="census13", data_root=ROOT, audit_root=AUDIT_ROOT)
    prepared = promote(adapter)
    loader = PostgreSQLLoader(PostgresConnection(host="localhost", port=DB_PORT, user=DB_USER, database="postgres"))
    instance = loader.create_instance("arecel_census13")
    loader.load_artifact(instance, prepared)
    before_load = diagnostics(PostgresConnection(host="localhost", port=DB_PORT, user=DB_USER, database=instance.database_name))
    truth = {}
    for split in ("valid", "test"):
        truth_path = ROOT / f"arecel/census13/truth/{split}.json"
        truth[split] = json.loads(truth_path.read_text(encoding="utf-8")) if truth_path.is_file() else collect_truth(instance, splits[split])
    for split, artifact in truth.items():
        write_json(ROOT / f"arecel/census13/truth/{split}.json", artifact)
    catalog = build_catalog()
    conn = PostgresConnection(host="localhost", port=DB_PORT, user=DB_USER, database=instance.database_name)
    relation_oid = conn.relation_oid(RELATION)
    columns = tuple(conn.table_columns("census13"))
    conn.close()
    sample_provider = PostgreSQLAnalyzeSampleProvider(PostgresConnection(host="localhost", port=DB_PORT, user=DB_USER, database="postgres"))
    sample_manifest = ROOT / BENCHMARK_ID / "artifacts" / SAMPLE_ID / "manifest.json"
    if sample_manifest.is_file():
        sample = load_sample_manifest(BENCHMARK_ID, SAMPLE_ID, ROOT)
        sample_result = {"status": "REUSED", "artifact_id": sample.artifact_id}
    else:
        sample_result = sample_provider.capture_sample(instance, RELATION, benchmark_id=BENCHMARK_ID, parent_data_artifact_id=prepared.id, artifact_id=SAMPLE_ID, root=ROOT)
        sample = load_sample_manifest(BENCHMARK_ID, SAMPLE_ID, ROOT)
    repo_manifest = ROOT / BENCHMARK_ID / "artifacts" / REPO_ID / "manifest.json"
    if repo_manifest.is_file():
        repo = load_repository_artifact(BENCHMARK_ID, REPO_ID, ROOT)
        repo_result = {"status": "REUSED", "present_count": sum(item.state == "PRESENT" for item in repo.candidate_states), "absent_native_count": sum(item.state == "ABSENT_NATIVE" for item in repo.candidate_states)}
    else:
        repo_result = PostgreSQLStatisticsRepositoryProvider(PostgresConnection(host="localhost", port=DB_PORT, user=DB_USER, database="postgres"), sample_provider).acquire_repository(instance, sample, catalog, benchmark_id=BENCHMARK_ID, artifact_id=REPO_ID, root=ROOT, retain_definitions=False)
        if repo_result.get("status") != "PASS":
            raise RuntimeError(repo_result)
        repo = load_repository_artifact(BENCHMARK_ID, REPO_ID, ROOT)
    after_repo = diagnostics(PostgresConnection(host="localhost", port=DB_PORT, user=DB_USER, database=instance.database_name))
    order_advisor = order_for(catalog, "advisor", {}, columns)
    valid_eval, valid_adapter = make_evaluator(repo, instance, catalog, order_advisor, records, "valid", relation_oid)
    baseline = cached_baseline(valid_eval) or valid_eval.evaluate_design(Design(()))
    singleton_path = EXP / "valid-singletons.json"
    if singleton_path.is_file():
        singleton_rows = json.loads(singleton_path.read_text(encoding="utf-8"))["rows"]
        if len(singleton_rows) != 156:
            raise RuntimeError("cached singleton profile does not cover the full universe")
    else:
        singleton_rows = profile(valid_eval, catalog, baseline)
        singleton_rows.sort(key=lambda row: (-row["singleton_improvement"], row["candidate_id"]))
    singleton_rank = {row["candidate_id"]: index for index, row in enumerate(singleton_rows)}
    singleton_order = order_for(catalog, "singleton", singleton_rank, columns)
    write_json(EXP / "valid-baseline.json", {"objective": baseline.aggregate_objective, "vector_digest": state_digest(baseline), "query_count": len(baseline.query_evaluations), "zero_truth_excluded": len(splits["valid"]) - len(positive(splits["valid"])), "rows": state_rows(baseline)})
    write_json(EXP / "valid-singletons.json", {"candidate_universe_count": 156, "rows": singleton_rows, "ranking_digest": digest(singleton_rows)})
    write_json(EXP / "singleton-precedence.json", {"policy": "valid singleton objective descending; candidate_id tie-break", "rows": [{"precedence_rank": i, "candidate_id": cid} for i, cid in enumerate(singleton_order)]})
    valid_adapter.close()
    full_ids = [item.candidate_id for item in catalog.candidates]
    if len(full_ids) != 156:
        raise RuntimeError(f"candidate universe count {len(full_ids)} != 156")
    greedy_eval, greedy_adapter = make_evaluator(repo, instance, catalog, singleton_order, records, "valid", relation_oid)
    greedy_baseline = cached_baseline(greedy_eval) or greedy_eval.evaluate_design(Design(()))
    greedy_result = greedy(greedy_eval, catalog, greedy_baseline, singleton_order, relation_oid, singleton_rows)
    write_json(EXP / "greedy-trajectory.json", greedy_result)
    write_json(EXP / "accepted-round-correctness.json", {"accepted_rounds": greedy_result["accepted_rounds"], "all_exact": all(item["exact_match"] for item in greedy_result["accepted_rounds"])})
    final_ids = greedy_result["final_design"]
    greedy_adapter.close()
    modes = ("singleton", "reverse", "advisor", "lexical", "raw")
    orders = {mode: order_for(catalog, mode, singleton_rank, columns) for mode in modes}
    valid_order_results = {}
    for mode in modes:
        data, a = evaluate(repo, instance, catalog, records, "valid", relation_oid, {"full_present": full_ids}, orders[mode])
        valid_order_results[mode] = data["full_present"]
        a.close()
    write_json(EXP / "valid-order-sensitivity.json", {"orders": {mode: {"order_digest": digest(orders[mode]), "order": orders[mode], "evaluation": valid_order_results[mode]} for mode in modes}})
    frozen = {"empty": [], "best_valid_singleton": [singleton_rows[0]["candidate_id"]], "full_present_valid_order": full_ids, "final_selected_valid_order": final_ids}
    test_frozen, a = evaluate(repo, instance, catalog, records, "test", relation_oid, frozen, singleton_order)
    a.close()
    write_json(EXP / "test-results.json", test_frozen)
    controls = {}
    for mode in ("singleton", "advisor", "raw", "reverse"):
        data, a = evaluate(repo, instance, catalog, records, "test", relation_oid, {"full_present": full_ids, "final_selected": final_ids}, orders[mode])
        controls[mode] = data
        a.close()
    write_json(EXP / "test-order-controls.json", {"orders": {mode: {"order_digest": digest(orders[mode]), "full_present": controls[mode]["full_present"], "final_selected": controls[mode]["final_selected"]} for mode in controls}})
    final_replay, a = evaluate(repo, instance, catalog, records, "test", relation_oid, {"final_selected": final_ids}, singleton_order)
    a.close()
    if final_replay["final_selected"]["vector_digest"] != test_frozen["final_selected_valid_order"]["vector_digest"]:
        raise RuntimeError("final replay vector mismatch")
    after_final = diagnostics(PostgresConnection(host="localhost", port=DB_PORT, user=DB_USER, database=instance.database_name))
    write_json(EXP / "generalization.json", {"valid_baseline_objective": baseline.aggregate_objective, "valid_final_objective": greedy_result["final_objective"], "valid_final_improvement": baseline.aggregate_objective - greedy_result["final_objective"], "test_empty_objective": test_frozen["empty"]["objective"], "test_best_valid_singleton_objective": test_frozen["best_valid_singleton"]["objective"], "test_full_present_objective": test_frozen["full_present_valid_order"]["objective"], "test_final_objective": test_frozen["final_selected_valid_order"]["objective"], "test_final_improvement_vs_empty": test_frozen["empty"]["objective"] - test_frozen["final_selected_valid_order"]["objective"]})
    write_json(EXP / "statistics-stability.json", {"repository_ordinary_statistics_fingerprint": repo.ordinary_statistics_fingerprint, "after_load": before_load, "after_repository": after_repo, "after_final": after_final, "fingerprint_stable": before_load["ordinary_statistics_fingerprint"] == repo.ordinary_statistics_fingerprint == after_repo["ordinary_statistics_fingerprint"] == after_final["ordinary_statistics_fingerprint"], "physical_extended_statistics_absent_after_acquisition": after_repo["physical_extended_statistics_count"] == 0, "sample": {"artifact_id": sample.artifact_id, "payload_sha256": sample.payload_sha256, "tuple_count": sample.sample_tuple_count, "estimated_total_rows": sample.estimated_total_rows}, "repository": {"artifact_id": repo.artifact_id, "digest": repo.repository_digest, "present_count": sum(item.state == "PRESENT" for item in repo.candidate_states), "absent_native_count": sum(item.state == "ABSENT_NATIVE" for item in repo.candidate_states)}})
    write_json(EXP / "work-accounting.json", {"truth_queries": {split: len(splits[split]) for split in ("valid", "test")}, "positive_queries": {split: len(positive(splits[split])) for split in ("valid", "test")}, "candidate_universe": {"mcv": 78, "fd": 78, "total": 156}, "singleton_native_evaluations": 156, "greedy": greedy_result["search_accounting"], "greedy_explain_calls": greedy_result["explain_calls"], "random_order_optional": False, "rq3_or_runtime_experiments": False})
    write_json(EXP / "diagnostics.json", {"instance": instance.to_dict(), "relation_oid": relation_oid, "columns": columns, "loader_statistics_policy": instance.metadata.get("statistics_maintenance_policy"), "truth": {split: {key: truth[split][key] for key in ("query_count", "positive_query_count", "zero_query_count")} for split in truth}, "repository_acquisition": {key: repo_result.get(key) for key in ("status", "sample_import_count", "analyze_count", "present_count", "absent_native_count", "cleanup")}, "sample_capture": {key: sample_result.get(key) for key in ("status", "tuple_count", "estimated_total_rows", "manifest_path")}})
    write_json(EXP / "provenance.json", {"dataset": "census13", "archive_sha256": ARCHIVE_SHA256, "upstream_commit": UPSTREAM_COMMIT, "audit_root": str(AUDIT_ROOT), "canonical_workload": str(CANONICAL), "benchmark_id": BENCHMARK_ID, "relation": RELATION, "statistics_target": TARGET, "objective_policy": "positive_truth_only; zero-truth rows excluded from q-error objective", "design_split": "valid", "evaluation_split": "test", "train_provenance_only": True, "candidate_universe": "all 78 unordered pairs x 2 mechanisms = 156; no screening", "sample_artifact_id": sample.artifact_id, "repository_artifact_id": repo.artifact_id, "repository_digest": repo.repository_digest})
    write_json(EXP / "experiment.json", {"experiment_id": "census13-rq1-rq2-heldout-v1", "status": "PASS", "scope": "RQ1/RQ2 only", "instance_id": instance.instance_id, "database_name": instance.database_name, "postgres_source": sample.postgres_source, "artifact_root": str(ROOT / "arecel/census13"), "outputs": sorted(path.name for path in EXP.glob("*.json"))})


if __name__ == "__main__":
    main()
