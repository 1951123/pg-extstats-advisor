"""Persist four DMV samples, search each frozen world, and compare portability."""

from __future__ import annotations

import csv
import hashlib
import json
import math
import statistics
import sys
from collections import Counter
from pathlib import Path
from typing import Any

import psycopg

ROOT = Path(__file__).resolve().parents[1]
TOOLS = ROOT / "tools"
if str(TOOLS) not in sys.path:
    sys.path.insert(0, str(TOOLS))

import dmv_m2_17b_frozen_sample as acquisition
import dmv_m2_17c_frozen_singletons as singleton_tool
import dmv_m2_17d_frozen_full72_add as search_tool

from pg_extstats_advisor.cost.empirical import EmpiricalMechanismCountCostModel
from pg_extstats_advisor.cost.model import MaintenanceBudget
from pg_extstats_advisor.evaluator.native import NativeEvaluator
from pg_extstats_advisor.models import CandidateId, Design
from pg_extstats_advisor.orchestration import load_prepared_run
from pg_extstats_advisor.payloads.repository import PayloadRepository
from pg_extstats_advisor.postgres.adapter import PostgresAdapter

PREPARED_ROOT = ROOT / "experiments/dmv-m2-15-singletons/prepared-run"
DATASET_PATH = ROOT / "experiments/environment/dmv-dataset.json"
BUILD_ENV = ROOT / "experiments/environment/postgresql-16.14-build.json"
MODEL_PATH = ROOT / "calibration/dmv-pg16.14-m2-16-r1/maintenance-model.json"
OUT = ROOT / "experiments/dmv-m2-20-multisample-design-stability"
DATASETS = ROOT / "datasets"
DSN = f"host={ROOT}/.build/pg16.14-experiment-socket port=55436 dbname=pgextadv_exp16_dmv user=postgres"
TARGET = "public.dmv"
SAMPLE_REL = "public.pgextadv_frozen_sample"
EXPECTED_ROWS = 11_591_877
EXPECTED_SAMPLE_ROWS = 30_000
EXPECTED_CATALOG = "36 MCV + 36 FD"
EXPECTED_REPOSITORY_A = "0e928015a57a20624775c355e6dbb08e56cedd4437f5f54a7d0801c5aa7c6807"
EXPECTED_SAMPLE_A = "59dc8dbe007a81cbd10a11894ff8abf9b4a8f0cac11523221dbd63e34dd4ca7f"
EXPECTED_DESIGN_A = "c196630393e536612b600cd1abbabf025961ed1e3c977477d0c0e0a8f0ef99a9"
EXPECTED_MODEL = "f8885af9b1411bb417dcb1fb93f5e368d5e89c0eac29e7803d0c5e8563204714"
EXPECTED_PATCH = "22c7f48632585e81fd8a557dc8bffba873ac5da070aca31713e22c60261c3b4f"
UPSTREAM_SHA = "f6d077142737920858ce958ccdb75c6ee137a63b5b0853c70693d401ac7e3471"
SAMPLES = ("A", "B", "C", "D", "E")
NEW_SAMPLES = ("B", "C", "D", "E")
METADATA: Any = None


def digest(value: Any) -> str:
    return hashlib.sha256(json.dumps(value, sort_keys=True, separators=(",", ":"), default=str).encode()).hexdigest()


def quote_ident(value: str) -> str:
    return '"' + value.replace('"', '""') + '"'


def write_json(path: Path, value: Any) -> None:
    path.write_text(json.dumps(value, sort_keys=True, indent=2, default=str) + "\n")


def write_csv(path: Path, rows: list[dict[str, Any]], fields: list[str]) -> None:
    with path.open("w", newline="") as handle:
        writer = csv.DictWriter(handle, fieldnames=fields, lineterminator="\n")
        writer.writeheader()
        writer.writerows({field: row.get(field) for field in fields} for row in rows)


def clean_db(conn: psycopg.Connection[Any], candidates: tuple[Any, ...]) -> None:
    conn.execute("SELECT pg_hypothetical_extstats_reset()")
    for candidate in candidates:
        schema, _ = candidate.relation_name.split(".", 1)
        name = str(dict(candidate.definition)["statistics_name"])
        conn.execute(f"DROP STATISTICS IF EXISTS {quote_ident(schema)}.{quote_ident(name)}")
    conn.execute(f"DROP TABLE IF EXISTS {SAMPLE_REL}")
    conn.execute("SELECT set_config(%s,%s,false)", ("pg_extstats.frozen_sample_mode", "off"))
    conn.execute("SELECT set_config(%s,%s,false)", ("pg_extstats.frozen_sample_relation", ""))
    conn.execute("SELECT set_config(%s,%s,false)", ("pg_extstats.frozen_totalrows", "0"))
    conn.commit()


def source_schema_signature(dataset: dict[str, Any]) -> str:
    return digest({"relation": TARGET, "columns": dataset["ordered_column_schema"]})


def row_count(conn: psycopg.Connection[Any]) -> int:
    return int(conn.execute(f"SELECT count(*) FROM {TARGET}").fetchone()[0])


def create_shells(conn: psycopg.Connection[Any], candidates: tuple[Any, ...]) -> None:
    for candidate in candidates:
        schema, relation = candidate.relation_name.split(".", 1)
        kind = "mcv" if candidate.mechanism.value == "mcv" else "dependencies"
        name = str(dict(candidate.definition)["statistics_name"])
        attrs = ", ".join(quote_ident(item) for item in candidate.attributes)
        qname = f"{quote_ident(schema)}.{quote_ident(name)}"
        conn.execute(
            f"CREATE STATISTICS {qname} ({kind}) ON {attrs} FROM {quote_ident(schema)}.{quote_ident(relation)}"
        )
        conn.execute(f"ALTER STATISTICS {qname} SET STATISTICS 100")


