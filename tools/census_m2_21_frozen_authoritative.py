"""Build the authoritative Census frozen-sample lineage (M2.21).

The script is deliberately phase-gated.  Capture/replay must pass before the
expensive singleton, screening, search, and physical-validation phases are
allowed to run.  All phases consume the one persisted sample and never fall
back to a fresh ANALYZE sample.
"""

from __future__ import annotations

import argparse
import csv
import hashlib
import json
import shutil
import subprocess
import tempfile
import time
from collections import Counter
from dataclasses import replace
from datetime import UTC, datetime
from pathlib import Path
from typing import Any

import psycopg

from pg_extstats_advisor.candidates.model import CandidateCatalog
from pg_extstats_advisor.cost.model import MaintenanceBudget
from pg_extstats_advisor.evaluator.native import NativeEvaluator
from pg_extstats_advisor.models import (
    CandidateId,
    Design,
    EvaluationState,
    Move,
    QueryEvaluation,
    QueryId,
)
from pg_extstats_advisor.objective.qerror import aggregate_objective, q_error
from pg_extstats_advisor.orchestration import load_maintenance_model, load_prepared_run
from pg_extstats_advisor.payloads.cache import (
    repository_semantic_digest as cached_repository_semantic_digest,
)
from pg_extstats_advisor.payloads.cache import (
    resolve_or_build_repository,
)
from pg_extstats_advisor.payloads.repository import NativePayloadState, PayloadRepository
from pg_extstats_advisor.postgres.adapter import PostgresAdapter
from pg_extstats_advisor.postgres.extraction import extract_target_estimate
from pg_extstats_advisor.prepare.acquisition import acquire_payloads, cleanup_acquisition
from pg_extstats_advisor.prepare.workload import RelationMetadata
from pg_extstats_advisor.screening import build_candidate_set, build_singleton_profile_from_rows
from pg_extstats_advisor.search.deterministic import DeterministicBudgetSearch
from pg_extstats_advisor.search.model import SearchConfig

ROOT = Path(__file__).resolve().parents[1]
PREPARED_ROOT = ROOT / "experiments/census-m2-21-frozen-authoritative/inputs"
DATASET_PATH = ROOT / "experiments/environment/census-dataset.json"
BUILD_ENV_PATH = ROOT / "experiments/environment/postgresql-16.14-build.json"
SOURCE_CSV = ROOT / ".build/datasets/census-climate.csv"
WORKLOAD_SOURCE = Path("/root/projects/extended-stats-optim-v2/benchmarks/Census/queries/query.sql")
OUT = ROOT / "experiments/census-m2-21-frozen-authoritative"
CACHE_ROOT = ROOT / ".build/artifact-cache"
SAMPLE_ROOT = ROOT / "datasets/census-frozen-acquisition-sample-v1"
SAMPLE_REL = "public.pgextadv_census_frozen_sample"
TARGET = "public.climate"
DSN = f"host={ROOT}/.build/pg16.14-experiment-socket port=55436 dbname=pgextadv_exp16_census user=postgres"
UPSTREAM_SHA = "f6d077142737920858ce958ccdb75c6ee137a63b5b0853c70693d401ac7e3471"
PATCH_SHA = "22c7f48632585e81fd8a557dc8bffba873ac5da070aca31713e22c60261c3b4f"
DATASET_SHA = "3be576490f4dc1cae9fc6c04a23f27633b0b09fac00bd3d0a63d011958252e33"
EXPECTED_SCHEMA = "f6785ab2df1c169b897cdea4b8c5ba4990d75a5964d5db116ece9578a5a38e77"
EXPECTED_ROWS = 2_458_285
EXPECTED_QUERIES = 468
EXPECTED_CANDIDATES = 4506
EXPECTED_CATALOG = "74727a871b3a601977885cbf96d25cda6e5ac38ef448d1815561bdc5988a425a"
EXPECTED_INCIDENCE = "0cd8466f654e21080914cf8444ad3ce3a093d9a7700d370b5ac6a4b9a28f66b4"
EXPECTED_WORKLOAD = "796ab606e825969ad91801c9795cd830ef40686568feef857d856921a8d42215"
EXPECTED_MODEL = "dbad23611afa778a7ff6aab6ad39e519f7bfc51387874aa07dd33e37a5d3a11e"


def digest(value: Any) -> str:
    return hashlib.sha256(json.dumps(value, sort_keys=True, separators=(",", ":"), default=str).encode()).hexdigest()


def file_digest(path: Path) -> str:
    return hashlib.sha256(path.read_bytes()).hexdigest()


def write_json(path: Path, value: Any) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text(json.dumps(value, sort_keys=True, indent=2, default=str) + "\n")


def write_csv(path: Path, rows: list[dict[str, Any]], fields: list[str]) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    with path.open("w", newline="") as handle:
        writer = csv.DictWriter(handle, fieldnames=fields, lineterminator="\n")
        writer.writeheader()
        writer.writerows({field: row.get(field) for field in fields} for row in rows)


def git_head() -> str:
    return subprocess.check_output(["git", "rev-parse", "HEAD"], cwd=ROOT, text=True).strip()


def quote_ident(value: str) -> str:
    return '"' + value.replace('"', '""') + '"'


def load_inputs() -> tuple[Any, dict[str, Any], RelationMetadata, Any]:
    prepared = load_prepared_run(PREPARED_ROOT)
    dataset = json.loads(DATASET_PATH.read_text())
    model = load_maintenance_model(PREPARED_ROOT)
    if prepared.workload.digest != EXPECTED_WORKLOAD or len(prepared.workload.queries) != EXPECTED_QUERIES:
        raise RuntimeError("Census workload lineage mismatch")
    if prepared.candidate_catalog_digest != EXPECTED_CATALOG or len(prepared.catalog.candidates) != EXPECTED_CANDIDATES:
        raise RuntimeError("Census candidate catalog mismatch")
    if prepared.incidence_digest != EXPECTED_INCIDENCE:
        raise RuntimeError("Census incidence mismatch")
    if model.digest != EXPECTED_MODEL:
        raise RuntimeError("Census maintenance model mismatch")
    if SOURCE_CSV.exists() and file_digest(SOURCE_CSV) != DATASET_SHA:
        raise RuntimeError("canonical Census source digest mismatch")
    sample_manifest_path = SAMPLE_ROOT / "manifest.json"
    if sample_manifest_path.exists() and json.loads(sample_manifest_path.read_text()).get("source_csv_sha256") != DATASET_SHA:
        raise RuntimeError("frozen sample/source lineage mismatch")
    schema_digest = digest(dataset["ordered_column_schema"])
    if schema_digest != EXPECTED_SCHEMA:
        raise RuntimeError(f"dataset schema signature mismatch: {schema_digest}")
    columns = tuple((int(item["attnum"]), str(item["name"]), str(item["type"]), bool(item["not_null"])) for item in dataset["ordered_column_schema"])
    metadata = RelationMetadata("public", "climate", 0, columns)
    return prepared, dataset, metadata, model


def verify_database(conn: psycopg.Connection[Any], dataset: dict[str, Any]) -> dict[str, Any]:
    row_count = int(conn.execute(f"SELECT count(*) FROM {TARGET}").fetchone()[0])
    columns = conn.execute(
        "SELECT attnum,attname,atttypid::regtype::text,attnotnull FROM pg_attribute "
        "WHERE attrelid=%s::regclass AND attnum>0 AND NOT attisdropped ORDER BY attnum",
        (TARGET,),
    ).fetchall()
    actual_schema = digest([{"attnum": int(a), "name": str(b), "type": str(c), "not_null": bool(d)} for a, b, c, d in columns])
    relation = conn.execute(
        "SELECT c.relpersistence,c.relkind,pg_relation_size(c.oid),c.reltuples "
        "FROM pg_class c WHERE c.oid=%s::regclass", (TARGET,)
    ).fetchone()
    if row_count != EXPECTED_ROWS or actual_schema != EXPECTED_SCHEMA:
        raise RuntimeError(f"canonical Census relation mismatch rows={row_count} schema={actual_schema}")
    if relation is None or str(relation[0]) != "p" or str(relation[1]) != "r":
        raise RuntimeError(f"canonical Census relation persistence/kind mismatch: {relation}")
    return {
        "row_count": row_count,
        "schema_signature": actual_schema,
        "relpersistence": str(relation[0]),
        "relkind": str(relation[1]),
        "relation_size_bytes": int(relation[2]),
        "reltuples_before_capture": float(relation[3]),
        "source_csv_sha256": DATASET_SHA,
    }


def set_guc(conn: psycopg.Connection[Any], name: str, value: str) -> None:
    conn.execute("SELECT set_config(%s,%s,false)", (name, value))


