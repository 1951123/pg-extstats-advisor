"""Validate frozen-sample hypothetical CE against physical extstats CE."""

from __future__ import annotations

import csv
import hashlib
import json
import statistics
from pathlib import Path
from typing import Any

import psycopg

from pg_extstats_advisor.deploy.sql import qualified_relation_name, quote_identifier
from pg_extstats_advisor.evaluator.native import NativeEvaluator
from pg_extstats_advisor.incidence.index import IncidenceIndex
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
from pg_extstats_advisor.postgres.adapter import PostgresAdapter
from pg_extstats_advisor.postgres.extraction import extract_target_estimate

ROOT = Path(__file__).resolve().parents[1]
PREPARED_ROOT = ROOT / "experiments/dmv-m2-15-singletons/prepared-run"
FROZEN_ROOT = ROOT / "datasets/dmv-frozen-acquisition-sample-v1"
REPOSITORY_ROOT = ROOT / "experiments/dmv-m2-17b-frozen-acquisition-sample/build-1/repository"
SEARCH_ROOT = ROOT / "experiments/dmv-m2-17d-frozen-full72-add"
OUT = ROOT / "experiments/dmv-m2-18-frozen-hyp-vs-physical"
BUILD_ENV = ROOT / "experiments/environment/postgresql-16.14-build.json"
DSN = f"host={ROOT}/.build/pg16.14-experiment-socket port=55436 dbname=pgextadv_exp16_dmv user=postgres"
TARGET = "public.dmv"
SAMPLE = "public.pgextadv_frozen_sample"
SAMPLE_DIGEST = "59dc8dbe007a81cbd10a11894ff8abf9b4a8f0cac11523221dbd63e34dd4ca7f"
BINARY_DIGEST = "c3b30ddfebf118cb9137bae122e2913e19e88d0cbee14b2c93e980c7693cf463"
EXPECTED_BASE_STATS = "bf6db08fd40e3873e1fc51675b0ff77de011b20fa20bcdf4f2e5817c4f7fc4fc"
EXPECTED_REPOSITORY = "0e928015a57a20624775c355e6dbb08e56cedd4437f5f54a7d0801c5aa7c6807"
EXPECTED_BASELINE = 42791.986480127205
EXPECTED_BASELINE_VECTOR = "f3254350d068cbd625fffd5537cd25573961da71779977e73811c1d6acb4a93f"
EXPECTED_FINAL = 22014.061316846422
EXPECTED_FINAL_VECTOR = "24cc3f4f9cc2e4987d877e4bb7874f458cf84a9aa781733adb5d3a29189551a4"
EXPECTED_FINAL_COST = "175.422291756478746"
EXPECTED_M217C_PROFILE = "e85f2392fd95ac2a122c1f9ff4702a2246e6913e7db13a662c10e26de6fb95ab"
EXPECTED_PATCH = "22c7f48632585e81fd8a557dc8bffba873ac5da070aca31713e22c60261c3b4f"
UPSTREAM_SHA = "f6d077142737920858ce958ccdb75c6ee137a63b5b0853c70693d401ac7e3471"


def digest(value: Any) -> str:
    return hashlib.sha256(
        json.dumps(value, sort_keys=True, separators=(",", ":"), default=str).encode()
    ).hexdigest()


def file_digest(path: Path) -> str:
    return hashlib.sha256(path.read_bytes()).hexdigest()


def git_head() -> str:
    import subprocess

    return subprocess.run(
        ["git", "rev-parse", "HEAD"], cwd=ROOT, check=True, capture_output=True, text=True
    ).stdout.strip()


def quote_ident(value: str) -> str:
    return '"' + value.replace('"', '""') + '"'


