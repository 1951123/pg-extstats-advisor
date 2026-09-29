"""Measure fixed-design CE robustness across independent native ANALYZE samples."""

from __future__ import annotations

import csv
import hashlib
import json
import statistics
import subprocess
import sys
import time
from pathlib import Path
from typing import Any

import psycopg

from pg_extstats_advisor.models import (
    CandidateId,
    Design,
    EvaluationState,
    QueryEvaluation,
    QueryId,
)
from pg_extstats_advisor.objective.qerror import aggregate_objective, q_error
from pg_extstats_advisor.orchestration import load_prepared_run
from pg_extstats_advisor.payloads.repository import NativePayloadState, PayloadRepository
from pg_extstats_advisor.postgres.extraction import extract_target_estimate

ROOT = Path(__file__).resolve().parents[1]
PREPARED_ROOT = ROOT / "experiments/dmv-m2-15-singletons/prepared-run"
REPOSITORY_ROOT = ROOT / "experiments/dmv-m2-17b-frozen-acquisition-sample/build-1/repository"
SEARCH_ROOT = ROOT / "experiments/dmv-m2-17d-frozen-full72-add"
M218_ROOT = ROOT / "experiments/dmv-m2-18-frozen-hyp-vs-physical"
OUT = ROOT / "experiments/dmv-m2-19-fixed-design-fresh-sample-robustness"
DSN = f"host={ROOT}/.build/pg16.14-experiment-socket port=55436 dbname=pgextadv_exp16_dmv user=postgres"
TARGET = "public.dmv"
EXPECTED_ROWS = 11_591_877
EXPECTED_DESIGN_DIGEST = "c196630393e536612b600cd1abbabf025961ed1e3c977477d0c0e0a8f0ef99a9"
EXPECTED_REPOSITORY = "0e928015a57a20624775c355e6dbb08e56cedd4437f5f54a7d0801c5aa7c6807"
EXPECTED_WORKLOAD = "0e8ef80c0da0d6a9810b2e8f8910034f8a743804a3859b2c9b61d461b8d0bf4c"
EXPECTED_EFFECTIVE_WORKLOAD = "e790933cfc4f0f42d92807170b76cec080621c5b463dab83e23474a0a51151d8"
EXPECTED_SAMPLE_A_BASELINE = 42791.986480127205
EXPECTED_SAMPLE_A_DESIGN = 22014.061316846422
EXPECTED_SAMPLE_A_VECTOR = "24cc3f4f9cc2e4987d877e4bb7874f458cf84a9aa781733adb5d3a29189551a4"
EXPECTED_SAMPLE_A_BASELINE_VECTOR = "f3254350d068cbd625fffd5537cd25573961da71779977e73811c1d6acb4a93f"
EXPECTED_PATCH = "22c7f48632585e81fd8a557dc8bffba873ac5da070aca31713e22c60261c3b4f"
UPSTREAM_SHA = "f6d077142737920858ce958ccdb75c6ee137a63b5b0853c70693d401ac7e3471"
RUN_COUNT = 10


def digest(value: Any) -> str:
    return hashlib.sha256(
        json.dumps(value, sort_keys=True, separators=(",", ":"), default=str).encode()
    ).hexdigest()


def git_head() -> str:
    return subprocess.run(
        ["git", "rev-parse", "HEAD"], cwd=ROOT, check=True, capture_output=True, text=True
    ).stdout.strip()


def quote_ident(value: str) -> str:
    return '"' + value.replace('"', '""') + '"'


def design_digest(candidates: tuple[Any, ...]) -> str:
    return digest([
        {"candidate_id": str(c.candidate_id), "relation_name": c.relation_name,
         "mechanism": c.mechanism.value, "attributes": list(c.attributes),
         "precedence_rank": c.precedence_rank}
        for c in candidates
    ])