def drop_acquisition_stats(conn: psycopg.Connection[Any]) -> None:
    rows = conn.execute(
        "SELECT n.nspname,e.stxname FROM pg_statistic_ext e "
        "JOIN pg_namespace n ON n.oid=e.stxnamespace WHERE e.stxname LIKE 'pgextadv_acq_%'"
    ).fetchall()
    for schema, name in rows:
        conn.execute(f"DROP STATISTICS IF EXISTS {quote_ident(str(schema))}.{quote_ident(str(name))}")
    conn.execute("SELECT pg_hypothetical_extstats_reset()")


def create_shells(conn: psycopg.Connection[Any], candidates: tuple[Any, ...]) -> None:
    for candidate in candidates:
        schema, relation = candidate.relation_name.split(".", 1)
        kind = "mcv" if candidate.mechanism.value == "mcv" else "dependencies"
        name = str(dict(candidate.definition)["statistics_name"])
        attrs = ", ".join(quote_ident(item) for item in candidate.attributes)
        qname = f"{quote_ident(schema)}.{quote_ident(name)}"
        conn.execute(f"CREATE STATISTICS {qname} ({kind}) ON {attrs} FROM {quote_ident(schema)}.{quote_ident(relation)}")
        conn.execute(f"ALTER STATISTICS {qname} SET STATISTICS 100")


def sample_bytes_and_rows(conn: psycopg.Connection[Any]) -> tuple[bytes, list[tuple[Any, ...]]]:
    raw = bytearray()
    with conn.cursor().copy(f"COPY {SAMPLE_REL} TO STDOUT (FORMAT binary)") as copy:
        for chunk in copy:
            raw.extend(chunk)
    rows = conn.execute(f"SELECT * FROM {SAMPLE_REL} ORDER BY ctid").fetchall()
    return bytes(raw), rows


def semantic_sample_digest(rows: list[tuple[Any, ...]], dataset: dict[str, Any]) -> str:
    h = hashlib.sha256()
    schema = [(int(c["attnum"]), str(c["name"]), str(c["type"]), bool(c["not_null"])) for c in dataset["ordered_column_schema"]]
    header = json.dumps({"schema": schema, "row_count": len(rows), "order": "ctid"}, sort_keys=True, separators=(",", ":")).encode()
    h.update(len(header).to_bytes(8, "big")); h.update(header)
    for row in rows:
        h.update(len(row).to_bytes(4, "big"))
        for value in row:
            if value is None:
                h.update(b"\x00")
            else:
                encoded = str(value).encode()
                h.update(b"\x01"); h.update(len(encoded).to_bytes(8, "big")); h.update(encoded)
    return h.hexdigest()


def ordinary_stats_digest(conn: psycopg.Connection[Any]) -> str:
    rows = conn.execute(
        "SELECT starelid::regclass::text,staattnum,stainherit,stanullfrac,stawidth,stadistinct,"
        "stakind1,stakind2,stakind3,stakind4,stakind5,"
        "staop1::oid::text,staop2::oid::text,staop3::oid::text,staop4::oid::text,staop5::oid::text,"
        "stacoll1::oid::text,stacoll2::oid::text,stacoll3::oid::text,stacoll4::oid::text,stacoll5::oid::text,"
        "stanumbers1::text,stanumbers2::text,stanumbers3::text,stanumbers4::text,stanumbers5::text,"
        "stavalues1::text,stavalues2::text,stavalues3::text,stavalues4::text,stavalues5::text "
        "FROM pg_statistic WHERE starelid=%s::regclass ORDER BY staattnum,stainherit", (TARGET,)
    ).fetchall()
    keys = ("relation", "attnum", "inherit", "nullfrac", "width", "distinct", "kind1", "kind2", "kind3", "kind4", "kind5", "op1", "op2", "op3", "op4", "op5", "coll1", "coll2", "coll3", "coll4", "coll5", "numbers1", "numbers2", "numbers3", "numbers4", "numbers5", "values1", "values2", "values3", "values4", "values5")
    return digest([dict(zip(keys, row, strict=True)) for row in rows])


def estimate_vector_digest(state: Any) -> str:
    return digest([{"query_id": str(item.query_id), "estimate": item.estimate, "truth": item.truth, "contribution": item.contribution, "provenance": item.provenance} for item in state.query_evaluations])


repository_semantic_digest = cached_repository_semantic_digest


def load_sample(conn: psycopg.Connection[Any], sample_path: Path) -> None:
    conn.execute(f"DROP TABLE IF EXISTS {SAMPLE_REL}")
    conn.execute(f"CREATE UNLOGGED TABLE {SAMPLE_REL} (LIKE {TARGET} INCLUDING DEFAULTS)")
    with conn.cursor().copy(f"COPY {SAMPLE_REL} FROM STDIN (FORMAT binary)") as copy:
        copy.write(sample_path.read_bytes())
    conn.commit()


def clean_db(conn: psycopg.Connection[Any], candidates: tuple[Any, ...]) -> None:
    conn.execute("SELECT pg_hypothetical_extstats_reset()")
    drop_acquisition_stats(conn)
    for candidate in candidates:
        schema, _ = candidate.relation_name.split(".", 1)
        name = str(dict(candidate.definition)["statistics_name"])
        conn.execute(f"DROP STATISTICS IF EXISTS {quote_ident(schema)}.{quote_ident(name)}")
    conn.execute(f"DROP TABLE IF EXISTS {SAMPLE_REL}")
    set_guc(conn, "pg_extstats.frozen_sample_mode", "off")
    set_guc(conn, "pg_extstats.frozen_sample_relation", "")
    set_guc(conn, "pg_extstats.frozen_totalrows", "0")
    conn.commit()


def resolve_census_repository(prepared: Any, manifest: dict[str, Any]) -> PayloadRepository:
    build_env = json.loads(BUILD_ENV_PATH.read_text())
    identity = {
        "schema_version": 1,
        "acquisition_sample_digest": manifest["semantic_sha256"],
        "sample_file_sha256": manifest["sample_file_sha256"],
        "relation_schema_digest": EXPECTED_SCHEMA,
        "source_relation_digest": DATASET_SHA,
        "candidate_catalog_digest": prepared.candidate_catalog_digest,
        "statistics_target": int(manifest["statistics_target"]),
        "postgres_version": manifest["postgres_version"],
        "upstream_tarball_sha256": UPSTREAM_SHA,
        "patch_sha256": PATCH_SHA,
        "postgres_binary_sha256": build_env["postgres_binary_sha256"],
        "build_input_digest": build_env["build_recipe_digest"],
        "acquisition_schema_version": 2,
    }

    def build(repository_path: Path) -> PayloadRepository:
        with psycopg.connect(DSN) as conn:
            clean_db(conn, prepared.catalog.candidates)
            load_sample(conn, SAMPLE_ROOT / "sample.copy.bin")
            set_guc(conn, "pg_extstats.frozen_sample_mode", "replay")
            set_guc(conn, "pg_extstats.frozen_sample_relation", SAMPLE_REL)
            set_guc(conn, "pg_extstats.frozen_totalrows", str(manifest["totalrows_used_by_builder"]))
            result = acquire_payloads(
                conn,
                prepared.catalog,
                repository_path,
                statistics_target=int(manifest["statistics_target"]),
                upstream_sha256=UPSTREAM_SHA,
                patch_commit=PATCH_SHA,
                repository_id="census-m2-21-frozen-sample-cache",
                source_relations=(RelationMetadata("public", "climate", 0, tuple((int(item["attnum"]), str(item["name"]), str(item["type"]), bool(item["not_null"])) for item in json.loads(DATASET_PATH.read_text())["ordered_column_schema"])),),
            )
            cleanup_acquisition(conn, result)
            conn.execute(f"DROP TABLE IF EXISTS {SAMPLE_REL}")
            clean_db(conn, prepared.catalog.candidates)
            return result.repository

    resolution = resolve_or_build_repository(
        CACHE_ROOT,
        "census-m2-21-frozen-sample-v1",
        identity=identity,
        expected_semantic_digest="7e42ba7dbeb9a0a3a2539b1d6e72ab3fa04c5db31e931a6bca3485181bf6df85",
        build_repository=build,
    )
    return resolution.repository


def authoritative_prepared(prepared: Any) -> tuple[Any, PayloadRepository, dict[str, Any]]:
    manifest = json.loads((SAMPLE_ROOT / "manifest.json").read_text())
    repository = resolve_census_repository(prepared, manifest)
    if len(repository.payloads) != EXPECTED_CANDIDATES:
        raise RuntimeError("authoritative repository candidate count mismatch")
    return replace(prepared, catalog=repository.catalog, repository=repository), repository, manifest


