"""M2.37 Experiment C: acquisition-input size cost characterization."""

from __future__ import annotations

import argparse
import json
import shutil
import time
from typing import Any

import psycopg
from dmv_m2_37_config_scaling import physical_payload_check
from m2_37_common import (
    DMV_DSN,
    DMV_PREPARED,
    DMV_REPOSITORY,
    EXPECTED_PG,
    ROOT,
    TARGET,
    TARGET_T,
    build_m2_36_suite,
    create_definitions,
    drop_definitions,
    environment_metadata,
    explain_objective,
    git_head,
    median_stats,
    stat_counts,
    write_csv,
    write_json,
)

from pg_extstats_advisor.models import CandidateId, Design
from pg_extstats_advisor.orchestration import load_prepared_run
from pg_extstats_advisor.payloads.repository import PayloadRepository
from pg_extstats_advisor.postgres.adapter import PostgresAdapter
from pg_extstats_advisor.prepare.acquisition import (
    _acquisition_logical_metadata,
    acquire_payloads,
    cleanup_acquisition,
)

OUT = ROOT / "experiments/dmv-m2-37-performance-scaling/input-scaling"
RAW = OUT / "raw"
INPUT_SIZES = (30_000, 60_000, 120_000, 240_000)
SOURCE_TABLE = "public.dmv_m2_37_source"


def physical_one(conn: psycopg.Connection[Any], repository: PayloadRepository, workload: Any, config: dict[str, Any], repetition: int, warmup: bool) -> dict[str, Any]:
    candidates = tuple(repository.catalog.by_id[CandidateId(item)] for item in config["selected_design"])
    before = time.perf_counter()
    drop_definitions(conn, repository)
    conn.execute("SELECT pg_hypothetical_extstats_reset()")
    conn.commit()
    cleanup_before = time.perf_counter() - before
    started = time.perf_counter()
    create_definitions(conn, candidates)
    conn.commit()
    create_s = time.perf_counter() - started
    started = time.perf_counter()
    conn.execute(f"ANALYZE {TARGET}")
    conn.commit()
    analyze_s = time.perf_counter() - started
    payload = physical_payload_check(conn, repository, candidates)
    explain, _ = explain_objective(conn, workload)
    started = time.perf_counter()
    drop_definitions(conn, repository)
    conn.execute("SELECT pg_hypothetical_extstats_reset()")
    conn.commit()
    cleanup_after = time.perf_counter() - started
    if stat_counts(conn) != (0, 0):
        raise RuntimeError("input-scaling physical cleanup leaked state")
    materialization = cleanup_before + create_s + analyze_s + cleanup_after
    return {
        "path": "physical", "input_rows": config["input_rows"], "config_id": config["config_id"],
        "repetition": repetition, "warmup": warmup, "create_s": create_s, "analyze_s": analyze_s,
        "explain_s": explain["explain_elapsed_seconds"], "cleanup_s": cleanup_before + cleanup_after,
        "physical_materialization_s": materialization, "per_design_total_s": materialization + explain["explain_elapsed_seconds"],
        "objective": explain["objective"], "estimate_vector_digest": explain["estimate_vector_digest"],
        "planner_calls": explain["planner_calls"], "payload_check": payload,
    }


def hypothetical_one(conn: psycopg.Connection[Any], adapter: PostgresAdapter, workload: Any, config: dict[str, Any], repetition: int, warmup: bool) -> dict[str, Any]:
    design = Design(tuple(CandidateId(item) for item in config["selected_design"]))
    started = time.perf_counter()
    adapter.activate_design(design)
    activation_s = time.perf_counter() - started
    explain, _ = explain_objective(conn, workload)
    return {
        "path": "hypothetical", "input_rows": config["input_rows"], "config_id": config["config_id"],
        "repetition": repetition, "warmup": warmup, "activation_s": activation_s,
        "explain_s": explain["explain_elapsed_seconds"], "per_design_total_s": activation_s + explain["explain_elapsed_seconds"],
        "objective": explain["objective"], "estimate_vector_digest": explain["estimate_vector_digest"],
        "planner_calls": explain["planner_calls"],
    }


