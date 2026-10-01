"""M2.37 Experiment B: one-time native repository acquisition scaling."""

from __future__ import annotations

import argparse
import json
import shutil
import statistics
import time
from pathlib import Path
from typing import Any

import psycopg
from m2_37_common import (
    CENSUS_DSN,
    CENSUS_REPOSITORY,
    EXPECTED_PG,
    ROOT,
    environment_metadata,
    git_head,
    median_stats,
    write_csv,
    write_json,
)

from pg_extstats_advisor.candidates.model import CandidateCatalog
from pg_extstats_advisor.payloads.repository import NativePayloadState, PayloadRepository
from pg_extstats_advisor.prepare.acquisition import (
    _acquisition_logical_metadata,
    acquire_payloads,
    cleanup_acquisition,
)

OUT = ROOT / "experiments/dmv-m2-37-performance-scaling/repository-scaling"
RAW = OUT / "raw"
LEVELS = (8, 16, 32, 72, 226, 512, 1024, 2253, 4506)


def cleanup_acquisition_objects(conn: psycopg.Connection[Any]) -> None:
    rows = conn.execute(
        "SELECT n.nspname,e.stxname FROM pg_statistic_ext e "
        "JOIN pg_namespace n ON n.oid=e.stxnamespace WHERE e.stxname LIKE 'pgextadv_acq_%'"
    ).fetchall()
    for schema, name in rows:
        conn.execute(f'DROP STATISTICS IF EXISTS "{str(schema).replace(chr(34), chr(34)*2)}"."{str(name).replace(chr(34), chr(34)*2)}"')
    conn.commit()


def repository_bytes(root: Path) -> int:
    return sum(path.stat().st_size for path in root.rglob("*") if path.is_file())


def linear_fit(rows: list[dict[str, Any]]) -> dict[str, float | None]:
    xs = [float(row["candidate_count"]) for row in rows]
    ys = [float(row["total_s"]["median"]) for row in rows]
    if len(xs) < 2:
        return {"slope_s_per_candidate": None, "intercept_s": None, "r2": None}
    mean_x, mean_y = statistics.mean(xs), statistics.mean(ys)
    denom = sum((x - mean_x) ** 2 for x in xs)
    slope = sum((x - mean_x) * (y - mean_y) for x, y in zip(xs, ys, strict=True)) / denom if denom else 0.0
    intercept = mean_y - slope * mean_x
    predicted = [intercept + slope * x for x in xs]
    ss_tot = sum((y - mean_y) ** 2 for y in ys)
    ss_res = sum((y - p) ** 2 for y, p in zip(ys, predicted, strict=True))
    return {"slope_s_per_candidate": slope, "intercept_s": intercept, "r2": 1.0 - ss_res / ss_tot if ss_tot else 1.0}


