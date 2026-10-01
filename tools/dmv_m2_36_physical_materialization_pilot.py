"""Bounded DMV physical-per-design materialization timing pilot.

This is a diagnostic timing experiment.  It does not alter advisor semantics,
search, or the frozen M2.18/M2.35 artifacts.
"""

from __future__ import annotations

import argparse
import csv
import hashlib
import json
import os
import platform
import statistics
import subprocess
import time
from pathlib import Path
from typing import Any

import psycopg

from pg_extstats_advisor.models import Candidate, CandidateId, Design, QueryEvaluation
from pg_extstats_advisor.objective.qerror import aggregate_objective, q_error
from pg_extstats_advisor.orchestration import load_prepared_run
from pg_extstats_advisor.payloads.repository import NativePayloadState, PayloadRepository
from pg_extstats_advisor.postgres.adapter import PostgresAdapter
from pg_extstats_advisor.postgres.extraction import extract_target_estimate

ROOT = Path(__file__).resolve().parents[1]
PREPARED_ROOT = ROOT / "experiments/dmv-m2-15-singletons/prepared-run"
REPOSITORY_ROOT = ROOT / ".build/artifact-cache/dmv-m2-17b-frozen-sample-v1/repository"
SEARCH_ROOT = ROOT / "experiments/dmv-m2-17d-frozen-full72-add"
FROZEN_ROOT = ROOT / "datasets/dmv-frozen-acquisition-sample-v1"
OUT = ROOT / "experiments/dmv-m2-36-physical-materialization-pilot"
DSN = f"host={ROOT}/.build/pg16.14-experiment-socket port=55436 dbname=pgextadv_exp16_dmv user=postgres"
TARGET = "public.dmv"
SAMPLE = "public.pgextadv_frozen_sample"
EXPECTED_SAMPLE = "59dc8dbe007a81cbd10a11894ff8abf9b4a8f0cac11523221dbd63e34dd4ca7f"
EXPECTED_SAMPLE_BINARY = "c3b30ddfebf118cb9137bae122e2913e19e88d0cbee14b2c93e980c7693cf463"
EXPECTED_BASE_STATS = "bf6db08fd40e3873e1fc51675b0ff77de011b20fa20bcdf4f2e5817c4f7fc4fc"
EXPECTED_BASELINE = 42791.986480127205
EXPECTED_POSTGRES = "16.14"
STATISTICS_TARGET = 100
REPETITIONS = 3
WARMUPS = 1


def digest(value: Any) -> str:
    return hashlib.sha256(
        json.dumps(value, sort_keys=True, separators=(",", ":"), default=str).encode()
    ).hexdigest()


def file_digest(path: Path) -> str:
    return hashlib.sha256(path.read_bytes()).hexdigest()


def git_head() -> str:
    return subprocess.run(
        ["git", "rev-parse", "HEAD"], cwd=ROOT, check=True, capture_output=True, text=True
    ).stdout.strip()


def quote_identifier(value: str) -> str:
    return '"' + value.replace('"', '""') + '"'


def qualified_relation(value: str) -> str:
    schema, relation = value.split(".", 1) if "." in value else ("public", value)
    return f"{quote_identifier(schema)}.{quote_identifier(relation)}"


def statistic_name(candidate: Candidate) -> str:
    return str(dict(candidate.definition)["statistics_name"])


def relation_schema(candidate: Candidate) -> str:
    return candidate.relation_name.split(".", 1)[0] if "." in candidate.relation_name else "public"


def stat_counts(conn: psycopg.Connection[Any]) -> tuple[int, int]:
    stats = int(conn.execute(
        "SELECT count(*) FROM pg_statistic_ext WHERE stxname LIKE 'pgextadv_%'"
    ).fetchone()[0])
    data = int(conn.execute(
        "SELECT count(*) FROM pg_statistic_ext_data d "
        "JOIN pg_statistic_ext e ON e.oid=d.stxoid "
        "WHERE e.stxname LIKE 'pgextadv_%'"
    ).fetchone()[0])
    return stats, data


def set_replay(conn: psycopg.Connection[Any], mode: str) -> None:
    conn.execute("SELECT set_config(%s,%s,false)", ("pg_extstats.frozen_sample_mode", mode))
    conn.execute("SELECT set_config(%s,%s,false)", ("pg_extstats.frozen_sample_relation", SAMPLE))
    conn.execute("SELECT set_config(%s,%s,false)", ("pg_extstats.frozen_totalrows", "11687702"))


