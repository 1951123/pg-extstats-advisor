#!/usr/bin/env python3
"""M2.27b: compare native and reservoir global-target decision structure.

This consumes only the frozen M2.26 native artifacts and M2.27a Bundle v2
cache.  It performs ordinary-statistics replay on the isolated advisor cluster
and never creates extstats or starts any search.
"""

from __future__ import annotations

import argparse
import csv
import hashlib
import json
import math
import statistics
import sys
from pathlib import Path
from typing import Any

import psycopg

ROOT = Path(__file__).resolve().parents[1]
if str(ROOT / "src") not in sys.path:
    sys.path.insert(0, str(ROOT / "src"))

from pg_extstats_advisor.capture.bundle import decode_sample
from pg_extstats_advisor.capture.bundle_v2 import verify_bundle_v2
from pg_extstats_advisor.capture.fidelity import (
    adjacent_quality,
    adjacent_stability,
    aggregate_qerror,
    gate,
    knee_structure,
    per_query_diagnostics,
    spearman,
    target_summary,
)
from pg_extstats_advisor.capture.manifest import canonical_digest
from pg_extstats_advisor.postgres.extraction import extract_target_estimate
from pg_extstats_advisor.prepare.workload import RelationMetadata
from pg_extstats_advisor.sql.analysis import analyze_query

OUT = ROOT / "experiments/dmv-m2-27b-target-selection-fidelity"
M26 = ROOT / "experiments/dmv-m2-26-statistics-target-stability"
M27A = ROOT / "experiments/dmv-m2-27a-reservoir-target-characterization"
BUNDLE = ROOT / ".build/production-captures/dmv-m2-27a-v2"
ADVISOR_DSN = "host=/root/projects/pg-extstats-advisor/.build/pg16.14-advisor-socket port=55438 dbname=postgres user=postgres"
PREPARED = ROOT / "experiments/dmv-m2-15-singletons/prepared-run/workload.json"
SOURCE_ROWS = 11_591_877
WORKLOAD_SIZE = 1963
TARGETS = (100, 300, 1000)
COLUMNS = ("record_type", "registration_class", "state", "county", "body_type", "fuel_type", "reg_valid_date", "color", "scofflaw_indicator", "suspension_indicator", "revocation_indicator")


def digest(value: Any) -> str:
    return hashlib.sha256(json.dumps(value, sort_keys=True, separators=(",", ":"), default=str).encode()).hexdigest()


def write_json(path: Path, value: Any) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text(json.dumps(value, sort_keys=True, indent=2, default=str) + "\n")


def load_json(path: Path) -> Any:
    return json.loads(path.read_text(encoding="utf-8"))


def qerror(estimate: float, truth: float) -> float:
    estimate = max(float(estimate), 1.0)
    return max(estimate / float(truth), float(truth) / estimate)


def vector_digests(vector: list[dict[str, Any]]) -> dict[str, str]:
    return {
        "objective_digest": digest({"aggregate_qerror": aggregate_qerror(item["q_error"] for item in vector)}),
        "estimate_vector_digest": digest([{"query_id": item["query_id"], "estimate": item["estimate"]} for item in vector]),
        "qerror_vector_digest": digest([{"query_id": item["query_id"], "q_error": item["q_error"]} for item in vector]),
    }