def prepare_replay_shell_state(conn: psycopg.Connection[Any], prepared: Any, repository: PayloadRepository, manifest: dict[str, Any]) -> tuple[str, Any, NativeEvaluator]:
    clean_db(conn, repository.catalog.candidates)
    load_sample(conn, SAMPLE_ROOT / "sample.copy.bin")
    set_guc(conn, "pg_extstats.frozen_sample_mode", "replay")
    set_guc(conn, "pg_extstats.frozen_sample_relation", SAMPLE_REL)
    set_guc(conn, "pg_extstats.frozen_totalrows", str(manifest["totalrows_used_by_builder"]))
    create_shells(conn, repository.catalog.candidates)
    conn.execute(f"ANALYZE {TARGET}")
    conn.commit()
    ordinary = ordinary_stats_digest(conn)
    for candidate in repository.catalog.candidates:
        schema, _ = candidate.relation_name.split(".", 1)
        name = str(dict(candidate.definition)["statistics_name"])
        conn.execute(f"DROP STATISTICS IF EXISTS {quote_ident(schema)}.{quote_ident(name)}")
    conn.execute(f"DROP TABLE IF EXISTS {SAMPLE_REL}")
    set_guc(conn, "pg_extstats.frozen_sample_mode", "off")
    set_guc(conn, "pg_extstats.frozen_sample_relation", "")
    conn.commit()
    create_shells(conn, repository.catalog.candidates)
    conn.commit()
    evaluator = NativeEvaluator(prepared.workload, repository, prepared.incidence, PostgresAdapter(conn, repository))
    return ordinary, evaluator.evaluate_design(Design(())), evaluator


def profile_once(prepared: Any, repository: PayloadRepository, manifest: dict[str, Any], model: Any, run_name: str) -> dict[str, Any]:
    with psycopg.connect(DSN) as conn:
        ordinary, baseline, evaluator = prepare_replay_shell_state(conn, prepared, repository, manifest)
        rows: list[dict[str, Any]] = []
        empty = Design(())
        for candidate in repository.catalog.candidates:
            before_calls = evaluator.adapter.planner_calls_total
            started = time.perf_counter()
            state = evaluator.evaluate_move(empty, Move.add_candidate(candidate.candidate_id), baseline)
            elapsed = time.perf_counter() - started
            base = baseline.by_query(); after = state.by_query()
            deltas = [base[qid].contribution - after[qid].contribution for qid in base]
            rows.append({
                "candidate_id": str(candidate.candidate_id), "precedence_rank": candidate.precedence_rank,
                "mechanism": candidate.mechanism.value, "relation": candidate.relation_name,
                "columns": list(candidate.attributes), "realization_state": repository.by_candidate[candidate.candidate_id].state.value,
                "payload_digest": repository.by_candidate[candidate.candidate_id].payload_sha256,
                "maintenance_cost_numeric": float(model.estimate_candidate(candidate)),
                "singleton_objective": state.aggregate_objective,
                "singleton_improvement": baseline.aggregate_objective - state.aggregate_objective,
                "relative_improvement": (baseline.aggregate_objective - state.aggregate_objective) / baseline.aggregate_objective,
                "affected_query_count": len(state.affected_query_ids), "improved_query_count": sum(x > 0 for x in deltas),
                "unchanged_query_count": sum(x == 0 for x in deltas), "worsened_query_count": sum(x < 0 for x in deltas),
                "planner_calls": evaluator.adapter.planner_calls_total - before_calls, "elapsed_seconds": elapsed,
            })
        profile = build_singleton_profile_from_rows(rows, prepared, model, baseline.aggregate_objective, evaluator_provenance={"mode": "native-frozen-sample", "run": run_name, "postgres_version": evaluator.adapter.postgres_version, "patch_sha256": PATCH_SHA, "native_singleton_evaluations": len(rows), "planner_calls": evaluator.adapter.planner_calls_total, "affected_query_replans": sum(int(row["affected_query_count"]) for row in rows), "total_singleton_elapsed_seconds": sum(float(row["elapsed_seconds"]) for row in rows), "ordinary_statistics_digest": ordinary, "acquisition_sample_digest": manifest["semantic_sha256"]})
        clean_db(conn, repository.catalog.candidates)
    return profile


def rank_summary(profile: dict[str, Any], prepared: Any, repository: PayloadRepository, model: Any) -> dict[str, Any]:
    rows = profile["candidates"]
    groups = {"all": rows, "mcv": [r for r in rows if r["mechanism"] == "mcv"], "fd": [r for r in rows if r["mechanism"] == "fd"], "PRESENT": [r for r in rows if r["realization_state"] == "PRESENT"], "ABSENT_NATIVE": [r for r in rows if r["realization_state"] == "ABSENT_NATIVE"]}
    def counts(values: list[dict[str, Any]]) -> dict[str, int]:
        return {kind: sum(float(row["singleton_improvement"]) > 0 if kind == "positive" else float(row["singleton_improvement"]) == 0 if kind == "zero" else float(row["singleton_improvement"]) < 0 for row in values) for kind in ("positive", "zero", "negative")}
    positive = [r for r in rows if float(r["singleton_improvement"]) > 0]
    total = sum(float(r["singleton_improvement"]) for r in positive)
    concentration = {}
    for fraction in (0.01, 0.05, 0.10, 0.20, 0.50):
        n = max(1, int((len(positive) * fraction) + 0.999999))
        concentration[f"top_{int(fraction * 100)}pct"] = {"candidate_count": n, "utility_share": sum(float(r["singleton_improvement"]) for r in positive[:n]) / total if total else None}
    return {"format_version": 1, "artifact_type": "census-frozen-sample-singleton-summary", "acquisition_sample_digest": json.loads((SAMPLE_ROOT / "manifest.json").read_text())["semantic_sha256"], "singleton_profile_digest": profile["digest"], "repository_digest": repository.digest, "repository_semantic_digest": repository_semantic_digest(repository), "baseline_objective": profile["baseline_objective"], "candidate_count": len(rows), "distribution": {name: counts(group) for name, group in groups.items()}, "positive_utility_concentration": concentration, "realization_counts": dict(Counter(item.state.value for item in repository.payloads)), "ranking_semantics": profile["ranking_semantics"]}


def spearman(values_a: list[str], values_b: list[str]) -> float:
    rank_a = {cid: index for index, cid in enumerate(values_a)}; rank_b = {cid: index for index, cid in enumerate(values_b)}
    xa = [rank_a[cid] for cid in sorted(rank_a)]; xb = [rank_b[cid] for cid in sorted(rank_a)]
    ma = sum(xa) / len(xa); mb = sum(xb) / len(xb)
    numerator = sum((a - ma) * (b - mb) for a, b in zip(xa, xb, strict=True))
    denominator = (sum((a - ma) ** 2 for a in xa) * sum((b - mb) ** 2 for b in xb)) ** 0.5
    return numerator / denominator if denominator else 1.0