def verify_sample() -> dict[str, Any]:
    manifest_path = FROZEN_ROOT / "manifest.json"
    sample_path = FROZEN_ROOT / "sample.copy.bin"
    if not manifest_path.exists() or not sample_path.exists():
        raise RuntimeError("frozen sample manifest or binary is missing")
    manifest = json.loads(manifest_path.read_text())
    payload = sample_path.read_bytes()
    if manifest["semantic_sha256"] != SAMPLE_DIGEST:
        raise RuntimeError("frozen sample semantic digest mismatch")
    if manifest["sample_file_sha256"] != BINARY_DIGEST or hashlib.sha256(payload).hexdigest() != BINARY_DIGEST:
        raise RuntimeError("frozen sample binary digest mismatch")
    if manifest["row_count"] != 30000 or manifest["source_relation_row_count"] != 11591877:
        raise RuntimeError("frozen sample row-count mismatch")
    if int(manifest["totalrows_used_by_builder"]) != 11687702:
        raise RuntimeError("frozen sample totalrows mismatch")
    return manifest


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
        {
            "query_id": str(item.query_id), "estimate": item.estimate, "truth": item.truth,
            "contribution": item.contribution, "provenance": item.provenance,
        }
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
    values = [item.contribution for item in state.query_evaluations]
    return {
        "mean": statistics.fmean(values), "median": statistics.median(values),
        "p90": quantile(values, 0.9), "max": max(values), "query_count": len(values),
    }


def state_payload(state: EvaluationState) -> list[dict[str, Any]]:
    return [
        {"query_id": str(item.query_id), "estimate": item.estimate, "truth": item.truth,
         "contribution": item.contribution, "provenance": item.provenance}
        for item in state.query_evaluations
    ]


def names_for(candidates: tuple[Any, ...]) -> tuple[str, ...]:
    return tuple(str(dict(candidate.definition)["statistics_name"]) for candidate in candidates)


def drop_definitions(conn: psycopg.Connection[Any], repository: PayloadRepository, candidates: tuple[Any, ...] | None = None) -> None:
    for candidate in candidates or tuple(item.candidate for item in repository.payloads):
        schema = candidate.relation_name.split(".", 1)[0]
        name = str(dict(candidate.definition)["statistics_name"])
        conn.execute(f"DROP STATISTICS IF EXISTS {quote_ident(schema)}.{quote_ident(name)}")


def create_definitions(conn: psycopg.Connection[Any], candidates: tuple[Any, ...]) -> None:
    for candidate in candidates:
        schema, relation = candidate.relation_name.split(".", 1)
        mechanism = "mcv" if candidate.mechanism.value == "mcv" else "dependencies"
        name = str(dict(candidate.definition)["statistics_name"])
        attrs = ", ".join(quote_ident(item) for item in candidate.attributes)
        qname = f"{quote_ident(schema)}.{quote_ident(name)}"
        conn.execute(
            f"CREATE STATISTICS {qname} ({mechanism}) ON {attrs} "
            f"FROM {quote_ident(schema)}.{quote_ident(relation)}"
        )
        conn.execute(f"ALTER STATISTICS {qname} SET STATISTICS 100")


def set_replay(conn: psycopg.Connection[Any], mode: str, relation: str = SAMPLE) -> None:
    conn.execute("SELECT set_config(%s,%s,false)", ("pg_extstats.frozen_sample_mode", mode))
    conn.execute("SELECT set_config(%s,%s,false)", ("pg_extstats.frozen_sample_relation", relation))
    conn.execute("SELECT set_config(%s,%s,false)", ("pg_extstats.frozen_totalrows", "11687702"))


def load_sample(conn: psycopg.Connection[Any]) -> None:
    conn.execute(f"DROP TABLE IF EXISTS {SAMPLE}")
    conn.execute(f"CREATE UNLOGGED TABLE {SAMPLE} (LIKE {TARGET} INCLUDING DEFAULTS)")
    with conn.cursor().copy(f"COPY {SAMPLE} FROM STDIN (FORMAT binary)") as copy:
        copy.write((FROZEN_ROOT / "sample.copy.bin").read_bytes())


def stat_counts(conn: psycopg.Connection[Any]) -> tuple[int, int]:
    stats = int(conn.execute("SELECT count(*) FROM pg_statistic_ext WHERE stxname LIKE 'pgextadv_acq_%'").fetchone()[0])
    data = int(conn.execute(
        "SELECT count(*) FROM pg_statistic_ext_data d JOIN pg_statistic_ext e ON e.oid=d.stxoid "
        "WHERE e.stxname LIKE 'pgextadv_acq_%'"
    ).fetchone()[0])
    return stats, data


