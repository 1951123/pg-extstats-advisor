"""Authoritative DMV singleton refresh from the persisted frozen sample."""

from __future__ import annotations

import csv
import hashlib
import json
import time
from collections import Counter
from pathlib import Path
from typing import Any

import psycopg

from pg_extstats_advisor.evaluator.native import NativeEvaluator
from pg_extstats_advisor.models import CandidateId, Design, Move
from pg_extstats_advisor.orchestration import load_prepared_run
from pg_extstats_advisor.payloads.repository import PayloadRepository
from pg_extstats_advisor.postgres.adapter import PostgresAdapter

ROOT = Path(__file__).resolve().parents[1]
PREPARED_ROOT = ROOT / "experiments/dmv-m2-15-singletons/prepared-run"
FROZEN_ROOT = ROOT / "datasets/dmv-frozen-acquisition-sample-v1"
FROZEN_REPO_ROOT = ROOT / "experiments/dmv-m2-17b-frozen-acquisition-sample/build-1/repository"
M217B_BUILD = ROOT / "experiments/dmv-m2-17b-frozen-acquisition-sample/build-1/summary.json"
OUT = ROOT / "experiments/dmv-m2-17c-frozen-singletons"
MODEL_PATH = ROOT / "calibration/dmv-pg16.14-m2-16-r1/maintenance-model.json"
DSN = f"host={ROOT}/.build/pg16.14-experiment-socket port=55436 dbname=pgextadv_exp16_dmv user=postgres"
TARGET = "public.dmv"
SAMPLE = "public.pgextadv_frozen_sample"
SEMANTIC_DIGEST = "59dc8dbe007a81cbd10a11894ff8abf9b4a8f0cac11523221dbd63e34dd4ca7f"
BINARY_DIGEST = "c3b30ddfebf118cb9137bae122e2913e19e88d0cbee14b2c93e980c7693cf463"
EXPECTED_BASELINE = 42791.986480127205
EXPECTED_VECTOR_DIGEST = "f3254350d068cbd625fffd5537cd25573961da71779977e73811c1d6acb4a93f"
EXPECTED_BASE_STATS_DIGEST = "bf6db08fd40e3873e1fc51675b0ff77de011b20fa20bcdf4f2e5817c4f7fc4fc"
EXPECTED_MODEL_DIGEST = "f8885af9b1411bb417dcb1fb93f5e368d5e89c0eac29e7803d0c5e8563204714"
EXPECTED_PATCH_SHA = "22c7f48632585e81fd8a557dc8bffba873ac5da070aca31713e22c60261c3b4f"
UPSTREAM_SHA = "f6d077142737920858ce958ccdb75c6ee137a63b5b0853c70693d401ac7e3471"


def digest(value: Any) -> str:
    return hashlib.sha256(
        json.dumps(value, sort_keys=True, separators=(",", ":"), default=str).encode()
    ).hexdigest()


def verify_frozen_artifact() -> dict[str, Any]:
    manifest_path = FROZEN_ROOT / "manifest.json"
    sample_path = FROZEN_ROOT / "sample.copy.bin"
    if not manifest_path.exists() or not sample_path.exists():
        raise RuntimeError("frozen sample manifest or binary is missing")
    manifest = json.loads(manifest_path.read_text())
    binary = sample_path.read_bytes()
    if manifest["semantic_sha256"] != SEMANTIC_DIGEST:
        raise RuntimeError("frozen semantic digest mismatch")
    if manifest["sample_file_sha256"] != BINARY_DIGEST:
        raise RuntimeError("frozen binary manifest digest mismatch")
    if hashlib.sha256(binary).hexdigest() != BINARY_DIGEST:
        raise RuntimeError("frozen binary digest mismatch")
    if manifest["row_count"] != 30000 or manifest["source_relation_row_count"] != 11591877:
        raise RuntimeError("frozen sample cardinality mismatch")
    if int(manifest["totalrows_used_by_builder"]) != 11687702:
        raise RuntimeError("frozen totalrows mismatch")
    return manifest


def set_config(conn: psycopg.Connection, name: str, value: str) -> None:
    conn.execute("SELECT set_config(%s,%s,false)", (name, value))