def native_records() -> dict[str, dict[str, Any]]:
    raw = load_json(M26 / "baseline-stability.json")["samples"]
    ordinary_records = {item["sample_name"]: item for item in load_json(M26 / "ordinary-stats-stability.json")["samples"]}
    manifests = {item["sample_name"]: item for item in (load_json(M26 / "samples.json")["samples"])}
    result: dict[str, dict[str, Any]] = {}
    for sample in raw:
        vector = [{"query_id": item["query_id"], "estimate": float(item["estimate"]), "truth": float(item["truth"]), "q_error": float(item["contribution"])} for item in sample["vector"]]
        aggregate = aggregate_qerror(item["q_error"] for item in vector)
        if not math.isclose(aggregate, float(sample["objective"]), rel_tol=0.0, abs_tol=1e-8):
            raise RuntimeError(f"M2.26 objective is not aggregate q-error: {sample['sample_name']}")
        manifest = manifests[sample["sample_name"]]
        ordinary = ordinary_records[sample["sample_name"]]
        semantic = [{"column": col["attname"], "null_frac": col["nullfrac"], "n_distinct": col["distinct"], "mcv_count": col["mcv_count"], "histogram_count": col["hist_count"], "correlation": None, "mcv_values_digest": col["values_digest"], "mcv_frequencies_digest": col["numbers_digest"]} for col in ordinary["columns"]]
        result[sample["sample_name"]] = {"mechanism": "native", "target": int(sample["target"]), "realization": sample["sample_name"].split("-")[1], "vector": vector, "aggregate_objective": aggregate, "mean_qerror": aggregate / WORKLOAD_SIZE, "sample_rows": 300 * int(sample["target"]), "source_population": SOURCE_ROWS, "statistics_digest": ordinary["digest"], "ordinary_semantic": semantic, "sample_semantic_digest": manifest["sample_semantic_digest"], "sample_binary_digest": manifest["sample_sha256"], "snapshot_identity": "m2-26-native-replay-lineage", "acquisition_method": "native_postgresql_analyze", "native_analyze_equivalent": True, **vector_digests(vector)}
    if len(result) != 9:
        raise RuntimeError("M2.26 native baseline does not contain all nine realizations")
    return result


def ordinary_semantic(c: psycopg.Connection[Any]) -> list[dict[str, Any]]:
    rows = c.execute("SELECT attname,null_frac,n_distinct,cardinality(most_common_vals),cardinality(histogram_bounds),correlation,most_common_vals::text,most_common_freqs::text,histogram_bounds::text FROM pg_stats WHERE schemaname='public' AND tablename='dmv' ORDER BY attname").fetchall()
    return [{"column": str(row[0]), "null_frac": row[1], "n_distinct": row[2], "mcv_count": row[3], "histogram_count": row[4], "correlation": row[5], "mcv_values_digest": digest(row[6]), "mcv_frequencies_digest": digest(row[7]), "histogram_digest": digest(row[8])} for row in rows]


def replay_reservoir(cell: dict[str, Any], queries: list[dict[str, Any]]) -> dict[str, Any]:
    target = int(cell["statistics_target"])
    sample_path = ROOT / ".build/artifact-cache" / cell["artifact_cache_relative"] / "sample.copy.bin"
    rows = decode_sample(sample_path, int(cell["sample_row_count"]), len(COLUMNS))
    c = psycopg.connect(ADVISOR_DSN, autocommit=False)
    try:
        c.execute("DROP TABLE IF EXISTS public.dmv")
        c.execute("CREATE UNLOGGED TABLE public.dmv (" + ",".join(f'"{name}" text' for name in COLUMNS) + ")")
        with c.cursor().copy("COPY public.dmv FROM STDIN") as copy:
            for row in rows:
                copy.write_row(tuple(row))
        c.execute("SELECT set_config('default_statistics_target', %s, false)", (str(target),))
        c.execute("SELECT set_config('pg_extstats.frozen_sample_mode', 'replay', false)")
        c.execute("SELECT set_config('pg_extstats.frozen_sample_relation', 'public.dmv', false)")
        c.execute("SELECT set_config('pg_extstats.frozen_totalrows', %s, false)", (str(SOURCE_ROWS),))
        c.execute("ANALYZE public.dmv")
        vector = []
        for item in queries:
            plan = c.execute(f"EXPLAIN (FORMAT JSON) {item['sql']}").fetchone()[0]
            estimate = extract_target_estimate(plan, "dmv")
            vector.append({"query_id": item["query_id"], "estimate": float(estimate), "truth": float(item["truth"]), "q_error": qerror(estimate, float(item["truth"]))})
        ext_count = int(c.execute("SELECT count(*) FROM pg_statistic_ext WHERE stxrelid='public.dmv'::regclass").fetchone()[0])
        active = c.execute("SELECT pg_hypothetical_extstats_active()").fetchone()[0]
        if ext_count != 0 or active not in (None, [], ""):
            raise RuntimeError("reservoir replay has extstats contamination")
        aggregate = aggregate_qerror(item["q_error"] for item in vector)
        semantic = ordinary_semantic(c)
        result = {"mechanism": "reservoir", "target": target, "realization": str(cell["realization_id"]), "vector": vector, "aggregate_objective": aggregate, "mean_qerror": aggregate / WORKLOAD_SIZE, "sample_rows": len(rows), "source_population": SOURCE_ROWS, "statistics_digest": digest(semantic), "ordinary_semantic": semantic, "sample_semantic_digest": cell["semantic_sha256"], "sample_binary_digest": cell["binary_sha256"], "snapshot_identity": cell["snapshot_identity"], "acquisition_method": "deterministic_reservoir_v1", "native_analyze_equivalent": False, "extstats_count": ext_count, "hypothetical_active": active in (None, [], ""), **vector_digests(vector)}
        c.commit()
        return result
    finally:
        c.close()