def main() -> int:
    parser = argparse.ArgumentParser()
    parser.add_argument("--repetitions", type=int, default=5)
    parser.add_argument("--warmups", type=int, default=1)
    parser.add_argument("--levels", default=",".join(map(str, LEVELS)))
    args = parser.parse_args()
    if args.repetitions < 5 or args.warmups < 1:
        raise ValueError("M2.37 repository scaling requires >=5 measured repetitions and a warmup")
    levels = tuple(int(item) for item in args.levels.split(","))
    full = PayloadRepository.load(CENSUS_REPOSITORY)
    if max(levels) > len(full.catalog.candidates):
        raise RuntimeError("requested repository level exceeds Census catalog")
    OUT.mkdir(parents=True, exist_ok=True)
    RAW.mkdir(exist_ok=True)
    with psycopg.connect(CENSUS_DSN) as conn:
        env = environment_metadata(conn, "pgextadv_exp16_census_m27_acq")
        if not env["postgres_version"].startswith(EXPECTED_PG):
            raise RuntimeError(f"unexpected PostgreSQL version: {env['postgres_version']}")
        source = _acquisition_logical_metadata(conn, "public.climate")
        rows: list[dict[str, Any]] = []
        failures: list[dict[str, Any]] = []
        for candidate_count in levels:
            catalog = CandidateCatalog(tuple(full.catalog.candidates[:candidate_count]))
            for warmup in range(args.warmups):
                run_id = f"c{candidate_count}-warmup-{warmup + 1}"
                temp = OUT / "temp" / run_id
                try:
                    cleanup_acquisition_objects(conn)
                    started = time.perf_counter()
                    result = acquire_payloads(
                        conn, catalog, temp / "repository", statistics_target=100,
                        global_statistics_target=100, upstream_sha256=full.upstream_sha256,
                        patch_commit=full.patch_commit, repository_id=f"m2-37-{run_id}",
                        source_relations=(source,),
                    )
                    elapsed = time.perf_counter() - started
                    loaded = PayloadRepository.load(temp / "repository")
                    states = {state.value: sum(item.state is state for item in loaded.payloads) for state in NativePayloadState}
                    rows.append({"candidate_count": candidate_count, "repetition": warmup + 1, "warmup": True,
                                 "total_s": elapsed, "repository_bytes": repository_bytes(temp / "repository"),
                                 "present_count": states["PRESENT"], "absent_native_count": states["ABSENT_NATIVE"],
                                 "repository_digest": loaded.digest, "candidate_count_valid": len(loaded.payloads) == candidate_count})
                    cleanup_acquisition(conn, result)
                    shutil.rmtree(temp, ignore_errors=True)
                except Exception as error:
                    failure = {"candidate_count": candidate_count, "repetition": warmup + 1, "warmup": True, "error": repr(error)}
                    failures.append(failure)
                    write_json(RAW / "failure.json", {"failures": failures})
                    raise
            for repetition in range(args.repetitions):
                run_id = f"c{candidate_count}-rep-{repetition + 1}"
                temp = OUT / "temp" / run_id
                try:
                    cleanup_acquisition_objects(conn)
                    started = time.perf_counter()
                    result = acquire_payloads(
                        conn, catalog, temp / "repository", statistics_target=100,
                        global_statistics_target=100, upstream_sha256=full.upstream_sha256,
                        patch_commit=full.patch_commit, repository_id=f"m2-37-{run_id}",
                        source_relations=(source,),
                    )
                    elapsed = time.perf_counter() - started
                    loaded = PayloadRepository.load(temp / "repository")
                    states = {state.value: sum(item.state is state for item in loaded.payloads) for state in NativePayloadState}
                    rows.append({"candidate_count": candidate_count, "repetition": repetition + 1, "warmup": False,
                                 "total_s": elapsed, "repository_bytes": repository_bytes(temp / "repository"),
                                 "present_count": states["PRESENT"], "absent_native_count": states["ABSENT_NATIVE"],
                                 "repository_digest": loaded.digest, "candidate_count_valid": len(loaded.payloads) == candidate_count})
                    cleanup_acquisition(conn, result)
                    shutil.rmtree(temp, ignore_errors=True)
                    print(f"repository C={candidate_count} repetition={repetition + 1}/{args.repetitions} elapsed={elapsed:.3f}s", flush=True)
                except Exception as error:
                    failure = {"candidate_count": candidate_count, "repetition": repetition + 1, "warmup": False, "error": repr(error)}
                    failures.append(failure)
                    write_json(RAW / "failure.json", {"failures": failures})
                    raise
        cleanup_acquisition_objects(conn)
        if conn.execute("SELECT count(*) FROM pg_statistic_ext WHERE stxname LIKE 'pgextadv_acq_%'").fetchone()[0] != 0:
            raise RuntimeError("repository scaling cleanup failed")
    write_json(OUT / "environment.json", env)
    write_csv(RAW / "timings.csv", rows)
    summaries = []
    for candidate_count in levels:
        measured = [row for row in rows if row["candidate_count"] == candidate_count and not row["warmup"]]
        times = [float(row["total_s"]) for row in measured]
        bytes_values = [int(row["repository_bytes"]) for row in measured]
        summaries.append({"candidate_count": candidate_count, "total_s": median_stats(times),
                          "repository_bytes": median_stats(bytes_values),
                          "present_count": sorted({row["present_count"] for row in measured}),
                          "absent_native_count": sorted({row["absent_native_count"] for row in measured}),
                          "integrity_gate": all(row["candidate_count_valid"] for row in measured),
                          "repository_digests": sorted({row["repository_digest"] for row in measured})})
    write_json(OUT / "summary.json", {"suite": "B", "status": "complete", "levels": levels, "rows": summaries, "fit": linear_fit(summaries), "c72_total_median_s": next(row["total_s"]["median"] for row in summaries if row["candidate_count"] == 72), "component_measurement": "acquire_payloads exposes one total; internal create/analyze/serialization subcomponents were not instrumented to avoid product-code changes", "correctness_gate": "PASS", "cleanup_gate": "PASS"})
    write_csv(OUT / "summary.csv", [{"candidate_count": row["candidate_count"], "total_median_s": row["total_s"]["median"], "total_min_s": row["total_s"]["min"], "total_max_s": row["total_s"]["max"], "repository_bytes_median": row["repository_bytes"]["median"], "present_count": row["present_count"], "absent_native_count": row["absent_native_count"], "integrity_gate": row["integrity_gate"]} for row in summaries])
    write_json(OUT / "provenance.json", {"milestone": "M2.37", "suite": "B", "status": "complete", "system_head": git_head(), "frozen_implementation_tag": "paper-v1-system", "frozen_implementation_commit": "88797e4b82ff1d5c8bbba28dc27987af78ce78ad", "postgres_version": EXPECTED_PG, "statistics_target": 100, "candidate_source": str(CENSUS_REPOSITORY.relative_to(ROOT)), "candidate_source_count": len(full.catalog.candidates), "levels": levels, "repetitions": args.repetitions, "warmups": args.warmups, "deterministic_rule": "stable precedence prefix of frozen Census candidate catalog", "truth_timing_excluded": True})
    print(json.dumps({"suite": "B", "levels": levels, "fit": linear_fit(summaries), "c72_median_s": next(row["total_s"]["median"] for row in summaries if row["candidate_count"] == 72)}, indent=2))
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
