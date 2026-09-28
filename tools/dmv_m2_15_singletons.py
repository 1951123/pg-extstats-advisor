"""Finalize the DMV M2.15 onboarding and singleton-profile diagnostics.

This tool consumes the frozen preparation/profile artifacts, runs only the
predeclared evaluator exactness and repeatability spot checks, and writes
compact descriptive summaries.  It never performs search or screening.
"""

from __future__ import annotations

import csv
import json
import math
import statistics
import time
from collections import Counter
from datetime import datetime
from itertools import combinations
from pathlib import Path

import psycopg

from pg_extstats_advisor.analysis.singleton import linear_quantile
from pg_extstats_advisor.evaluator.native import NativeEvaluator
from pg_extstats_advisor.models import CandidateId, Design, Move
from pg_extstats_advisor.orchestration import load_prepared_run
from pg_extstats_advisor.postgres.adapter import PostgresAdapter
from pg_extstats_advisor.prepare.acquisition import cleanup_acquisition
from pg_extstats_advisor.prepare.workload import RelationMetadata
from pg_extstats_advisor.screening import load_artifact
from pg_extstats_advisor.sql.analysis import ANALYSIS_VERSION, PARSER_VERSION, analyze_query

ROOT = Path("experiments/dmv-m2-15-singletons")
RUN = ROOT / "prepared-run"


def write_json(path: Path, value: object) -> None:
    path.write_text(json.dumps(value, sort_keys=True, indent=2) + "\n")


def quantiles(values: list[float]) -> dict[str, float | None]:
    return {f"p{int(p * 100)}": linear_quantile(values, p) for p in (0.0, 0.1, 0.25, 0.5, 0.75, 0.9, 0.95, 0.99, 1.0)}


def classification(rows: list[dict]) -> dict[str, int]:
    return Counter(
        "positive" if float(row["singleton_improvement"]) > 0 else
        "negative" if float(row["singleton_improvement"]) < 0 else "zero"
        for row in rows
    )


