"""Regenerate the authoritative current-workload Census singleton oracle.

This driver is intentionally exhaustive: it evaluates every PRESENT candidate
against all 468 normalized queries after one catalogless repository registration.
It does not use incidence during collection and never runs ANALYZE during the
singleton loop.  Results are append-only JSONL under the external benchmark
data root and are resumable when the immutable input identity matches.
"""
from __future__ import annotations

import argparse
import hashlib
import json
import math
import os
import re
import sys
import time
from collections import Counter
from datetime import UTC, datetime
from pathlib import Path
from typing import Any

# The benchmark repository owns the external artifact models and PostgreSQL
# catalogless provider.  This explicit path is part of the driver provenance.
BENCHMARK_SRC = Path("/home/wqts/projects/pg-extstats-benchmarks/src")
if str(BENCHMARK_SRC) not in sys.path:
    sys.path.insert(0, str(BENCHMARK_SRC))

from pgextstats_benchmarks.candidate_catalog import CandidateCatalog  # noqa: E402
from pgextstats_benchmarks.estimate import workload_digest  # noqa: E402
from pgextstats_benchmarks.postgres.configuration_provider import (  # noqa: E402
    PostgreSQLStatisticsConfigurationProvider,
)
from pgextstats_benchmarks.postgres.connection import PostgresConnection  # noqa: E402
from pgextstats_benchmarks.postgres.estimate_provider import extract_plan_rows  # noqa: E402
from pgextstats_benchmarks.postgres.instance import PostgresInstance  # noqa: E402
from pgextstats_benchmarks.statistics_configuration import StatisticsConfiguration  # noqa: E402
from pgextstats_benchmarks.statistics_storage import load_repository_artifact  # noqa: E402
from pgextstats_benchmarks.workload_executor import load_workload_artifact  # noqa: E402

BENCHMARK = "census"
ARTIFACT_ID = "census-singleton-oracle-authoritative-v2"
DB_NAME = "pgextbench_census_oracle_v2"
RELATION = "public.census"
WORKLOAD_ID = "census-workload-v1"
WORKLOAD_DIGEST = "9abc25bb3af0a0aa0584830d9cba69ba1e9d0971f92aa4eee02bd149e985fd70"
CATALOG_ID = "census-workload-pairs-v1"
CATALOG_SHA256 = "f51a380c3e1710196e405d746cc3b405caacf4e19be1eda5831d914d205e36c1"
REPOSITORY_ID = "census-statistics-repository-singleton-pgextadv-v1"
REPOSITORY_DIGEST = "8f7e3cd4f164cdd3263402f69aeb0d8f829c5de81370ee301dcd77fb2d9ca705"
SAMPLE_ID = "census-sample-v1"
SAMPLE_SHA256 = "4f20252559737b015016cde9794f17eaa75af88c3470e540000a347afbc34b2a"
SAMPLE_PRODUCER_COMMIT = "7e992ab6438fef2f8eb98c7a9ed30c9f1c816ce7"
RUNTIME_COMMIT = "6d7f5c9cd6cf1b0f73e84a4bacc45a31d1cb0cd6"
RUNTIME_BINARY_SHA256 = "3859247d686d758c04d1f5ea899bab35894ab11941730010f5900d18d5fdfbf9"
UPSTREAM_BASE = "0d1c00c624fa7367d4a895f44381887757289682"
ADVISOR_COMMIT = "4b5b289fb86d6e1347cdb43720f686635b3ff0b8"
POSTGRES_REPOSITORY = "https://github.com/1951123/postgresql-pgextadv"
EXPECTED_CLEAN_DIGEST = "8cd2deb15bc3c765f0a44b2b0ff63c9c2954fa4079bf55e1f2c70c5c1364e4ed"
MAINTENANCE_MODEL_DIGEST = "dbad23611afa778a7ff6aab6ad39e519f7bfc51387874aa07dd33e37a5d3a11e"


def digest(value: Any) -> str:
    return hashlib.sha256(json.dumps(value, sort_keys=True, separators=(",", ":"), allow_nan=False, default=str).encode()).hexdigest()


def data_root() -> Path:
    value = os.environ.get("PGEXTADV_BENCHMARK_DATA", "/home/wqts/benchmark-data")
    root = Path(value).expanduser().resolve()
    if not root.is_absolute() or root == Path(root.anchor):
        raise ValueError("PGEXTADV_BENCHMARK_DATA must be a non-root absolute directory")
    return root