def ordinary_stats_digest(conn: psycopg.Connection[Any]) -> str:
    rows = conn.execute(
        "SELECT starelid::regclass::text,staattnum,stainherit,stanullfrac,stawidth,stadistinct,"
        "stakind1,stakind2,stakind3,stakind4,stakind5,"
        "staop1::oid::text,staop2::oid::text,staop3::oid::text,staop4::oid::text,staop5::oid::text,"
        "stacoll1::oid::text,stacoll2::oid::text,stacoll3::oid::text,stacoll4::oid::text,stacoll5::oid::text,"
        "stanumbers1::text,stanumbers2::text,stanumbers3::text,stanumbers4::text,stanumbers5::text,"
        "stavalues1::text,stavalues2::text,stavalues3::text,stavalues4::text,stavalues5::text "
        "FROM pg_statistic WHERE starelid='public.dmv'::regclass ORDER BY staattnum,stainherit"
    ).fetchall()
    keys = (
        "relation", "attnum", "inherit", "nullfrac", "width", "distinct", "kind1", "kind2",
        "kind3", "kind4", "kind5", "op1", "op2", "op3", "op4", "op5", "coll1", "coll2",
        "coll3", "coll4", "coll5", "numbers1", "numbers2", "numbers3", "numbers4", "numbers5",
        "values1", "values2", "values3", "values4", "values5",
    )
    return digest([dict(zip(keys, row, strict=True)) for row in rows])


def estimate_vector_digest(state: EvaluationState) -> str:
    return digest([
        {"query_id": str(item.query_id), "estimate": item.estimate, "truth": item.truth,
         "contribution": item.contribution, "provenance": item.provenance}
        for item in state.query_evaluations
    ])


def quantile(values: list[float], probability: float) -> float:
    ordered = sorted(values)
    position = (len(ordered) - 1) * probability
    lower = int(position)
    upper = min(lower + 1, len(ordered) - 1)
    fraction = position - lower
    return ordered[lower] + (ordered[upper] - ordered[lower]) * fraction


def distribution(state: EvaluationState) -> dict[str, float | int]:
    values = [float(item.contribution) for item in state.query_evaluations]
    return {
        "mean": statistics.fmean(values), "median": statistics.median(values),
        "p90": quantile(values, 0.9), "p99": quantile(values, 0.99),
        "max": max(values), "query_count": len(values),
    }


def set_native_mode(conn: psycopg.Connection[Any]) -> None:
    conn.execute("SELECT set_config(%s,%s,false)", ("pg_extstats.frozen_sample_mode", "off"))
    conn.execute("SELECT set_config(%s,%s,false)", ("pg_extstats.frozen_sample_relation", ""))
    conn.execute("SELECT set_config(%s,%s,false)", ("pg_extstats.frozen_totalrows", "0"))


def drop_definitions(conn: psycopg.Connection[Any], candidates: tuple[Any, ...]) -> None:
    for candidate in candidates:
        schema, _ = candidate.relation_name.split(".", 1)
        name = str(dict(candidate.definition)["statistics_name"])
        conn.execute(f"DROP STATISTICS IF EXISTS {quote_ident(schema)}.{quote_ident(name)}")


def create_definitions(conn: psycopg.Connection[Any], candidates: tuple[Any, ...]) -> None:
    for candidate in candidates:
        schema, relation = candidate.relation_name.split(".", 1)
        kind = "mcv" if candidate.mechanism.value == "mcv" else "dependencies"
        name = str(dict(candidate.definition)["statistics_name"])
        attrs = ", ".join(quote_ident(item) for item in candidate.attributes)
        qualified = f"{quote_ident(schema)}.{quote_ident(name)}"
        conn.execute(
            f"CREATE STATISTICS {qualified} ({kind}) ON {attrs} "
            f"FROM {quote_ident(schema)}.{quote_ident(relation)}"
        )
        conn.execute(f"ALTER STATISTICS {qualified} SET STATISTICS 100")