def stat_counts(conn: psycopg.Connection[Any]) -> tuple[int, int]:
    definitions = int(conn.execute("SELECT count(*) FROM pg_statistic_ext WHERE stxname LIKE 'pgextadv_acq_%'").fetchone()[0])
    data = int(conn.execute(
        "SELECT count(*) FROM pg_statistic_ext_data d JOIN pg_statistic_ext e ON e.oid=d.stxoid "
        "WHERE e.stxname LIKE 'pgextadv_acq_%'"
    ).fetchone()[0])
    return definitions, data


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
    keys = ("relation", "attnum", "inherit", "nullfrac", "width", "distinct", "kind1", "kind2", "kind3", "kind4", "kind5", "op1", "op2", "op3", "op4", "op5", "coll1", "coll2", "coll3", "coll4", "coll5", "numbers1", "numbers2", "numbers3", "numbers4", "numbers5", "values1", "values2", "values3", "values4", "values5")
    return digest([dict(zip(keys, row, strict=True)) for row in rows])


def estimate_vector_digest(state: Any) -> str:
    return digest([{"query_id": str(item.query_id), "estimate": item.estimate, "truth": item.truth, "contribution": item.contribution, "provenance": item.provenance} for item in state.query_evaluations])


def candidate_identity(candidate: Any) -> dict[str, Any]:
    return {"candidate_id": str(candidate.candidate_id), "relation_name": candidate.relation_name, "mechanism": candidate.mechanism.value, "attributes": list(candidate.attributes), "definition": dict(candidate.definition), "precedence_rank": candidate.precedence_rank}


def design_digest(ids: list[str] | tuple[str, ...], candidates_by_id: dict[Any, Any]) -> str:
    return digest([candidate_identity(candidates_by_id[CandidateId(cid)]) for cid in ids])


def sample_manifest(dataset: dict[str, Any], sample_info: dict[str, Any], sample_dir: Path) -> dict[str, Any]:
    binary = sample_dir / "sample.copy.bin"
    return {
        "format_version": 1, "dataset_id": dataset["dataset_id"], "target_relation": TARGET,
        "sample_relation": SAMPLE_REL, "sample_file": "sample.copy.bin", "sample_file_sha256": hashlib.sha256(binary.read_bytes()).hexdigest(),
        "semantic_sha256": sample_info["semantic_sha256"], "row_count": sample_info["row_count"], "source_relation_row_count": sample_info["source_relation_row_count"],
        "totalrows_used_by_builder": sample_info["totalrows_used_by_builder"], "ordered_column_schema": dataset["ordered_column_schema"],
        "schema_signature": source_schema_signature(dataset), "source_logical_fingerprint": dataset["logical_relation_fingerprint"],
        "statistics_target": 100, "sampling_algorithm": "PostgreSQL 16.14 native block sampler + Vitter reservoir; rows sorted by physical TID",
        "capture_method": "native capture mode from canonical full relation, captured once",
        "postgres_version": "16.14", "upstream_tarball_sha256": UPSTREAM_SHA, "patch_sha256": EXPECTED_PATCH,
        "build_input_digest": json.loads(BUILD_ENV.read_text())["build_recipe_digest"], "relation_identity": TARGET,
        "captured_at": sample_info["captured_at"],
    }


def configure_acquisition(sample_name: str) -> tuple[Path, Path]:
    sample_dir = DATASETS / "dmv-frozen-acquisition-sample-v1" if sample_name == "A" else DATASETS / f"dmv-frozen-acquisition-sample-v2-{sample_name.lower()}"
    sample_out = OUT / f"sample-{sample_name.lower()}-acquisition"
    acquisition.SAMPLE = sample_dir
    acquisition.OUT = sample_out
    sample_dir.mkdir(parents=True, exist_ok=True)
    sample_out.mkdir(parents=True, exist_ok=True)
    return sample_dir, sample_out


def capture_new_sample(sample_name: str, prepared: Any, dataset: dict[str, Any], metadata: Any) -> dict[str, Any]:
    sample_dir, sample_out = configure_acquisition(sample_name)
    if (sample_dir / "sample.copy.bin").exists() or (sample_out / "build-1").exists():
        raise RuntimeError(f"refusing to overwrite sample {sample_name}")
    with psycopg.connect(DSN) as conn:
        acquisition.ensure_sample_table(conn)
        acquisition.drop_acquisition_stats(conn)
        result, info, build1 = acquisition.capture_sample(conn, prepared, metadata)
        acquisition.cleanup_acquisition(conn, result)
        conn.execute(f"DROP TABLE IF EXISTS {SAMPLE_REL}")
        conn.execute("SELECT set_config(%s,%s,false)", ("pg_extstats.frozen_sample_mode", "off"))
        conn.commit()
    manifest = sample_manifest(dataset, info, sample_dir)
    (sample_dir / "manifest.json").write_text(json.dumps(manifest, sort_keys=True, indent=2) + "\n")
    write_json(sample_out / "build-1" / "summary.json", build1)
    return {"name": sample_name, "sample_dir": str(sample_dir), "output_dir": str(sample_out), "manifest": manifest, "build1": build1}


def load_existing_sample(sample_name: str) -> dict[str, Any]:
    sample_dir, sample_out = configure_acquisition(sample_name)
    manifest_path = sample_dir / "manifest.json"
    summary_path = sample_out / "build-1" / "summary.json"
    if not (sample_dir / "sample.copy.bin").exists() or not manifest_path.exists() or not summary_path.exists():
        raise RuntimeError(f"persisted sample {sample_name} is incomplete")
    return {"name": sample_name, "sample_dir": str(sample_dir), "output_dir": str(sample_out), "manifest": json.loads(manifest_path.read_text()), "build1": json.loads(summary_path.read_text())}