def load_sample_and_definitions(conn: psycopg.Connection, repository: PayloadRepository) -> tuple[str, ...]:
    conn.execute("DROP TABLE IF EXISTS public.pgextadv_frozen_sample")
    conn.execute(f"CREATE UNLOGGED TABLE {SAMPLE} (LIKE {TARGET} INCLUDING DEFAULTS)")
    binary = (FROZEN_ROOT / "sample.copy.bin").read_bytes()
    with conn.cursor().copy(f"COPY {SAMPLE} FROM STDIN (FORMAT binary)") as copy:
        copy.write(binary)
    names: list[str] = []
    for frozen in repository.payloads:
        candidate = frozen.candidate
        definition = dict(candidate.definition)
        name = str(definition["statistics_name"])
        schema, relation = candidate.relation_name.split(".", 1)
        mechanism = "mcv" if candidate.mechanism.value == "mcv" else "dependencies"
        attrs = ", ".join('"' + item.replace('"', '""') + '"' for item in candidate.attributes)
        qname = f'"{schema}"."{name}"'
        conn.execute(
            f"CREATE STATISTICS {qname} ({mechanism}) ON {attrs} FROM \"{schema}\".\"{relation}\""
        )
        conn.execute(f"ALTER STATISTICS {qname} SET STATISTICS 100")
        names.append(name)
    conn.commit()
    set_config(conn, "pg_extstats.frozen_sample_mode", "replay")
    set_config(conn, "pg_extstats.frozen_sample_relation", SAMPLE)
    set_config(conn, "pg_extstats.frozen_totalrows", "11687702")
    conn.execute(f"ANALYZE {TARGET}")
    conn.commit()
    return tuple(names)


def ordinary_stats_digest(conn: psycopg.Connection) -> str:
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


def estimate_vector_digest(state: Any) -> str:
    return digest([
        {
            "query_id": str(item.query_id),
            "estimate": item.estimate,
            "truth": item.truth,
            "contribution": item.contribution,
            "provenance": item.provenance,
        }
        for item in state.query_evaluations
    ])


def candidate_identity(candidate: Any) -> dict[str, Any]:
    return {
        "candidate_id": str(candidate.candidate_id),
        "relation_name": candidate.relation_name,
        "mechanism": candidate.mechanism.value,
        "attributes": list(candidate.attributes),
        "definition": dict(candidate.definition),
        "precedence_rank": candidate.precedence_rank,
    }


def run_profile(prepared: Any, repository: PayloadRepository, dsn: str, run_id: str) -> dict[str, Any]:
    started = time.perf_counter()
    with psycopg.connect(dsn) as conn:
        evaluator = NativeEvaluator(
            prepared.workload,
            repository,
            prepared.incidence,
            PostgresAdapter(conn, repository),
        )
        empty = Design(())
        baseline = evaluator.evaluate_design(empty)
        rows: list[dict[str, Any]] = []
        for candidate in repository.catalog.candidates:
            before_calls = evaluator.adapter.planner_calls_total
            t0 = time.perf_counter()
            state = evaluator.evaluate_move(
                empty,
                Move.add_candidate(CandidateId(str(candidate.candidate_id))),
                baseline,
            )
            rows.append(
                {
                    **candidate_identity(candidate),
                    "realization_state": repository.by_candidate[candidate.candidate_id].state.value,
                    "singleton_objective": state.aggregate_objective,
                    "singleton_improvement": baseline.aggregate_objective - state.aggregate_objective,
                    "affected_query_count": len(state.affected_query_ids),
                    "reused_query_count": len(state.reused_query_ids),
                    "native_evaluator_count": 1,
                    "planner_calls": evaluator.adapter.planner_calls_total - before_calls,
                    "elapsed_seconds": time.perf_counter() - t0,
                }
            )
        rows.sort(key=lambda row: (-float(row["singleton_improvement"]), int(row["precedence_rank"]), row["candidate_id"]))
        for rank, row in enumerate(rows, 1):
            row["singleton_rank"] = rank
        return {
            "run_id": run_id,
            "rows": rows,
            "baseline_objective": baseline.aggregate_objective,
            "baseline_estimate_vector_digest": estimate_vector_digest(baseline),
            "planner_calls": evaluator.adapter.planner_calls_total,
            "elapsed_seconds": time.perf_counter() - started,
        }