def relation_metadata(conn: psycopg.Connection[Any]) -> dict[str, Any]:
    row = conn.execute(
        "SELECT c.reltuples,c.relpages,c.relnatts,c.relpersistence,c.relkind,n.nspname,c.relname "
        "FROM pg_class c JOIN pg_namespace n ON n.oid=c.relnamespace WHERE c.oid='public.dmv'::regclass"
    ).fetchone()
    columns = conn.execute(
        "SELECT attnum,attname,atttypid::regtype::text,attnotnull,attcollation::regcollation::text "
        "FROM pg_attribute WHERE attrelid='public.dmv'::regclass AND attnum > 0 AND NOT attisdropped ORDER BY attnum"
    ).fetchall()
    settings = {
        name: str(conn.execute(f"SHOW {name}").fetchone()[0])
        for name in (
            "default_statistics_target", "jit", "max_parallel_workers_per_gather",
            "random_page_cost", "cpu_tuple_cost", "cpu_operator_cost", "cpu_index_tuple_cost",
            "effective_cache_size", "work_mem",
        )
    }
    return {
        "relation": list(row), "columns": [list(item) for item in columns], "settings": settings,
        "digest": digest({"relation": list(row), "columns": [list(item) for item in columns], "settings": settings}),
    }


def physical_payloads(conn: psycopg.Connection[Any], candidates: tuple[Any, ...]) -> dict[str, dict[str, Any]]:
    names = names_for(candidates)
    rows = conn.execute(
        "SELECT e.stxname,e.oid,e.stxkind,d.stxoid IS NOT NULL,"
        "pg_mcv_list_send(d.stxdmcv),pg_dependencies_send(d.stxddependencies) "
        "FROM pg_statistic_ext e LEFT JOIN pg_statistic_ext_data d ON d.stxoid=e.oid "
        "WHERE e.stxname = ANY(%s)", (list(names),)
    ).fetchall()
    by_name = {str(row[0]): row for row in rows}
    output: dict[str, dict[str, Any]] = {}
    for candidate in candidates:
        name = str(dict(candidate.definition)["statistics_name"])
        row = by_name.get(name)
        if row is None:
            raise RuntimeError(f"physical statistic missing: {name}")
        mcv_bytes = bytes(row[4]) if row[4] is not None else None
        fd_bytes = bytes(row[5]) if row[5] is not None else None
        payload = mcv_bytes if candidate.mechanism.value == "mcv" else fd_bytes
        output[str(candidate.candidate_id)] = {
            "statistics_name": name, "physical_oid": int(row[1]), "stxkind": str(row[2]),
            "data_row_exists": bool(row[3]), "physical_realization_state": "PRESENT" if payload else "ABSENT_NATIVE",
            "physical_payload_digest": hashlib.sha256(payload).hexdigest() if payload else None,
        }
    return output


def physical_state(conn: psycopg.Connection[Any], workload: Any, design: Design) -> EvaluationState:
    active = conn.execute("SELECT pg_hypothetical_extstats_active()").fetchone()[0]
    if active not in (None, [], ()):  # no overlay must be active for physical evaluation
        raise RuntimeError(f"physical-only state has active hypothetical overlay: {active}")
    queries = tuple(sorted(workload.queries, key=lambda item: item.query_id))
    version = str(conn.execute("SHOW server_version").fetchone()[0])
    evaluations = []
    for query in queries:
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


def build_physical_sample(
    conn: psycopg.Connection[Any], repository: PayloadRepository, candidates: tuple[Any, ...]
) -> tuple[str, dict[str, Any], dict[str, dict[str, Any]]]:
    drop_definitions(conn, repository)
    load_sample(conn)
    create_definitions(conn, candidates)
    conn.commit()
    set_replay(conn, "replay")
    conn.execute(f"ANALYZE {TARGET}")
    conn.commit()
    base = ordinary_stats_digest(conn)
    metadata = relation_metadata(conn)
    counts = stat_counts(conn)
    payloads = physical_payloads(conn, candidates)
    if counts != (len(candidates), len(candidates)):
        raise RuntimeError(f"physical state statistics/data count mismatch: {counts}")
    conn.execute(f"DROP TABLE IF EXISTS {SAMPLE}")
    set_replay(conn, "off")
    conn.execute("SELECT pg_hypothetical_extstats_reset()")
    conn.commit()
    return base, metadata, payloads