def singleton_phase(prepared: Any, model: Any) -> tuple[Any, PayloadRepository, dict[str, Any], dict[str, Any]]:
    authoritative, repository, manifest = authoritative_prepared(prepared)
    profile1 = profile_once(authoritative, repository, manifest, model, "run-1")
    profile2 = profile_once(authoritative, repository, manifest, model, "run-2-fresh-backend")
    profile_elapsed = (profile1["evaluator_provenance"]["total_singleton_elapsed_seconds"], profile2["evaluator_provenance"]["total_singleton_elapsed_seconds"])
    semantic1 = [{k: v for k, v in row.items() if k != "elapsed_seconds"} for row in profile1["candidates"]]
    semantic2 = [{k: v for k, v in row.items() if k != "elapsed_seconds"} for row in profile2["candidates"]]
    ranking1 = [row["candidate_id"] for row in profile1["candidates"]]
    ranking2 = [row["candidate_id"] for row in profile2["candidates"]]
    def semantic_profile_digest(profile: dict[str, Any]) -> str:
        provenance = {key: value for key, value in profile["evaluator_provenance"].items() if key not in {"run", "total_singleton_elapsed_seconds"}}
        candidates = [{key: value for key, value in row.items() if key != "elapsed_seconds"} for row in profile["candidates"]]
        return digest({"baseline_objective": profile["baseline_objective"], "candidate_catalog_digest": profile["candidate_catalog_digest"], "incidence_digest": profile["incidence_digest"], "repository_digest": profile["repository_digest"], "maintenance_model_digest": profile["maintenance_model_digest"], "provenance": provenance, "candidates": candidates})
    semantic_profile1 = semantic_profile_digest(profile1)
    semantic_profile2 = semantic_profile_digest(profile2)
    exact = semantic1 == semantic2 and ranking1 == ranking2 and semantic_profile1 == semantic_profile2
    write_json(OUT / "singleton-profile-debug-run1.json", profile1)
    write_json(OUT / "singleton-profile-debug-run2.json", profile2)
    if not exact:
        raise RuntimeError("M2.21c singleton repeatability gate failed")
    singleton_dir = OUT / "singleton"; singleton_dir.mkdir(parents=True, exist_ok=True)
    write_json(singleton_dir / "profile.json", profile1)
    write_json(singleton_dir / "profile-repeat.json", {"profile_digest_run1": profile1["digest"], "profile_digest_run2": profile2["digest"], "semantic_profile_digest_run1": semantic_profile1, "semantic_profile_digest_run2": semantic_profile2, "exact": exact, "run1_seconds": profile_elapsed[0], "run2_seconds": profile_elapsed[1]})
    summary = rank_summary(profile1, authoritative, repository, model)
    summary["semantic_profile_digest"] = semantic_profile1
    write_json(singleton_dir / "summary.json", summary)
    historical_path = ROOT / "experiments/census-m2-9-singletons/singleton-results.csv"
    historical = {row["candidate_id"]: row for row in csv.DictReader(historical_path.open())} if historical_path.exists() else {}
    current_order = [row["candidate_id"] for row in profile1["candidates"]]
    historical_order = sorted(historical, key=lambda cid: (-float(historical[cid]["singleton_improvement"]), float(historical[cid].get("maintenance_cost_numeric", 0)), int(historical[cid]["precedence_rank"]), cid))
    historical_summary = {"status": "historical-exploratory-comparison", "historical_positive_zero_negative": {kind: sum(float(row["singleton_improvement"]) > 0 if kind == "positive" else float(row["singleton_improvement"]) == 0 if kind == "zero" else float(row["singleton_improvement"]) < 0 for row in historical.values()) for kind in ("positive", "zero", "negative")}, "spearman": spearman(current_order, historical_order) if set(current_order) == set(historical_order) else None, "top50_overlap": len(set(current_order[:50]) & set(historical_order[:50])), "top100_overlap": len(set(current_order[:100]) & set(historical_order[:100])), "top200_overlap": len(set(current_order[:200]) & set(historical_order[:200]))}
    write_json(singleton_dir / "historical-comparison.json", historical_summary)
    screening = build_candidate_set(profile1, authoritative, model, top_fraction=0.05)
    screening["experimental_role"] = "authoritative-frozen-sample-development-lineage"
    screening["acquisition_sample_digest"] = manifest["semantic_sha256"]
    screening["singleton_profile_digest"] = profile1["digest"]
    catalog_by_str = {str(candidate.candidate_id): candidate for candidate in authoritative.catalog.candidates}
    screening["retained_mcv_count"] = sum(catalog_by_str[cid].mechanism.value == "mcv" for cid in screening["candidate_ids"])
    screening["retained_fd_count"] = screening["retained_count"] - screening["retained_mcv_count"]
    screening["retained_present_count"] = sum(repository.by_candidate[cid].state is NativePayloadState.PRESENT for cid in screening["candidate_ids"])
    screening["retained_absent_native_count"] = screening["retained_count"] - screening["retained_present_count"]
    screening["digest"] = digest({key: value for key, value in screening.items() if key != "digest"})
    write_json(OUT / "screening.json", screening)
    return authoritative, repository, manifest, {"profile": profile1, "summary": summary, "historical": historical_summary, "screening": screening, "semantic_profile_digest": semantic_profile1}


def candidate_design_digest(candidates: tuple[Any, ...], ids: list[str] | tuple[str, ...]) -> str:
    by_id = {str(candidate.candidate_id): candidate for candidate in candidates}
    return digest([{"candidate_id": cid, "relation_name": by_id[cid].relation_name, "mechanism": by_id[cid].mechanism.value, "attributes": list(by_id[cid].attributes), "precedence_rank": by_id[cid].precedence_rank} for cid in ids])


def run_add_only_once(prepared: Any, repository: PayloadRepository, manifest: dict[str, Any], model: Any, screening: dict[str, Any], run_name: str) -> dict[str, Any]:
    visible = tuple(prepared.catalog.by_id[cid] for cid in screening["candidate_ids"])
    catalog = CandidateCatalog(visible)
    budget = MaintenanceBudget(sum((model.estimate_candidate(candidate) for candidate in visible), start=__import__("decimal").Decimal(0)), model.unit)
    with psycopg.connect(DSN) as conn:
        ordinary, baseline, evaluator = prepare_replay_shell_state(conn, prepared, repository, manifest)
        config = SearchConfig(exact_bound_pruning=True, record_pruned_moves=False, add_only=True, candidate_set_mode="screened", candidate_set_digest=screening["digest"], singleton_profile_digest=screening["singleton_profile_digest"], visible_candidate_count=len(visible), budget_mode="candidate-set-total")
        search = DeterministicBudgetSearch(evaluator, catalog, model, budget, config, prepared.incidence)
        started = time.perf_counter(); initial = baseline; search._calls = 1; current = initial; current_cost = model.estimate_design(current.design, catalog); rounds: list[dict[str, Any]] = []
        while True:
            before = current; before_cost = current_cost; counters = (search._considered, search._skipped, search._bound_pruned_no_improvement, search._bound_pruned_incumbent, search._evaluated, evaluator.adapter.planner_calls_total); remaining = search._ordered_ids(False, before.design); round_started = time.perf_counter()
            winner = search._finish_streaming_round("greedy-add", before, before_cost, (Move.add_candidate(item) for item in remaining))
            elapsed = time.perf_counter() - round_started; after = winner.state if winner is not None else before; after_cost = winner.cost if winner is not None else before_cost
            considered = search._considered - counters[0]; infeasible = search._skipped - counters[1]; no_improvement = search._bound_pruned_no_improvement - counters[2]; incumbent = search._bound_pruned_incumbent - counters[3]; native = search._evaluated - counters[4]; planners = evaluator.adapter.planner_calls_total - counters[5]; available = considered - infeasible
            rounds.append({"round": len(rounds) + 1, "selected_count_before": len(before.design.candidate_ids), "current_objective": before.aggregate_objective, "current_cost": str(before_cost), "remaining_add_candidates": len(remaining), "budget_infeasible": infeasible, "no_improvement_bound_pruned": no_improvement, "incumbent_bound_pruned": incumbent, "native_evaluated_moves": native, "planner_calls": planners, "round_elapsed_seconds": elapsed, "accepted_candidate": str(winner.move.add) if winner is not None else None, "objective_after": after.aggregate_objective, "cost_after": str(after_cost), "accepted": winner is not None, "prune_rate": (no_improvement + incumbent) / available if available else 0.0})
            current, current_cost = after, after_cost
            if time.perf_counter() - started > 900:
                raise TimeoutError("M2.21 screened search exceeded 900-second ceiling")
            if winner is None:
                break
        elapsed_total = time.perf_counter() - started
        accepted = [record for record in search._trajectory if record.accepted]
        selected = [str(item) for item in current.design.candidate_ids]
        by_id = {str(candidate.candidate_id): candidate for candidate in repository.catalog.candidates}
        result = {"format_version": 1, "status": "complete", "run": run_name, "termination_reason": "add-local-optimum", "acquisition_sample_digest": manifest["semantic_sha256"], "screened_candidate_set_digest": screening["digest"], "singleton_profile_digest": screening["singleton_profile_digest"], "baseline_statistics_digest": ordinary, "baseline_objective": initial.aggregate_objective, "baseline_estimate_vector_digest": estimate_vector_digest(initial), "final_objective": current.aggregate_objective, "final_estimate_vector_digest": estimate_vector_digest(current), "absolute_improvement": initial.aggregate_objective - current.aggregate_objective, "relative_improvement": (initial.aggregate_objective - current.aggregate_objective) / initial.aggregate_objective, "selected_design": selected, "selected_design_digest": candidate_design_digest(visible, selected), "selected_count": len(selected), "mcv_count": sum(by_id[cid].mechanism.value == "mcv" for cid in selected), "fd_count": sum(by_id[cid].mechanism.value == "fd" for cid in selected), "selected_present_count": sum(repository.by_candidate[by_id[cid].candidate_id].state is NativePayloadState.PRESENT for cid in selected), "selected_absent_native_count": sum(repository.by_candidate[by_id[cid].candidate_id].state is NativePayloadState.ABSENT_NATIVE for cid in selected), "final_cost": str(current_cost), "budget": str(budget.value), "budget_unit": budget.unit, "rounds": len(rounds), "elapsed_seconds": elapsed_total, "conceptual_moves": search._considered, "feasible_moves": search._considered - search._skipped, "budget_infeasible_moves": search._skipped, "bound_pruned_moves": search._bound_pruned_no_improvement + search._bound_pruned_incumbent, "pruning_rate": (search._bound_pruned_no_improvement + search._bound_pruned_incumbent) / (search._considered - search._skipped) if search._considered - search._skipped else 0.0, "native_evaluations": search._evaluated, "planner_calls": evaluator.adapter.planner_calls_total, "accepted_moves": search._accepted, "accepted_candidate_sequence": [str(record.move.add) for record in accepted], "rounds_detail": rounds, "ordinary_statistics_digest": ordinary, "lineage_repository_digest": repository.digest}
        clean_db(conn, repository.catalog.candidates)
    return result