def summarize(rows: list[dict[str, Any]]) -> dict[str, Any]:
    groups = {
        "all": rows,
        "mcv": [r for r in rows if r["mechanism"] == "mcv"],
        "fd": [r for r in rows if r["mechanism"] == "fd"],
        "PRESENT": [r for r in rows if r["realization_state"] == "PRESENT"],
        "ABSENT_NATIVE": [r for r in rows if r["realization_state"] == "ABSENT_NATIVE"],
    }
    def signs(values: list[dict[str, Any]]) -> dict[str, int]:
        return dict(Counter(
            "positive" if float(item["singleton_improvement"]) > 0 else
            "negative" if float(item["singleton_improvement"]) < 0 else "zero"
            for item in values
        ))
    ordered = sorted(rows, key=lambda row: float(row["singleton_improvement"]), reverse=True)
    concentration = {}
    for fraction in (0.01, 0.05, 0.10, 0.20, 0.50):
        count = max(1, int(len(ordered) * fraction))
        concentration[f"top_{int(fraction * 100)}pct"] = {
            "count": count,
            "improvement_sum": sum(float(item["singleton_improvement"]) for item in ordered[:count]),
            "positive_improvement_sum": sum(max(0.0, float(item["singleton_improvement"])) for item in ordered[:count]),
        }
    return {
        "counts": {name: signs(value) for name, value in groups.items()},
        "positive_utility_concentration": concentration,
        "absent_native": [
            {
                "candidate_id": item["candidate_id"],
                "attributes": item["attributes"],
                "singleton_objective": item["singleton_objective"],
                "singleton_improvement": item["singleton_improvement"],
                "singleton_rank": item["singleton_rank"],
            }
            for item in groups["ABSENT_NATIVE"]
        ],
    }


def historical_comparison(rows: list[dict[str, Any]]) -> dict[str, Any]:
    old = json.loads((ROOT / "experiments/dmv-m2-15-singletons/singleton-profile.json").read_text())
    old_by_id = {str(item["candidate_id"]): item for item in old["candidates"]}
    new_by_id = {item["candidate_id"]: item for item in rows}
    common = sorted(set(old_by_id) & set(new_by_id))
    old_order = [str(item["candidate_id"]) for item in sorted(old["candidates"], key=lambda x: int(x["singleton_rank"]))]
    new_order = [item["candidate_id"] for item in rows]
    top10_old, top10_new = set(old_order[:10]), set(new_order[:10])
    paired = [(float(old_by_id[c]["singleton_improvement"]), float(new_by_id[c]["singleton_improvement"])) for c in common]
    # Spearman rank correlation without adding a dependency: rank ties use mean rank.
    def rank(values: list[float]) -> dict[float, float]:
        order = sorted(enumerate(values), key=lambda item: item[1])
        output: dict[float, float] = {}
        i = 0
        while i < len(order):
            j = i
            while j + 1 < len(order) and order[j + 1][1] == order[i][1]:
                j += 1
            value = (i + j + 2) / 2
            for k in range(i, j + 1):
                output[order[k][0]] = value
            i = j + 1
        return output
    old_r = rank([x[0] for x in paired]); new_r = rank([x[1] for x in paired])
    xo = sum(old_r[i] for i in old_r) / len(old_r); xn = sum(new_r[i] for i in new_r) / len(new_r)
    num = sum((old_r[i] - xo) * (new_r[i] - xn) for i in old_r)
    den = (sum((old_r[i] - xo) ** 2 for i in old_r) * sum((new_r[i] - xn) ** 2 for i in new_r)) ** 0.5
    return {
        "classification": "historical exploratory comparison",
        "old_artifact": "experiments/dmv-m2-15-singletons/singleton-profile.json",
        "old_realization_counts": dict(Counter(str(item["realization_state"]) for item in old["candidates"])),
        "new_realization_counts": dict(Counter(str(item["realization_state"]) for item in rows)),
        "common_candidate_count": len(common),
        "top10_overlap_count": len(top10_old & top10_new),
        "spearman_rank_correlation": num / den if den else None,
        "exact_equality_required": False,
    }


def write_csv(path: Path, rows: list[dict[str, Any]]) -> None:
    fields = [
        "singleton_rank", "candidate_id", "mechanism", "attributes", "precedence_rank",
        "realization_state", "singleton_objective", "singleton_improvement",
        "affected_query_count", "reused_query_count", "native_evaluator_count", "planner_calls", "elapsed_seconds",
    ]
    with path.open("w", newline="") as handle:
        writer = csv.DictWriter(handle, fieldnames=fields, lineterminator="\n")
        writer.writeheader()
        for row in rows:
            writer.writerow({key: json.dumps(row[key], separators=(",", ":")) if key == "attributes" else row[key] for key in fields})