def drop_definitions(conn: psycopg.Connection[Any], repository: PayloadRepository) -> None:
    for frozen in repository.payloads:
        candidate = frozen.candidate
        qname = f"{quote_identifier(relation_schema(candidate))}.{quote_identifier(statistic_name(candidate))}"
        conn.execute(f"DROP STATISTICS IF EXISTS {qname}")


def create_definitions(conn: psycopg.Connection[Any], candidates: tuple[Candidate, ...]) -> None:
    for candidate in candidates:
        mechanism = "mcv" if candidate.mechanism.value == "mcv" else "dependencies"
        qname = f"{quote_identifier(relation_schema(candidate))}.{quote_identifier(statistic_name(candidate))}"
        attrs = ", ".join(quote_identifier(item) for item in candidate.attributes)
        conn.execute(
            f"CREATE STATISTICS {qname} ({mechanism}) ON {attrs} "
            f"FROM {qualified_relation(candidate.relation_name)}"
        )
        conn.execute(f"ALTER STATISTICS {qname} SET STATISTICS {STATISTICS_TARGET}")


def load_sample(conn: psycopg.Connection[Any]) -> None:
    conn.execute(f"DROP TABLE IF EXISTS {SAMPLE}")
    conn.execute(f"CREATE UNLOGGED TABLE {SAMPLE} (LIKE {TARGET} INCLUDING DEFAULTS)")
    with conn.cursor().copy(f"COPY {SAMPLE} FROM STDIN (FORMAT binary)") as copy:
        copy.write((FROZEN_ROOT / "sample.copy.bin").read_bytes())


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


def verify_sample() -> dict[str, Any]:
    manifest = json.loads((FROZEN_ROOT / "manifest.json").read_text())
    payload = FROZEN_ROOT / "sample.copy.bin"
    if manifest["semantic_sha256"] != EXPECTED_SAMPLE:
        raise RuntimeError("frozen sample semantic digest mismatch")
    if manifest["sample_file_sha256"] != EXPECTED_SAMPLE_BINARY:
        raise RuntimeError("frozen sample binary digest mismatch")
    if file_digest(payload) != EXPECTED_SAMPLE_BINARY:
        raise RuntimeError("frozen sample file digest mismatch")
    if manifest["row_count"] != 30000:
        raise RuntimeError("unexpected frozen sample row count")
    return manifest


def candidate_identity(candidate: Candidate) -> dict[str, Any]:
    return {
        "candidate_id": str(candidate.candidate_id),
        "relation_name": candidate.relation_name,
        "mechanism": candidate.mechanism.value,
        "attributes": list(candidate.attributes),
        "precedence_rank": candidate.precedence_rank,
    }


def ordered_candidate_digest(candidates: tuple[Candidate, ...]) -> str:
    return digest([candidate_identity(candidate) for candidate in candidates])


def build_design_suite(repository: PayloadRepository) -> list[dict[str, Any]]:
    search = json.loads((SEARCH_ROOT / "final-result.json").read_text())
    by_id = repository.catalog.by_id
    accepted_ids = [CandidateId(str(item["candidate_id"])) for item in search["accepted_sequence"]]
    if len(accepted_ids) != 31:
        raise RuntimeError("unexpected DMV accepted sequence length")
    states = [
        ("empty", "empty design", ()),
        ("prefix-01", "M2.17d accepted ADD trajectory prefix 1", tuple(accepted_ids[:1])),
        ("prefix-05", "M2.17d accepted ADD trajectory prefix 5", tuple(accepted_ids[:5])),
        ("prefix-16", "M2.17d accepted ADD trajectory prefix 16", tuple(accepted_ids[:16])),
        ("prefix-30", "M2.17d accepted ADD trajectory prefix 30", tuple(accepted_ids[:30])),
        (
            "final-31",
            "M2.17d final 31-object recommendation",
            tuple(CandidateId(str(item)) for item in search["selected_design"]),
        ),
    ]
    suite: list[dict[str, Any]] = []
    for design_id, source, ids in states:
        design = repository.catalog.normalize_design(set(ids))
        candidates = tuple(by_id[item] for item in design.candidate_ids)
        if design_id == "final-31" and set(design.candidate_ids) != set(accepted_ids):
            raise RuntimeError("final design is not the accepted sequence set")
        suite.append({
            "design_id": design_id,
            "source": source,
            "selected_design": [str(item) for item in design.candidate_ids],
            "selected_count": len(candidates),
            "mcv_count": sum(item.mechanism.value == "mcv" for item in candidates),
            "fd_count": sum(item.mechanism.value == "fd" for item in candidates),
            "absent_native_count": sum(
                repository.by_candidate[item.candidate_id].state is NativePayloadState.ABSENT_NATIVE
                for item in candidates
            ),
            "ordered_candidate_digest": ordered_candidate_digest(candidates),
        })
    return suite