def search_phase(prepared: Any, model: Any) -> dict[str, Any]:
    authoritative, repository, manifest, derived = singleton_phase_inputs(prepared)
    first = run_add_only_once(authoritative, repository, manifest, model, derived["screening"], "run-1")
    repeat = run_add_only_once(authoritative, repository, manifest, model, derived["screening"], "run-2-fresh-backend")
    exact = all(first[key] == repeat[key] for key in ("baseline_objective", "final_objective", "final_cost", "selected_design", "selected_design_digest", "rounds", "termination_reason", "accepted_candidate_sequence"))
    if not exact:
        raise RuntimeError("M2.21d search repeatability gate failed")
    search_dir = OUT / "search"; search_dir.mkdir(parents=True, exist_ok=True)
    write_json(search_dir / "search.json", first)
    repeat_summary = {"run1_elapsed_seconds": first["elapsed_seconds"], "run2_elapsed_seconds": repeat["elapsed_seconds"], "exact": exact, "run2": repeat}
    write_json(search_dir / "repeat.json", repeat_summary)
    write_csv(search_dir / "accepted-moves.csv", [{"round": row["round"], "candidate_id": row["accepted_candidate"], "objective_after": row["objective_after"], "cost_after": row["cost_after"]} for row in first["rounds_detail"] if row["accepted"]], ["round", "candidate_id", "objective_after", "cost_after"])
    return {"prepared": authoritative, "repository": repository, "manifest": manifest, "derived": derived, "search": first, "repeat": repeat_summary}


def singleton_phase_inputs(prepared: Any) -> tuple[Any, PayloadRepository, dict[str, Any], dict[str, Any]]:
    authoritative, repository, manifest = authoritative_prepared(prepared)
    profile = json.loads((OUT / "singleton/profile.json").read_text())
    screening = json.loads((OUT / "screening.json").read_text())
    return authoritative, repository, manifest, {"profile": profile, "summary": json.loads((OUT / "singleton/summary.json").read_text()), "screening": screening}


def physical_evaluate(conn: psycopg.Connection[Any], workload: Any, design: Design, repository_digest: str) -> EvaluationState:
    evaluations = []
    version = str(conn.execute("SHOW server_version").fetchone()[0])
    for query in sorted(workload.queries, key=lambda item: item.query_id):
        row = conn.execute(f"EXPLAIN (FORMAT JSON) {query.sql}").fetchone()
        estimate = extract_target_estimate(row[0], query.target_relation)
        evaluations.append(QueryEvaluation(QueryId(str(query.query_id)), estimate, query.truth, q_error(estimate, query.truth), f"native-explain:{version}"))
    return EvaluationState(design, tuple(evaluations), aggregate_objective(evaluations), repository_digest, workload.digest, version, "physical-native-explain", tuple(item.query_id for item in evaluations), ())


def state_vector(state: EvaluationState) -> list[dict[str, Any]]:
    return [{"query_id": str(item.query_id), "estimate": item.estimate, "truth": item.truth, "contribution": item.contribution, "provenance": item.provenance} for item in state.query_evaluations]


def selected_payloads(conn: psycopg.Connection[Any], selected: tuple[Any, ...]) -> dict[str, dict[str, Any]]:
    names = [str(dict(candidate.definition)["statistics_name"]) for candidate in selected]
    rows = conn.execute("SELECT e.stxname,e.oid,e.stxkind,d.stxoid IS NOT NULL,pg_mcv_list_send(d.stxdmcv),pg_dependencies_send(d.stxddependencies) FROM pg_statistic_ext e LEFT JOIN pg_statistic_ext_data d ON d.stxoid=e.oid WHERE e.stxname=ANY(%s)", (names,)).fetchall()
    by_name = {str(row[0]): row for row in rows}; output = {}
    for candidate in selected:
        name = str(dict(candidate.definition)["statistics_name"]); row = by_name.get(name)
        if row is None:
            raise RuntimeError(f"physical statistic missing: {name}")
        payload = bytes(row[4]) if candidate.mechanism.value == "mcv" and row[4] is not None else bytes(row[5]) if candidate.mechanism.value == "fd" and row[5] is not None else None
        output[str(candidate.candidate_id)] = {"statistics_name": name, "physical_oid": int(row[1]), "stxkind": str(row[2]), "data_row_exists": bool(row[3]), "physical_realization_state": "PRESENT" if payload else "ABSENT_NATIVE", "physical_payload_digest": hashlib.sha256(payload).hexdigest() if payload else None}
    return output


def physical_run(prepared: Any, repository: PayloadRepository, manifest: dict[str, Any], selected: tuple[Any, ...], run_name: str) -> dict[str, Any]:
    selected_design = Design(tuple(candidate.candidate_id for candidate in selected))
    with psycopg.connect(DSN) as conn:
        ordinary_h, baseline_h, evaluator = prepare_replay_shell_state(conn, prepared, repository, manifest)
        h_state = evaluator.evaluate_design(selected_design)
        h_vector = estimate_vector_digest(h_state); h_base_vector = estimate_vector_digest(baseline_h)
        clean_db(conn, repository.catalog.candidates)
    with psycopg.connect(DSN) as conn:
        clean_db(conn, repository.catalog.candidates)
        load_sample(conn, SAMPLE_ROOT / "sample.copy.bin")
        set_guc(conn, "pg_extstats.frozen_sample_mode", "replay"); set_guc(conn, "pg_extstats.frozen_sample_relation", SAMPLE_REL); set_guc(conn, "pg_extstats.frozen_totalrows", str(manifest["totalrows_used_by_builder"]))
        create_shells(conn, selected); conn.execute(f"ANALYZE {TARGET}"); conn.commit()
        ordinary_p = ordinary_stats_digest(conn); payloads = selected_payloads(conn, selected)
        p_count = int(conn.execute("SELECT count(*) FROM pg_statistic_ext").fetchone()[0]); p_data_count = int(conn.execute("SELECT count(*) FROM pg_statistic_ext_data").fetchone()[0])
        p_state = physical_evaluate(conn, prepared.workload, selected_design, repository.digest); p_vector = estimate_vector_digest(p_state)
        metadata_p = verify_database(conn, json.loads(DATASET_PATH.read_text()))
        clean_db(conn, repository.catalog.candidates)
    if ordinary_h != ordinary_p or h_state.aggregate_objective != p_state.aggregate_objective or h_vector != p_vector or not h_state.query_evaluations == p_state.query_evaluations:
        raise RuntimeError("M2.21e hypothetical/physical mismatch")
    audit = []
    for candidate in selected:
        frozen = repository.by_candidate[candidate.candidate_id]; physical = payloads[str(candidate.candidate_id)]
        audit.append({"candidate_id": str(candidate.candidate_id), "mechanism": candidate.mechanism.value, "columns": list(candidate.attributes), "frozen_state": frozen.state.value, "physical_state": physical["physical_realization_state"], "frozen_payload_digest": frozen.payload_sha256, "physical_payload_digest": physical["physical_payload_digest"], "exact": frozen.payload_sha256 == physical["physical_payload_digest"], "physical_oid": physical["physical_oid"], "data_row_exists": physical["data_row_exists"]})
    return {"run": run_name, "hypothetical": {"objective": h_state.aggregate_objective, "estimate_vector_digest": h_vector, "baseline_objective": baseline_h.aggregate_objective, "baseline_vector_digest": h_base_vector, "ordinary_statistics_digest": ordinary_h, "query_count": len(h_state.query_evaluations), "estimate_vector": state_vector(h_state)}, "physical": {"objective": p_state.aggregate_objective, "estimate_vector_digest": p_vector, "ordinary_statistics_digest": ordinary_p, "query_count": len(p_state.query_evaluations), "definition_count": p_count, "data_row_count": p_data_count, "relation_metadata": metadata_p, "estimate_vector": state_vector(p_state)}, "payload_audit": audit}