def main() -> int:
    manifest = verify_frozen_artifact()
    prepared = load_prepared_run(PREPARED_ROOT)
    repository = PayloadRepository.load(FROZEN_REPO_ROOT)
    model = json.loads(MODEL_PATH.read_text())
    if model.get("digest") != EXPECTED_MODEL_DIGEST:
        raise RuntimeError("accepted M2.16 maintenance model digest mismatch")
    old_ids = [str(item.candidate_id) for item in prepared.catalog.candidates]
    new_ids = [str(item.candidate_id) for item in repository.catalog.candidates]
    if old_ids != new_ids:
        raise RuntimeError("frozen repository candidate identity/order mismatch")
    if sum(c.mechanism.value == "mcv" for c in repository.catalog.candidates) != 36 or sum(c.mechanism.value == "fd" for c in repository.catalog.candidates) != 36:
        raise RuntimeError("candidate mechanism counts are not 36/36")
    protocol = {
        "milestone": "M2.17c",
        "artifact_type": "authoritative-frozen-sample-singleton-profile",
        "acquisition_sample_digest": SEMANTIC_DIGEST,
        "acquisition_sample_binary_sha256": BINARY_DIGEST,
        "sample_manifest": str(FROZEN_ROOT / "manifest.json"),
        "sample_rows": manifest["row_count"],
        "original_relation_rows": manifest["source_relation_row_count"],
        "frozen_totalrows": manifest["totalrows_used_by_builder"],
        "frozen_repository_digest": repository.digest,
        "base_statistics_digest": EXPECTED_BASE_STATS_DIGEST,
        "baseline_objective": EXPECTED_BASELINE,
        "baseline_estimate_vector_digest": EXPECTED_VECTOR_DIGEST,
        "candidate_catalog_digest": digest([candidate_identity(c) for c in repository.catalog.candidates]),
        "historical_candidate_catalog_digest": prepared.candidate_catalog_digest,
        "incidence_digest": prepared.incidence_digest,
        "workload_digest": prepared.workload.digest,
        "effective_workload_digest": prepared.effective_workload_digest,
        "truth_policy": "positive_truth_only",
        "truth_policy_digest": digest({"policy": "positive_truth_only", "effective_workload_digest": prepared.effective_workload_digest}),
        "maintenance_model_digest": EXPECTED_MODEL_DIGEST,
        "maintenance_model_scope": "M2.16 production-style native ANALYZE model; frozen replay timing excluded",
        "upstream_tarball_sha256": UPSTREAM_SHA,
        "patch_sha256": EXPECTED_PATCH_SHA,
        "search_started": False,
        "candidate_screening": False,
        "random_sampling": 0,
        "reacquisition": 0,
    }
    OUT.mkdir(parents=True, exist_ok=True)
    (OUT / "protocol.json").write_text(json.dumps(protocol, sort_keys=True, indent=2) + "\n")
    dsn = DSN
    try:
        with psycopg.connect(dsn) as conn:
            load_sample_and_definitions(conn, repository)
            ext_count = int(conn.execute("SELECT count(*) FROM pg_statistic_ext WHERE stxname LIKE 'pgextadv_acq_%'").fetchone()[0])
            data_count = int(conn.execute("SELECT count(*) FROM pg_statistic_ext_data d JOIN pg_statistic_ext e ON e.oid=d.stxoid WHERE e.stxname LIKE 'pgextadv_acq_%'").fetchone()[0])
            if ext_count != 72 or data_count != 72:
                raise RuntimeError(f"unexpected preflight statistics counts: {ext_count}/{data_count}")
            base_digest = ordinary_stats_digest(conn)
            if base_digest != EXPECTED_BASE_STATS_DIGEST:
                raise RuntimeError(f"ordinary statistics digest mismatch: {base_digest}")
            evaluator = NativeEvaluator(prepared.workload, repository, prepared.incidence, PostgresAdapter(conn, repository))
            baseline = evaluator.evaluate_design(Design(()))
            if baseline.aggregate_objective != EXPECTED_BASELINE or estimate_vector_digest(baseline) != EXPECTED_VECTOR_DIGEST:
                raise RuntimeError("frozen baseline gate failed")
            # close this session before the independent singleton runs
            evaluator.adapter.reset_overlay()
    except Exception:
        with psycopg.connect(dsn) as cleanup:
            cleanup_experiment(cleanup, repository)
        raise
    run1 = run_profile(prepared, repository, dsn, "run-1")
    run2 = run_profile(prepared, repository, dsn, "run-2-fresh-backend-session")
    rows1, rows2 = run1["rows"], run2["rows"]
    semantic_rows1 = [{key: value for key, value in row.items() if key != "elapsed_seconds"} for row in rows1]
    semantic_rows2 = [{key: value for key, value in row.items() if key != "elapsed_seconds"} for row in rows2]
    profile_digest1, profile_digest2 = digest(semantic_rows1), digest(semantic_rows2)
    if semantic_rows1 != semantic_rows2 or profile_digest1 != profile_digest2:
        raise RuntimeError("singleton determinism gate failed")
    summary = summarize(rows1)
    result = {
        **protocol,
        "candidate_count": len(rows1),
        "realization_counts": dict(Counter(row["realization_state"] for row in rows1)),
        "run1": {key: value for key, value in run1.items() if key != "rows"},
        "run2": {key: value for key, value in run2.items() if key != "rows"},
        "profile_digest": profile_digest1,
        "run1_run2_exact": True,
        "summary": summary,
        "historical_comparison": historical_comparison(rows1),
    }
    (OUT / "singleton-profile.json").write_text(json.dumps({**result, "candidates": rows1}, sort_keys=True, indent=2) + "\n")
    write_csv(OUT / "singleton-results.csv", rows1)
    (OUT / "summary.json").write_text(json.dumps(summary, sort_keys=True, indent=2) + "\n")
    (OUT / "report.md").write_text(
        "# M2.17c — DMV frozen-sample singleton refresh\n\n"
        f"The persisted sample `{SEMANTIC_DIGEST}` was replayed without random sampling. "
        f"Baseline objective `{EXPECTED_BASELINE}` and estimate-vector digest `{EXPECTED_VECTOR_DIGEST}` "
        "passed exactly. All 72 candidates were evaluated twice from empty design; the semantic profile "
        f"digest `{profile_digest1}` matched exactly between runs. Realizations were "
        f"{result['realization_counts']}.\n\n"
        "The M2.16 maintenance model remains a separate production-style ANALYZE model; frozen replay "
        "timings were not used for calibration. Historical M2.15 comparison is descriptive only.\n"
    )
    cleanup = None
    with psycopg.connect(dsn) as conn:
        cleanup = cleanup_experiment(conn, repository)
    (OUT / "cleanup.json").write_text(json.dumps(cleanup, sort_keys=True, indent=2) + "\n")
    # Recheck that cleanup did not alter the persisted artifact.
    verify_frozen_artifact()
    print(json.dumps(result, sort_keys=True, indent=2))
    return 0