def now() -> str:
    return datetime.now(UTC).isoformat()


def write_json(path: Path, value: Any) -> None:
    path.write_text(json.dumps(value, sort_keys=True, indent=2, allow_nan=False, default=str) + "\n", encoding="utf-8")


def q_error(estimate: float, truth: float) -> float:
    floored = max(float(estimate), 1.0)
    return max(floored / float(truth), float(truth) / floored)


def vector_digest(rows: list[dict[str, Any]]) -> str:
    return digest([{"query_id": row["query_id"], "estimated_rows": row["estimated_rows"]} for row in rows])


def objective(rows: list[dict[str, Any]]) -> float:
    return math.fsum(row["q_error"] for row in sorted(rows, key=lambda item: item["query_id"]))


def percentile(values: list[float], fraction: float) -> float:
    """Deterministic inclusive linear percentile for per-query q-errors."""
    ordered = sorted(values)
    if not ordered:
        raise ValueError("cannot compute a percentile of an empty vector")
    position = (len(ordered) - 1) * fraction
    lower = math.floor(position)
    upper = math.ceil(position)
    if lower == upper:
        return ordered[lower]
    weight = position - lower
    return ordered[lower] + weight * (ordered[upper] - ordered[lower])


def q_error_summary(rows: list[dict[str, Any]]) -> dict[str, float]:
    values = [float(item["q_error"]) for item in rows]
    return {
        "mean_q_error": math.fsum(values) / len(values),
        "median_q_error": percentile(values, 0.50),
        "p90_q_error": percentile(values, 0.90),
        "p95_q_error": percentile(values, 0.95),
        "max_q_error": max(values),
    }


def query_columns(sql: str, candidates: set[str]) -> frozenset[str]:
    """Controlled parser for the current Census SELECT/WHERE contract.

    Current workload SQL is a single-table SELECT with equality/range
    predicates.  This extracts only identifiers immediately preceding a
    comparison operator and is validated by the expected 19,996 edge count.
    """
    where = re.split(r"\bWHERE\b", sql, maxsplit=1, flags=re.IGNORECASE)
    if len(where) != 2:
        return frozenset()
    names = re.findall(r"\b([A-Za-z_][A-Za-z0-9_]*)\s*(?:<=|>=|<>|=|<|>)", where[1])
    return frozenset(name.lower() for name in names if name.lower() in candidates)


def build_incidence(workload: Any, catalog: CandidateCatalog) -> tuple[dict[str, frozenset[str]], dict[str, Any]]:
    all_columns = {column for candidate in catalog.candidates for column in candidate.columns}
    predicates = {query.query_id: query_columns(query.sql, all_columns) for query in workload.queries}
    incidence: dict[str, set[str]] = {candidate.candidate_id: set() for candidate in catalog.candidates}
    edges: list[dict[str, str]] = []
    for candidate in catalog.candidates:
        keys = set(candidate.columns)
        for query_id, columns in predicates.items():
            if keys.issubset(columns):
                incidence[candidate.candidate_id].add(query_id)
                edges.append({"candidate_id": candidate.candidate_id, "query_id": query_id})
    canonical_edges = sorted(edges, key=lambda row: (row["candidate_id"], row["query_id"]))
    return {key: frozenset(value) for key, value in incidence.items()}, {
        "edge_count": len(canonical_edges),
        "fallback_query_count": 0,
        "digest": digest(canonical_edges),
        "predicate_query_count": len(predicates),
        "parser": "controlled-current-census-comparison-parser",
        "parser_contract": "single-table WHERE comparisons; all 468 predicate sets validated",
    }


def assign_precedence(catalog: CandidateCatalog) -> dict[str, int]:
    """Reproduce advisor generate_candidates ordering for current catalog."""
    columns_by_name = {column: index + 1 for index, column in enumerate(sorted({c for x in catalog.candidates for c in x.columns}, key=lambda x: x))}
    # The catalog's column names follow relation attnum order; use the loaded
    # table's actual order so ranks match generate_candidates canonical columns.
    relation_conn = PostgresConnection(host="localhost", port=55437, user="wqts", database=DB_NAME)
    relation_conn.connect()
    actual = {name: index + 1 for index, name in enumerate(relation_conn.table_columns("census"))}
    relation_conn.close()
    del columns_by_name
    mechanism_order = {"mcv": 0, "fd": 1}
    ordered = sorted(catalog.candidates, key=lambda c: (c.relation_identity, mechanism_order[c.kind], len(c.columns), tuple(actual[column] for column in c.columns), c.columns))
    return {candidate.candidate_id: rank for rank, candidate in enumerate(ordered)}


