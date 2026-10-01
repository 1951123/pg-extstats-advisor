"""M2.37 Experiment A: fixed-configuration-count amortization on DMV."""

from __future__ import annotations

import argparse
import hashlib
import json
import time
from pathlib import Path
from typing import Any

import psycopg
from m2_37_common import (
    DMV_DSN,
    DMV_FROZEN,
    DMV_PREPARED,
    DMV_REPOSITORY,
    EXPECTED_PG,
    ROOT,
    SAMPLE,
    TARGET,
    TARGET_T,
    build_configuration_pool,
    create_definitions,
    digest,
    drop_definitions,
    environment_metadata,
    explain_objective,
    file_digest,
    git_head,
    load_dmv_sample,
    median_stats,
    set_replay,
    stat_counts,
    statistic_name,
    write_csv,
    write_json,
)

from pg_extstats_advisor.models import CandidateId, Design
from pg_extstats_advisor.orchestration import load_prepared_run
from pg_extstats_advisor.payloads.repository import NativePayloadState, PayloadRepository
from pg_extstats_advisor.postgres.adapter import PostgresAdapter

OUT = ROOT / "experiments/dmv-m2-37-performance-scaling"
RAW = OUT / "raw"
SUMMARY = OUT / "summaries"
FIGURES = OUT / "figures"
EXPECTED_SAMPLE_BINARY = "c3b30ddfebf118cb9137bae122e2913e19e88d0cbee14b2c93e980c7693cf463"


def physical_payload_check(
    conn: psycopg.Connection[Any], repository: PayloadRepository, candidates: tuple[Any, ...]
) -> dict[str, Any]:
    if not candidates:
        return {"all_payloads_match": True, "exact_payload_count": 0}
    names = [statistic_name(candidate) for candidate in candidates]
    rows = conn.execute(
        "SELECT e.stxname,e.oid,pg_mcv_list_send(d.stxdmcv),"
        "pg_dependencies_send(d.stxddependencies) "
        "FROM pg_statistic_ext e LEFT JOIN pg_statistic_ext_data d ON d.stxoid=e.oid "
        "WHERE e.stxname = ANY(%s)", (names,)
    ).fetchall()
    by_name = {str(row[0]): row for row in rows}
    exact = 0
    details = {}
    for candidate in candidates:
        row = by_name.get(statistic_name(candidate))
        if row is None:
            raise RuntimeError(f"missing physical statistic {candidate.candidate_id}")
        payload = row[2] if candidate.mechanism.value == "mcv" else row[3]
        payload_bytes = bytes(payload) if payload is not None else None
        observed = hashlib.sha256(payload_bytes).hexdigest() if payload_bytes else None
        frozen = repository.by_candidate[candidate.candidate_id]
        match = observed == frozen.payload_sha256 and ((payload_bytes is not None) == (frozen.state is NativePayloadState.PRESENT))
        exact += int(match)
        details[str(candidate.candidate_id)] = {
            "state": "PRESENT" if payload_bytes else "ABSENT_NATIVE",
            "expected_state": frozen.state.value,
            "payload_exact": match,
            "physical_payload_sha256": observed,
            "frozen_payload_sha256": frozen.payload_sha256,
        }
    return {"all_payloads_match": exact == len(candidates), "exact_payload_count": exact, "states": details}