def prepare_hypothetical(
    conn: psycopg.Connection[Any], repository: PayloadRepository, all_candidates: tuple[Any, ...],
    workload: Any, incidence: IncidenceIndex, design: Design,
) -> tuple[EvaluationState, dict[str, Any], list[int], EvaluationState]:
    base, metadata, _ = build_physical_sample(conn, repository, all_candidates)
    drop_definitions(conn, repository)
    create_definitions(conn, all_candidates)
    conn.commit()
    counts = stat_counts(conn)
    if counts != (72, 0):
        raise RuntimeError(f"hypothetical shell state mismatch: {counts}")
    evaluator = NativeEvaluator(workload, repository, incidence, PostgresAdapter(conn, repository))
    baseline = evaluator.evaluate_design(Design(()))
    state = evaluator.evaluate_design(design)
    active = conn.execute("SELECT pg_hypothetical_extstats_active()").fetchone()[0]
    active_oids = [int(item) for item in (active or [])]
    if len(active_oids) != 31:
        raise RuntimeError(f"hypothetical active count mismatch: {active}")
    return state, {
        "ordinary_statistics_digest": base, "relation_metadata": metadata,
        "physical_definition_count": counts[0], "physical_data_row_count": counts[1],
        "active_hypothetical_count": len(active_oids), "active_hypothetical_oids": active_oids,
        "planner_extstats_mode": "hypothetical overlay; selected 31 active; all 72 shells; zero data rows",
    }, active_oids, baseline


def relative_counts(state: EvaluationState, baseline: EvaluationState) -> dict[str, int]:
    before = baseline.by_query()
    after = state.by_query()
    deltas = [before[item].contribution - after[item].contribution for item in after]
    return {
        "improved": sum(delta > 0 for delta in deltas),
        "unchanged": sum(delta == 0 for delta in deltas),
        "worsened": sum(delta < 0 for delta in deltas),
    }


def selected_identity_digest(candidates: tuple[Any, ...]) -> str:
    return digest([
        {"candidate_id": str(c.candidate_id), "relation_name": c.relation_name,
         "mechanism": c.mechanism.value, "attributes": list(c.attributes),
         "precedence_rank": c.precedence_rank}
        for c in candidates
    ])


def write_json(path: Path, value: Any) -> None:
    path.write_text(json.dumps(value, sort_keys=True, indent=2, default=str) + "\n")


def write_csv(path: Path, rows: list[dict[str, Any]], fields: list[str]) -> None:
    with path.open("w", newline="") as handle:
        writer = csv.DictWriter(handle, fieldnames=fields, lineterminator="\n")
        writer.writeheader()
        writer.writerows({key: row.get(key) for key in fields} for row in rows)


def cleanup_db(conn: psycopg.Connection[Any], repository: PayloadRepository) -> dict[str, Any]:
    conn.execute("SELECT pg_hypothetical_extstats_reset()")
    drop_definitions(conn, repository)
    conn.execute(f"DROP TABLE IF EXISTS {SAMPLE}")
    set_replay(conn, "off")
    conn.commit()
    active = conn.execute("SELECT pg_hypothetical_extstats_active()").fetchone()[0]
    stats, data = stat_counts(conn)
    return {
        "residual_statistics": stats, "residual_data_rows": data,
        "hypothetical_active": active in (None, [], ()),
        "sample_relation_removed": conn.execute("SELECT to_regclass('public.pgextadv_frozen_sample')").fetchone()[0] is None,
    }