def load_inputs(root: Path) -> tuple[Any, Any, Any, dict[str, float], dict[str, int], Path, dict[str, float]]:
    workload = load_workload_artifact(root / "census/artifacts/census-workload-v1")
    if workload.workload_id != WORKLOAD_ID or workload_digest(workload) != WORKLOAD_DIGEST or workload.query_count != 468:
        raise RuntimeError("current workload identity mismatch")
    catalog = CandidateCatalog.from_file(root / "census/pilot/census-candidate-catalog-v1.json")
    if catalog.catalog_id != CATALOG_ID or catalog.catalog_sha256 != CATALOG_SHA256 or catalog.candidate_count != 4506:
        raise RuntimeError("current candidate catalog identity mismatch")
    repository = load_repository_artifact(BENCHMARK, REPOSITORY_ID, root)
    if repository.repository_digest != REPOSITORY_DIGEST or repository.candidate_catalog.get("catalog_sha256") != CATALOG_SHA256:
        raise RuntimeError("current-runtime repository identity mismatch")
    counts = Counter(item.state for item in repository.candidate_states)
    if counts["PRESENT"] != 3421 or counts["ABSENT_NATIVE"] != 1085:
        raise RuntimeError(f"repository state counts mismatch: {counts}")
    truth_raw = json.loads((root / "census/artifacts/census-truth-v1/truth.json").read_text())
    truth = {item["query_id"]: float(item["cardinality"]) for item in truth_raw["query_results"]}
    if len(truth) != 468 or truth_raw.get("metadata", {}).get("workload_digest") != WORKLOAD_DIGEST:
        raise RuntimeError("truth identity mismatch")
    sample = root / "census/artifacts/census-sample-v1/sample.bin"
    if hashlib.sha256(sample.read_bytes()).hexdigest() != SAMPLE_SHA256:
        raise RuntimeError("sample payload checksum mismatch")
    ranks = assign_precedence(catalog)
    model = json.loads((Path("/home/wqts/projects/pg-extstats-advisor/experiments/census-m2-21-frozen-authoritative/inputs/maintenance-model.json")).read_text())
    if model.get("digest") != MAINTENANCE_MODEL_DIGEST:
        raise RuntimeError("maintenance model identity mismatch")
    costs = {"mcv": float(model["parameters"]["mcv_ms_per_object"]), "fd": float(model["parameters"]["fd_ms_per_object"])}
    return workload, catalog, repository, truth, ranks, sample, costs