def semantic_workload(raw: dict[str, Any]) -> list[dict[str, Any]]:
    return sorted([{"query_id": str(item["query_id"]), "sql": str(item["sql"]), "truth": float(item["truth"]), "target_relation": str(item["target_relation"]).replace("public.", "")} for item in raw["queries"]], key=lambda item: item["query_id"])


def audit_inputs() -> dict[str, Any]:
    protocol = load_json(M26 / "protocol.json")
    bundle = verify_bundle_v2(BUNDLE)
    native_workload = load_json(PREPARED)
    reservoir_workload = load_json(BUNDLE / "workload.json")
    native_queries = semantic_workload(native_workload)
    reservoir_queries = semantic_workload(reservoir_workload)
    if native_queries != reservoir_queries:
        raise RuntimeError("native and reservoir workload/truth populations differ")
    native_truth = [(item["query_id"], item["truth"]) for item in native_queries]
    truth_raw = load_json(BUNDLE / "truth.json")["queries"]
    reservoir_truth = sorted((str(item["query_id"]), float(item["truth"])) for item in truth_raw if float(item["truth"]) > 0)
    if native_truth != reservoir_truth:
        raise RuntimeError("native and reservoir exact truth values differ")
    if int(protocol["source_rows"]) != SOURCE_ROWS or int(load_json(BUNDLE / "environment.json")["source_population_rows"]) != SOURCE_ROWS:
        raise RuntimeError("source population mismatch")
    return {"source_population_rows": SOURCE_ROWS, "workload_size": len(native_queries), "native_workload_digest": protocol["effective_workload_digest"], "reservoir_workload_digest": load_json(BUNDLE / "workload.json")["effective_workload_digest"], "semantic_workload_digest": canonical_digest(native_queries), "semantic_workload_equal": native_queries == reservoir_queries, "truth_semantic_digest": canonical_digest(native_truth), "truth_semantic_equal": native_truth == reservoir_truth, "native_protocol": {"effective_workload_digest": protocol["effective_workload_digest"], "build_recipe_digest": protocol["postgres_build_recipe_digest"], "patch_sha256": protocol["patch_sha256"], "sampling_method": protocol["sampling_method"], "ordinary_only_baseline": True, "physical_extstats_objects": 72, "hypothetical_active_design": "empty during baseline evaluator"}, "reservoir_bundle": {"verification": bundle, "native_analyze_equivalent": False, "ordinary_only": True, "extstats_contamination": False}}