def evaluate_run(
    prepared: Any, repository: PayloadRepository, all_candidates: tuple[Any, ...], selected: tuple[Any, ...],
    design: Design, run_id: str,
) -> dict[str, Any]:
    with psycopg.connect(DSN) as conn:
        h_state, h_meta, h_oids, h_baseline = prepare_hypothetical(
            conn, repository, all_candidates, prepared.workload, prepared.incidence, design
        )
        h_payload = state_payload(h_state)
        h_digest = estimate_vector_digest(h_state)
        h_base = h_meta["ordinary_statistics_digest"]
        # Rebuild the selected physical state from the same persisted sample.
        conn.execute("SELECT pg_hypothetical_extstats_reset()")
        # Establish the no-extstats physical baseline before materializing the
        # selected definitions.  A physical EXPLAIN with selected objects
        # present is, by construction, not an empty-design baseline.
        build_physical_sample(conn, repository, ())
        conn.commit()
        with psycopg.connect(DSN) as baseline_conn:
            p_baseline = physical_state(baseline_conn, prepared.workload, Design(()))
        p_base, p_metadata, p_payloads = build_physical_sample(conn, repository, selected)
        p_counts = stat_counts(conn)
        conn.commit()
    with psycopg.connect(DSN) as physical_conn:
        physical_active = physical_conn.execute("SELECT pg_hypothetical_extstats_active()").fetchone()[0]
        p_state = physical_state(physical_conn, prepared.workload, design)
        p_digest = estimate_vector_digest(p_state)
        p_values = state_payload(p_state)
    if h_base != EXPECTED_BASE_STATS or p_base != EXPECTED_BASE_STATS or h_base != p_base:
        raise RuntimeError(f"ordinary-statistics mismatch H={h_base} P={p_base}")
    if h_meta["relation_metadata"] != p_metadata:
        raise RuntimeError("relation metadata mismatch between H and P")
    if h_state.aggregate_objective != EXPECTED_FINAL or p_state.aggregate_objective != EXPECTED_FINAL:
        raise RuntimeError(f"objective mismatch H={h_state.aggregate_objective} P={p_state.aggregate_objective}")
    if h_baseline.aggregate_objective != EXPECTED_BASELINE or p_baseline.aggregate_objective != EXPECTED_BASELINE:
        raise RuntimeError("baseline objective mismatch during H/P validation")
    if estimate_vector_digest(h_baseline) != EXPECTED_BASELINE_VECTOR or estimate_vector_digest(p_baseline) != EXPECTED_BASELINE_VECTOR:
        raise RuntimeError("baseline estimate-vector mismatch during H/P validation")
    if h_digest != EXPECTED_FINAL_VECTOR or p_digest != EXPECTED_FINAL_VECTOR or h_digest != p_digest:
        raise RuntimeError(f"estimate-vector mismatch H={h_digest} P={p_digest}")
    if physical_active not in (None, [], ()):
        raise RuntimeError(f"physical-only overlay is active: {physical_active}")
    return {
        "run_id": run_id, "hypothetical": {"objective": h_state.aggregate_objective, "estimate_vector_digest": h_digest,
            "distribution": distribution(h_state), "relative_to_empty": relative_counts(h_state, h_baseline),
            "baseline_objective": h_baseline.aggregate_objective, "baseline_vector_digest": estimate_vector_digest(h_baseline),
            "state": h_meta, "active_oids": h_oids, "estimate_vector": h_payload},
        "physical": {"objective": p_state.aggregate_objective, "estimate_vector_digest": p_digest,
            "distribution": distribution(p_state), "relative_to_empty": relative_counts(p_state, p_baseline),
            "baseline_objective": p_baseline.aggregate_objective, "baseline_vector_digest": estimate_vector_digest(p_baseline),
            "state": {"ordinary_statistics_digest": p_base,
                "relation_metadata": p_metadata, "physical_definition_count": p_counts[0],
                "physical_data_row_count": p_counts[1], "hypothetical_active": physical_active in (None, [], ()),
                "relation_metadata_exact_vs_hypothetical": h_meta["relation_metadata"] == p_metadata},
            "payloads": p_payloads, "estimate_vector": p_values},
    }