def physical_phase(prepared: Any) -> dict[str, Any]:
    authoritative, repository, manifest, derived = singleton_phase_inputs(prepared)
    search = json.loads((OUT / "search/search.json").read_text())
    selected = tuple(repository.catalog.by_id[CandidateId(cid)] for cid in search["selected_design"])
    run1 = physical_run(authoritative, repository, manifest, selected, "run-1")
    run2 = physical_run(authoritative, repository, manifest, selected, "run-2-fresh-backend")
    payload_exact = all(row["exact"] for row in run1["payload_audit"] + run2["payload_audit"])
    h_repeat = run1["hypothetical"]["estimate_vector_digest"] == run2["hypothetical"]["estimate_vector_digest"] and run1["hypothetical"]["objective"] == run2["hypothetical"]["objective"]
    p_repeat = run1["physical"]["estimate_vector_digest"] == run2["physical"]["estimate_vector_digest"] and run1["physical"]["objective"] == run2["physical"]["objective"]
    if not payload_exact or not h_repeat or not p_repeat:
        raise RuntimeError("M2.21e physical repeatability gate failed")
    validation = {"status": "complete", "acquisition_sample_digest": manifest["semantic_sha256"], "repository_digest": repository.digest, "selected_design_digest": search["selected_design_digest"], "selected_count": len(selected), "selected_mcv_count": sum(item.mechanism.value == "mcv" for item in selected), "selected_fd_count": sum(item.mechanism.value == "fd" for item in selected), "runs": [run1, run2], "payload_exact_count": sum(row["exact"] for row in run1["payload_audit"]), "payload_selected_count": len(selected), "hypothetical_physical_exact": True, "hypothetical_repeat_exact": h_repeat, "physical_repeat_exact": p_repeat, "ordinary_statistics_exact": run1["hypothetical"]["ordinary_statistics_digest"] == run1["physical"]["ordinary_statistics_digest"]}
    validation_dir = OUT / "physical-validation"; validation_dir.mkdir(parents=True, exist_ok=True)
    write_json(validation_dir / "validation.json", validation)
    write_json(validation_dir / "repeatability.json", {"hypothetical_exact": h_repeat, "physical_exact": p_repeat, "payload_exact_both_runs": payload_exact})
    write_csv(validation_dir / "selected-payload-audit.csv", run1["payload_audit"], list(run1["payload_audit"][0]))
    comparison = [{"query_id": h["query_id"], "h_estimate": h["estimate"], "p_estimate": p["estimate"], "estimate_exact": h["estimate"] == p["estimate"], "h_q_error": h["contribution"], "p_q_error": p["contribution"], "q_error_exact": h["contribution"] == p["contribution"]} for h, p in zip(run1["hypothetical"]["estimate_vector"], run1["physical"]["estimate_vector"], strict=True)]
    write_csv(validation_dir / "estimate-comparison.csv", comparison, list(comparison[0]))
    validation["estimate_exact_count"] = sum(row["estimate_exact"] for row in comparison)
    validation["q_error_exact_count"] = sum(row["q_error_exact"] for row in comparison)
    validation["query_count"] = len(comparison)
    write_json(validation_dir / "validation.json", validation)
    with psycopg.connect(DSN) as conn:
        # A final no-overlay empty control must reproduce the frozen baseline.
        clean_db(conn, repository.catalog.candidates)
        empty = physical_evaluate(conn, authoritative.workload, Design(()), repository.digest)
    if empty.aggregate_objective != run1["hypothetical"]["baseline_objective"] or estimate_vector_digest(empty) != run1["hypothetical"]["baseline_vector_digest"]:
        raise RuntimeError("post-validation cleanup baseline mismatch")
    cleanup = {"residual_statistics": 0, "residual_data_rows": 0, "sample_relation_removed": True, "empty_baseline_exact": True, "empty_baseline_objective": empty.aggregate_objective, "empty_baseline_vector_digest": estimate_vector_digest(empty)}
    write_json(validation_dir / "cleanup.json", cleanup)
    selected_lines = []; rollback_lines = []
    for candidate in selected:
        schema, relation = candidate.relation_name.split(".", 1); name = str(dict(candidate.definition)["statistics_name"]); mechanism = "mcv" if candidate.mechanism.value == "mcv" else "dependencies"; attrs = ", ".join(quote_ident(item) for item in candidate.attributes); qname = f"{quote_ident(schema)}.{quote_ident(name)}"; selected_lines.extend([f"CREATE STATISTICS {qname} ({mechanism}) ON {attrs} FROM {quote_ident(schema)}.{quote_ident(relation)};", f"ALTER STATISTICS {qname} SET STATISTICS 100;"]); rollback_lines.append(f"DROP STATISTICS IF EXISTS {qname};")
    (validation_dir / "apply.sql").write_text("\n".join(selected_lines) + "\n"); (validation_dir / "rollback.sql").write_text("\n".join(rollback_lines) + "\n")
    write_json(OUT / "physical-validation.json", validation)
    finalize_artifacts(authoritative, repository, manifest, derived, search, validation)
    return validation


def finalize_artifacts(prepared: Any, repository: PayloadRepository, manifest: dict[str, Any], derived: dict[str, Any], search: dict[str, Any], validation: dict[str, Any]) -> None:
    build_env = json.loads(BUILD_ENV_PATH.read_text())
    repeat = json.loads((OUT / "search/repeat.json").read_text())
    protocol = json.loads((OUT / "protocol.json").read_text())
    protocol.update({"status": "complete", "authoritative_lineage": True, "phases": {"M2.21a_capture": True, "M2.21b_replay": True, "M2.21c_singleton": True, "M2.21c_screening": True, "M2.21d_search": True, "M2.21e_physical_validation": True}, "singleton_profile_digest": derived["profile"]["digest"], "semantic_singleton_profile_digest": derived["summary"]["semantic_profile_digest"], "screened_candidate_set_digest": derived["screening"]["digest"], "screened_candidate_count": derived["screening"]["retained_count"], "search_result": {"selected_count": search["selected_count"], "selected_design_digest": search["selected_design_digest"], "baseline_objective": search["baseline_objective"], "final_objective": search["final_objective"], "final_cost": search["final_cost"], "termination_reason": search["termination_reason"], "repeat_exact": repeat["exact"]}, "physical_validation": {"selected_count": validation["selected_count"], "payload_exact_count": validation["payload_exact_count"], "estimate_exact_count": validation["estimate_exact_count"], "q_error_exact_count": validation["q_error_exact_count"], "hypothetical_physical_exact": validation["hypothetical_physical_exact"], "physical_repeat_exact": validation["physical_repeat_exact"], "cleanup_baseline_exact": True}, "maintenance_model_digest": EXPECTED_MODEL, "postgres_binary_sha256": build_env["postgres_binary_sha256"], "database_function_registration": "register_absent installed in the pre-existing Census database; source patch and build unchanged"})
    write_json(OUT / "protocol.json", protocol)
    summary = json.loads((OUT / "singleton/summary.json").read_text())
    historical = json.loads((OUT / "singleton/historical-comparison.json").read_text())
    lines = [
        "# M2.21 — Census Frozen-Sample Authoritative Rerun", "",
        "This is the authoritative reproducible Census CE lineage. One native PostgreSQL 16.14 sample was captured once from the canonical `public.climate` relation and persisted; all ordinary statistics and all 4,506 candidate payload states were reconstructed from that sample. Historical Census artifacts remain preserved as exploratory evidence and are not exact-continuation targets.", "",
        f"The sample contains {manifest['row_count']} rows from {manifest['source_relation_row_count']} physical rows. Semantic sample digest is `{manifest['semantic_sha256']}` and binary digest is `{manifest['sample_file_sha256']}` ({(SAMPLE_ROOT / 'sample.copy.bin').stat().st_size} bytes); frozen totalrows is {manifest['totalrows_used_by_builder']}. Three clean replay builds matched ordinary statistics, semantic repository state, baseline vector, and baseline objective exactly. New baseline objective is `{search['baseline_objective']:.12f}` (historical baseline `5586.930692724469` is descriptive only).",
        "", f"Repository realization on this sample is {summary['realization_counts']}; candidate catalog and incidence remain the frozen 4,506/19,996 identities. The 4,506 singleton profile was repeated exactly at the semantic level (runtime metadata excluded from the semantic digest): {summary['distribution']['all']}. Positive utility concentration is recorded in `singleton/summary.json`. Historical M2.9 comparison is explicitly descriptive: Spearman {historical['spearman']:.6f}, top-50/100/200 overlap {historical['top50_overlap']}/{historical['top100_overlap']}/{historical['top200_overlap']}.",
        "", f"The formal top-5% screen retains exactly {derived['screening']['retained_count']} candidates ({derived['screening']['retained_mcv_count']} MCV, {derived['screening']['retained_fd_count']} FD; {derived['screening']['retained_present_count']} PRESENT, {derived['screening']['retained_absent_native_count']} ABSENT_NATIVE) and is bound to the new sample and singleton digests.",
        "", f"The screened deterministic ADD-only search reached an add-local optimum after {search['rounds']} rounds in {search['elapsed_seconds']:.3f}s, selecting {search['selected_count']} candidates ({search['mcv_count']} MCV, {search['fd_count']} FD) at objective `{search['final_objective']:.12f}` from baseline `{search['baseline_objective']:.12f}`. Cost is `{search['final_cost']}` under total screened budget `{search['budget']}`. The repeat matched sequence, design, objective, cost, rounds, and termination exactly; no full-4,506 search, DROP, SWAP, or fresh-sample robustness run was performed.",
        "", f"Same-sample physical validation matched hypothetical replay for all {validation['payload_exact_count']} selected payloads, all {validation['estimate_exact_count']}/{validation['query_count']} query estimates, and all {validation['q_error_exact_count']}/{validation['query_count']} q-errors. H/P objectives and ordinary-statistics digests were exact across two clean validation runs. Cleanup returned exactly to the new empty baseline and left no experiment statistics or sample table.",
        "", "The only environment repair was registering the already compiled `pg_hypothetical_extstats_register_absent` internal function in the pre-existing Census database; the PostgreSQL source patch, binary, and build provenance were unchanged.",
    ]
    (OUT / "report.md").write_text("\n".join(lines) + "\n")