def stat_counts(conn: psycopg.Connection[Any]) -> tuple[int, int]:
    definitions = int(conn.execute(
        "SELECT count(*) FROM pg_statistic_ext WHERE stxname LIKE 'pgextadv_acq_%'"
    ).fetchone()[0])
    data = int(conn.execute(
        "SELECT count(*) FROM pg_statistic_ext_data d JOIN pg_statistic_ext e ON e.oid=d.stxoid "
        "WHERE e.stxname LIKE 'pgextadv_acq_%'"
    ).fetchone()[0])
    return definitions, data


def physical_payloads(conn: psycopg.Connection[Any], candidates: tuple[Any, ...]) -> dict[str, dict[str, Any]]:
    names = [str(dict(c.definition)["statistics_name"]) for c in candidates]
    rows = conn.execute(
        "SELECT e.stxname,e.oid,e.stxkind,d.stxoid IS NOT NULL,"
        "pg_mcv_list_send(d.stxdmcv),pg_dependencies_send(d.stxddependencies) "
        "FROM pg_statistic_ext e LEFT JOIN pg_statistic_ext_data d ON d.stxoid=e.oid "
        "WHERE e.stxname = ANY(%s)", (names,)
    ).fetchall()
    by_name = {str(row[0]): row for row in rows}
    output: dict[str, dict[str, Any]] = {}
    for candidate in candidates:
        name = str(dict(candidate.definition)["statistics_name"])
        row = by_name.get(name)
        if row is None:
            raise RuntimeError(f"missing physical statistic {name}")
        raw = row[4] if candidate.mechanism.value == "mcv" else row[5]
        payload = bytes(raw) if raw is not None else None
        output[str(candidate.candidate_id)] = {
            "statistics_name": name, "oid": int(row[1]), "data_row_exists": bool(row[3]),
            "state": NativePayloadState.PRESENT.value if payload else NativePayloadState.ABSENT_NATIVE.value,
            "payload_digest": hashlib.sha256(payload).hexdigest() if payload else None,
            "payload_bytes": len(payload) if payload else 0,
        }
    return output


def physical_state(conn: psycopg.Connection[Any], workload: Any, design: Design) -> EvaluationState:
    active = conn.execute("SELECT pg_hypothetical_extstats_active()").fetchone()[0]
    if active not in (None, [], ()):
        raise RuntimeError(f"hypothetical overlay unexpectedly active: {active}")
    version = str(conn.execute("SHOW server_version").fetchone()[0])
    evaluations = []
    for query in sorted(workload.queries, key=lambda item: item.query_id):
        row = conn.execute(f"EXPLAIN (FORMAT JSON) {query.sql}").fetchone()
        estimate = extract_target_estimate(row[0], query.target_relation)
        evaluations.append(QueryEvaluation(
            QueryId(str(query.query_id)), estimate, query.truth, q_error(estimate, query.truth),
            f"native-explain:{version}",
        ))
    return EvaluationState(
        design, tuple(evaluations), aggregate_objective(evaluations), EXPECTED_REPOSITORY,
        workload.digest, "16.14", "physical-native-explain", tuple(item.query_id for item in evaluations), (),
    )


def row_count(conn: psycopg.Connection[Any]) -> int:
    return int(conn.execute(f"SELECT count(*) FROM {TARGET}").fetchone()[0])


def schema_signature(conn: psycopg.Connection[Any]) -> str:
    rows = conn.execute(
        "SELECT attnum,attname,atttypid::regtype::text,attnotnull,attcollation::regcollation::text "
        "FROM pg_attribute WHERE attrelid='public.dmv'::regclass AND attnum > 0 AND NOT attisdropped "
        "ORDER BY attnum"
    ).fetchall()
    return digest({"relation": "public.dmv", "columns": [list(row) for row in rows]})