def physical_payload_check(
    conn: psycopg.Connection[Any], repository: PayloadRepository, candidates: tuple[Candidate, ...]
) -> dict[str, Any]:
    if not candidates:
        return {"all_payloads_match": True, "exact_payload_count": 0, "states": {}}
    names = [statistic_name(candidate) for candidate in candidates]
    rows = conn.execute(
        "SELECT e.stxname,e.oid,d.stxoid IS NOT NULL,pg_mcv_list_send(d.stxdmcv),"
        "pg_dependencies_send(d.stxddependencies) "
        "FROM pg_statistic_ext e LEFT JOIN pg_statistic_ext_data d ON d.stxoid=e.oid "
        "WHERE e.stxname = ANY(%s)", (names,)
    ).fetchall()
    by_name = {str(row[0]): row for row in rows}
    states: dict[str, dict[str, Any]] = {}
    exact = 0
    for candidate in candidates:
        row = by_name.get(statistic_name(candidate))
        if row is None:
            raise RuntimeError(f"missing physical statistic {candidate.candidate_id}")
        payload = row[3] if candidate.mechanism.value == "mcv" else row[4]
        payload_bytes = bytes(payload) if payload is not None else None
        observed = hashlib.sha256(payload_bytes).hexdigest() if payload_bytes else None
        expected = repository.by_candidate[candidate.candidate_id].payload_sha256
        match = observed == expected and (
            (payload_bytes is not None) ==
            (repository.by_candidate[candidate.candidate_id].state is NativePayloadState.PRESENT)
        )
        exact += int(match)
        states[str(candidate.candidate_id)] = {
            "mechanism": candidate.mechanism.value,
            "physical_oid": int(row[1]),
            "data_row_exists": bool(row[2]),
            "physical_state": "PRESENT" if payload_bytes else "ABSENT_NATIVE",
            "frozen_state": repository.by_candidate[candidate.candidate_id].state.value,
            "physical_payload_digest": observed,
            "frozen_payload_digest": expected,
            "payload_exact": match,
        }
    return {"all_payloads_match": exact == len(candidates), "exact_payload_count": exact, "states": states}


def explain_objective(conn: psycopg.Connection[Any], workload: Any) -> tuple[dict[str, Any], dict[str, float]]:
    explain_started = time.perf_counter()
    raw: list[tuple[str, float, float]] = []
    for query in sorted(workload.queries, key=lambda item: item.query_id):
        row = conn.execute(f"EXPLAIN (FORMAT JSON) {query.sql}").fetchone()
        if row is None:
            raise RuntimeError(f"EXPLAIN returned no row for {query.query_id}")
        raw.append((str(query.query_id), extract_target_estimate(row[0], query.target_relation), query.truth))
    explain_seconds = time.perf_counter() - explain_started
    objective_started = time.perf_counter()
    evaluations = tuple(
        QueryEvaluation(query_id, estimate, truth, q_error(estimate, truth), "native-explain:16.14")
        for query_id, estimate, truth in raw
    )
    objective = aggregate_objective(evaluations)
    objective_seconds = time.perf_counter() - objective_started
    rows = [
        {"query_id": str(item.query_id), "estimate": item.estimate, "truth": item.truth, "q_error": item.contribution}
        for item in evaluations
    ]
    return ({
        "objective": objective,
        "estimate_vector_digest": digest(rows),
        "query_count": len(evaluations),
        "planner_calls": len(evaluations),
        "explain_elapsed_seconds": explain_seconds,
        "objective_elapsed_seconds": objective_seconds,
    }, {str(item.query_id): item.estimate for item in evaluations})