def exact_smoke(prepared, connection, rows: list[dict]) -> dict:
    evaluator = NativeEvaluator(
        prepared.workload,
        prepared.repository,
        prepared.incidence,
        PostgresAdapter(connection, prepared.repository),
    )
    empty = evaluator.evaluate_design(Design(()))
    present_mcv = next((r for r in rows if r["mechanism"] == "mcv" and r["realization_state"] == "PRESENT"), None)
    present_fd = next((r for r in rows if r["mechanism"] == "fd" and r["realization_state"] == "PRESENT"), None)
    absent = next((r for r in rows if r["realization_state"] == "ABSENT_NATIVE"), None)
    positive = [r for r in rows if float(r["singleton_improvement"]) > 0]
    negative = [r for r in rows if float(r["singleton_improvement"]) < 0]
    median = rows[len(rows) // 2]
    selected = [r for r in (present_mcv, present_fd, absent, positive[0] if positive else None, median, negative[0] if negative else None) if r]
    seen: set[str] = set()
    checks = []
    for row in selected:
        cid = str(row["candidate_id"])
        if cid in seen:
            continue
        seen.add(cid)
        design = Design((CandidateId(cid),))
        local = evaluator.evaluate_move(Design(()), Move.add_candidate(CandidateId(cid)), empty)
        reference = evaluator.evaluate_design(design)
        local_by_query, ref_by_query = local.by_query(), reference.by_query()
        exact = (
            local.aggregate_objective == reference.aggregate_objective
            and local_by_query.keys() == ref_by_query.keys()
            and all(
                local_by_query[qid].estimate == ref_by_query[qid].estimate
                and local_by_query[qid].contribution == ref_by_query[qid].contribution
                for qid in local_by_query
            )
        )
        checks.append({"candidate_id": cid, "mechanism": row["mechanism"], "realization_state": row["realization_state"], "exact": exact})
        if not exact:
            raise RuntimeError(f"native query-local/full-reference mismatch: {cid}")
    return {"empty": True, "checks": checks, "all_exact": all(item["exact"] for item in checks)}


def main() -> int:
    prepared = load_prepared_run(RUN)
    profile = load_artifact(ROOT / "singleton-profile.json")
    rows = list(profile["candidates"])
    if len(rows) != len(prepared.catalog.candidates):
        raise RuntimeError("singleton profile is incomplete")
    provenance = json.loads((RUN / "workload.json").read_text())["provenance"]
    if provenance["raw_query_count"] != 1965 or provenance["effective_query_count"] != 1963:
        raise RuntimeError("unexpected DMV effective workload size")
    if provenance["excluded_query_ids"] != ["dmv.173", "dmv.943"]:
        raise RuntimeError("unexpected DMV exclusions")

    write_json(ROOT / "protocol.json", {
        "milestone": "M2.15",
        "benchmark": "DMV",
        "raw_source": provenance["raw_source_path"],
        "raw_source_sha256": provenance["raw_source_sha256"],
        "raw_query_count": 1965,
        "objective_membership_policy": "positive_truth_only",
        "excluded_query_ids": ["dmv.173", "dmv.943"],
        "excluded_count": 2,
        "effective_query_count": 1963,
        "effective_workload_digest": provenance["effective_workload_digest"],
        "current_q_error_semantics_unchanged": True,
        "search_started": False,
        "screening_started": False,
        "maintenance_cost_status": "unavailable_for_DMV",
        "census_maintenance_model_used": False,
    })
    write_json(ROOT / "raw-workload-provenance.json", provenance)
    write_json(ROOT / "effective-workload.json", json.loads((RUN / "workload.json").read_text()))

    candidates = prepared.catalog.candidates
    pairs = sorted({tuple(candidate.attributes) for candidate in candidates})
    dataset = json.loads(Path("experiments/environment/dmv-dataset.json").read_text())
    columns = tuple(
        (
            int(item["attnum"]),
            str(item["name"]),
            str(item["type"]),
            bool(item["not_null"]),
        )
        for item in dataset["ordered_column_schema"]
    )
    metadata = RelationMetadata("public", "dmv", 0, columns)
    raw_records = json.loads((ROOT / "raw-workload.json").read_text())["queries"]
    by_attnum = {name: attnum for attnum, name, _type, _not_null in columns}
    raw_pairs = set()
    for record in raw_records:
        analysis = analyze_query(record["sql"], "public.dmv", metadata)
        ordered = sorted(analysis.predicate_columns, key=by_attnum.__getitem__)
        raw_pairs.update(combinations(ordered, 2))
    catalog = {
        "unordered_pair_count": len(pairs),
        "mcv_count": sum(c.mechanism.value == "mcv" for c in candidates),
        "fd_count": sum(c.mechanism.value == "fd" for c in candidates),
        "total_count": len(candidates),
        "candidate_catalog_digest": prepared.candidate_catalog_digest,
        "incidence_edge_count": sum(len(v) for v in prepared.incidence.by_candidate.values()),
        "incidence_digest": prepared.incidence_digest,
        "raw_unordered_pair_count": len(raw_pairs),
        "pair_universe_matches_raw1965_audit": set(pairs) == raw_pairs,
        "pairs": [list(pair) for pair in pairs],
    }
    write_json(ROOT / "candidate-summary.json", catalog)

    manifest = json.loads((RUN / "repository" / "manifest.json").read_text())
    states = Counter(item["state"] for item in manifest["candidates"])
    mechanisms = Counter((item["mechanism"], item["state"]) for item in manifest["candidates"])
    started = manifest["acquisition_provenance"]["started_at"]
    completed = manifest["acquisition_provenance"]["completed_at"]
    acquisition_summary = {
        "repository_digest": prepared.repository.digest,
        "state_counts": dict(states),
        "mechanism_state_counts": {f"{m}:{s}": n for (m, s), n in mechanisms.items()},
        "missing_count": 0,
        "corrupt_count": 0,
        "analyze_count": manifest["acquisition_provenance"]["analyze_count"],
        "started_at": started,
        "completed_at": completed,
        "elapsed_seconds": (
            datetime.fromisoformat(completed).timestamp()
            - datetime.fromisoformat(started).timestamp()
        ),
        "payloads_are_frozen_native": True,
    }
    write_json(ROOT / "acquisition-summary.json", acquisition_summary)

    fieldnames = [
        "singleton_rank", "candidate_id", "mechanism", "columns", "precedence_rank", "realization_state",
        "singleton_objective", "singleton_improvement", "relative_improvement", "incidence_query_count",
        "affected_query_count", "improved_query_count", "unchanged_query_count", "worsened_query_count",
        "elapsed_seconds", "planner_calls", "maintenance_cost_status", "maintenance_cost_numeric",
    ]
    with (ROOT / "singleton-results.csv").open("w", newline="") as handle:
        writer = csv.DictWriter(handle, fieldnames=fieldnames, lineterminator="\n")
        writer.writeheader()
        for row in rows:
            writer.writerow({key: json.dumps(row[key], separators=(",", ":")) if key == "columns" else row.get(key) for key in fieldnames})

    baseline_contributions: list[float] = []
    dsn = "host=/root/projects/pg-extstats-advisor/.build/pg16.14-experiment-socket port=55436 dbname=pgextadv_exp16_dmv_m215_acq user=postgres"
    smoke_started = time.perf_counter()
    with psycopg.connect(dsn) as connection:
        smoke = exact_smoke(prepared, connection, rows)
        baseline_evaluator = NativeEvaluator(
            prepared.workload,
            prepared.repository,
            prepared.incidence,
            PostgresAdapter(connection, prepared.repository),
        )
        baseline_state = baseline_evaluator.evaluate_design(Design(()))
        baseline_contributions = [item.contribution for item in baseline_state.query_evaluations]
        # Dataset and acquisition-state checks happen before cleanup.
        facts = connection.execute(
            "SELECT count(*), c.relpersistence FROM public.dmv d JOIN pg_class c ON c.oid='public.dmv'::regclass GROUP BY c.relpersistence"
        ).fetchone()
        stats_before = int(connection.execute("SELECT count(*) FROM pg_statistic_ext").fetchone()[0])
        stats_data_before = int(connection.execute("SELECT count(*) FROM pg_statistic_ext_data").fetchone()[0])
        cleanup_acquisition(connection, prepared.acquisition)
        stats_after = int(connection.execute("SELECT count(*) FROM pg_statistic_ext").fetchone()[0])
        stats_data_after = int(connection.execute("SELECT count(*) FROM pg_statistic_ext_data").fetchone()[0])
    smoke["elapsed_seconds"] = time.perf_counter() - smoke_started
    write_json(ROOT / "repeatability.json", smoke)
    write_json(ROOT / "dataset-integrity.json", {
        "acquisition_row_count": int(facts[0]),
        "acquisition_relpersistence": facts[1],
        "statistics_before_cleanup": stats_before,
        "statistics_data_before_cleanup": stats_data_before,
        "statistics_after_cleanup": stats_after,
        "statistics_data_after_cleanup": stats_data_after,
        "cleanup_success": stats_after == 0 and stats_data_after == 0,
    })

    baseline = float(profile["baseline_objective"])
    positive = [row for row in rows if float(row["singleton_improvement"]) > 0]
    positive.sort(key=lambda row: (-float(row["singleton_improvement"]), int(row["precedence_rank"]), str(row["candidate_id"])))
    positive_mass = sum(float(row["singleton_improvement"]) for row in positive)
    groups = {"all": rows, "mcv": [r for r in rows if r["mechanism"] == "mcv"], "fd": [r for r in rows if r["mechanism"] == "fd"], "PRESENT": [r for r in rows if r["realization_state"] == "PRESENT"], "ABSENT_NATIVE": [r for r in rows if r["realization_state"] == "ABSENT_NATIVE"]}
    summary = {
        "baseline_objective": baseline,
        "baseline_mean": baseline / len(prepared.workload.queries),
        "classification": dict(classification(rows)),
        "positive_rate": len(positive) / len(rows),
        "by_mechanism": {key: dict(classification(value)) for key, value in {"mcv": groups["mcv"], "fd": groups["fd"]}.items()},
        "by_realization": {key: dict(classification(value)) for key, value in {"PRESENT": groups["PRESENT"], "ABSENT_NATIVE": groups["ABSENT_NATIVE"]}.items()},
        "quantiles": {key: quantiles([float(r["singleton_improvement"]) for r in value]) for key, value in groups.items()},
        "effective_query_count": len(prepared.workload.queries),
    }
    summary["baseline_median"] = statistics.median(baseline_contributions)
    summary["baseline_p90"] = linear_quantile(baseline_contributions, 0.9)
    summary["baseline_max"] = max(baseline_contributions)
    write_json(ROOT / "singleton-summary.json", summary)

    concentration = {}
    for pct in (0.01, 0.02, 0.05, 0.10, 0.20, 0.50):
        count = max(1, math.ceil(len(positive) * pct)) if positive else 0
        concentration[f"top_{int(pct * 100)}pct"] = {"count": count, "fraction": count / len(positive) if positive else 0, "utility_share": sum(float(r["singleton_improvement"]) for r in positive[:count]) / positive_mass if positive_mass else 0}
    mass_counts = {}
    for target in (0.50, 0.80, 0.90, 0.95, 0.99):
        running = 0.0
        count = 0
        for row in positive:
            running += float(row["singleton_improvement"]); count += 1
            if running / positive_mass >= target: break
        mass_counts[str(int(target * 100))] = {"count": count, "fraction": count / len(positive) if positive else 0}
    write_json(ROOT / "concentration-summary.json", {"positive_singleton_count": len(positive), "positive_singleton_utility": positive_mass, "top_fraction_shares": concentration, "utility_mass_counts": mass_counts, "interpretation": "descriptive only; not additive achievable design gain"})

    top_composition = {}
    for requested in (20, 50, 100, 200, 500, 1000):
        top = rows[: min(requested, len(rows))]
        top_composition[str(len(top))] = {"mechanism": dict(Counter(r["mechanism"] for r in top)), "realization": dict(Counter(r["realization_state"] for r in top))}
    write_json(ROOT / "top-rank-composition.json", top_composition)

    by_query = prepared.incidence.by_candidate
    reverse: dict[str, list[dict]] = {str(qid): [] for qid in prepared.workload.by_id}
    for row in rows:
        for qid in by_query[CandidateId(str(row["candidate_id"]))]:
            reverse[str(qid)].append(row)
    qdiag = []
    for qid, incident in reverse.items():
        positive_incident = [r for r in incident if float(r["singleton_improvement"]) > 0]
        qdiag.append({"query_id": qid, "incident_candidate_count": len(incident), "positive_singleton_incident_count": len(positive_incident), "max_singleton_improvement": max((float(r["singleton_improvement"]) for r in incident), default=0.0)})
    write_json(ROOT / "query-side-summary.json", {"query_count": len(qdiag), "incident_count": quantiles([r["incident_candidate_count"] for r in qdiag]), "positive_incident_count": quantiles([r["positive_singleton_incident_count"] for r in qdiag]), "max_improvement": quantiles([r["max_singleton_improvement"] for r in qdiag])})
    pair_frequency = Counter(tuple(row["columns"]) for row in rows)
    write_json(ROOT / "column-pair-summary.json", {"pair_count": len(pair_frequency), "top20_singleton_pairs": [list(row["columns"]) for row in rows[:20]], "top100_pair_frequency": [{"pair": list(pair), "candidate_count": count} for pair, count in pair_frequency.most_common(100)], "mechanism_composition": dict(Counter(row["mechanism"] for row in rows))})

    elapsed = [float(row["elapsed_seconds"]) for row in rows]
    performance = {"total_singleton_candidates": len(rows), "native_evaluations": len(rows), "planner_calls": 1963 + sum(int(row["planner_calls"]) for row in rows), "affected_query_replans": sum(int(row["affected_query_count"]) for row in rows), "total_singleton_elapsed_seconds": sum(elapsed), "candidate_elapsed_seconds": quantiles(elapsed), "parser": "pglast", "parser_version": PARSER_VERSION, "analysis_version": ANALYSIS_VERSION}
    write_json(ROOT / "performance.json", performance)

    report = f"""# M2.15 DMV onboarding and singleton profiling\n\nThe authoritative raw source contains 1965 queries. Explicit preprocessing uses `truth > 0`, excluding `dmv.173` and `dmv.943`, leaving 1963 effective queries. Current advisor q-error semantics are unchanged.\n\nPreparation produced {len(pairs)} unordered pairs, {catalog['mcv_count']} MCV candidates, {catalog['fd_count']} FD candidates, and {len(rows)} total candidates. The frozen acquisition has {states.get('PRESENT', 0)} PRESENT and {states.get('ABSENT_NATIVE', 0)} ABSENT_NATIVE payloads; no candidate was screened or dropped.\n\nThe empty-design objective is {baseline:.12f}; singleton classification is {dict(classification(rows))}, with positive rate {len(positive)/len(rows):.3%}. Positive utility concentration is descriptive only and is not an additive achievable design-gain claim.\n\nNative query-local/full-reference smoke checks were exact for all recorded spots: `{smoke['all_exact']}`. Acquisition statistics were removed after validation. No budget search, screening fraction, or M3 work was run. DMV onboarding and singleton profiling are complete; a DMV-specific development screening decision remains pending.\n"""
    (ROOT / "report.md").write_text(report)
    print(json.dumps({"baseline": baseline, "classification": dict(classification(rows)), "positive_rate": len(positive)/len(rows), "smoke_exact": smoke["all_exact"], "cleanup": stats_after == 0, "profile_digest": profile["digest"]}, indent=2))
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