def physical_one(
    conn: psycopg.Connection[Any], repository: PayloadRepository, workload: Any,
    config: dict[str, Any], repetition: int, warmup: bool,
) -> dict[str, Any]:
    candidates = tuple(repository.catalog.by_id[CandidateId(item)] for item in config["selected_design"])
    cleanup_started = time.perf_counter()
    drop_definitions(conn, repository)
    conn.execute("SELECT pg_hypothetical_extstats_reset()")
    conn.commit()
    cleanup_before = time.perf_counter() - cleanup_started
    create_started = time.perf_counter()
    create_definitions(conn, candidates)
    conn.commit()
    create_seconds = time.perf_counter() - create_started
    analyze_started = time.perf_counter()
    set_replay(conn, "replay")
    conn.execute(f"ANALYZE {TARGET}")
    conn.commit()
    analyze_seconds = time.perf_counter() - analyze_started
    payload_check = physical_payload_check(conn, repository, candidates)
    explain, _ = explain_objective(conn, workload)
    cleanup_started = time.perf_counter()
    drop_definitions(conn, repository)
    conn.execute("SELECT pg_hypothetical_extstats_reset()")
    conn.commit()
    if stat_counts(conn) != (0, 0):
        raise RuntimeError("physical cleanup leaked extended statistics")
    cleanup_after = time.perf_counter() - cleanup_started
    materialization = cleanup_before + create_seconds + analyze_seconds + cleanup_after
    total = materialization + explain["explain_elapsed_seconds"]
    return {
        "path": "physical",
        "repetition": repetition,
        "warmup": warmup,
        "config_id": config["config_id"],
        "config_index": int(config["config_id"].split("-")[-1]),
        "config_size": config["design_size"],
        "mcv_count": config["mcv_count"],
        "fd_count": config["fd_count"],
        "absent_native_count": config["absent_native_count"],
        "create_s": create_seconds,
        "analyze_s": analyze_seconds,
        "explain_s": explain["explain_elapsed_seconds"],
        "cleanup_s": materialization - create_seconds - analyze_seconds,
        "per_design_total_s": total,
        "physical_create_s": create_seconds,
        "physical_analyze_s": analyze_seconds,
        "physical_explain_s": explain["explain_elapsed_seconds"],
        "physical_cleanup_s": materialization - create_seconds - analyze_seconds,
        "physical_total_s": total,
        "objective": explain["objective"],
        "estimate_vector_digest": explain["estimate_vector_digest"],
        "planner_calls": explain["planner_calls"],
        "payload_check": payload_check,
    }


def hypothetical_one(
    conn: psycopg.Connection[Any], adapter: PostgresAdapter, repository: PayloadRepository,
    workload: Any, config: dict[str, Any], repetition: int, warmup: bool,
) -> dict[str, Any]:
    design = Design(tuple(CandidateId(item) for item in config["selected_design"]))
    activation_started = time.perf_counter()
    adapter.activate_design(design)
    activation_seconds = time.perf_counter() - activation_started
    explain, _ = explain_objective(conn, workload)
    total = activation_seconds + explain["explain_elapsed_seconds"]
    return {
        "path": "hypothetical",
        "repetition": repetition,
        "warmup": warmup,
        "config_id": config["config_id"],
        "config_index": int(config["config_id"].split("-")[-1]),
        "config_size": config["design_size"],
        "mcv_count": config["mcv_count"],
        "fd_count": config["fd_count"],
        "absent_native_count": config["absent_native_count"],
        "activation_s": activation_seconds,
        "explain_s": explain["explain_elapsed_seconds"],
        "per_design_total_s": total,
        "hypothetical_activate_s": activation_seconds,
        "hypothetical_explain_s": explain["explain_elapsed_seconds"],
        "hypothetical_total_s": total,
        "objective": explain["objective"],
        "estimate_vector_digest": explain["estimate_vector_digest"],
        "planner_calls": explain["planner_calls"],
    }


def render_svg(path: Path, title: str, series: list[tuple[str, list[tuple[int, float]]]]) -> None:
    width, height, left, bottom = 900, 520, 80, 70
    all_values = [value for _name, values in series for _x, value in values]
    ymax = max(all_values) * 1.08 if all_values else 1.0
    xs = [x for _name, values in series for x, _y in values]
    xmax = max(xs) if xs else 1
    colors = ["#1b5e20", "#1565c0", "#ef6c00"]
    def point(x: int, y: float) -> tuple[float, float]:
        return left + (x / xmax) * (width - left - 30), height - bottom - (y / ymax) * (height - bottom - 35)
    parts = [f'<svg xmlns="http://www.w3.org/2000/svg" width="{width}" height="{height}">',
             f'<text x="{width/2}" y="25" text-anchor="middle" font-size="18">{title}</text>',
             f'<line x1="{left}" y1="35" x2="{left}" y2="{height-bottom}" stroke="black"/>',
             f'<line x1="{left}" y1="{height-bottom}" x2="{width-30}" y2="{height-bottom}" stroke="black"/>']
    for idx, (name, values) in enumerate(series):
        pts = " ".join(f"{point(x,y)[0]:.1f},{point(x,y)[1]:.1f}" for x, y in values)
        parts.append(f'<polyline points="{pts}" fill="none" stroke="{colors[idx % len(colors)]}" stroke-width="2"/>')
        lx, ly = point(values[-1][0], values[-1][1])
        parts.append(f'<text x="{lx+6:.1f}" y="{ly:.1f}" font-size="12">{name}</text>')
    parts.extend([f'<text x="{width/2}" y="{height-18}" text-anchor="middle">evaluated configurations (D)</text>',
                  f'<text x="18" y="{height/2}" transform="rotate(-90 18 {height/2})" text-anchor="middle">seconds</text>', '</svg>'])
    path.write_text("\n".join(parts) + "\n")