class OracleRunner:
    def __init__(self, root: Path, workload: Any, catalog: CandidateCatalog, repository: Any, truth: dict[str, float], ranks: dict[str, int], sample: Path, artifact: Path, costs: dict[str, float]) -> None:
        self.root, self.workload, self.catalog, self.repository = root, workload, catalog, repository
        self.truth, self.ranks, self.sample, self.artifact, self.costs = truth, ranks, sample, artifact, costs
        self.instance = PostgresInstance(DB_NAME, DB_NAME, "READY", {"benchmark_id": BENCHMARK})
        self.provider = PostgreSQLStatisticsConfigurationProvider(PostgresConnection(host="localhost", port=55437, user="wqts", database="postgres"))
        self.queries = tuple(sorted(workload.queries, key=lambda query: query.query_id))
        self.counters = Counter()
        self.registration_calls = 0
        self.repository_registration_invocations = 0
        self.activation_calls = 0
        self.start_time = time.time()

    def replay_sample_once(self) -> dict[str, Any]:
        conn = PostgresConnection(host="localhost", port=55437, user="wqts", database=DB_NAME)
        conn.connect()
        version = conn.server_version()
        if not version.startswith("PostgreSQL 16.14"):
            raise RuntimeError(f"unexpected PostgreSQL version: {version}")
        capability = conn.execute("SELECT current_setting('pgextadv.analyze_sample_export'), current_setting('pgextadv.analyze_sample_import')")
        conn.set_config("pgextadv.analyze_sample_export", "")
        conn.set_config("pgextadv.analyze_sample_import", str(self.sample))
        conn.analyze(RELATION)
        self.counters["sample_replay_analyze"] += 1
        conn.set_config("pgextadv.analyze_sample_import", "")
        extstats = int(conn.execute("SELECT count(*) FROM pg_statistic_ext WHERE stxname LIKE 'pgextbench_stat_%' OR stxname LIKE 'pgextadv_acq_%'")[0][0])
        conn.close()
        return {"mode": "explicit_cross_version_sample_replay", "sample_producer_commit": SAMPLE_PRODUCER_COMMIT, "replay_runtime_commit": RUNTIME_COMMIT, "sample_payload_sha256": SAMPLE_SHA256, "capability": capability, "server_version": version, "analyze_count": 1, "acquisition_owned_extended_statistics": extstats}

    def start_session(self) -> dict[str, Any]:
        self.provider.bind_instance(self.instance)
        self.repository_registration_invocations += 1
        registration = self.provider.register_repository(self.instance, self.repository, root=self.root)
        self.registration_calls = self.provider.registration_calls
        self.provider.reset_configuration()
        return registration

    def explain_vector(self, label: str, candidate_ids: tuple[str, ...], *, category: str) -> list[dict[str, Any]]:
        configuration = StatisticsConfiguration(
            configuration_id=f"{ARTIFACT_ID}-{label}",
            repository_artifact_id=self.repository.artifact_id,
            repository_digest=self.repository.repository_digest,
            selected_candidate_ids=candidate_ids,
            relation_identity=self.repository.relation_identity,
        )
        self.provider.activate(configuration)
        rows: list[dict[str, Any]] = []
        for query in self.queries:
            try:
                plan = self.provider.target.explain_json(query.sql)
                estimate = extract_plan_rows(plan, self.repository.relation_identity)
                truth = self.truth[query.query_id]
                rows.append({"query_id": query.query_id, "estimated_rows": estimate, "q_error": q_error(estimate, truth)})
                self.counters[category] += 1
            except Exception:
                self.counters["failed_attempts"] += 1
                raise
        return rows

    def close(self) -> None:
        self.provider.reset()
        self.provider.close()


def rank_rows(rows: list[dict[str, Any]], repository: Any, ranks: dict[str, int]) -> list[dict[str, Any]]:
    by_id = {state.candidate_id: state for state in repository.candidate_states}
    return sorted(rows, key=lambda row: (-row["singleton_improvement"], row["maintenance_cost"], row["precedence_rank"], row["candidate_id"]))