def main() -> int:
    if OUT.exists():
        raise RuntimeError(f"refusing to overwrite existing output directory: {OUT}")
    manifest = verify_sample()
    prepared = load_prepared_run(PREPARED_ROOT)
    repository = PayloadRepository.load(REPOSITORY_ROOT)
    if repository.digest != EXPECTED_REPOSITORY:
        raise RuntimeError("frozen repository digest mismatch")
    search = json.loads((SEARCH_ROOT / "final-result.json").read_text())
    repeatability = json.loads((SEARCH_ROOT / "repeatability.json").read_text())
    selected_ids = tuple(CandidateId(str(item)) for item in search["selected_design"])
    all_candidates = repository.catalog.candidates
    by_id = repository.catalog.by_id
    if search["selected_count"] != 31 or search["selected_mcv_count"] != 8 or search["selected_fd_count"] != 23:
        raise RuntimeError("M2.17d selected design count mismatch")
    if search["selected_maintenance_cost"] != EXPECTED_FINAL_COST or search["final_objective"] != EXPECTED_FINAL:
        raise RuntimeError("M2.17d final result mismatch")
    if any(repository.by_candidate[item].state is not NativePayloadState.PRESENT for item in selected_ids):
        raise RuntimeError("selected design contains a non-PRESENT realization")
    if set(selected_ids) != {CandidateId(str(item["candidate_id"])) for item in search["accepted_sequence"]}:
        raise RuntimeError("selected design does not match accepted sequence candidate set")
    selected = tuple(by_id[item] for item in selected_ids)
    design = Design(selected_ids)
    final_design_digest = selected_identity_digest(selected)
    if not repeatability["exact_match"]:
        raise RuntimeError("M2.17d repeatability artifact is not exact")
    singleton_profile = json.loads((ROOT / "experiments/dmv-m2-17c-frozen-singletons/singleton-profile.json").read_text())
    if singleton_profile["profile_digest"] != EXPECTED_M217C_PROFILE:
        raise RuntimeError("M2.17c profile digest mismatch")
    build_env = json.loads(BUILD_ENV.read_text())
    OUT.mkdir(parents=True)
    protocol = {
        "milestone": "M2.18", "artifact_type": "authoritative-frozen-sample-hypothetical-vs-physical",
        "status": "running", "repo_head": git_head(), "acquisition_sample_digest": SAMPLE_DIGEST,
        "acquisition_sample_binary_sha256": BINARY_DIGEST, "sample_rows": manifest["row_count"],
        "original_relation_rows": manifest["source_relation_row_count"], "frozen_totalrows": manifest["totalrows_used_by_builder"],
        "ordinary_statistics_digest": EXPECTED_BASE_STATS, "frozen_repository_digest": EXPECTED_REPOSITORY,
        "baseline_objective": EXPECTED_BASELINE, "baseline_estimate_vector_digest": EXPECTED_BASELINE_VECTOR,
        "m217c_singleton_profile_digest": EXPECTED_M217C_PROFILE,
        "m217d_search_artifact_sha256": file_digest(SEARCH_ROOT / "final-result.json"),
        "m217d_search_commit": "a4c50d87aa00fed96ff6020537eb65361b139477",
        "final_design_digest": final_design_digest, "selected_design": [str(item) for item in selected_ids],
        "selected_count": len(selected), "selected_mcv_count": sum(item.mechanism.value == "mcv" for item in selected),
        "selected_fd_count": sum(item.mechanism.value == "fd" for item in selected), "final_cost": EXPECTED_FINAL_COST,
        "workload_digest": prepared.workload.digest, "effective_workload_digest": prepared.effective_workload_digest,
        "incidence_digest": prepared.incidence_digest,
        "candidate_catalog_digest": digest([
            {"candidate_id": str(item.candidate_id), "relation_name": item.relation_name,
             "mechanism": item.mechanism.value, "attributes": list(item.attributes),
             "precedence_rank": item.precedence_rank}
            for item in all_candidates
        ]),
        "pg_upstream_tarball_sha256": UPSTREAM_SHA, "patch_sha256": EXPECTED_PATCH,
        "build_input_digest": build_env["build_recipe_digest"], "postgres_version": "16.14",
        "fresh_random_sampling": 0, "native_heap_sampling": 0, "new_search_runs": 0,
        "maintenance_calibration_runs": 0, "sample_to_sample_robustness_tested": False,
        "search_or_screening": False,
    }
    write_json(OUT / "protocol.json", protocol)
    write_json(OUT / "final-design.json", {
        "selected_design": [str(item) for item in selected_ids], "selected_order_precedence": [str(item.candidate_id) for item in selected],
        "m217d_accepted_sequence": [str(item["candidate_id"]) for item in search["accepted_sequence"]],
        "accepted_sequence_set_matches": True, "final_design_digest": final_design_digest,
        "selected_count": 31, "mcv_count": 8, "fd_count": 23, "all_present": True,
        "maintenance_cost": EXPECTED_FINAL_COST,
    })
    runs = [
        evaluate_run(prepared, repository, all_candidates, selected, design, "run-1"),
        evaluate_run(prepared, repository, all_candidates, selected, design, "run-2-fresh-backend"),
    ]
    for run in runs:
        write_json(OUT / f"{run['run_id'].replace('-', '_')}.json", run)
    h1, p1 = runs[0]["hypothetical"], runs[0]["physical"]
    h2, p2 = runs[1]["hypothetical"], runs[1]["physical"]
    audit_rows = []
    for candidate in selected:
        cid = str(candidate.candidate_id)
        frozen = repository.by_candidate[candidate.candidate_id]
        a = p1["payloads"][cid]; b = p2["payloads"][cid]
        audit_rows.append({
            "selected_order": selected_ids.index(candidate.candidate_id) + 1, "candidate_id": cid,
            "mechanism": candidate.mechanism.value, "columns": json.dumps(list(candidate.attributes), separators=(",", ":")),
            "frozen_realization_state": frozen.state.value, "physical_realization_state_run1": a["physical_realization_state"],
            "physical_realization_state_run2": b["physical_realization_state"], "physical_oid_run1": a["physical_oid"],
            "physical_oid_run2": b["physical_oid"], "frozen_payload_digest": frozen.payload_sha256,
            "physical_payload_digest_run1": a["physical_payload_digest"], "physical_payload_digest_run2": b["physical_payload_digest"],
            "data_row_run1": a["data_row_exists"], "data_row_run2": b["data_row_exists"],
            "exact_run1": frozen.payload_sha256 == a["physical_payload_digest"],
            "exact_run2": frozen.payload_sha256 == b["physical_payload_digest"],
        })
    write_csv(OUT / "selected-payload-audit.csv", audit_rows, list(audit_rows[0]))
    comparison_rows = []
    for h, p in zip(h1["estimate_vector"], p1["estimate_vector"], strict=True):
        comparison_rows.append({
            "query_id": h["query_id"], "h_estimate": h["estimate"], "p_estimate": p["estimate"],
            "estimate_exact": h["estimate"] == p["estimate"], "h_q_error": h["contribution"],
            "p_q_error": p["contribution"], "q_error_exact": h["contribution"] == p["contribution"],
        })
    write_csv(OUT / "estimate-comparison.csv", comparison_rows, list(comparison_rows[0]))
    apply_lines, rollback_lines = [], []
    for candidate in selected:
        name = str(dict(candidate.definition)["statistics_name"])
        relation = qualified_relation_name(candidate.relation_name)
        schema = candidate.relation_name.split(".", 1)[0]
        mechanism = "mcv" if candidate.mechanism.value == "mcv" else "dependencies"
        attrs = ", ".join(quote_identifier(item) for item in candidate.attributes)
        qname = f"{quote_identifier(schema)}.{quote_identifier(name)}"
        apply_lines.extend([
            f"CREATE STATISTICS {qname} ({mechanism}) ON {attrs} FROM {relation};",
            f"ALTER STATISTICS {qname} SET STATISTICS 100;",
        ])
        rollback_lines.append(f"DROP STATISTICS IF EXISTS {qname};")
    (OUT / "apply.sql").write_text("\n".join(apply_lines) + "\n")
    (OUT / "rollback.sql").write_text("\n".join(rollback_lines) + "\n")
    exact_payload = all(row["exact_run1"] and row["exact_run2"] for row in audit_rows)
    exact_estimates = all(row["estimate_exact"] for row in comparison_rows)
    exact_qerrors = all(row["q_error_exact"] for row in comparison_rows)
    exact_h_repeat = h1["estimate_vector_digest"] == h2["estimate_vector_digest"] and h1["objective"] == h2["objective"]
    exact_p_repeat = p1["estimate_vector_digest"] == p2["estimate_vector_digest"] and p1["objective"] == p2["objective"]
    write_json(OUT / "repeatability.json", {
        "run1": {"h_objective": h1["objective"], "p_objective": p1["objective"], "h_digest": h1["estimate_vector_digest"], "p_digest": p1["estimate_vector_digest"]},
        "run2": {"h_objective": h2["objective"], "p_objective": p2["objective"], "h_digest": h2["estimate_vector_digest"], "p_digest": p2["estimate_vector_digest"]},
        "h_exact_across_runs": exact_h_repeat, "p_exact_across_runs": exact_p_repeat,
        "h_equals_p_run1": h1["objective"] == p1["objective"] and h1["estimate_vector_digest"] == p1["estimate_vector_digest"],
        "h_equals_p_run2": h2["objective"] == p2["objective"] and h2["estimate_vector_digest"] == p2["estimate_vector_digest"],
        "payload_exact_both_runs": exact_payload,
    })
    cleanup_results = []
    with psycopg.connect(DSN) as conn:
        cleanup_results.append(cleanup_db(conn, repository))
        empty = physical_state(conn, prepared.workload, Design(()))
        cleanup_results[-1]["baseline_after_cleanup"] = empty.aggregate_objective
        cleanup_results[-1]["baseline_digest_after_cleanup"] = estimate_vector_digest(empty)
        cleanup_results[-1]["baseline_exact"] = empty.aggregate_objective == EXPECTED_BASELINE
        cleanup_results[-1]["baseline_vector_exact"] = estimate_vector_digest(empty) == EXPECTED_BASELINE_VECTOR
    write_json(OUT / "cleanup.json", {"runs": cleanup_results, "sample_unchanged": verify_sample()["semantic_sha256"] == SAMPLE_DIGEST and verify_sample()["sample_file_sha256"] == BINARY_DIGEST, "repository_unchanged": PayloadRepository.load(REPOSITORY_ROOT).digest == EXPECTED_REPOSITORY})
    if not (exact_payload and exact_estimates and exact_qerrors and exact_h_repeat and exact_p_repeat and cleanup_results[0]["baseline_exact"] and cleanup_results[0]["baseline_vector_exact"]):
        raise RuntimeError("M2.18 exact validation gate failed")
    protocol["status"] = "complete"
    protocol["exact_payload_count"] = sum(row["exact_run1"] and row["exact_run2"] for row in audit_rows)
    protocol["exact_estimate_count"] = sum(row["estimate_exact"] for row in comparison_rows)
    protocol["exact_qerror_count"] = sum(row["q_error_exact"] for row in comparison_rows)
    write_json(OUT / "protocol.json", protocol)
    write_json(OUT / "hypothetical.json", {"run1": h1, "run2": h2})
    write_json(OUT / "physical.json", {"run1": p1, "run2": p2})
    report = [
        "# M2.18 — Frozen-sample hypothetical vs physical exact validation", "",
        "The final 31-stat DMV design was loaded directly from the authoritative M2.17d artifact. Both states use the same persisted acquisition sample, ordinary-statistics realization, PostgreSQL 16.14 build, workload, and truth vector. The only semantic difference is hypothetical payload replay versus ordinary physical catalog payload consumption.", "",
        f"H and P both have objective `{h1['objective']}` and estimate-vector digest `{h1['estimate_vector_digest']}`. All {len(comparison_rows)} query estimates and q-errors are exact; all {len(audit_rows)} selected physical payloads match their frozen repository payload bytes in both runs.", "",
        f"Relative to the same empty baseline, H counts are {h1['relative_to_empty']} and P counts are {p1['relative_to_empty']}; these counts are exact between modes and runs. Relation metadata and ordinary-statistics digests are exact between H and P and match the M2.17b digest.", "",
        f"State H used {h1['state']['physical_definition_count']} shell definitions, zero physical data rows, and 31 active hypothetical candidates. State P used {p1['state']['physical_definition_count']} physical definitions and {p1['state']['physical_data_row_count']} data rows in a fresh no-overlay backend.", "",
        "This is conditional same-sample mechanism validation only. It does not test fresh-sample robustness: no random or native heap sampling was run.", "",
        f"Cleanup restored the empty baseline exactly and left no experiment statistics/data rows. DDL artifacts contain {len(apply_lines) // 2} CREATE STATISTICS and {len(rollback_lines)} DROP STATISTICS statements.",
    ]
    (OUT / "report.md").write_text("\n".join(report) + "\n")
    print(json.dumps({"status": "complete", "h_objective": h1["objective"], "p_objective": p1["objective"], "exact_estimates": len(comparison_rows), "exact_payloads": len(audit_rows)}, indent=2, sort_keys=True))
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