def run_one(prepared: Any, selected: tuple[Any, ...], run_id: str) -> dict[str, Any]:
    with psycopg.connect(DSN) as conn:
        conn.execute("SELECT pg_hypothetical_extstats_reset()")
        set_native_mode(conn)
        drop_definitions(conn, selected)
        conn.commit()
        before_rows = row_count(conn)
        if before_rows != EXPECTED_ROWS:
            raise RuntimeError(f"canonical DMV row count mismatch before {run_id}: {before_rows}")
        create_definitions(conn, selected)
        conn.commit()
        analyze_started = time.perf_counter()
        conn.execute(f"ANALYZE {TARGET}")
        conn.commit()
        analyze_seconds = time.perf_counter() - analyze_started
        base_digest = ordinary_stats_digest(conn)
        payloads = physical_payloads(conn, selected)
        if stat_counts(conn) != (len(selected), len(selected)):
            raise RuntimeError(f"native ANALYZE did not materialize all selected objects for {run_id}")
        states = {cid: item["state"] for cid, item in payloads.items()}
        payload_bundle = digest({cid: payloads[cid]["payload_digest"] for cid in sorted(payloads)})
        # DDL is transactional: dropping only extended statistics gives an
        # empty-design view while preserving the just-built ordinary stats.
        conn.execute("BEGIN")
        drop_definitions(conn, selected)
        empty_started = time.perf_counter()
        empty = physical_state(conn, prepared.workload, Design(()))
        empty_seconds = time.perf_counter() - empty_started
        empty_digest = ordinary_stats_digest(conn)
        conn.rollback()
        if empty_digest != base_digest:
            raise RuntimeError(f"ordinary statistics changed in empty view for {run_id}")
        design_started = time.perf_counter()
        design_state = physical_state(conn, prepared.workload, Design(tuple(c.candidate_id for c in selected)))
        evaluation_seconds = time.perf_counter() - design_started
        after_digest = ordinary_stats_digest(conn)
        after_rows = row_count(conn)
        if after_digest != base_digest or after_rows != EXPECTED_ROWS:
            raise RuntimeError(f"state changed between empty/design for {run_id}")
        conn.execute("SELECT pg_hypothetical_extstats_reset()")
        drop_definitions(conn, selected)
        set_native_mode(conn)
        conn.commit()
        residual = stat_counts(conn)
        if residual != (0, 0):
            raise RuntimeError(f"residual extstats after {run_id}: {residual}")
    empty_values = {str(item.query_id): item for item in empty.query_evaluations}
    design_values = {str(item.query_id): item for item in design_state.query_evaluations}
    deltas = [empty_values[qid].contribution - design_values[qid].contribution for qid in design_values]
    improved = sum(delta > 0 for delta in deltas)
    unchanged = sum(delta == 0 for delta in deltas)
    worsened = sum(delta < 0 for delta in deltas)
    return {
        "run_id": run_id, "ordinary_statistics_digest": base_digest,
        "payload_bundle_digest": payload_bundle, "payloads": payloads, "states": states,
        "present_count": sum(state == NativePayloadState.PRESENT.value for state in states.values()),
        "absent_count": sum(state == NativePayloadState.ABSENT_NATIVE.value for state in states.values()),
        "empty_objective": empty.aggregate_objective, "design_objective": design_state.aggregate_objective,
        "absolute_improvement": empty.aggregate_objective - design_state.aggregate_objective,
        "relative_improvement": (empty.aggregate_objective - design_state.aggregate_objective) / empty.aggregate_objective,
        "empty_distribution": distribution(empty), "design_distribution": distribution(design_state),
        "improved_queries": improved, "unchanged_queries": unchanged, "worsened_queries": worsened,
        "analyze_seconds": analyze_seconds, "empty_evaluation_seconds": empty_seconds,
        "evaluation_seconds": evaluation_seconds, "total_seconds": analyze_seconds + empty_seconds + evaluation_seconds,
        "empty_vector_digest": estimate_vector_digest(empty), "design_vector_digest": estimate_vector_digest(design_state),
        "empty_vector": [{"query_id": str(i.query_id), "q_error": i.contribution} for i in empty.query_evaluations],
        "design_vector": [{"query_id": str(i.query_id), "q_error": i.contribution} for i in design_state.query_evaluations],
    }