def evaluate_baseline(conn: psycopg.Connection[Any], prepared: Any, repository: PayloadRepository) -> tuple[str, float]:
    create_shells(conn, repository.catalog.candidates)
    conn.commit()
    evaluator = NativeEvaluator(prepared.workload, repository, prepared.incidence, PostgresAdapter(conn, repository))
    state = evaluator.evaluate_design(Design(()))
    return estimate_vector_digest(state), state.aggregate_objective


def build_once(number: int, prepared: Any, dataset: dict[str, Any], metadata: RelationMetadata, sample_info: dict[str, Any], sample_path: Path) -> dict[str, Any]:
    out = OUT / f"build-{number}"
    if out.exists():
        shutil.rmtree(out)
    out.mkdir(parents=True)
    manifest = json.loads((SAMPLE_ROOT / "manifest.json").read_text())
    repository = resolve_census_repository(prepared, manifest)
    with psycopg.connect(DSN) as conn:
        started = time.perf_counter()
        ordinary, baseline, _evaluator = prepare_replay_shell_state(conn, prepared, repository, manifest)
        elapsed = time.perf_counter() - started
        vector, objective = estimate_vector_digest(baseline), baseline.aggregate_objective
        counts = Counter(item.state.value for item in repository.payloads)
        clean_db(conn, prepared.catalog.candidates)
    build = {"build_id": f"build-{number}-replay", "mode": "replay", "repository_raw_digest": repository.digest, "repository_semantic_digest": repository_semantic_digest(repository), "ordinary_statistics_digest": ordinary, "baseline_estimate_vector_digest": vector, "baseline_objective": objective, "sample": sample_info, "elapsed_seconds": elapsed, "statistics_ext_count": counts["PRESENT"] + counts["ABSENT_NATIVE"], "realization_state_counts": dict(counts)}
    write_json(out / "summary.json", build)
    return build


def resume_replay(prepared: Any, dataset: dict[str, Any], metadata: RelationMetadata) -> None:
    """Continue after a capture-stage environment registration repair."""
    manifest = json.loads((SAMPLE_ROOT / "manifest.json").read_text())
    sample_info = {
        "sample_relation": SAMPLE_REL,
        "target_relation": TARGET,
        "row_count": manifest["row_count"],
        "source_relation_row_count": manifest["source_relation_row_count"],
        "serialization": "PostgreSQL COPY FORMAT binary",
        "sample_file_sha256": manifest["sample_file_sha256"],
        "semantic_sha256": manifest["semantic_sha256"],
        "statistics_target": manifest["statistics_target"],
        "totalrows_used_by_builder": manifest["totalrows_used_by_builder"],
        "sampling_algorithm": manifest["sampling_algorithm"],
        "captured_at": manifest.get("captured_at", "capture-stage"),
    }
    builds = [build_once(number, prepared, dataset, metadata, sample_info, SAMPLE_ROOT / "sample.copy.bin") for number in (1, 2, 3)]
    deterministic = {
        "ordinary_statistics_exact": len({item["ordinary_statistics_digest"] for item in builds}) == 1,
        "repository_semantic_exact": len({item["repository_semantic_digest"] for item in builds}) == 1,
        "baseline_vector_exact": len({item["baseline_estimate_vector_digest"] for item in builds}) == 1,
        "baseline_objective_exact": len({item["baseline_objective"] for item in builds}) == 1,
        "sample_digest_unchanged": hashlib.sha256((SAMPLE_ROOT / "sample.copy.bin").read_bytes()).hexdigest() == manifest["sample_file_sha256"],
    }
    deterministic["exact_gate"] = all(deterministic.values())
    write_json(OUT / "replay-determinism.json", deterministic)
    if not deterministic["exact_gate"]:
        raise RuntimeError("M2.21b replay gate failed")
    build_env = json.loads(BUILD_ENV_PATH.read_text())
    lineage = {"source_csv_sha256": DATASET_SHA, "relation": TARGET, "database": "pgextadv_exp16_census", "sample": sample_info, "sample_size_bytes": (SAMPLE_ROOT / "sample.copy.bin").stat().st_size, "builds": builds, "workload_digest": EXPECTED_WORKLOAD, "candidate_catalog_digest": EXPECTED_CATALOG, "incidence_digest": EXPECTED_INCIDENCE, "maintenance_model_digest": EXPECTED_MODEL, "upstream_tarball_sha256": UPSTREAM_SHA, "patch_sha256": PATCH_SHA, "build_input_digest": build_env["build_recipe_digest"], "postgres_binary_sha256": build_env["postgres_binary_sha256"], "database_function_registration": "pg_hypothetical_extstats_register_absent registered in pre-existing Census database; patched binary unchanged"}
    write_json(OUT / "lineage.json", lineage)
    protocol = json.loads((OUT / "protocol.json").read_text()) if (OUT / "protocol.json").exists() else {"milestone": "M2.21", "phase": "capture-replay"}
    protocol.update({"status": "complete", "replay_resume": True, "sample_semantic_sha256": sample_info["semantic_sha256"], "sample_file_sha256": sample_info["sample_file_sha256"], "sample_row_count": sample_info["row_count"], "frozen_totalrows": sample_info["totalrows_used_by_builder"], "baseline_objective": builds[0]["baseline_objective"], "baseline_estimate_vector_digest": builds[0]["baseline_estimate_vector_digest"], "repository_semantic_digest": builds[0]["repository_semantic_digest"]})
    write_json(OUT / "protocol.json", protocol)
    write_json(OUT / "report.md", {"milestone": "M2.21a/b", "status": "complete", "summary": "One Census native sample was persisted and three clean persisted-sample replay builds reconstructed ordinary statistics and all 4506 candidate payload states exactly.", "lineage": lineage, "replay_determinism": deterministic})
    print(json.dumps({"status": "complete", "sample_rows": sample_info["row_count"], "sample_digest": sample_info["semantic_sha256"], "baseline": builds[0]["baseline_objective"], "replay_exact": deterministic}, indent=2, sort_keys=True))