def physical_repetition(
    conn: psycopg.Connection[Any], repository: PayloadRepository, workload: Any,
    config: dict[str, Any], repetition: int, warmup: bool,
) -> dict[str, Any]:
    by_id = repository.catalog.by_id
    candidates = tuple(by_id[CandidateId(item)] for item in config["selected_design"])
    reset_started = time.perf_counter()
    drop_definitions(conn, repository)
    conn.execute("SELECT pg_hypothetical_extstats_reset()")
    conn.commit()
    cleanup_before = time.perf_counter() - reset_started

    create_started = time.perf_counter()
    for candidate in candidates:
        mechanism = "mcv" if candidate.mechanism.value == "mcv" else "dependencies"
        qname = f"{quote_identifier(relation_schema(candidate))}.{quote_identifier(statistic_name(candidate))}"
        attrs = ", ".join(quote_identifier(item) for item in candidate.attributes)
        conn.execute(
            f"CREATE STATISTICS {qname} ({mechanism}) ON {attrs} FROM {qualified_relation(candidate.relation_name)}"
        )
    create_seconds = time.perf_counter() - create_started
    alter_started = time.perf_counter()
    for candidate in candidates:
        qname = f"{quote_identifier(relation_schema(candidate))}.{quote_identifier(statistic_name(candidate))}"
        conn.execute(f"ALTER STATISTICS {qname} SET STATISTICS {STATISTICS_TARGET}")
    conn.commit()
    alter_seconds = time.perf_counter() - alter_started

    analyze_started = time.perf_counter()
    set_replay(conn, "replay")
    conn.execute(f"ANALYZE {TARGET}")
    conn.commit()
    analyze_seconds = time.perf_counter() - analyze_started

    verify_started = time.perf_counter()
    payload_check = physical_payload_check(conn, repository, candidates)
    counts = stat_counts(conn)
    ordinary_digest = ordinary_stats_digest(conn)
    verify_seconds = time.perf_counter() - verify_started
    if counts != (len(candidates), len(candidates)):
        raise RuntimeError(f"physical state count mismatch for {config['design_id']}: {counts}")

    explain, _ = explain_objective(conn, workload)
    reset_after_started = time.perf_counter()
    drop_definitions(conn, repository)
    conn.execute("SELECT pg_hypothetical_extstats_reset()")
    conn.commit()
    residual = stat_counts(conn)
    cleanup_after = time.perf_counter() - reset_after_started
    if residual != (0, 0):
        raise RuntimeError(f"physical cleanup leaked state: {residual}")
    materialization = cleanup_before + create_seconds + alter_seconds + analyze_seconds + cleanup_after
    total = materialization + verify_seconds + explain["explain_elapsed_seconds"] + explain["objective_elapsed_seconds"]
    return {
        "path": "physical",
        "design_id": config["design_id"],
        "repetition": repetition,
        "warmup": warmup,
        "cleanup_before_elapsed_seconds": cleanup_before,
        "create_elapsed_seconds": create_seconds,
        "alter_elapsed_seconds": alter_seconds,
        "analyze_elapsed_seconds": analyze_seconds,
        "catalog_check_elapsed_seconds": verify_seconds,
        "explain_elapsed_seconds": explain["explain_elapsed_seconds"],
        "objective_elapsed_seconds": explain["objective_elapsed_seconds"],
        "cleanup_after_elapsed_seconds": cleanup_after,
        "physical_materialization_seconds": materialization,
        "physical_total_seconds": total,
        **explain,
        "ordinary_statistics_digest": ordinary_digest,
        "statistic_count": counts[0],
        "data_row_count": counts[1],
        "payload_check": payload_check,
    }