def replay_sample(sample: dict[str, Any], prepared: Any, metadata: Any, number: int) -> dict[str, Any]:
    sample_name = sample["name"]
    sample_dir, sample_out = configure_acquisition(sample_name)
    info = {
        "sample_relation": SAMPLE_REL, "target_relation": TARGET, "row_count": sample["manifest"]["row_count"],
        "source_relation_row_count": sample["manifest"]["source_relation_row_count"], "serialization_sha256": sample["manifest"]["sample_file_sha256"],
        "semantic_sha256": sample["manifest"]["semantic_sha256"], "statistics_target": 100,
        "totalrows_used_by_builder": sample["manifest"]["totalrows_used_by_builder"], "captured_at": sample["manifest"]["captured_at"],
    }
    with psycopg.connect(DSN) as conn:
        load_persisted_sample(conn, sample_dir)
        acquisition.drop_acquisition_stats(conn)
        result, build = acquisition.replay_build(conn, prepared, metadata, info, number)
        acquisition.cleanup_acquisition(conn, result)
        conn.execute(f"DROP TABLE IF EXISTS {SAMPLE_REL}")
        create_shells(conn, tuple(prepared.catalog.candidates))
        conn.execute("SELECT set_config(%s,%s,false)", ("pg_extstats.frozen_sample_mode", "off"))
        conn.commit()
        repo = PayloadRepository.load(sample_out / "build-1" / "repository")
        baseline = NativeEvaluator(prepared.workload, repo, prepared.incidence, PostgresAdapter(conn, repo)).evaluate_design(Design(()))
        build["ordinary_statistics_digest"] = ordinary_stats_digest(conn)
        build["baseline_estimate_vector_digest"] = estimate_vector_digest(baseline)
        build["baseline_objective"] = baseline.aggregate_objective
        clean_db(conn, tuple(prepared.catalog.candidates))
    write_json(sample_out / f"build-{number}" / "summary.json", build)
    return build


def repository_state_rows(repository: PayloadRepository) -> list[dict[str, Any]]:
    return [{"candidate_id": str(item.candidate.candidate_id), "mechanism": item.candidate.mechanism.value, "attributes": list(item.candidate.attributes), "state": item.state.value, "payload_sha256": item.payload_sha256, "payload_size": len(item.payload) if item.payload is not None else None} for item in repository.payloads]


def repository_semantic_digest(repository: PayloadRepository) -> str:
    """Digest payload semantics while excluding acquisition timestamps/OIDs."""
    return digest(sorted(repository_state_rows(repository), key=lambda row: row["candidate_id"]))


def load_persisted_sample(conn: psycopg.Connection[Any], sample_dir: Path) -> None:
    conn.execute(f"DROP TABLE IF EXISTS {SAMPLE_REL}")
    conn.execute(f"CREATE UNLOGGED TABLE {SAMPLE_REL} (LIKE {TARGET} INCLUDING DEFAULTS)")
    with conn.cursor().copy(f"COPY {SAMPLE_REL} FROM STDIN (FORMAT binary)") as copy:
        copy.write((sample_dir / "sample.copy.bin").read_bytes())
    conn.commit()


def profile_sample(sample: dict[str, Any], prepared: Any, repository: PayloadRepository) -> dict[str, Any]:
    sample_name = sample["name"]
    sample_dir, sample_out = configure_acquisition(sample_name)
    with psycopg.connect(DSN) as conn:
        load_persisted_sample(conn, sample_dir)
        acquisition.drop_acquisition_stats(conn)
        info = sample["manifest"]
        # Build physical definitions/payloads through replay before singleton evaluation.
        result, build = acquisition.replay_build(conn, prepared, METADATA, {
            "sample_relation": SAMPLE_REL, "target_relation": TARGET, "row_count": info["row_count"],
            "source_relation_row_count": info["source_relation_row_count"], "serialization_sha256": info["sample_file_sha256"],
            "semantic_sha256": info["semantic_sha256"], "statistics_target": 100, "totalrows_used_by_builder": info["totalrows_used_by_builder"], "captured_at": info["captured_at"],
        }, 5)
        acquisition.cleanup_acquisition(conn, result)
        conn.execute(f"DROP TABLE IF EXISTS {SAMPLE_REL}")
        create_shells(conn, tuple(prepared.catalog.candidates))
        conn.commit()
        repo_for_profile = PayloadRepository.load(sample_out / "build-1" / "repository")
        profile1 = singleton_tool.run_profile(prepared, repo_for_profile, DSN, "run-1")
        profile2 = singleton_tool.run_profile(prepared, repo_for_profile, DSN, "run-2-fresh-backend-session")
        semantic1 = [{key: value for key, value in row.items() if key != "elapsed_seconds"} for row in profile1["rows"]]
        semantic2 = [{key: value for key, value in row.items() if key != "elapsed_seconds"} for row in profile2["rows"]]
        if semantic1 != semantic2:
            raise RuntimeError(f"singleton replay nondeterministic for {sample_name}")
        clean_db(conn, tuple(prepared.catalog.candidates))
    profile_digest = digest(semantic1)
    profile = {"sample": sample_name, "acquisition_sample_digest": info["semantic_sha256"], "repository_digest": repo_for_profile.digest, "profile_digest": profile_digest, "run1_run2_exact": True, "baseline_objective": profile1["baseline_objective"], "baseline_estimate_vector_digest": profile1["baseline_estimate_vector_digest"], "realization_counts": dict(Counter(row["realization_state"] for row in profile1["rows"])), "candidates": profile1["rows"]}
    write_json(sample_out / "singleton-profile.json", profile)
    write_csv(sample_out / "singleton-results.csv", profile1["rows"], list(profile1["rows"][0]))
    sample["singleton"] = profile
    sample["repository"] = repo_for_profile
    sample["profile_build"] = build
    return sample