def sample_a_vectors(prepared: Any, repository: PayloadRepository, selected: tuple[Any, ...]) -> tuple[dict[str, float], dict[str, float]]:
    """Recover compact sample-A per-query references from the existing replay lineage."""
    sys.path.insert(0, str(ROOT / "tools"))
    from dmv_m2_18_frozen_hyp_vs_physical import prepare_hypothetical

    all_candidates = repository.catalog.candidates
    with psycopg.connect(DSN) as conn:
        design = Design(tuple(candidate.candidate_id for candidate in selected))
        design_state, _, _, baseline_state = prepare_hypothetical(
            conn, repository, all_candidates, prepared.workload, prepared.incidence, design
        )
        baseline = {str(item.query_id): float(item.contribution) for item in baseline_state.query_evaluations}
        design_values = {str(item.query_id): float(item.contribution) for item in design_state.query_evaluations}
        conn.execute("SELECT pg_hypothetical_extstats_reset()")
        drop_definitions(conn, all_candidates)
        conn.execute("DROP TABLE IF EXISTS public.pgextadv_frozen_sample")
        set_native_mode(conn)
        conn.commit()
        if stat_counts(conn) != (0, 0):
            raise RuntimeError("sample-A reference cleanup failed")
    return baseline, design_values


def write_json(path: Path, value: Any) -> None:
    path.write_text(json.dumps(value, sort_keys=True, indent=2, default=str) + "\n")


def write_csv(path: Path, rows: list[dict[str, Any]], fields: list[str]) -> None:
    with path.open("w", newline="") as handle:
        writer = csv.DictWriter(handle, fieldnames=fields, lineterminator="\n")
        writer.writeheader()
        writer.writerows({field: row.get(field) for field in fields} for row in rows)