def hypothetical_repetition(
    conn: psycopg.Connection[Any], adapter: PostgresAdapter, repository: PayloadRepository,
    workload: Any, config: dict[str, Any], repetition: int, warmup: bool,
) -> dict[str, Any]:
    lookup_started = time.perf_counter()
    candidates = tuple(repository.catalog.by_id[CandidateId(item)] for item in config["selected_design"])
    design = Design(tuple(item.candidate_id for item in candidates))
    lookup_seconds = time.perf_counter() - lookup_started
    # Activation replaces the backend's ordered active list.  Calling the
    # full reset primitive here would also discard the one-time repository
    # registration, so reset cost is measured only at final teardown.
    reset_before = 0.0
    activation_started = time.perf_counter()
    adapter.activate_design(design)
    activation_seconds = time.perf_counter() - activation_started
    explain, _ = explain_objective(conn, workload)
    reset_after = 0.0
    setup = lookup_seconds + reset_before + activation_seconds + reset_after
    total = setup + explain["explain_elapsed_seconds"] + explain["objective_elapsed_seconds"]
    return {
        "path": "hypothetical",
        "design_id": config["design_id"],
        "repetition": repetition,
        "warmup": warmup,
        "repository_lookup_elapsed_seconds": lookup_seconds,
        "overlay_reset_before_elapsed_seconds": reset_before,
        "activation_elapsed_seconds": activation_seconds,
        "overlay_reset_after_elapsed_seconds": reset_after,
        "hypothetical_activation_component_seconds": setup,
        "hypothetical_total_seconds": total,
        **explain,
        "active_candidate_count": len(candidates),
    }


def environment_metadata(conn: psycopg.Connection[Any]) -> dict[str, Any]:
    settings = {}
    for name in ("shared_buffers", "work_mem", "random_page_cost", "jit", "max_parallel_workers_per_gather"):
        settings[name] = str(conn.execute(f"SHOW {name}").fetchone()[0])
    memory_total = None
    meminfo = Path("/proc/meminfo")
    if meminfo.exists():
        for line in meminfo.read_text().splitlines():
            if line.startswith("MemTotal:"):
                memory_total = int(line.split()[1]) * 1024
                break
    storage = os.statvfs(ROOT)
    return {
        "machine": platform.machine(),
        "platform": platform.platform(),
        "cpu_count": os.cpu_count(),
        "processor": platform.processor(),
        "python_version": platform.python_version(),
        "memory_total_bytes": memory_total,
        "storage_available_bytes": storage.f_bavail * storage.f_frsize,
        "postgres_version": str(conn.execute("SHOW server_version").fetchone()[0]),
        "database": "pgextadv_exp16_dmv",
        "dsn_socket": str(ROOT / ".build/pg16.14-experiment-socket"),
        "settings": settings,
        "cache_protocol": "one untimed warmup plus three measured repetitions; no cache flushing",
        "cache_flushed": False,
        "postgres_startup_measured": False,
    }


def aggregate_rows(rows: list[dict[str, Any]], path: str) -> list[dict[str, Any]]:
    measured = [row for row in rows if row["path"] == path and not row["warmup"]]
    result = []
    for design_id in sorted({row["design_id"] for row in measured}):
        items = [row for row in measured if row["design_id"] == design_id]
        fields = (
            ("physical_materialization_seconds", "physical_total_seconds")
            if path == "physical" else
            ("hypothetical_activation_component_seconds", "hypothetical_total_seconds")
        )
        item = {"path": path, "design_id": design_id, "repetitions": len(items)}
        for field in fields:
            values = [float(row[field]) for row in items]
            item[field + "_median"] = statistics.median(values)
            item[field + "_min"] = min(values)
            item[field + "_max"] = max(values)
            item[field + "_range"] = max(values) - min(values)
        item["explain_median"] = statistics.median(float(row["explain_elapsed_seconds"]) for row in items)
        item["objective_median"] = statistics.median(float(row["objective_elapsed_seconds"]) for row in items)
        item["objective_values"] = [float(row["objective"]) for row in items]
        item["estimate_vector_digests"] = [row["estimate_vector_digest"] for row in items]
        item["planner_calls"] = sorted({int(row["planner_calls"]) for row in items})
        result.append(item)
    return result


def write_json(path: Path, value: Any) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text(json.dumps(value, sort_keys=True, indent=2, default=str) + "\n")


def write_csv(path: Path, rows: list[dict[str, Any]]) -> None:
    if not rows:
        return
    path.parent.mkdir(parents=True, exist_ok=True)
    fields = list(rows[0])
    with path.open("w", newline="") as handle:
        writer = csv.DictWriter(handle, fieldnames=fields, lineterminator="\n")
        writer.writeheader()
        writer.writerows({field: row.get(field) for field in fields} for row in rows)