def prepare_search_state(sample: dict[str, Any], prepared: Any, candidates: tuple[Any, ...]) -> tuple[PayloadRepository, str, str, float]:
    sample_name = sample["name"]
    sample_dir, sample_out = configure_acquisition(sample_name)
    info = sample["manifest"]
    with psycopg.connect(DSN) as conn:
        load_persisted_sample(conn, sample_dir)
        acquisition.drop_acquisition_stats(conn)
        result, build = acquisition.replay_build(conn, prepared, METADATA, {
            "sample_relation": SAMPLE_REL, "target_relation": TARGET, "row_count": info["row_count"],
            "source_relation_row_count": info["source_relation_row_count"], "serialization_sha256": info["sample_file_sha256"],
            "semantic_sha256": info["semantic_sha256"], "statistics_target": 100, "totalrows_used_by_builder": info["totalrows_used_by_builder"], "captured_at": info["captured_at"],
        }, 7)
        repo = PayloadRepository.load(sample_out / "build-1" / "repository")
        build_base = build["ordinary_statistics_digest"]
        acquisition.cleanup_acquisition(conn, result)
        conn.execute(f"DROP TABLE IF EXISTS {SAMPLE_REL}")
        create_shells(conn, candidates)
        conn.commit()
        baseline = NativeEvaluator(prepared.workload, repo, prepared.incidence, PostgresAdapter(conn, repo)).evaluate_design(Design(()))
        build_base = ordinary_stats_digest(conn)
        build_vector = estimate_vector_digest(baseline)
        build_baseline = baseline.aggregate_objective
    return repo, build_base, build_vector, build_baseline


def compact_search_result(sample: dict[str, Any], run: dict[str, Any], singleton: dict[str, dict[str, Any]], model: Any, budget: Any, prepared: Any) -> dict[str, Any]:
    initial, final = run["initial"], run["final"]
    selected = [str(item) for item in final.design.candidate_ids]
    accepted = run["accepted"]
    singleton_signs = Counter("positive" if float(row["frozen_singleton_improvement"]) > 0 else "zero" if float(row["frozen_singleton_improvement"]) == 0 else "negative" for row in accepted)
    return {
        "sample": sample["name"], "acquisition_sample_digest": sample["manifest"]["semantic_sha256"], "status": "performance-ceiling" if run["timed_out"] else "complete", "termination_reason": "performance-ceiling" if run["timed_out"] else "add-local-optimum",
        "baseline_objective": initial.aggregate_objective, "final_objective": final.aggregate_objective, "absolute_improvement": initial.aggregate_objective - final.aggregate_objective,
        "relative_improvement": (initial.aggregate_objective - final.aggregate_objective) / initial.aggregate_objective, "baseline_vector_digest": search_tool.estimate_vector_digest(initial), "final_vector_digest": search_tool.estimate_vector_digest(final),
        "design_digest": design_digest(selected, sample["repository"].catalog.by_id), "selected_design": selected, "selected_count": len(selected), "mcv_count": sum(singleton[cid]["mechanism"] == "mcv" for cid in selected), "fd_count": sum(singleton[cid]["mechanism"] == "fd" for cid in selected),
        "selected_present_count": sum(singleton[cid]["realization_state"] == "PRESENT" for cid in selected), "selected_absent_native_count": sum(singleton[cid]["realization_state"] == "ABSENT_NATIVE" for cid in selected), "maintenance_cost": str(model.estimate_design(final.design, sample["repository"].catalog)),
        "budget": str(budget.value), "rounds": len(run["rounds"]), "elapsed_seconds": run["elapsed_seconds"], "planner_calls": run["adapter_planner_calls"], "native_evaluations": run["search"]._evaluated, "conceptual_moves": run["search"]._considered,
        "accepted_singleton_positive": singleton_signs["positive"], "accepted_singleton_zero": singleton_signs["zero"], "accepted_singleton_negative": singleton_signs["negative"], "accepted": accepted,
        "rounds_detail": [{key: row[key] for key in ("round", "accepted_candidate", "objective_after", "contextual_improvement", "cost_after")} for row in run["rounds"]],
        "lineage": {"repository_digest": repository_semantic_digest(sample["repository"]), "raw_repository_digest": sample["repository"].digest, "workload_digest": prepared.workload.digest, "effective_workload_digest": prepared.effective_workload_digest, "incidence_digest": prepared.incidence_digest},
    }


def compact_build_summary(build: dict[str, Any]) -> dict[str, Any]:
    """Persist replay lineage without duplicating per-query/statistic arrays."""
    keys = (
        "build_id", "mode", "sample", "elapsed_seconds", "repository_digest",
        "ordinary_statistics_digest", "baseline_estimate_vector_digest",
        "baseline_objective", "statistics_ext_count", "statistics_ext_data_count",
    )
    return {key: build[key] for key in keys if key in build}


def compact_sample_record(sample: dict[str, Any]) -> dict[str, Any]:
    """Keep the manifest and semantic lineage, not a second trajectory copy."""
    record = {
        "name": sample["name"],
        "manifest": sample["manifest"],
        "repository_path": sample.get("repository_path"),
        "build1": compact_build_summary(sample["build1"]),
        "singleton": sample.get("singleton"),
        "final": sample.get("final"),
    }
    for key in ("replay2", "replay3", "profile_build"):
        if key in sample:
            record[key] = compact_build_summary(sample[key])
    return record


def rank_map(rows: list[dict[str, Any]]) -> dict[str, float]:
    ordered = sorted(rows, key=lambda row: float(row["singleton_improvement"]))
    return {row["candidate_id"]: float(index + 1) for index, row in enumerate(ordered)}


def spearman(left: dict[str, float], right: dict[str, float]) -> float:
    keys = sorted(left)
    lx = [left[key] for key in keys]; rx = [right[key] for key in keys]
    mx = statistics.fmean(lx); my = statistics.fmean(rx)
    num = sum((a - mx) * (b - my) for a, b in zip(lx, rx, strict=True))
    den = math.sqrt(sum((a - mx) ** 2 for a in lx) * sum((b - my) ** 2 for b in rx))
    return num / den if den else 0.0