def main() -> int:
    parser = argparse.ArgumentParser()
    parser.add_argument("--repetitions", type=int, default=5)
    parser.add_argument("--warmups", type=int, default=1)
    parser.add_argument("--sizes", default=",".join(map(str, INPUT_SIZES)))
    args = parser.parse_args()
    if args.repetitions < 5 or args.warmups < 1:
        raise ValueError("M2.37 input scaling requires >=5 measured repetitions and a warmup")
    sizes = tuple(int(item) for item in args.sizes.split(","))
    original = PayloadRepository.load(DMV_REPOSITORY)
    prepared = load_prepared_run(DMV_PREPARED)
    suite = build_m2_36_suite(original)
    OUT.mkdir(parents=True, exist_ok=True); RAW.mkdir(exist_ok=True)
    rows: list[dict[str, Any]] = []
    acquisition_rows: list[dict[str, Any]] = []
    renamed = False
    try:
        with psycopg.connect(DMV_DSN) as conn:
            env = environment_metadata(conn, "pgextadv_exp16_dmv")
            if not env["postgres_version"].startswith(EXPECTED_PG):
                raise RuntimeError(f"unexpected PostgreSQL version: {env['postgres_version']}")
            drop_definitions(conn, original)
            conn.execute(f"ALTER TABLE {TARGET} RENAME TO dmv_m2_37_source")
            conn.commit(); renamed = True
            for input_rows in sizes:
                conn.execute(f"DROP TABLE IF EXISTS {TARGET}")
                conn.execute(f"CREATE UNLOGGED TABLE {TARGET} (LIKE {SOURCE_TABLE} INCLUDING DEFAULTS)")
                conn.execute(f"INSERT INTO {TARGET} SELECT * FROM {SOURCE_TABLE} ORDER BY ctid LIMIT %s", (input_rows,))
                conn.commit()
                source = _acquisition_logical_metadata(conn, TARGET)
                temp = OUT / "temp" / f"n{input_rows}" / "repository"
                started = time.perf_counter()
                acquired = acquire_payloads(
                    conn, original.catalog, temp, statistics_target=TARGET_T,
                    global_statistics_target=TARGET_T, upstream_sha256=original.upstream_sha256,
                    patch_commit=original.patch_commit, repository_id=f"m2-37-input-{input_rows}",
                    source_relations=(source,),
                )
                acquisition_s = time.perf_counter() - started
                repository = PayloadRepository.load(temp)
                present = sum(item.state.value == "PRESENT" for item in repository.payloads)
                absent = sum(item.state.value == "ABSENT_NATIVE" for item in repository.payloads)
                bytes_on_disk = sum(path.stat().st_size for path in temp.rglob("*") if path.is_file())
                acquisition_rows.append({"input_rows": input_rows, "acquisition_s": acquisition_s, "candidate_count": len(repository.payloads), "present_count": present, "absent_native_count": absent, "repository_bytes": bytes_on_disk, "repository_digest": repository.digest})
                design_rows = [{**config, "input_rows": input_rows} for config in suite]
                drop_definitions(conn, repository); conn.commit()
                for warmup in range(args.warmups):
                    for config in design_rows:
                        physical_one(conn, repository, prepared.workload, config, warmup + 1, True)
                for repetition in range(args.repetitions):
                    for config in design_rows:
                        rows.append(physical_one(conn, repository, prepared.workload, config, repetition + 1, False))
                    print(f"input N={input_rows} physical repetition={repetition + 1}/{args.repetitions}", flush=True)
                create_definitions(conn, repository.catalog.candidates); conn.commit()
                adapter = PostgresAdapter(conn, repository); adapter.register_repository()
                for warmup in range(args.warmups):
                    for config in design_rows:
                        hypothetical_one(conn, adapter, prepared.workload, config, warmup + 1, True)
                for repetition in range(args.repetitions):
                    for config in design_rows:
                        rows.append(hypothetical_one(conn, adapter, prepared.workload, config, repetition + 1, False))
                    print(f"input N={input_rows} hypothetical repetition={repetition + 1}/{args.repetitions}", flush=True)
                adapter.reset_overlay(); drop_definitions(conn, repository); conn.commit()
                cleanup_acquisition(conn, acquired)
                shutil.rmtree(temp.parent.parent, ignore_errors=True)
                if stat_counts(conn) != (0, 0):
                    raise RuntimeError(f"input scaling cleanup leaked at N={input_rows}")
            conn.execute(f"DROP TABLE IF EXISTS {TARGET}")
            conn.execute(f"ALTER TABLE {SOURCE_TABLE} RENAME TO dmv")
            conn.commit(); renamed = False
    finally:
        if renamed:
            # Best-effort restoration if an error occurs after source rename.
            with psycopg.connect(DMV_DSN) as cleanup:
                cleanup.execute(f"DROP TABLE IF EXISTS {TARGET}")
                cleanup.execute(f"ALTER TABLE {SOURCE_TABLE} RENAME TO dmv")
                cleanup.commit()
    write_json(OUT / "environment.json", env)
    write_json(OUT / "acquisition.json", {"suite": "C", "rows": acquisition_rows, "input_sizes": sizes, "target": TARGET_T})
    write_csv(RAW / "timings.csv", rows)
    write_csv(RAW / "acquisition.csv", acquisition_rows)
    summaries = []
    for input_rows in sizes:
        p = [row for row in rows if row["input_rows"] == input_rows and row["path"] == "physical" and not row["warmup"]]
        h = [row for row in rows if row["input_rows"] == input_rows and row["path"] == "hypothetical" and not row["warmup"]]
        summaries.append({"input_rows": input_rows, "physical_materialization_s": median_stats(row["physical_materialization_s"] for row in p), "physical_total_s": median_stats(row["per_design_total_s"] for row in p), "hypothetical_activation_s": median_stats(row["activation_s"] for row in h), "hypothetical_total_s": median_stats(row["per_design_total_s"] for row in h), "physical_explain_s": median_stats(row["explain_s"] for row in p), "hypothetical_explain_s": median_stats(row["explain_s"] for row in h), "ratio": median_stats(row["per_design_total_s"] for row in p)["median"] / median_stats(row["per_design_total_s"] for row in h)["median"], "removable_materialization_fraction": median_stats(row["physical_materialization_s"] / row["per_design_total_s"] for row in p)["median"]})
    write_json(OUT / "summary.json", {"suite": "C", "status": "complete", "rows": summaries, "acquisition": acquisition_rows, "truth_timing_excluded": True, "objective_interpretation": "diagnostic only because input row count varies; timing is primary", "correctness_gate": "NOT_APPLICABLE_FOR_N_VARIATION", "cleanup_gate": "PASS"})
    write_csv(OUT / "summary.csv", [{"input_rows": row["input_rows"], "physical_materialization_median_s": row["physical_materialization_s"]["median"], "physical_total_median_s": row["physical_total_s"]["median"], "hypothetical_activation_median_s": row["hypothetical_activation_s"]["median"], "hypothetical_total_median_s": row["hypothetical_total_s"]["median"], "ratio": row["ratio"], "removable_materialization_fraction": row["removable_materialization_fraction"]} for row in summaries])
    write_json(OUT / "provenance.json", {"milestone": "M2.37", "suite": "C", "status": "complete", "system_head": git_head(), "frozen_implementation_tag": "paper-v1-system", "frozen_implementation_commit": "88797e4b82ff1d5c8bbba28dc27987af78ce78ad", "postgres_version": EXPECTED_PG, "statistics_target": TARGET_T, "input_sizes": sizes, "derivation": "isolated DMV database: rename original public.dmv to source, create an UNLOGGED public.dmv with deterministic ORDER BY ctid LIMIT N, run normal native ANALYZE, restore original table", "design_suite": [item["config_id"] for item in suite], "repetitions": args.repetitions, "warmups": args.warmups, "truth_timing_excluded": True})
    print(json.dumps({"suite": "C", "input_sizes": sizes, "rows": len(rows), "cleanup": "PASS"}, indent=2))
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