def main() -> int:
    parser = argparse.ArgumentParser()
    parser.add_argument("--repetitions", type=int, default=REPETITIONS)
    parser.add_argument("--warmups", type=int, default=WARMUPS)
    args = parser.parse_args()
    if args.repetitions < 3 or args.warmups < 1:
        raise ValueError("pilot requires at least three measured repetitions and one warmup")
    if OUT.exists():
        raise RuntimeError(f"refusing to overwrite existing output directory: {OUT}")
    sample_manifest = verify_sample()
    repository_load_started = time.perf_counter()
    repository = PayloadRepository.load(REPOSITORY_ROOT)
    repository_load_seconds = time.perf_counter() - repository_load_started
    prepared = load_prepared_run(PREPARED_ROOT)
    prepared_by_id = {candidate.candidate_id: candidate for candidate in prepared.catalog.candidates}
    for candidate in repository.catalog.candidates:
        reference = prepared_by_id.get(candidate.candidate_id)
        if reference is None or candidate.mechanism != reference.mechanism or candidate.attributes != reference.attributes or candidate.definition != reference.definition or candidate.precedence_rank != reference.precedence_rank or candidate.relation_name != reference.relation_name:
            raise RuntimeError(f"repository logical candidate mismatch: {candidate.candidate_id}")
    suite = build_design_suite(repository)
    rows: list[dict[str, Any]] = []
    with psycopg.connect(DSN) as physical_conn:
        env = environment_metadata(physical_conn)
        if env["postgres_version"].startswith(EXPECTED_POSTGRES) is False:
            raise RuntimeError(f"unexpected PostgreSQL version: {env['postgres_version']}")
        drop_definitions(physical_conn, repository)
        physical_conn.execute(f"DROP TABLE IF EXISTS {SAMPLE}")
        physical_conn.commit()
        sample_started = time.perf_counter()
        load_sample(physical_conn)
        # Match the authoritative M2.18/M2.35 acquisition order: the frozen
        # ordinary-statistics realization is captured with the complete shell
        # present, then the shell is removed before per-design timing.
        create_definitions(physical_conn, repository.catalog.candidates)
        set_replay(physical_conn, "replay")
        physical_conn.execute(f"ANALYZE {TARGET}")
        physical_conn.commit()
        drop_definitions(physical_conn, repository)
        physical_conn.commit()
        sample_reconstruction_seconds = time.perf_counter() - sample_started
        ordinary_digest = ordinary_stats_digest(physical_conn)
        if ordinary_digest != EXPECTED_BASE_STATS:
            raise RuntimeError(f"ordinary statistics mismatch: {ordinary_digest}")
        baseline_info, _ = explain_objective(physical_conn, prepared.workload)
        if baseline_info["objective"] != EXPECTED_BASELINE:
            raise RuntimeError(f"baseline objective mismatch: {baseline_info['objective']}")
        for config in suite:
            for warmup in range(args.warmups):
                physical_repetition(physical_conn, repository, prepared.workload, config, warmup + 1, True)
            for repetition in range(args.repetitions):
                print(f"physical {config['design_id']} repetition {repetition + 1}/{args.repetitions}", flush=True)
                rows.append(physical_repetition(physical_conn, repository, prepared.workload, config, repetition + 1, False))
        drop_definitions(physical_conn, repository)
        physical_conn.execute(f"DROP TABLE IF EXISTS {SAMPLE}")
        set_replay(physical_conn, "off")
        physical_conn.commit()
        if stat_counts(physical_conn) != (0, 0) or physical_conn.execute("SELECT to_regclass(%s)", (SAMPLE,)).fetchone()[0] is not None:
            raise RuntimeError("physical pilot cleanup failed")

    shell_setup_started = time.perf_counter()
    with psycopg.connect(DSN) as shell_conn:
        drop_definitions(shell_conn, repository)
        create_definitions(shell_conn, repository.catalog.candidates)
        shell_conn.commit()
        shell_setup_seconds = time.perf_counter() - shell_setup_started
        if stat_counts(shell_conn) != (len(repository.catalog.candidates), 0):
            raise RuntimeError("hypothetical shell setup mismatch")
        registration_started = time.perf_counter()
        adapter = PostgresAdapter(shell_conn, repository)
        adapter.register_repository()
        registration_seconds = time.perf_counter() - registration_started
        for config in suite:
            for warmup in range(args.warmups):
                hypothetical_repetition(shell_conn, adapter, repository, prepared.workload, config, warmup + 1, True)
            for repetition in range(args.repetitions):
                print(f"hypothetical {config['design_id']} repetition {repetition + 1}/{args.repetitions}", flush=True)
                rows.append(hypothetical_repetition(shell_conn, adapter, repository, prepared.workload, config, repetition + 1, False))
        adapter.reset_overlay()
        drop_definitions(shell_conn, repository)
        shell_conn.commit()
        if stat_counts(shell_conn) != (0, 0):
            raise RuntimeError("hypothetical pilot cleanup failed")

    physical_aggregates = aggregate_rows(rows, "physical")
    hypothetical_aggregates = aggregate_rows(rows, "hypothetical")
    by_design_p = {row["design_id"]: row for row in physical_aggregates}
    by_design_h = {row["design_id"]: row for row in hypothetical_aggregates}
    comparisons = []
    for config in suite:
        p, h = by_design_p[config["design_id"]], by_design_h[config["design_id"]]
        comparisons.append({
            "design_id": config["design_id"],
            "physical_materialization_median": p["physical_materialization_seconds_median"],
            "physical_total_median": p["physical_total_seconds_median"],
            "hypothetical_activation_median": h["hypothetical_activation_component_seconds_median"],
            "hypothetical_total_median": h["hypothetical_total_seconds_median"],
            "ratio_physical_over_hypothetical": p["physical_total_seconds_median"] / h["hypothetical_total_seconds_median"],
            "common_explain_physical_median": p["explain_median"],
            "common_explain_hypothetical_median": h["explain_median"],
            "planner_calls_physical": p["planner_calls"],
            "planner_calls_hypothetical": h["planner_calls"],
            "objective_physical": p["objective_values"],
            "objective_hypothetical": h["objective_values"],
        })
    p_material = [row["physical_materialization_median"] for row in comparisons]
    h_activation = [row["hypothetical_activation_median"] for row in comparisons]
    p_total = [row["physical_total_median"] for row in comparisons]
    h_total = [row["hypothetical_total_median"] for row in comparisons]
    median_delta = statistics.median(p - h for p, h in zip(p_material, h_activation, strict=True))
    hypothetical_one_time = repository_load_seconds + shell_setup_seconds + registration_seconds
    break_even = hypothetical_one_time / median_delta if median_delta > 0 else None
    ratios = [row["ratio_physical_over_hypothetical"] for row in comparisons]
    ratio_span = max(ratios) - min(ratios)
    signal = "STRONG" if min(ratios) > 2 and median_delta > 0 else "MODERATE" if min(ratios) > 1.25 and median_delta > 0 else "WEAK" if max(ratios) > 1 else "NONE"
    protocol = {
        "milestone": "M2.36",
        "artifact_type": "bounded-dmv-physical-materialization-pilot",
        "status": "complete",
        "purpose": "bounded DMV materialization-cost characterization; not a universal speedup study",
        "system_head": git_head(),
        "frozen_implementation_tag": "paper-v1-system",
        "frozen_implementation_commit": "88797e4b82ff1d5c8bbba28dc27987af78ce78ad",
        "postgres_version": EXPECTED_POSTGRES,
        "sample_manifest": sample_manifest,
        "sample_semantic_sha256": EXPECTED_SAMPLE,
        "sample_file_sha256": EXPECTED_SAMPLE_BINARY,
        "payload_repository_path": str(REPOSITORY_ROOT.relative_to(ROOT)),
        "payload_repository_digest": repository.digest,
        "workload_digest": prepared.workload.digest,
        "query_count": len(prepared.workload.queries),
        "statistics_target": STATISTICS_TARGET,
        "design_suite": suite,
        "repetitions": args.repetitions,
        "warmups": args.warmups,
        "environment": env,
        "one_time_setup_seconds": {
            "repository_load_seconds": repository_load_seconds,
            "sample_reconstruction_seconds": sample_reconstruction_seconds,
            "hypothetical_shell_setup_seconds": shell_setup_seconds,
            "hypothetical_repository_registration_seconds": registration_seconds,
            "hypothetical_total_setup_seconds": hypothetical_one_time,
        },
        "realization_rule": "physical fresh-ANALYZE objective is diagnostic unless payload/ordinary-state matching holds; M2.18/M2.35 remain authoritative fidelity evidence",
        "cache_rule": "one untimed warmup plus three measured repetitions; no OS/PostgreSQL cache flushing",
        "new_search_runs": 0,
        "new_sampling_runs": 0,
        "new_maintenance_calibration_runs": 0,
    }
    OUT.mkdir(parents=True)
    write_json(OUT / "protocol.json", protocol)
    write_json(OUT / "design-suite.json", {"designs": suite})
    write_json(OUT / "environment.json", env)
    write_json(OUT / "timings.json", {"rows": rows})
    write_json(OUT / "aggregate.json", {"physical": physical_aggregates, "hypothetical": hypothetical_aggregates, "comparisons": comparisons})
    write_csv(OUT / "timings.csv", rows)
    write_csv(OUT / "comparisons.csv", comparisons)
    summary = {
        "milestone": "M2.36",
        "status": "complete",
        "design_count": len(suite),
        "measured_repetitions_per_design_path": args.repetitions,
        "physical_materialization_median_seconds": statistics.median(p_material),
        "physical_materialization_range_seconds": [min(p_material), max(p_material)],
        "hypothetical_activation_median_seconds": statistics.median(h_activation),
        "hypothetical_activation_range_seconds": [min(h_activation), max(h_activation)],
        "physical_total_median_seconds": statistics.median(p_total),
        "physical_total_range_seconds": [min(p_total), max(p_total)],
        "hypothetical_total_median_seconds": statistics.median(h_total),
        "hypothetical_total_range_seconds": [min(h_total), max(h_total)],
        "ratio_median": statistics.median(ratios),
        "ratio_range": [min(ratios), max(ratios)],
        "ratio_span": ratio_span,
        "break_even_design_count": break_even,
        "one_time_hypothetical_setup_seconds": hypothetical_one_time,
        "common_explain_query_count": len(prepared.workload.queries),
        "all_planner_call_counts_match": all(
            row["planner_calls_physical"] == row["planner_calls_hypothetical"] == [len(prepared.workload.queries)]
            for row in comparisons
        ),
        "realization_match_counts": {
            "physical_rows_with_all_payloads_matching": sum(
                1 for row in rows if row["path"] == "physical" and row["payload_check"]["all_payloads_match"]
            ),
            "physical_rows_total": sum(1 for row in rows if row["path"] == "physical"),
        },
        "practical_signal": signal,
        "objective_comparison_interpretation": "diagnostic only; timing is primary and M2.18/M2.35 remain authoritative same-realization fidelity evidence",
    }
    write_json(OUT / "summary.json", summary)
    report = [
        "# M2.36 — Bounded DMV Physical-Per-Design Materialization Pilot", "",
        "This diagnostic pilot measures repeated physical materialization work for six deterministic DMV designs. It does not claim universal speedup, advisor superiority, production acceleration, or Census-scale physical replay.", "",
        f"The suite contains {len(suite)} designs: empty, accepted trajectory prefixes 1/5/16/30, and the final 31-object recommendation. Each path uses one untimed warmup and {args.repetitions} measured repetitions.", "",
        f"Median physical materialization component is {summary['physical_materialization_median_seconds']:.6f}s (range {summary['physical_materialization_range_seconds'][0]:.6f}–{summary['physical_materialization_range_seconds'][1]:.6f}s); median hypothetical activation component is {summary['hypothetical_activation_median_seconds']:.6f}s (range {summary['hypothetical_activation_range_seconds'][0]:.6f}–{summary['hypothetical_activation_range_seconds'][1]:.6f}s).",
        f"Median descriptive total ratio is {summary['ratio_median']:.3f}× (range {summary['ratio_range'][0]:.3f}–{summary['ratio_range'][1]:.3f}); one-time hypothetical setup is {hypothetical_one_time:.6f}s and the transparent median-delta break-even estimate is {break_even!r} design evaluations.", "",
        f"All physical rows had payload matching: {summary['realization_match_counts']['physical_rows_with_all_payloads_matching']}/{summary['realization_match_counts']['physical_rows_total']}. Objective values are retained for diagnostic comparison only; M2.18/M2.35 remain the authoritative fidelity evidence.", "",
        f"Practical signal: **{signal}**, bounded to this DMV environment and timing protocol.",
    ]
    (OUT / "report.md").write_text("\n".join(report) + "\n")
    print(json.dumps(summary, indent=2, sort_keys=True))
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