def evaluate_cross_matrix(samples: dict[str, dict[str, Any]], designs: dict[str, dict[str, Any]], prepared: Any, candidates: tuple[Any, ...]) -> tuple[list[dict[str, Any]], dict[str, dict[str, Any]]]:
    matrix: list[dict[str, Any]] = []
    baseline_by_sample: dict[str, dict[str, Any]] = {}
    for eval_name in SAMPLES:
        sample = samples[eval_name]; sample_dir, sample_out = configure_acquisition(eval_name); info = sample["manifest"]
        with psycopg.connect(DSN) as conn:
            load_persisted_sample(conn, sample_dir); acquisition.drop_acquisition_stats(conn)
            result, build = acquisition.replay_build(conn, prepared, METADATA, {
                "sample_relation": SAMPLE_REL, "target_relation": TARGET, "row_count": info["row_count"], "source_relation_row_count": info["source_relation_row_count"], "serialization_sha256": info["sample_file_sha256"], "semantic_sha256": info["semantic_sha256"], "statistics_target": 100, "totalrows_used_by_builder": info["totalrows_used_by_builder"], "captured_at": info.get("captured_at", "historical-sample-a"),
            }, 8)
            repo = sample["repository"] if eval_name == "A" else PayloadRepository.load(sample_out / "build-1" / "repository")
            replay_repo = PayloadRepository.load(sample_out / "build-8" / "repository")
            if repository_semantic_digest(replay_repo) != repository_semantic_digest(repo):
                raise RuntimeError(f"cross-evaluation repository mismatch for {eval_name}")
            base_digest = build["ordinary_statistics_digest"]
            acquisition.cleanup_acquisition(conn, result); conn.execute(f"DROP TABLE IF EXISTS {SAMPLE_REL}"); create_shells(conn, candidates); conn.commit()
            evaluator = NativeEvaluator(prepared.workload, repo, prepared.incidence, PostgresAdapter(conn, repo))
            baseline = evaluator.evaluate_design(Design(()))
            base_digest = ordinary_stats_digest(conn)
            baseline_by_sample[eval_name] = {"objective": baseline.aggregate_objective, "vector_digest": estimate_vector_digest(baseline), "ordinary_statistics_digest": base_digest}
            for source_name in SAMPLES:
                state = evaluator.evaluate_design(Design(tuple(CandidateId(cid) for cid in designs[source_name]["selected_design"])))
                matrix.append({"design_source_sample": source_name, "evaluation_sample": eval_name, "design_source_sample_digest": samples[source_name]["manifest"]["semantic_sha256"], "evaluation_sample_digest": sample["manifest"]["semantic_sha256"], "objective": state.aggregate_objective, "vector_digest": estimate_vector_digest(state), "diagonal": source_name == eval_name})
            clean_db(conn, candidates)
    return matrix, baseline_by_sample