def main() -> int:
    resume = len(sys.argv) == 2 and sys.argv[1] == "--resume"
    if OUT.exists() and not resume:
        raise RuntimeError(f"refusing to overwrite existing output directory: {OUT}")
    if git_head() != "a077b2814a4adc229b7460f7161834534ae7a481":
        raise RuntimeError("M2.19 requires the clean M2.18 main commit")
    prepared = load_prepared_run(PREPARED_ROOT)
    repository = PayloadRepository.load(REPOSITORY_ROOT)
    search = json.loads((SEARCH_ROOT / "final-result.json").read_text())
    m218 = json.loads((M218_ROOT / "final-design.json").read_text())
    selected_ids = tuple(str(item) for item in search["selected_design"])
    candidates_by_id = repository.catalog.by_id
    selected = tuple(candidates_by_id[CandidateId(item)] for item in selected_ids)
    if len(selected) != 31 or sum(c.mechanism.value == "mcv" for c in selected) != 8 or sum(c.mechanism.value == "fd" for c in selected) != 23:
        raise RuntimeError("M2.17d final design cardinality mismatch")
    if design_digest(selected) != EXPECTED_DESIGN_DIGEST or m218["final_design_digest"] != EXPECTED_DESIGN_DIGEST:
        raise RuntimeError("fixed design digest mismatch")
    if repository.digest != EXPECTED_REPOSITORY:
        raise RuntimeError("candidate repository changed")
    if prepared.workload.digest != EXPECTED_WORKLOAD or prepared.effective_workload_digest != EXPECTED_EFFECTIVE_WORKLOAD:
        raise RuntimeError("workload/truth lineage changed")
    if search["selected_count"] != 31 or search["selected_mcv_count"] != 8 or search["selected_fd_count"] != 23:
        raise RuntimeError("authoritative design artifact mismatch")
    OUT.mkdir(parents=True, exist_ok=True)
    with psycopg.connect(DSN) as conn:
        conn.execute("SELECT pg_hypothetical_extstats_reset()")
        set_native_mode(conn)
        drop_definitions(conn, selected)
        conn.commit()
        canonical_rows = row_count(conn)
        if canonical_rows != EXPECTED_ROWS:
            raise RuntimeError(f"canonical DMV row count mismatch: {canonical_rows}")
        canonical_schema = schema_signature(conn)
    protocol = {
        "milestone": "M2.19", "status": "running", "repo_head": git_head(),
        "fixed_design_source": str(SEARCH_ROOT / "final-result.json"),
        "fixed_design_digest": EXPECTED_DESIGN_DIGEST, "selected_count": 31,
        "selected_mcv_count": 8, "selected_fd_count": 23, "maintenance_cost": "175.422291756478746",
        "repository_digest": repository.digest, "workload_digest": prepared.workload.digest,
        "effective_workload_digest": prepared.effective_workload_digest, "raw_query_count": 1965,
        "effective_query_count": 1963, "truth_policy": "positive_truth_only", "sample_a_digest": "59dc8dbe007a81cbd10a11894ff8abf9b4a8f0cac11523221dbd63e34dd4ca7f",
        "sample_a_design_objective": EXPECTED_SAMPLE_A_DESIGN, "sample_a_empty_objective": EXPECTED_SAMPLE_A_BASELINE,
        "sample_a_relative_improvement": (EXPECTED_SAMPLE_A_BASELINE - EXPECTED_SAMPLE_A_DESIGN) / EXPECTED_SAMPLE_A_BASELINE,
        "fresh_realization_count": RUN_COUNT, "native_sampling": True, "frozen_replay_for_fresh_runs": False,
        "search_runs": 0, "screening_runs": 0, "maintenance_calibration_runs": 0,
        "design_selection_stability_tested": False, "postgres_version": "16.14", "patch_sha256": EXPECTED_PATCH,
        "upstream_tarball_sha256": UPSTREAM_SHA, "canonical_relation_rows": canonical_rows,
        "cluster_restart": False, "dataset_reload": False, "canonical_schema_signature": canonical_schema,
    }
    if resume:
        runs = [json.loads((OUT / f"run-{index:02d}.json").read_text()) for index in range(1, RUN_COUNT + 1)]
    else:
        write_json(OUT / "protocol.json", protocol)
        write_json(OUT / "design.json", {"source": str(SEARCH_ROOT / "final-result.json"), "fixed": True,
            "design_digest": EXPECTED_DESIGN_DIGEST, "selected_design": selected_ids, "selected_count": 31,
            "mcv_count": 8, "fd_count": 23, "maintenance_cost": "175.422291756478746", "sample_a_objective": EXPECTED_SAMPLE_A_DESIGN})
        runs = []
        for index in range(1, RUN_COUNT + 1):
            runs.append(run_one(prepared, selected, f"B{index}"))
            write_json(OUT / f"run-{index:02d}.json", runs[-1])
    sample_a_rel = protocol["sample_a_relative_improvement"]
    threshold = 0.8 * sample_a_rel
    relative = [float(run["relative_improvement"]) for run in runs]
    absolute = [float(run["absolute_improvement"]) for run in runs]
    objectives = [float(run["design_objective"]) for run in runs]
    positive = all(value > 0 for value in absolute)
    minimum_ratio = min(relative) / sample_a_rel
    classification = "strongly robust" if positive and min(relative) >= threshold else "moderately robust" if positive else "fragile"
    payload_rows = []
    for candidate in selected:
        cid = str(candidate.candidate_id)
        states = [run["states"][cid] for run in runs]
        digests = [run["payloads"][cid]["payload_digest"] for run in runs]
        payload_rows.append({"candidate_id": cid, "mechanism": candidate.mechanism.value,
            "columns": json.dumps(list(candidate.attributes), separators=(",", ":")), "sample_a_state": "PRESENT",
            **{run["run_id"]: run["states"][cid] for run in runs},
            "present_runs": sum(state == "PRESENT" for state in states), "absent_runs": sum(state == "ABSENT_NATIVE" for state in states),
            "distinct_payload_digests": len({item for item in digests if item is not None}), "state_flipped": len(set(states)) > 1})
    write_csv(OUT / "realization-states.csv", payload_rows,
        ["candidate_id", "mechanism", "columns", "sample_a_state"] + [run["run_id"] for run in runs] + ["present_runs", "absent_runs", "distinct_payload_digests", "state_flipped"])
    sample_a_baseline_vector_rows, sample_a_design_vector = sample_a_vectors(prepared, repository, selected)
    if abs(sum(sample_a_baseline_vector_rows.values()) - EXPECTED_SAMPLE_A_BASELINE) > 1e-9:
        raise RuntimeError("sample-A baseline query-vector objective mismatch")
    if abs(sum(sample_a_design_vector.values()) - EXPECTED_SAMPLE_A_DESIGN) > 1e-9:
        raise RuntimeError("sample-A design query-vector objective mismatch")
    query_rows = []
    top20 = sorted(sample_a_baseline_vector_rows, key=sample_a_baseline_vector_rows.get, reverse=True)[:20]
    for qid in sorted(sample_a_baseline_vector_rows):
        fresh_design = [next(item["q_error"] for item in run["design_vector"] if item["query_id"] == qid) for run in runs]
        fresh_empty = [next(item["q_error"] for item in run["empty_vector"] if item["query_id"] == qid) for run in runs]
        deltas = [empty - design for empty, design in zip(fresh_empty, fresh_design, strict=True)]
        query_rows.append({"query_id": qid, "sample_a_empty_qerror": sample_a_baseline_vector_rows[qid],
            "sample_a_design_qerror": sample_a_design_vector[qid], "improved_count": sum(delta > 0 for delta in deltas),
            "unchanged_count": sum(delta == 0 for delta in deltas), "worsened_count": sum(delta < 0 for delta in deltas),
            "min_design_qerror": min(fresh_design), "median_design_qerror": statistics.median(fresh_design), "max_design_qerror": max(fresh_design),
            "top20_sample_a_hardest": qid in top20})
    write_csv(OUT / "query-robustness.csv", query_rows,
        ["query_id", "sample_a_empty_qerror", "sample_a_design_qerror", "improved_count", "unchanged_count", "worsened_count", "min_design_qerror", "median_design_qerror", "max_design_qerror", "top20_sample_a_hardest"])
    run_rows = []
    for run in runs:
        run_rows.append({"run": run["run_id"], "ordinary_stats_digest": run["ordinary_statistics_digest"][:12],
            "payload_bundle_digest": run["payload_bundle_digest"][:12], "present": run["present_count"], "absent": run["absent_count"],
            "empty_objective": run["empty_objective"], "design_objective": run["design_objective"], "absolute_improvement": run["absolute_improvement"],
            "relative_improvement": run["relative_improvement"], "improved": run["improved_queries"], "unchanged": run["unchanged_queries"],
            "worsened": run["worsened_queries"], "analyze_seconds": run["analyze_seconds"]})
    write_csv(OUT / "runs.csv", run_rows, list(run_rows[0]))
    states_by_candidate = {row["candidate_id"]: row for row in payload_rows}
    flips = [cid for cid, row in states_by_candidate.items() if row["state_flipped"]]
    always_absent = [cid for cid, row in states_by_candidate.items() if row["present_runs"] == 0]
    query_categories = {
        "always_improved": sum(row["improved_count"] == RUN_COUNT for row in query_rows),
        "always_unchanged": sum(row["unchanged_count"] == RUN_COUNT for row in query_rows),
        "always_worsened": sum(row["worsened_count"] == RUN_COUNT for row in query_rows),
        "mixed": sum(0 < row["improved_count"] < RUN_COUNT or 0 < row["worsened_count"] < RUN_COUNT for row in query_rows),
    }
    top20_persistence = sum(next(row for row in query_rows if row["query_id"] == qid)["improved_count"] == RUN_COUNT for qid in top20)
    summary = {
        "status": "complete", "run_count": RUN_COUNT, "classification": classification,
        "design_objective": {"min": min(objectives), "median": statistics.median(objectives), "mean": statistics.fmean(objectives), "max": max(objectives), "std": statistics.stdev(objectives)},
        "absolute_improvement": {"min": min(absolute), "median": statistics.median(absolute), "mean": statistics.fmean(absolute), "max": max(absolute), "std": statistics.stdev(absolute)},
        "relative_improvement": {"min": min(relative), "median": statistics.median(relative), "mean": statistics.fmean(relative), "max": max(relative), "std": statistics.stdev(relative)},
        "sample_a_relative_improvement": sample_a_rel, "strong_gate_threshold": threshold, "minimum_relative_to_sample_a_ratio": minimum_ratio,
        "all_positive": positive, "candidate_always_present": 31 - len(flips) - len(always_absent), "candidate_state_flipped_count": len(flips),
        "candidate_always_absent_count": len(always_absent), "state_flipped_candidates": flips, "always_absent_candidates": always_absent,
        "query_categories": query_categories, "top20_sample_a_hardest_always_improved": top20_persistence, "top20_count": len(top20),
        "empty_max_range": [min(run["empty_distribution"]["max"] for run in runs), max(run["empty_distribution"]["max"] for run in runs)],
        "design_max_range": [min(run["design_distribution"]["max"] for run in runs), max(run["design_distribution"]["max"] for run in runs)],
        "empty_p90_range": [min(run["empty_distribution"]["p90"] for run in runs), max(run["empty_distribution"]["p90"] for run in runs)],
        "design_p90_range": [min(run["design_distribution"]["p90"] for run in runs), max(run["design_distribution"]["p90"] for run in runs)],
        "p99_available": True,
    }
    write_json(OUT / "summary.json", summary)
    protocol["status"] = "complete"
    protocol["ordinary_statistics_distinct_digest_count"] = len({run["ordinary_statistics_digest"] for run in runs})
    protocol["payload_bundle_distinct_digest_count"] = len({run["payload_bundle_digest"] for run in runs})
    protocol["classification"] = classification
    write_json(OUT / "protocol.json", protocol)
    report = [
        "# M2.19 — DMV fixed-design fresh-sample robustness", "",
        f"The immutable 31-stat design from M2.17d (8 MCV, 23 FD; digest `{EXPECTED_DESIGN_DIGEST}`) was deployed for {RUN_COUNT} independent native PostgreSQL 16.14 ANALYZE realizations on the canonical {EXPECTED_ROWS:,}-row DMV relation. No frozen-sample replay or search was used.", "",
        "Each run produced ordinary statistics and all selected extended-statistics payloads in one native ANALYZE. The empty-design objective was measured by transactional removal of only the 31 extended-statistics definitions and rollback, preserving the same ordinary-statistics realization; the design objective was then measured after rollback.", "",
        f"Design objective ranged from {min(objectives):.6f} to {max(objectives):.6f}; relative improvement ranged from {min(relative):.6%} to {max(relative):.6%}. Sample A relative improvement was {sample_a_rel:.6%}; the weakest fresh run retained {minimum_ratio:.2%} of that benefit. The preregistered engineering classification is **{classification}**.", "",
        f"Selected realization states were stable: {31 - len(flips) - len(always_absent)} candidates were PRESENT in every run, {len(flips)} flipped state, and {len(always_absent)} was always ABSENT_NATIVE. Flipped candidates: {', '.join(flips) if flips else 'none'}.", "",
        f"Across the 1,963 queries: {query_categories['always_improved']} were always improved, {query_categories['always_unchanged']} always unchanged, {query_categories['always_worsened']} always worsened, and {query_categories['mixed']} mixed across fresh runs. {top20_persistence}/{len(top20)} sample-A hardest queries improved in every fresh run.", "",
        "This measures ANALYZE sampling-noise robustness for a fixed recommendation from the same data distribution. It does not test design-selection stability, temporal/workload/schema/version drift, maintenance calibration, or M3.", "",
        "Cleanup after every run removed experiment statistics and reset the hypothetical registry; the canonical DMV relation remained unchanged.",
    ]
    (OUT / "report.md").write_text("\n".join(report) + "\n")
    print(json.dumps({"status": "complete", "runs": RUN_COUNT, "classification": classification,
        "min_relative_improvement": min(relative), "minimum_ratio_to_sample_a": minimum_ratio,
        "state_flips": len(flips), "all_positive": positive}, indent=2, sort_keys=True))
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