def summarize_side(records: dict[str, dict[str, Any]]) -> dict[str, Any]:
    out: dict[str, Any] = {}
    for target in TARGETS:
        values = [records[f"T{target}-{realization}"]["aggregate_objective"] for realization in ("A", "B", "C")]
        out[str(target)] = target_summary(values)
    out["quality_ordering"] = sorted(TARGETS, key=lambda target: out[str(target)]["mean_aggregate_qerror"])
    first = out["100"]; middle = out["300"]; last = out["1000"]
    quality_gains = [adjacent_quality(first, middle), adjacent_quality(middle, last)]
    stability_gains = [adjacent_stability(first, middle), adjacent_stability(middle, last)]
    out["quality_gains"] = quality_gains
    out["stability_gains"] = stability_gains
    out["quality_gain_ordering"] = ["first", "second"] if quality_gains[0] > quality_gains[1] else ["second", "first"]
    out["normalized_range_trend"] = ["improve" if value is not None and value > 0 else "worsen" if value is not None and value < 0 else "flat" for value in stability_gains]
    cv = [first["cv"], middle["cv"], last["cv"]]
    out["cv_trend"] = ["improve" if cv[1] < cv[0] else "worsen", "improve" if cv[2] < cv[1] else "worsen"]
    out["stability_ordering_normalized_range"] = sorted(TARGETS, key=lambda target: out[str(target)]["normalized_range"])
    out["stability_ordering_cv"] = sorted(TARGETS, key=lambda target: out[str(target)]["cv"])
    out["knee"] = knee_structure(quality_gains, stability_gains)
    return out


def contributor_columns(queries: dict[str, dict[str, Any]]) -> dict[str, list[str]]:
    metadata = RelationMetadata("public", "dmv", 0, tuple((index, name, "text", False) for index, name in enumerate(COLUMNS, 1)))
    result = {}
    for query_id, item in queries.items():
        try:
            result[query_id] = sorted(analyze_query(item["sql"], "public.dmv", metadata).predicate_columns)
        except ValueError:
            result[query_id] = []
    return result


def top_contributors(records: dict[str, dict[str, Any]], queries: dict[str, dict[str, Any]], columns: dict[str, list[str]], left: int = 300, right: int = 1000) -> list[dict[str, Any]]:
    old = records[f"T{left}-A"]["vector"]; new = records[f"T{right}-A"]["vector"]
    new_by = {item["query_id"]: item for item in new}
    rows = []
    for item in old:
        after = new_by[item["query_id"]]
        rows.append({"query_id": item["query_id"], "before_qerror": item["q_error"], "after_qerror": after["q_error"], "qerror_reduction": item["q_error"] - after["q_error"], "columns": columns.get(item["query_id"], []), "sql": queries[item["query_id"]]["sql"]})
    return sorted(rows, key=lambda item: (-item["qerror_reduction"], item["query_id"]))[:20]


def extreme_decomposition(records: dict[str, dict[str, Any]]) -> dict[str, Any]:
    result = {}
    for name, item in records.items():
        ascending = sorted(float(row["q_error"]) for row in item["vector"])
        descending = list(reversed(ascending))
        total = math.fsum(ascending)
        result[name] = {"median": statistics.median(ascending), "p90": ascending[max(0, math.ceil(0.90 * len(ascending)) - 1)], "p95": ascending[max(0, math.ceil(0.95 * len(ascending)) - 1)], "p99": ascending[max(0, math.ceil(0.99 * len(ascending)) - 1)], "max": descending[0], "top_contribution_shares": {str(k): math.fsum(descending[:k]) / total for k in (1, 10, 50)}}
    return result