def main() -> int:
    resume = len(sys.argv) == 2 and sys.argv[1] == "--resume"
    if OUT.exists() and not resume:
        raise RuntimeError(f"refusing to overwrite {OUT}")
    expected_head = "edae7732a26279156415c3395b65f2435f3e1e8c"
    import subprocess
    head = subprocess.run(["git", "rev-parse", "HEAD"], cwd=ROOT, check=True, capture_output=True, text=True).stdout.strip()
    if head != expected_head:
        raise RuntimeError(f"M2.20 requires HEAD {expected_head}, got {head}")
    prepared = load_prepared_run(PREPARED_ROOT); dataset = json.loads(DATASET_PATH.read_text()); metadata = acquisition.relation_metadata(dataset)
    global METADATA
    METADATA = metadata
    all_candidates = tuple(prepared.catalog.candidates)
    if len(all_candidates) != 72 or sum(c.mechanism.value == "mcv" for c in all_candidates) != 36 or sum(c.mechanism.value == "fd" for c in all_candidates) != 36:
        raise RuntimeError(f"candidate universe mismatch: {EXPECTED_CATALOG}")
    model = EmpiricalMechanismCountCostModel.load(MODEL_PATH)
    if model.digest != EXPECTED_MODEL:
        raise RuntimeError("maintenance model digest mismatch")
    OUT.mkdir(parents=True, exist_ok=True)
    samples: dict[str, dict[str, Any]] = {}
    a_manifest = json.loads((ROOT / "datasets/dmv-frozen-acquisition-sample-v1/manifest.json").read_text()); a_repo_path = ROOT / "experiments/dmv-m2-17b-frozen-acquisition-sample/build-1/repository"; a_repo = PayloadRepository.load(a_repo_path); a_build = json.loads((ROOT / "experiments/dmv-m2-17b-frozen-acquisition-sample/build-1/summary.json").read_text()); a_profile = json.loads((ROOT / "experiments/dmv-m2-17c-frozen-singletons/singleton-profile.json").read_text()); a_final = json.loads((ROOT / "experiments/dmv-m2-17d-frozen-full72-add/final-result.json").read_text())
    if a_manifest["semantic_sha256"] != EXPECTED_SAMPLE_A or a_repo.digest != EXPECTED_REPOSITORY_A or a_final["selected_count"] != 31:
        raise RuntimeError("authoritative A lineage mismatch")
    a_signs = Counter("positive" if float(row["frozen_singleton_improvement"]) > 0 else "zero" if float(row["frozen_singleton_improvement"]) == 0 else "negative" for row in a_final["accepted_sequence"])
    samples["A"] = {"name": "A", "manifest": a_manifest, "build1": a_build, "repository": a_repo, "repository_path": str(a_repo_path), "singleton": a_profile, "final": {"selected_design": a_final["selected_design"], "design_digest": EXPECTED_DESIGN_A, "baseline_objective": a_final["baseline_objective"], "final_objective": a_final["final_objective"], "relative_improvement": a_final["relative_improvement"], "selected_count": 31, "mcv_count": a_final["selected_mcv_count"], "fd_count": a_final["selected_fd_count"], "selected_present_count": a_final["selected_present_count"], "selected_absent_native_count": a_final["selected_absent_native_count"], "maintenance_cost": a_final["selected_maintenance_cost"], "rounds": a_final["round_count_including_final_no_improvement"], "elapsed_seconds": a_final["elapsed_seconds"], "accepted": a_final["accepted_sequence"], "accepted_singleton_positive": a_signs["positive"], "accepted_singleton_zero": a_signs["zero"], "accepted_singleton_negative": a_signs["negative"]}}
    for name in NEW_SAMPLES:
        sample_dir = DATASETS / f"dmv-frozen-acquisition-sample-v2-{name.lower()}"
        if resume and sample_dir.exists():
            samples[name] = load_existing_sample(name)
        else:
            samples[name] = capture_new_sample(name, prepared, dataset, metadata)
    for name in NEW_SAMPLES:
        sample = samples[name]
        b2 = replay_sample(sample, prepared, metadata, 2); b3 = replay_sample(sample, prepared, metadata, 3)
        repo1 = PayloadRepository.load(Path(sample["output_dir"]) / "build-1" / "repository"); repo2 = PayloadRepository.load(Path(sample["output_dir"]) / "build-2" / "repository"); repo3 = PayloadRepository.load(Path(sample["output_dir"]) / "build-3" / "repository")
        if not (repository_semantic_digest(repo1) == repository_semantic_digest(repo2) == repository_semantic_digest(repo3) and b2["ordinary_statistics_digest"] == b3["ordinary_statistics_digest"] and b2["baseline_estimate_vector_digest"] == b3["baseline_estimate_vector_digest"] and b2["baseline_objective"] == b3["baseline_objective"]):
            raise RuntimeError(f"replay determinism failed for {name}")
        sample["repository"] = repo1; sample["repository_path"] = str(Path(sample["output_dir"]) / "build-1" / "repository"); sample["build1"] = json.loads((Path(sample["output_dir"]) / "build-1" / "summary.json").read_text()); sample["replay2"] = b2; sample["replay3"] = b3
        sample = profile_sample(sample, prepared, repo1); samples[name] = sample
    # Ensure the persisted A profile is included with the same candidate schema.
    for name in SAMPLES:
        if len(samples[name]["repository"].catalog.candidates) != 72:
            raise RuntimeError(f"incomplete repository for {name}")
    catalog_digest = digest([candidate_identity(c) for c in all_candidates]); model_budget = MaintenanceBudget(sum((model.estimate_candidate(c) for c in all_candidates), start=__import__("decimal").Decimal(0)), model.unit)
    designs: dict[str, dict[str, Any]] = {"A": samples["A"]["final"]}
    for name in NEW_SAMPLES:
        existing_search = OUT / f"sample-{name.lower()}-search" / "search.json"
        if resume and existing_search.exists():
            result = json.loads(existing_search.read_text())
            samples[name]["final"] = result
            designs[name] = result
            continue
        repo, base_digest, base_vector, base_objective = prepare_search_state(samples[name], prepared, all_candidates)
        singleton = {row["candidate_id"]: row for row in samples[name]["singleton"]["candidates"]}
        search_tool.EXPECTED_BASELINE = base_objective; search_tool.EXPECTED_VECTOR_DIGEST = base_vector; search_tool.EXPECTED_REPOSITORY_DIGEST = repo.digest; search_tool.OUT = OUT / f"sample-{name.lower()}-search"; search_tool.OUT.mkdir(parents=True, exist_ok=True)
        run1 = search_tool.run_once(prepared, repo, model, model_budget, singleton, f"{name}-run-1", False)
        if run1["timed_out"]:
            raise RuntimeError(f"search ceiling exceeded for {name}")
        repeat = None
        if name == "B":
            repeat = search_tool.run_once(prepared, repo, model, model_budget, singleton, f"{name}-run-2", False)
            if search_tool.state_semantics(run1) != search_tool.state_semantics(repeat):
                raise RuntimeError("B search repeatability failed")
        result = compact_search_result(samples[name], run1, singleton, model, model_budget, prepared); result["baseline_statistics_digest"] = base_digest; result["maintenance_model_digest"] = model.digest; result["candidate_catalog_digest"] = catalog_digest; result["repeat_exact"] = repeat is None or search_tool.state_semantics(run1) == search_tool.state_semantics(repeat)
        write_json(search_tool.OUT / "search.json", result)
        write_csv(search_tool.OUT / "accepted-moves.csv", run1["accepted"], list(run1["accepted"][0]) if run1["accepted"] else ["accepted_round"])
        samples[name]["final"] = result; samples[name]["search_repository"] = repo
        clean = None
        with psycopg.connect(DSN) as conn:
            clean_db(conn, all_candidates); clean = {"residual_statistics": stat_counts(conn)[0], "residual_data_rows": stat_counts(conn)[1], "row_count": row_count(conn)}
        write_json(search_tool.OUT / "cleanup.json", clean)
    # The persisted search lineages are now all available for matrix evaluation.
    matrix_path = OUT / "cross-evaluation-matrix.csv"
    if resume and matrix_path.exists():
        matrix = list(csv.DictReader(matrix_path.open()))
        for row in matrix:
            row["objective"] = float(row["objective"])
            row["diagonal"] = row["diagonal"] == "True"
        baselines = {name: {"objective": designs[name]["baseline_objective"], "vector_digest": designs[name].get("baseline_vector_digest", ""), "ordinary_statistics_digest": ""} for name in SAMPLES}
    else:
        matrix, baselines = evaluate_cross_matrix(samples, designs, prepared, all_candidates)
    for row in matrix:
        if row["diagonal"]:
            expected = designs[row["design_source_sample"]]["final_objective"]
            if not math.isclose(row["objective"], expected, rel_tol=0, abs_tol=1e-9):
                raise RuntimeError(f"diagonal mismatch for {row['design_source_sample']}")
    write_csv(OUT / "cross-evaluation-matrix.csv", matrix, list(matrix[0]))
    pairwise = []; design_sets = {name: set(designs[name]["selected_design"]) for name in SAMPLES}
    for i, left in enumerate(SAMPLES):
        for right in SAMPLES[i + 1:]:
            inter = design_sets[left] & design_sets[right]; union = design_sets[left] | design_sets[right]
            pairwise.append({"left": left, "right": right, "intersection": len(inter), "union": len(union), "jaccard": len(inter) / len(union), "mcv_intersection": len({cid for cid in inter if samples[left]["repository"].catalog.by_id[CandidateId(cid)].mechanism.value == "mcv"}), "fd_intersection": len({cid for cid in inter if samples[left]["repository"].catalog.by_id[CandidateId(cid)].mechanism.value == "fd"})})
    write_csv(OUT / "pairwise-design-overlap.csv", pairwise, list(pairwise[0]))
    membership = []
    selected_freq = Counter(cid for ids in design_sets.values() for cid in ids)
    for candidate in all_candidates:
        cid = str(candidate.candidate_id); membership.append({"candidate_id": cid, "mechanism": candidate.mechanism.value, "columns": json.dumps(list(candidate.attributes), separators=(",", ":")), "selected_frequency": selected_freq[cid], "selected": ";".join(name for name in SAMPLES if cid in design_sets[name]), "accepted_rounds": ";".join(f"{name}:{next((r['accepted_round'] for r in designs[name].get('accepted', []) if r['candidate_id'] == cid), '')}" for name in SAMPLES), "singleton_positive_frequency": sum(float(next(row["singleton_improvement"] for row in samples[name]["singleton"]["candidates"] if row["candidate_id"] == cid)) > 0 for name in SAMPLES), "present_frequency": sum(next(row["realization_state"] for row in samples[name]["singleton"]["candidates"] if row["candidate_id"] == cid) == "PRESENT" for name in SAMPLES), "absent_frequency": sum(next(row["realization_state"] for row in samples[name]["singleton"]["candidates"] if row["candidate_id"] == cid) == "ABSENT_NATIVE" for name in SAMPLES)})
    write_csv(OUT / "design-membership.csv", membership, list(membership[0]))
    write_csv(OUT / "candidate-frequency.csv", membership, list(membership[0]))
    profile_names = {name: rank_map(samples[name]["singleton"]["candidates"]) for name in SAMPLES}; rank_rows = []
    for i, left in enumerate(SAMPLES):
        for right in SAMPLES[i + 1:]:
            left_top = [cid for cid, _ in sorted(profile_names[left].items(), key=lambda item: item[1])]; right_top = [cid for cid, _ in sorted(profile_names[right].items(), key=lambda item: item[1])]
            rank_rows.append({"left": left, "right": right, "spearman": spearman(profile_names[left], profile_names[right]), "top5_overlap": len(set(left_top[:5]) & set(right_top[:5])), "top10_overlap": len(set(left_top[:10]) & set(right_top[:10])), "top20_overlap": len(set(left_top[:20]) & set(right_top[:20]))})
    write_csv(OUT / "singleton-rank-stability.csv", rank_rows, list(rank_rows[0]))
    portability = []
    for source in SAMPLES:
        vals = [next(row["objective"] for row in matrix if row["design_source_sample"] == source and row["evaluation_sample"] == target) for target in SAMPLES]
        rels = [(baselines[target]["objective"] - vals[j]) / baselines[target]["objective"] for j, target in enumerate(SAMPLES)]
        gaps = [vals[j] - designs[target]["final_objective"] for j, target in enumerate(SAMPLES)]
        portability.append({"design_source_sample": source, "mean_objective": statistics.fmean(vals), "median_objective": statistics.median(vals), "worst_objective": max(vals), "mean_relative_improvement": statistics.fmean(rels), "minimum_relative_improvement": min(rels), "mean_relative_gap": statistics.fmean((gaps[j] / designs[target]["final_objective"] for j, target in enumerate(SAMPLES))), "maximum_relative_gap": max(gaps[j] / designs[target]["final_objective"] for j, target in enumerate(SAMPLES))})
    write_csv(OUT / "cross-objective-gaps.csv", [{"evaluation_sample": target, "local_design_objective": designs[target]["final_objective"], "best_foreign_objective": min(row["objective"] for row in matrix if row["evaluation_sample"] == target and row["design_source_sample"] != target), "worst_foreign_objective": max(row["objective"] for row in matrix if row["evaluation_sample"] == target and row["design_source_sample"] != target), "max_relative_gap": max((row["objective"] - designs[target]["final_objective"]) / designs[target]["final_objective"] for row in matrix if row["evaluation_sample"] == target and row["design_source_sample"] != target), "foreign_beats_local": any(row["objective"] < designs[target]["final_objective"] for row in matrix if row["evaluation_sample"] == target and row["design_source_sample"] != target)} for target in SAMPLES], ["evaluation_sample", "local_design_objective", "best_foreign_objective", "worst_foreign_objective", "max_relative_gap", "foreign_beats_local"])
    write_csv(OUT / "cross-sample-portability.csv", portability, list(portability[0]))
    singleton_sign_flips = [cid for cid in [str(c.candidate_id) for c in all_candidates] if len({"positive" if float(next(row["singleton_improvement"] for row in samples[name]["singleton"]["candidates"] if row["candidate_id"] == cid)) > 0 else "zero" if float(next(row["singleton_improvement"] for row in samples[name]["singleton"]["candidates"] if row["candidate_id"] == cid)) == 0 else "negative" for name in SAMPLES}) > 1]
    interaction = []
    for name in SAMPLES:
        accepted = designs[name].get("accepted", [])
        rescued = [row["candidate_id"] for row in accepted if float(row["frozen_singleton_improvement"]) <= 0 and float(row["contextual_improvement"]) > 0]
        interaction.append({"sample": name, "accepted_singleton_positive": designs[name].get("accepted_singleton_positive", 0), "accepted_singleton_zero": designs[name].get("accepted_singleton_zero", 0), "accepted_singleton_negative": designs[name].get("accepted_singleton_negative", 0), "contextual_rescue_count": len(rescued), "rescued_candidates": ";".join(rescued)})
    write_csv(OUT / "interaction-evidence.csv", interaction, list(interaction[0]))
    frequency_counts = Counter(row["selected_frequency"] for row in membership)
    summary = {"status": "complete", "sample_count": 5, "sample_names": list(SAMPLES), "design_digests": {name: designs[name]["design_digest"] for name in SAMPLES}, "design_overlap": {"jaccard_min": min(row["jaccard"] for row in pairwise), "jaccard_median": statistics.median(row["jaccard"] for row in pairwise), "jaccard_max": max(row["jaccard"] for row in pairwise)}, "selected_frequency_histogram": dict(frequency_counts), "consensus_core": [cid for cid, count in selected_freq.items() if count == 5], "near_core": [cid for cid, count in selected_freq.items() if count >= 4], "singleton_rank": {"spearman_min": min(row["spearman"] for row in rank_rows), "spearman_median": statistics.median(row["spearman"] for row in rank_rows), "spearman_max": max(row["spearman"] for row in rank_rows), "top5_min": min(row["top5_overlap"] for row in rank_rows), "top5_max": max(row["top5_overlap"] for row in rank_rows), "top10_min": min(row["top10_overlap"] for row in rank_rows), "top10_max": max(row["top10_overlap"] for row in rank_rows), "top20_min": min(row["top20_overlap"] for row in rank_rows), "top20_max": max(row["top20_overlap"] for row in rank_rows), "sign_flip_count": len(singleton_sign_flips), "sign_flip_candidates": singleton_sign_flips}, "realization_state": {"always_present": sum(row["present_frequency"] == 5 for row in membership), "always_absent": sum(row["absent_frequency"] == 5 for row in membership), "state_flipped": sum(row["present_frequency"] > 0 and row["absent_frequency"] > 0 for row in membership)}, "interaction": interaction, "cross_evaluation_complete": len(matrix) == 25, "diagonal_exact": True, "interpretation": "high design stability" if min(row["jaccard"] for row in pairwise) >= 0.8 and max(row["maximum_relative_gap"] for row in portability) < 0.1 else "multiple near-equivalent designs" if max(row["maximum_relative_gap"] for row in portability) < 0.1 else "material sample-sensitive design selection"}
    write_json(OUT / "summary.json", summary)
    write_json(OUT / "samples.json", {name: compact_sample_record(sample) for name, sample in samples.items()})
    protocol = {"milestone": "M2.20", "status": "complete", "sample_names": list(SAMPLES), "new_persisted_samples": 4, "candidate_catalog_digest": catalog_digest, "maintenance_model_digest": model.digest, "workload_digest": prepared.workload.digest, "effective_workload_digest": prepared.effective_workload_digest, "statistics_target": 100, "full_catalog_search": True, "search_mode": "deterministic-best-improvement-ADD-only", "screening": False, "drop_swap": False, "search_repeat_sample": "B", "cross_matrix_cells": 25, "diagonal_exact": True, "fresh_unpersisted_authoritative_results": False, "data_drift": False, "workload_drift": False, "patch_sha256": EXPECTED_PATCH, "upstream_tarball_sha256": UPSTREAM_SHA, "build_input_digest": json.loads(BUILD_ENV.read_text())["build_recipe_digest"], "source_logical_fingerprint": dataset["logical_relation_fingerprint"], "schema_signature": source_schema_signature(dataset)}
    write_json(OUT / "protocol.json", protocol)
    report = ["# M2.20 — DMV persisted multi-sample design-selection stability", "", "Five persisted acquisition worlds (A–E) share the canonical DMV relation, workload/truth, candidate universe, maintenance model, and PostgreSQL build. A is the existing authoritative frozen sample; B–E were independently captured once and then used only through persisted replay.", "", "Each new sample passed two clean replay gates for ordinary statistics, repository payloads, baseline vector, and baseline objective. Each of B–E then ran the complete 72-candidate deterministic ADD-only search from its own baseline; B was repeated exactly. The 5×5 matrix used hypothetical replay with each evaluation sample's own ordinary statistics and payload repository.", "", f"Design Jaccard ranged from {summary['design_overlap']['jaccard_min']:.4f} to {summary['design_overlap']['jaccard_max']:.4f}. Cross-sample evaluation and local-design gaps are in the CSV artifacts; the combined interpretation is **{summary['interpretation']}**.", "", "No sample screening, DROP/SWAP search, maintenance refit, data drift, workload drift, or PostgreSQL patch change was performed."]
    (OUT / "report.md").write_text("\n".join(report) + "\n")
    with psycopg.connect(DSN) as conn:
        clean_db(conn, all_candidates)
        if row_count(conn) != EXPECTED_ROWS or stat_counts(conn) != (0, 0):
            raise RuntimeError("final cleanup integrity failure")
    print(json.dumps({"status": "complete", "interpretation": summary["interpretation"], "design_jaccard": summary["design_overlap"], "diagonal_exact": True, "cross_cells": 25}, sort_keys=True, indent=2))
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