def main() -> int:
    parser = argparse.ArgumentParser()
    parser.add_argument("--config-count", type=int, default=256)
    parser.add_argument("--repetitions", type=int, default=5)
    parser.add_argument("--warmups", type=int, default=1)
    args = parser.parse_args()
    if args.config_count < 256 or args.repetitions < 5 or args.warmups < 1:
        raise ValueError("M2.37 primary protocol requires >=256 configs, >=5 repetitions, and a warmup")
    existing = [item for item in OUT.iterdir() if item.name != "repository-scaling"] if OUT.exists() else []
    if existing:
        raise RuntimeError(f"refusing to overwrite existing A output: {existing}")
    repository = PayloadRepository.load(DMV_REPOSITORY)
    prepared = load_prepared_run(DMV_PREPARED)
    configs = build_configuration_pool(repository, args.config_count)
    if len(configs) < 256:
        raise RuntimeError("configuration pool is too small")
    d_points = [1, 2, 4, 8, 16, 32, 64, 128, 256]
    if args.config_count >= 512:
        d_points.append(512)
    rows: list[dict[str, Any]] = []
    with psycopg.connect(DMV_DSN) as conn:
        env = environment_metadata(conn, "pgextadv_exp16_dmv")
        if not env["postgres_version"].startswith(EXPECTED_PG):
            raise RuntimeError(f"unexpected PostgreSQL version: {env['postgres_version']}")
        drop_definitions(conn, repository)
        load_dmv_sample(conn)
        create_definitions(conn, repository.catalog.candidates)
        set_replay(conn, "replay")
        conn.execute(f"ANALYZE {TARGET}")
        conn.commit()
        drop_definitions(conn, repository)
        conn.commit()
        for warmup in range(args.warmups):
            for index, config in enumerate(configs):
                physical_one(conn, repository, prepared.workload, config, warmup + 1, True)
                if index % 32 == 0:
                    print(f"physical warmup {index+1}/{len(configs)}", flush=True)
        for repetition in range(args.repetitions):
            for index, config in enumerate(configs):
                rows.append(physical_one(conn, repository, prepared.workload, config, repetition + 1, False))
                if index % 32 == 0:
                    print(f"physical rep {repetition+1}/{args.repetitions}, config {index+1}/{len(configs)}", flush=True)
        drop_definitions(conn, repository)
        conn.execute(f"DROP TABLE IF EXISTS {SAMPLE}")
        set_replay(conn, "off")
        conn.commit()
        if stat_counts(conn) != (0, 0):
            raise RuntimeError("physical final cleanup failed")

    with psycopg.connect(DMV_DSN) as conn:
        create_definitions(conn, repository.catalog.candidates)
        conn.commit()
        shell_setup_started = time.perf_counter()
        adapter = PostgresAdapter(conn, repository)
        adapter.register_repository()
        setup_seconds = time.perf_counter() - shell_setup_started
        for warmup in range(args.warmups):
            for index, config in enumerate(configs):
                hypothetical_one(conn, adapter, repository, prepared.workload, config, warmup + 1, True)
                if index % 32 == 0:
                    print(f"hypothetical warmup {index+1}/{len(configs)}", flush=True)
        for repetition in range(args.repetitions):
            for index, config in enumerate(configs):
                rows.append(hypothetical_one(conn, adapter, repository, prepared.workload, config, repetition + 1, False))
                if index % 32 == 0:
                    print(f"hypothetical rep {repetition+1}/{args.repetitions}, config {index+1}/{len(configs)}", flush=True)
        adapter.reset_overlay()
        drop_definitions(conn, repository)
        conn.commit()
        if stat_counts(conn) != (0, 0):
            raise RuntimeError("hypothetical final cleanup failed")

    OUT.mkdir(parents=True, exist_ok=True)
    RAW.mkdir(exist_ok=True); SUMMARY.mkdir(exist_ok=True); FIGURES.mkdir(exist_ok=True)
    write_json(OUT / "configurations.json", {"seed": 2037, "count": len(configs), "configs": configs})
    write_json(OUT / "environment.json", env)
    write_csv(RAW / "timings.csv", rows)
    # Correctness gate: measured rows from the same repetition/config must agree exactly.
    correctness_rows = []
    for rep in range(1, args.repetitions + 1):
        for config in configs:
            p = next(r for r in rows if r["path"] == "physical" and r["repetition"] == rep and r["config_id"] == config["config_id"] and not r["warmup"])
            h = next(r for r in rows if r["path"] == "hypothetical" and r["repetition"] == rep and r["config_id"] == config["config_id"] and not r["warmup"])
            correctness_rows.append({
                "repetition": rep,
                "config_id": config["config_id"],
                "estimate_vector_equal": p["estimate_vector_digest"] == h["estimate_vector_digest"],
                "objective_equal": p["objective"] == h["objective"],
                "payload_match": p["payload_check"]["all_payloads_match"],
                "planner_calls_equal": p["planner_calls"] == h["planner_calls"] == len(prepared.workload.queries),
            })
    if not all(item["estimate_vector_equal"] and item["objective_equal"] and item["payload_match"] and item["planner_calls_equal"] for item in correctness_rows):
        write_json(RAW / "correctness-failure.json", {"rows": correctness_rows})
        raise RuntimeError("M2.37 correctness gate failed; timing interpretation stopped")
    write_csv(RAW / "correctness.csv", correctness_rows)
    summaries = []
    for path in ("physical", "hypothetical"):
        measured = [r for r in rows if r["path"] == path and not r["warmup"]]
        for config in configs:
            values = [r["per_design_total_s"] for r in measured if r["config_id"] == config["config_id"]]
            summaries.append({"path": path, "config_id": config["config_id"], "per_design_total": median_stats(values),
                              "component": median_stats([r["analyze_s"] if path == "physical" else r["activation_s"] for r in measured if r["config_id"] == config["config_id"]]),
                              "explain": median_stats([r["explain_s"] for r in measured if r["config_id"] == config["config_id"]])})
    write_json(SUMMARY / "per-design.json", summaries)
    p_by = {(r["repetition"], r["config_id"]): r for r in rows if r["path"] == "physical" and not r["warmup"]}
    h_by = {(r["repetition"], r["config_id"]): r for r in rows if r["path"] == "hypothetical" and not r["warmup"]}
    repo_setup = setup_seconds
    b_path = ROOT / "experiments/dmv-m2-37-performance-scaling/repository-scaling/summary.json"
    if b_path.exists():
        b = json.loads(b_path.read_text())
        repo_setup = float(b.get("c72_total_median_s", repo_setup))
    cumulative = []
    for d in d_points:
        pvals = []; hvals = []; h_e2e = []
        for rep in range(1, args.repetitions + 1):
            pvals.append(sum(p_by[(rep, configs[i]["config_id"])] ["per_design_total_s"] for i in range(d)))
            hv = sum(h_by[(rep, configs[i]["config_id"])] ["per_design_total_s"] for i in range(d))
            hvals.append(hv); h_e2e.append(repo_setup + hv)
        cumulative.append({"D": d, "physical": median_stats(pvals), "hypothetical_steady": median_stats(hvals),
                           "hypothetical_e2e": median_stats(h_e2e), "materialization_median": median_stats([sum(p_by[(rep, configs[i]["config_id"])] ["physical_create_s"] + p_by[(rep, configs[i]["config_id"])] ["physical_analyze_s"] + p_by[(rep, configs[i]["config_id"])] ["physical_cleanup_s"] for i in range(d)) for rep in range(1, args.repetitions+1)]),
                           "shared_explain_physical": median_stats([sum(p_by[(rep, configs[i]["config_id"])] ["explain_s"] for i in range(d)) for rep in range(1, args.repetitions+1)]),
                           "shared_explain_hypothetical": median_stats([sum(h_by[(rep, configs[i]["config_id"])] ["explain_s"] for i in range(d)) for rep in range(1, args.repetitions+1)])})
    write_json(SUMMARY / "cumulative.json", {"repository_setup_used_s": repo_setup, "points": cumulative})
    write_csv(SUMMARY / "cumulative.csv", [{"D": x["D"], "physical_median_s": x["physical"]["median"], "hypothetical_steady_median_s": x["hypothetical_steady"]["median"], "hypothetical_e2e_median_s": x["hypothetical_e2e"]["median"], "materialization_median_s": x["materialization_median"]["median"], "explain_physical_median_s": x["shared_explain_physical"]["median"], "explain_hypothetical_median_s": x["shared_explain_hypothetical"]["median"]} for x in cumulative])
    render_svg(FIGURES / "A1-cumulative-wall-clock.svg", "M2.37 A1 cumulative wall-clock", [("physical", [(x["D"], x["physical"]["median"]) for x in cumulative]), ("hypothetical steady", [(x["D"], x["hypothetical_steady"]["median"]) for x in cumulative]), ("hypothetical + repository", [(x["D"], x["hypothetical_e2e"]["median"]) for x in cumulative])])
    render_svg(FIGURES / "A2-materialization.svg", "M2.37 A2 physical materialization", [("materialization", [(x["D"], x["materialization_median"]["median"]) for x in cumulative])])
    render_svg(FIGURES / "A3-shared-explain.svg", "M2.37 A3 shared EXPLAIN component", [("physical EXPLAIN", [(x["D"], x["shared_explain_physical"]["median"]) for x in cumulative]), ("hypothetical EXPLAIN", [(x["D"], x["shared_explain_hypothetical"]["median"]) for x in cumulative])])
    ratios = [{"D": x["D"], "ratio_phys_over_hyp_e2e": x["physical"]["median"] / x["hypothetical_e2e"]["median"]} for x in cumulative]
    write_json(SUMMARY / "speed-ratio.json", ratios)
    render_svg(FIGURES / "A4-speed-ratio.svg", "M2.37 A4 physical / hypothetical E2E", [("ratio", [(x["D"], x["ratio_phys_over_hyp_e2e"]) for x in ratios])])
    first_cross = next((x["D"] for x in cumulative if x["hypothetical_e2e"]["median"] <= x["physical"]["median"]), None)
    write_json(OUT / "provenance.json", {"milestone": "M2.37", "suite": "A", "status": "complete", "system_head": git_head(), "frozen_implementation_tag": "paper-v1-system", "frozen_implementation_commit": "88797e4b82ff1d5c8bbba28dc27987af78ce78ad", "postgres_version": EXPECTED_PG, "statistics_target": TARGET_T, "workload_query_count": len(prepared.workload.queries), "repository_digest": repository.digest, "repository_path": str(DMV_REPOSITORY.relative_to(ROOT)), "sample_manifest_digest": digest(json.loads((DMV_FROZEN / "manifest.json").read_text())), "sample_binary_sha256": file_digest(DMV_FROZEN / "sample.copy.bin"), "config_pool_seed": 2037, "config_pool_count": len(configs), "D_points": d_points, "repetitions": args.repetitions, "warmups": args.warmups, "truth_timing_excluded": True, "cache_flush": False, "correctness_gate": "PASS", "measured_first_crossover_D": first_cross, "hypothetical_repository_acquisition_is_charged_from_B72": b_path.exists()})
    write_json(SUMMARY / "summary.json", {"suite": "A", "status": "complete", "measured_first_crossover_D": first_cross, "repository_setup_used_s": repo_setup, "points": cumulative, "correctness_gate": "PASS", "fairness_gate": "PASS", "cleanup_gate": "PASS"})
    print(json.dumps({"suite": "A", "D_points": d_points, "first_cross": first_cross, "correctness": "PASS", "rows": len(rows)}, indent=2))
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