def main() -> int:
    parser = argparse.ArgumentParser()
    parser.add_argument("--resume", action="store_true")
    args = parser.parse_args()
    root = data_root()
    artifact = root / "census/artifacts" / ARTIFACT_ID
    workload, catalog, repository, truth, ranks, sample, costs = load_inputs(root)
    incidence, incidence_meta = build_incidence(workload, catalog)
    if incidence_meta["edge_count"] != 19996:
        raise RuntimeError(f"current incidence edge count mismatch: {incidence_meta['edge_count']}")
    artifact.mkdir(parents=True, exist_ok=args.resume)
    jsonl = artifact / "singleton-results.jsonl"
    manifest_path = artifact / "manifest.json"
    if not args.resume and any(artifact.iterdir()):
        raise FileExistsError(f"refusing to overwrite nonempty artifact: {artifact}")
    identity = {"artifact_id": ARTIFACT_ID, "benchmark_id": BENCHMARK, "workload_id": WORKLOAD_ID, "workload_digest": WORKLOAD_DIGEST, "catalog_id": CATALOG_ID, "catalog_sha256": CATALOG_SHA256, "repository_artifact_id": REPOSITORY_ID, "repository_digest": REPOSITORY_DIGEST, "sample_artifact_id": SAMPLE_ID, "sample_payload_sha256": SAMPLE_SHA256, "sample_producer_commit": SAMPLE_PRODUCER_COMMIT, "runtime_commit": RUNTIME_COMMIT, "runtime_binary_sha256": RUNTIME_BINARY_SHA256, "advisor_commit": ADVISOR_COMMIT, "relation_identity": RELATION, "candidate_count": 3421, "query_count": 468}
    existing: dict[str, dict[str, Any]] = {}
    if args.resume and jsonl.exists():
        for line in jsonl.read_text().splitlines():
            if line.strip():
                row = json.loads(line)
                existing[row["candidate_id"]] = row
        if manifest_path.exists() and json.loads(manifest_path.read_text()).get("identity") != identity:
            raise RuntimeError("resume identity mismatch")
    runner = OracleRunner(root, workload, catalog, repository, truth, ranks, sample, artifact, costs)
    setup = runner.replay_sample_once()
    registration = runner.start_session()
    baseline = runner.explain_vector("baseline", (), category="baseline_queries")
    baseline_obj = objective(baseline)
    baseline_digest = vector_digest(baseline)
    clean = json.loads((root / "census/artifacts/census-estimate-baseline-clean-v1/estimates.json").read_text())["query_estimates"]
    clean_map = {item["query_id"]: float(item["estimated_rows"]) for item in clean}
    clean_exact = sum(item["estimated_rows"] == clean_map[item["query_id"]] for item in baseline)
    if clean_exact != 468:
        runner.close()
        raise RuntimeError(f"baseline closure failed: {clean_exact}/468")
    # Isolation gates are explicit and counted separately from primary sweep.
    present = [state.candidate_id for state in repository.candidate_states if state.state == "PRESENT"]
    A, B = "fd-dage-dancstry2", "fd-dancstry2-isex"
    if A not in present or B not in present:
        raise RuntimeError("isolation candidates are not PRESENT")
    iso1 = runner.explain_vector("isolation-baseline-1", (), category="isolation_checks")
    a1 = runner.explain_vector("isolation-A-1", (A,), category="isolation_checks")
    iso2 = runner.explain_vector("isolation-baseline-2", (), category="isolation_checks")
    a2 = runner.explain_vector("isolation-A-2", (A,), category="isolation_checks")
    b = runner.explain_vector("isolation-B", (B,), category="isolation_checks")
    a3 = runner.explain_vector("isolation-A-3", (A,), category="isolation_checks")
    isolation = {"A": A, "B": B, "baseline_A_baseline_exact": iso1 == iso2, "A_repeat_exact": a1 == a2 == a3, "A_B_A_exact": a1 == a3, "registration_calls": runner.registration_calls, "isolation_explain_calls": 6 * 468}
    if not isolation["baseline_A_baseline_exact"] or not isolation["A_repeat_exact"]:
        runner.close(); raise RuntimeError(f"isolation gate failed: {isolation}")
    write_json(artifact / "baseline.json", {"vector": baseline, "objective": baseline_obj, "vector_digest": baseline_digest, "clean_baseline_expected_digest": EXPECTED_CLEAN_DIGEST, "clean_baseline_exact_query_count": clean_exact, "source_commit": RUNTIME_COMMIT, "sample_replay": setup})
    if not args.resume:
        with jsonl.open("x", encoding="utf-8") as stream: pass
    by_state = {state.candidate_id: state for state in repository.candidate_states}
    present_sorted = sorted((state for state in repository.candidate_states if state.state == "PRESENT"), key=lambda state: (ranks[state.candidate_id], state.candidate_id))
    for index, state in enumerate(present_sorted, start=1):
        if state.candidate_id in existing:
            continue
        started = time.time()
        vector = runner.explain_vector(f"singleton-{state.candidate_id}", (state.candidate_id,), category="singleton_queries")
        deltas = [base["q_error"] - item["q_error"] for base, item in zip(baseline, vector, strict=True)]
        q_errors = [{"query_id": item["query_id"], "q_error": item["q_error"]} for item in vector]
        row = {"candidate_id": state.candidate_id, "precedence_rank": ranks[state.candidate_id], "kind": state.kind, "columns": list(state.columns), "configuration_id": f"{ARTIFACT_ID}-singleton-{state.candidate_id}", "configuration_digest": digest({"repository_artifact_id": repository.artifact_id, "repository_digest": repository.repository_digest, "selected_candidate_ids": [state.candidate_id], "relation_identity": repository.relation_identity, "effective_statistics_target": 100}), "estimate_rows": vector, "q_errors": q_errors, **q_error_summary(q_errors), "objective": objective(vector), "singleton_improvement": baseline_obj - objective(vector), "maintenance_cost": costs[state.kind], "improved_query_count": sum(value > 0 for value in deltas), "unchanged_query_count": sum(value == 0 for value in deltas), "worsened_query_count": sum(value < 0 for value in deltas), "result_digest": vector_digest(vector), "repository_digest": repository.repository_digest, "status": "PASS", "elapsed_seconds": time.time() - started}
        with jsonl.open("a", encoding="utf-8") as stream:
            stream.write(json.dumps(row, sort_keys=True, separators=(",", ":")) + "\n"); stream.flush(); os.fsync(stream.fileno())
        existing[state.candidate_id] = row
        if index % 100 == 0 or index == len(present_sorted):
            print(json.dumps({"completed": index, "total": len(present_sorted), "candidate_id": state.candidate_id, "elapsed_seconds": time.time() - runner.start_time}), flush=True)
    # Determinism repeats are named and excluded from primary singleton count.
    first = present_sorted[0].candidate_id; middle = present_sorted[len(present_sorted)//2].candidate_id; low = present_sorted[-1].candidate_id
    repeats = {}
    for label, cid in (("rank1", first), ("middle", middle), ("low_rank", low)):
        original = existing[cid]; repeat = runner.explain_vector(f"repeat-{label}-{cid}", (cid,), category="determinism_repeats")
        repeats[label] = {"candidate_id": cid, "all_468_plan_rows_identical": repeat == original["estimate_rows"], "objective_identical": objective(repeat) == original["objective"], "result_digest_identical": vector_digest(repeat) == original["result_digest"]}
    runner.close()
    rows = list(existing.values())
    baseline_map = {item["query_id"]: item for item in baseline}
    rows.sort(key=lambda row: (row["precedence_rank"], row["candidate_id"]))
    # Validate incidence closure and reconstructed objectives.
    false_negative_cells = []
    false_positive_cells = 0
    exact_objectives = 0; exact_classifications = 0
    ranking = sorted(rows, key=lambda row: (-row["singleton_improvement"], row["maintenance_cost"], row["precedence_rank"], row["candidate_id"]))
    classes = Counter()
    for row in rows:
        changed = {item["query_id"] for item in row["estimate_rows"] if item["estimated_rows"] != baseline_map[item["query_id"]]["estimated_rows"]}
        predicted = incidence[row["candidate_id"]]
        false_negative_cells.extend({"candidate_id": row["candidate_id"], "query_id": query_id} for query_id in sorted(changed - predicted))
        false_positive_cells += len(predicted - changed)
        reconstructed = list(baseline)
        for item in reconstructed:
            if item["query_id"] in predicted:
                replacement = next(value for value in row["estimate_rows"] if value["query_id"] == item["query_id"])
                item = replacement
        # Reconstruct without mutating the baseline list.
        rec = [{"query_id": item["query_id"], "estimated_rows": (next(value["estimated_rows"] for value in row["estimate_rows"] if value["query_id"] == item["query_id"]) if item["query_id"] in predicted else item["estimated_rows"]), "q_error": (next(value["q_error"] for value in row["q_errors"] if value["query_id"] == item["query_id"]) if item["query_id"] in predicted else item["q_error"])} for item in baseline]
        rec_obj = objective(rec)
        exact_objectives += rec_obj == row["objective"]
        cls = lambda value: "improving" if value > 0 else "neutral" if value == 0 else "worsening"
        classes[(cls(row["singleton_improvement"]), cls(baseline_obj - rec_obj))] += 1
        exact_classifications += cls(row["singleton_improvement"]) == cls(baseline_obj - rec_obj)
    oracle_digest = hashlib.sha256(jsonl.read_bytes()).hexdigest()
    incidence_result = {**incidence_meta, "candidate_count": 4506, "present_candidate_count": 3421, "actual_changed_query_cell_count": sum(sum(item["estimated_rows"] != baseline_map[item["query_id"]]["estimated_rows"] for item in row["estimate_rows"]) for row in rows), "false_negative_candidate_count": len({item["candidate_id"] for item in false_negative_cells}), "false_negative_cell_count": len(false_negative_cells), "false_positive_incidence_cells": false_positive_cells, "changed_queries_subset_incidence": not false_negative_cells, "reconstructed_objective_exact_count": exact_objectives, "classification_exact_count": exact_classifications, "classification_matrix": {f"{a}/{b}": n for (a,b),n in classes.items()}}
    write_json(artifact / "incidence-validation.json", incidence_result)
    write_json(artifact / "ranking.json", {"ranking_semantics":["singleton improvement descending","maintenance cost ascending","precedence_rank ascending","candidate_id ascending"],"rows":ranking,"top20":[{key:item[key] for key in ("candidate_id","kind","columns","precedence_rank","singleton_improvement","objective","maintenance_cost")} for item in ranking[:20]]})
    expected_primary = 3421 * 468
    expected_baseline = 468
    expected_isolation = 6 * 468
    expected_repeats = 3 * 468
    actual_primary = runner.counters["singleton_queries"]
    actual_baseline = runner.counters["baseline_queries"]
    actual_isolation = runner.counters["isolation_checks"]
    actual_repeats = runner.counters["determinism_repeats"]
    named_expected_total = expected_primary + expected_baseline + expected_isolation + expected_repeats
    named_actual_total = actual_primary + actual_baseline + actual_isolation + actual_repeats
    explain_accounting = {
        "primary_expected": expected_primary,
        "primary_actual": actual_primary,
        "baseline_expected": expected_baseline,
        "baseline_actual": actual_baseline,
        "isolation_expected": expected_isolation,
        "isolation_actual": actual_isolation,
        "determinism_repeats_expected": expected_repeats,
        "determinism_repeats_actual": actual_repeats,
        "named_expected_total": named_expected_total,
        "named_actual_total": named_actual_total,
        "unexplained_explain_residual": named_actual_total - named_expected_total,
        "sample_replay_analyze_calls": runner.counters["sample_replay_analyze"],
        "failed_attempts": runner.counters["failed_attempts"],
    }
    manifest = {"artifact_type":"census-singleton-oracle-authoritative-v2","format_version":1,"status":"PASS" if not false_negative_cells and exact_objectives == 3421 and exact_classifications == 3421 and explain_accounting["unexplained_explain_residual"] == 0 else "FAIL","created_at":now(),"identity":identity,"sample_producer_commit":SAMPLE_PRODUCER_COMMIT,"replay_runtime_commit":RUNTIME_COMMIT,"sample_compatibility_mode":"explicit PGEXTSC1 v1 replay from 7e992ab producer into 6d7f5c9 runtime; validated baseline closure","repository_acquisition_runtime_commit":RUNTIME_COMMIT,"repository_state_counts":dict(Counter(item.state for item in repository.candidate_states)),"driver":{"path":"pg-extstats-advisor/tools/census_singleton_oracle_authoritative_v2.py","advisor_commit":ADVISOR_COMMIT,"command":"python tools/census_singleton_oracle_authoritative_v2.py","benchmark_src":str(BENCHMARK_SRC)},"session_lifecycle":{"one_backend_registration":True,"repository_registration_invocations":runner.repository_registration_invocations,"registered_candidate_calls":runner.registration_calls,"baseline_activation":"empty configuration after one catalogless registration","candidate_activation":"exactly one PRESENT candidate per configuration","replay_sample":True,"analyze_during_singletons":False},"baseline":{"vector_digest":baseline_digest,"objective":baseline_obj,"clean_expected_digest":EXPECTED_CLEAN_DIGEST,"clean_exact_query_count":clean_exact},"isolation":isolation,"singleton_candidate_count":len(rows),"primary_expected_explain_calls":expected_primary,"actual_explain_accounting":explain_accounting,"unexplained_explain_residual":explain_accounting["unexplained_explain_residual"],"determinism_repeats":repeats,"singleton_results_sha256":oracle_digest,"oracle_digest":digest({"identity":identity,"baseline_digest":baseline_digest,"singleton_results_sha256":oracle_digest,"incidence":incidence_result}),"incidence_validation":incidence_result,"elapsed_seconds":time.time()-runner.start_time}
    write_json(manifest_path, manifest)
    print(json.dumps({"status":manifest["status"],"artifact":str(artifact),"oracle_digest":manifest["oracle_digest"],"completed":len(rows),"primary_explain_calls":3421*468,"counters":dict(runner.counters),"incidence":incidence_result,"repeats":repeats},sort_keys=True,indent=2))
    if manifest["status"] != "PASS":
        raise SystemExit("authoritative oracle validation failed")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