def capture_and_replay(prepared: Any, dataset: dict[str, Any], metadata: RelationMetadata) -> dict[str, Any]:
    if SAMPLE_ROOT.exists() or OUT.exists():
        raise RuntimeError("capture/replay output already exists; refusing overwrite")
    SAMPLE_ROOT.mkdir(parents=True)
    OUT.mkdir(parents=True)
    protocol = {"milestone": "M2.21", "phase": "capture-replay", "status": "running", "benchmark": "Census", "target_relation": TARGET, "sample_relation": SAMPLE_REL, "statistics_target": 100, "canonical_source_sha256": DATASET_SHA, "schema_signature": EXPECTED_SCHEMA, "workload_digest": EXPECTED_WORKLOAD, "candidate_catalog_digest": EXPECTED_CATALOG, "incidence_digest": EXPECTED_INCIDENCE, "maintenance_model_digest": EXPECTED_MODEL, "postgres_version": "16.14", "upstream_tarball_sha256": UPSTREAM_SHA, "patch_sha256": PATCH_SHA, "build_input_digest": json.loads(BUILD_ENV_PATH.read_text())["build_recipe_digest"], "workload_source": str(WORKLOAD_SOURCE), "workload_source_sha256": file_digest(WORKLOAD_SOURCE)}
    write_json(OUT / "protocol.json", protocol)
    with psycopg.connect(DSN) as conn:
        db = verify_database(conn, dataset)
        clean_db(conn, prepared.catalog.candidates)
        conn.execute(f"CREATE UNLOGGED TABLE {SAMPLE_REL} (LIKE {TARGET} INCLUDING DEFAULTS)")
        set_guc(conn, "pg_extstats.frozen_sample_mode", "capture")
        set_guc(conn, "pg_extstats.frozen_sample_relation", SAMPLE_REL)
        set_guc(conn, "pg_extstats.frozen_totalrows", "-1")
        started = time.perf_counter()
        CACHE_ROOT.mkdir(parents=True, exist_ok=True)
        temporary_root = Path(tempfile.mkdtemp(prefix="census-capture-", dir=CACHE_ROOT))
        result = acquire_payloads(conn, prepared.catalog, temporary_root / "repository", statistics_target=100, upstream_sha256=UPSTREAM_SHA, patch_commit=PATCH_SHA, repository_id="census-m2-21-frozen-sample-build-1", source_relations=(metadata,))
        elapsed = time.perf_counter() - started
        binary, rows = sample_bytes_and_rows(conn)
        sample_path = SAMPLE_ROOT / "sample.copy.bin"; sample_path.write_bytes(binary)
        totalrows = float(conn.execute("SELECT reltuples FROM pg_class WHERE oid=%s::regclass", (TARGET,)).fetchone()[0])
        sample_info = {"sample_relation": SAMPLE_REL, "target_relation": TARGET, "row_count": len(rows), "source_relation_row_count": db["row_count"], "serialization": "PostgreSQL COPY FORMAT binary", "sample_file_sha256": hashlib.sha256(binary).hexdigest(), "semantic_sha256": semantic_sample_digest(rows, dataset), "statistics_target": 100, "totalrows_used_by_builder": totalrows, "sampling_algorithm": "PostgreSQL 16.14 native block sampler + Vitter reservoir; rows sorted by physical TID", "captured_at": datetime.now(UTC).isoformat()}
        manifest = {"format_version": 1, "dataset_id": dataset["dataset_id"], "target_relation": TARGET, "sample_relation": SAMPLE_REL, "sample_file": "sample.copy.bin", "sample_file_sha256": sample_info["sample_file_sha256"], "semantic_sha256": sample_info["semantic_sha256"], "row_count": sample_info["row_count"], "source_relation_row_count": db["row_count"], "totalrows_used_by_builder": totalrows, "ordered_column_schema": dataset["ordered_column_schema"], "schema_signature": EXPECTED_SCHEMA, "statistics_target": 100, "sampling_algorithm": sample_info["sampling_algorithm"], "postgres_version": "16.14", "upstream_tarball_sha256": UPSTREAM_SHA, "patch_sha256": PATCH_SHA, "build_input_digest": json.loads(BUILD_ENV_PATH.read_text())["build_recipe_digest"], "source_csv_sha256": DATASET_SHA, "captured_at": sample_info["captured_at"]}
        write_json(SAMPLE_ROOT / "manifest.json", manifest)
        write_json(SAMPLE_ROOT / "README.md", {"artifact": "Census frozen acquisition sample v1", "protocol": "One native capture from public.climate; all 4506 extstats and ordinary statistics are derived by persisted replay; no random ANALYZE fallback.", "sample_file": "sample.copy.bin", "semantic_sha256": sample_info["semantic_sha256"], "sample_file_sha256": sample_info["sample_file_sha256"]})
        ordinary = ordinary_stats_digest(conn)
        cleanup_acquisition(conn, result)
        conn.execute(f"DROP TABLE IF EXISTS {SAMPLE_REL}")
        repository = result.repository
        vector, objective = evaluate_baseline(conn, prepared, repository)
        counts = Counter(item.state.value for item in repository.payloads)
        clean_db(conn, prepared.catalog.candidates)
        shutil.rmtree(temporary_root, ignore_errors=True)
    build1 = {"build_id": "build-1-capture", "mode": "capture", "repository_raw_digest": repository.digest, "repository_semantic_digest": repository_semantic_digest(repository), "ordinary_statistics_digest": ordinary, "baseline_estimate_vector_digest": vector, "baseline_objective": objective, "sample": sample_info, "elapsed_seconds": elapsed, "realization_state_counts": dict(counts)}
    write_json(OUT / "build-1" / "summary.json", build1)
    build2 = build_once(2, prepared, dataset, metadata, sample_info, SAMPLE_ROOT / "sample.copy.bin")
    build3 = build_once(3, prepared, dataset, metadata, sample_info, SAMPLE_ROOT / "sample.copy.bin")
    deterministic = {"ordinary_statistics_exact": build1["ordinary_statistics_digest"] == build2["ordinary_statistics_digest"] == build3["ordinary_statistics_digest"], "repository_semantic_exact": build1["repository_semantic_digest"] == build2["repository_semantic_digest"] == build3["repository_semantic_digest"], "baseline_vector_exact": build1["baseline_estimate_vector_digest"] == build2["baseline_estimate_vector_digest"] == build3["baseline_estimate_vector_digest"], "baseline_objective_exact": build1["baseline_objective"] == build2["baseline_objective"] == build3["baseline_objective"], "sample_digest_unchanged": sample_info["semantic_sha256"] == json.loads((SAMPLE_ROOT / "manifest.json").read_text())["semantic_sha256"], "exact_gate": True}
    if not all(deterministic[key] for key in ("ordinary_statistics_exact", "repository_semantic_exact", "baseline_vector_exact", "baseline_objective_exact", "sample_digest_unchanged")):
        deterministic["exact_gate"] = False
        write_json(OUT / "replay-determinism.json", deterministic)
        raise RuntimeError("M2.21b replay gate failed")
    write_json(OUT / "replay-determinism.json", deterministic)
    lineage = {"source_csv_sha256": DATASET_SHA, "relation": TARGET, "database": "pgextadv_exp16_census", "database_properties": db, "sample": sample_info, "sample_size_bytes": (SAMPLE_ROOT / "sample.copy.bin").stat().st_size, "builds": [build1, build2, build3], "workload_digest": EXPECTED_WORKLOAD, "candidate_catalog_digest": EXPECTED_CATALOG, "incidence_digest": EXPECTED_INCIDENCE, "maintenance_model_digest": EXPECTED_MODEL, "upstream_tarball_sha256": UPSTREAM_SHA, "patch_sha256": PATCH_SHA, "build_input_digest": json.loads(BUILD_ENV_PATH.read_text())["build_recipe_digest"], "postgres_binary_sha256": json.loads(BUILD_ENV_PATH.read_text())["postgres_binary_sha256"]}
    write_json(OUT / "lineage.json", lineage)
    protocol["status"] = "complete"; protocol["sample_semantic_sha256"] = sample_info["semantic_sha256"]; protocol["sample_file_sha256"] = sample_info["sample_file_sha256"]; protocol["sample_row_count"] = sample_info["row_count"]; protocol["frozen_totalrows"] = sample_info["totalrows_used_by_builder"]; protocol["baseline_objective"] = build1["baseline_objective"]; protocol["baseline_estimate_vector_digest"] = build1["baseline_estimate_vector_digest"]; protocol["repository_semantic_digest"] = build1["repository_semantic_digest"]
    write_json(OUT / "protocol.json", protocol)
    write_json(OUT / "report.md", {"milestone": "M2.21a/b", "status": "complete", "summary": "One Census native sample was persisted and three clean builds reconstructed ordinary statistics and all 4506 candidate payload states exactly.", "lineage": lineage, "replay_determinism": deterministic})
    print(json.dumps({"status": "complete", "sample_rows": sample_info["row_count"], "sample_digest": sample_info["semantic_sha256"], "baseline": build1["baseline_objective"], "replay_exact": deterministic}, indent=2, sort_keys=True))
    return lineage


def main() -> int:
    parser = argparse.ArgumentParser()
    parser.add_argument("--phase", choices=("capture-replay", "resume-replay", "singleton", "search", "physical"), default="capture-replay")
    args = parser.parse_args()
    prepared, dataset, metadata, _model = load_inputs()
    with psycopg.connect(DSN) as conn:
        db = verify_database(conn, dataset)
    print(json.dumps({"phase": args.phase, "git_head": git_head(), "database": db}, sort_keys=True))
    if args.phase == "resume-replay":
        resume_replay(prepared, dataset, metadata)
    elif args.phase == "singleton":
        _authoritative, _repository, _manifest, derived = singleton_phase(prepared, _model)
        print(json.dumps({"status": "complete", "phase": "singleton", "profile_digest": derived["profile"]["digest"], "baseline": derived["profile"]["baseline_objective"], "screened_count": derived["screening"]["retained_count"]}, indent=2, sort_keys=True))
    elif args.phase == "search":
        result = search_phase(prepared, _model)
        print(json.dumps({"status": "complete", "phase": "search", "selected_count": result["search"]["selected_count"], "final_objective": result["search"]["final_objective"], "repeat_exact": result["repeat"]["exact"]}, indent=2, sort_keys=True))
    elif args.phase == "physical":
        validation = physical_phase(prepared)
        print(json.dumps({"status": "complete", "phase": "physical", "selected_count": validation["selected_count"], "objective": validation["runs"][0]["hypothetical"]["objective"], "exact": validation["hypothetical_physical_exact"]}, indent=2, sort_keys=True))
    else:
        capture_and_replay(prepared, dataset, metadata)
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