def run() -> None:
    audit = audit_inputs()
    native = native_records()
    workload = load_json(PREPARED)["queries"]
    queries = {str(item["query_id"]): item for item in workload}
    columns = contributor_columns(queries)
    cells = load_json(BUNDLE / "acquisition.json")["cells"]
    reservoir = {}
    for cell in cells:
        result = replay_reservoir(cell, workload)
        reservoir[f"T{result['target']}-{result['realization']}"] = result
    native_summary = summarize_side(native)
    reservoir_summary = summarize_side(reservoir)
    comparison_rows = []
    for mechanism, records in (("native", native), ("reservoir", reservoir)):
        for key, item in sorted(records.items()):
            comparison_rows.append({"mechanism": mechanism, "target": item["target"], "realization": item["realization"], "aggregate_objective": item["aggregate_objective"], "mean_qerror": item["mean_qerror"], "objective_digest": item["objective_digest"], "estimate_vector_digest": item["estimate_vector_digest"], "qerror_vector_digest": item["qerror_vector_digest"], "sample_rows": item["sample_rows"], "source_population": item["source_population"], "statistics_digest": item["statistics_digest"], "sample_semantic_digest": item["sample_semantic_digest"], "sample_binary_digest": item["sample_binary_digest"], "snapshot_identity": item["snapshot_identity"], "acquisition_method": item["acquisition_method"], "native_analyze_equivalent": item["native_analyze_equivalent"]})
    marginal_rows = []
    for mechanism, summary in (("native", native_summary), ("reservoir", reservoir_summary)):
        for left, right in ((100, 300), (300, 1000)):
            marginal_rows.append({"mechanism": mechanism, "from_target": left, "to_target": right, "quality_gain": adjacent_quality(summary[str(left)], summary[str(right)]), "stability_gain": adjacent_stability(summary[str(left)], summary[str(right)]), "from_cv": summary[str(left)]["cv"], "to_cv": summary[str(right)]["cv"]})
    canonical_rows = []
    canonical_diag = {}
    for target in TARGETS:
        n = native[f"T{target}-A"]; r = reservoir[f"T{target}-A"]
        relative_gap = (r["aggregate_objective"] - n["aggregate_objective"]) / n["aggregate_objective"]
        diag = per_query_diagnostics(n["vector"], r["vector"])
        native_stats = {item["column"]: item for item in n["ordinary_semantic"]}
        reservoir_stats = {item["column"]: item for item in r["ordinary_semantic"]}
        changed_columns = [column for column in sorted(native_stats) if native_stats[column] != reservoir_stats.get(column)]
        diag.update({"target": target, "native_aggregate": n["aggregate_objective"], "reservoir_aggregate": r["aggregate_objective"], "relative_aggregate_gap": relative_gap, "native_statistics_digest": n["statistics_digest"], "reservoir_statistics_digest": r["statistics_digest"], "ordinary_statistics_changed_column_count": len(changed_columns), "ordinary_statistics_changed_columns": changed_columns, "comparison_type": "canonical-A diagnostic, not paired-sample inference"})
        canonical_diag[str(target)] = diag
        canonical_rows.append(diag)
    native_gate = {"quality_ordering": native_summary["quality_ordering"], "quality_gain_ordering": native_summary["quality_gain_ordering"], "normalized_range_trend": native_summary["normalized_range_trend"], "cv_trend": native_summary["cv_trend"], "knee": native_summary["knee"]}
    reservoir_gate = {"quality_ordering": reservoir_summary["quality_ordering"], "quality_gain_ordering": reservoir_summary["quality_gain_ordering"], "normalized_range_trend": reservoir_summary["normalized_range_trend"], "cv_trend": reservoir_summary["cv_trend"], "knee": reservoir_summary["knee"]}
    gate_result = gate(native_gate, reservoir_gate)
    gate_result["native"] = native_gate; gate_result["reservoir"] = reservoir_gate
    top_rows = []
    for mechanism, records in (("native", native), ("reservoir", reservoir)):
        for row in top_contributors(records, queries, columns):
            top_rows.append({"mechanism": mechanism, **row})
    summary = {"milestone": "M2.27b", "status": "complete", "metric_semantics": {"canonical_metric": "aggregate_qerror_sum", "definition": "F(T,r)=sum over fixed positive-truth workload of q_error", "workload_size": WORKLOAD_SIZE, "mean_metric": "aggregate_qerror_sum / 1963", "m2_26_objective": "math.fsum of QueryEvaluation.contribution", "m2_27a_raw_objective": "statistics.fmean(q_error), a label/normalization bug corrected here; raw artifact preserved", "compute_bug": False}, "input_audit": audit, "native_target_summary": native_summary, "reservoir_target_summary": reservoir_summary, "quality_ordering": {"native": native_summary["quality_ordering"], "reservoir": reservoir_summary["quality_ordering"], "exact_agreement": native_summary["quality_ordering"] == reservoir_summary["quality_ordering"], "spearman_rho": spearman([native_summary[str(t)]["mean_aggregate_qerror"] for t in TARGETS], [reservoir_summary[str(t)]["mean_aggregate_qerror"] for t in TARGETS])}, "stability_ordering": {"normalized_range": {"native": native_summary["stability_ordering_normalized_range"], "reservoir": reservoir_summary["stability_ordering_normalized_range"], "agreement": native_summary["stability_ordering_normalized_range"] == reservoir_summary["stability_ordering_normalized_range"]}, "cv": {"native": native_summary["stability_ordering_cv"], "reservoir": reservoir_summary["stability_ordering_cv"], "agreement": native_summary["stability_ordering_cv"] == reservoir_summary["stability_ordering_cv"]}}, "marginal_quality": marginal_rows, "marginal_stability": marginal_rows, "knee_structure": {"native": native_summary["knee"], "reservoir": reservoir_summary["knee"], "agreement": native_summary["knee"]["pattern"] == reservoir_summary["knee"]["pattern"]}, "fidelity_gate": gate_result, "canonical_a": canonical_diag, "native_analyze_equivalent": False, "target_selection_fidelity_qualified": gate_result["target_selection_fidelity_qualified"], "extreme_query_decomposition": extreme_decomposition({**{f"native-{k}": v for k, v in native.items()}, **{f"reservoir-{k}": v for k, v in reservoir.items()}}), "no_cross_mechanism_pairing": True, "search_started": False, "production_capture_repeated": False}
    OUT.mkdir(parents=True, exist_ok=True)
    write_json(OUT / "summary.json", summary)
    write_json(OUT / "metric-semantics.json", summary["metric_semantics"])
    write_json(OUT / "canonical-diagnostics.json", canonical_diag)
    write_json(OUT / "extreme-query-decomposition.json", summary["extreme_query_decomposition"])
    with (OUT / "target-comparison.csv").open("w", newline="") as handle:
        writer = csv.DictWriter(handle, fieldnames=list(comparison_rows[0]), lineterminator="\n"); writer.writeheader(); writer.writerows(comparison_rows)
    with (OUT / "marginal-comparison.csv").open("w", newline="") as handle:
        writer = csv.DictWriter(handle, fieldnames=list(marginal_rows[0]), lineterminator="\n"); writer.writeheader(); writer.writerows(marginal_rows)
    with (OUT / "canonical-diagnostics.csv").open("w", newline="") as handle:
        fields = ["target", "native_aggregate", "reservoir_aggregate", "relative_aggregate_gap", "spearman_qerror", "median_absolute_log_qerror_difference", "p90_absolute_log_qerror_difference", "fraction_reservoir_qerror_greater", "fraction_reservoir_qerror_less", "native_statistics_digest", "reservoir_statistics_digest", "ordinary_statistics_changed_column_count", "ordinary_statistics_changed_columns", "comparison_type"]
        writer = csv.DictWriter(handle, fieldnames=fields, lineterminator="\n"); writer.writeheader(); writer.writerows({field: row.get(field) for field in fields} for row in canonical_rows)
    with (OUT / "top-contributors.csv").open("w", newline="") as handle:
        writer = csv.DictWriter(handle, fieldnames=["mechanism", "query_id", "before_qerror", "after_qerror", "qerror_reduction", "columns", "sql"], lineterminator="\n"); writer.writeheader(); writer.writerows(top_rows)
    (OUT / "README.md").write_text("# M2.27b: native-vs-reservoir target-selection fidelity\n\nThe canonical metric is aggregate q-error sum over the fixed 1,963-query positive-truth DMV workload. M2.26 native objectives already used this sum. M2.27a's historical `objective` field was a per-query mean; this milestone preserves that raw artifact and reports corrected aggregate values. Native and reservoir realization labels are mechanism-local and are not paired across mechanisms. `native_analyze_equivalent=false` remains mandatory: decision fidelity is not sampling equivalence. The conservative five-condition gate is workload-specific and does not produce a production target recommendation.\n")
    print(json.dumps({"status": "complete", "target_selection_fidelity_qualified": summary["target_selection_fidelity_qualified"], "native_ordering": summary["quality_ordering"]["native"], "reservoir_ordering": summary["quality_ordering"]["reservoir"]}, indent=2))


def main() -> int:
    parser = argparse.ArgumentParser(); parser.add_argument("command", choices=("run",))
    parser.parse_args(); run(); return 0


if __name__ == "__main__":
    raise SystemExit(main())