def cleanup_experiment(conn: psycopg.Connection, repository: PayloadRepository) -> dict[str, Any]:
    names = []
    for frozen in repository.payloads:
        name = str(dict(frozen.candidate.definition)["statistics_name"])
        schema = frozen.candidate.relation_name.split(".", 1)[0]
        conn.execute(f'DROP STATISTICS IF EXISTS "{schema}"."{name}"')
        names.append(name)
    conn.execute("DROP TABLE IF EXISTS public.pgextadv_frozen_sample")
    conn.execute("SELECT pg_hypothetical_extstats_reset()")
    conn.commit()
    residual_stats = int(conn.execute("SELECT count(*) FROM pg_statistic_ext WHERE stxname LIKE 'pgextadv_acq_%'").fetchone()[0])
    residual_data = int(conn.execute("SELECT count(*) FROM pg_statistic_ext_data d JOIN pg_statistic_ext e ON e.oid=d.stxoid WHERE e.stxname LIKE 'pgextadv_acq_%'").fetchone()[0])
    return {"dropped_statistics": len(names), "residual_statistics": residual_stats, "residual_data_rows": residual_data, "sample_relation_removed": conn.execute("SELECT to_regclass('public.pgextadv_frozen_sample')").fetchone()[0] is None}


if __name__ == "__main__":
    raise SystemExit(main())
