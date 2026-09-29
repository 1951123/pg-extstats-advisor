"""DMV M2.26: characterize one global PostgreSQL statistics target.

This experiment captures PostgreSQL's native block-sampler/reservoir output at
T=100, 300, and 1000, then replays each persisted sample without searching.
The same target is applied to ordinary columns and every frozen MCV/FD object.
"""

from __future__ import annotations

import hashlib
import json
import shutil
import statistics
import sys
import time
from collections import Counter
from datetime import UTC, datetime
from pathlib import Path
from typing import Any

import psycopg

ROOT = Path(__file__).resolve().parents[1]
if str(ROOT / "tools") not in sys.path:
    sys.path.insert(0, str(ROOT / "tools"))

from dmv_m2_17c_frozen_singletons import run_profile

from pg_extstats_advisor.evaluator.native import NativeEvaluator
from pg_extstats_advisor.models import Design
from pg_extstats_advisor.orchestration import load_prepared_run
from pg_extstats_advisor.payloads.cache import repository_semantic_digest
from pg_extstats_advisor.payloads.repository import PayloadRepository
from pg_extstats_advisor.postgres.adapter import PostgresAdapter
from pg_extstats_advisor.prepare.acquisition import acquire_payloads, cleanup_acquisition
from pg_extstats_advisor.prepare.workload import RelationMetadata

PREPARED = ROOT / "experiments/dmv-m2-15-singletons/prepared-run"
DATASET = ROOT / "experiments/environment/dmv-dataset.json"
BUILD_ENV = ROOT / "experiments/environment/postgresql-16.14-build.json"
OUT = ROOT / "experiments/dmv-m2-26-statistics-target-stability"
CACHE = ROOT / ".build/artifact-cache/dmv-statistics-target-stability-v1"
DSN = f"host={ROOT}/.build/pg16.14-experiment-socket port=55436 dbname=pgextadv_exp16_dmv user=postgres"
TARGET_REL = "public.dmv"
SAMPLE_REL = "public.pgextadv_m226_sample"
SOURCE_ROWS = 11_591_877
TARGETS = (100, 300, 1000)
REALIZATIONS = ("a", "b", "c")
UPSTREAM_SHA = "f6d077142737920858ce958ccdb75c6ee137a63b5b0853c70693d401ac7e3471"
PATCH_SHA = "22c7f48632585e81fd8a557dc8bffba873ac5da070aca31713e22c60261c3b4f"


def digest(value: Any) -> str:
    return hashlib.sha256(
        json.dumps(value, sort_keys=True, separators=(",", ":"), default=str).encode()
    ).hexdigest()


def write_json(path: Path, value: Any) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text(json.dumps(value, sort_keys=True, indent=2, default=str) + "\n")


def set_config(conn: psycopg.Connection[Any], name: str, value: str) -> None:
    conn.execute("SELECT set_config(%s, %s, false)", (name, value))


def qident(value: str) -> str:
    return '"' + value.replace('"', '""') + '"'


def relation_metadata(dataset: dict[str, Any]) -> RelationMetadata:
    return RelationMetadata(
        "public",
        "dmv",
        0,
        tuple(
            (int(c["attnum"]), str(c["name"]), str(c["type"]), bool(c["not_null"]))
            for c in dataset["ordered_column_schema"]
        ),
    )


def set_target(conn: psycopg.Connection[Any], target: int) -> None:
    """Set both ordinary-column and extended-statistics targets explicitly."""

    set_config(conn, "default_statistics_target", str(target))
    for row in conn.execute(
        "SELECT attname FROM pg_attribute WHERE attrelid='public.dmv'::regclass "
        "AND attnum > 0 AND NOT attisdropped ORDER BY attnum"
    ).fetchall():
        conn.execute(
            f"ALTER TABLE public.dmv ALTER COLUMN {qident(str(row[0]))} SET STATISTICS {target}"
        )


def reset_target(conn: psycopg.Connection[Any]) -> None:
    for row in conn.execute(
        "SELECT attname FROM pg_attribute WHERE attrelid='public.dmv'::regclass "
        "AND attnum > 0 AND NOT attisdropped ORDER BY attnum"
    ).fetchall():
        conn.execute(
            f"ALTER TABLE public.dmv ALTER COLUMN {qident(str(row[0]))} SET STATISTICS -1"
        )
    set_config(conn, "default_statistics_target", "100")


def clean_stats(conn: psycopg.Connection[Any]) -> None:
    conn.execute("SELECT pg_hypothetical_extstats_reset()")
    rows = conn.execute(
        "SELECT n.nspname,e.stxname FROM pg_statistic_ext e "
        "JOIN pg_namespace n ON n.oid=e.stxnamespace WHERE e.stxname LIKE 'pgextadv_acq_%' "
        "ORDER BY e.oid DESC"
    ).fetchall()
    for schema, name in rows:
        conn.execute(f"DROP STATISTICS IF EXISTS {qident(str(schema))}.{qident(str(name))}")
    conn.execute(f"DROP TABLE IF EXISTS {SAMPLE_REL}")
    set_config(conn, "pg_extstats.frozen_sample_mode", "off")
    set_config(conn, "pg_extstats.frozen_sample_relation", "")
    set_config(conn, "pg_extstats.frozen_totalrows", "0")
    conn.commit()


def sample_copy(conn: psycopg.Connection[Any]) -> tuple[bytes, list[tuple[Any, ...]]]:
    raw = bytearray()
    with conn.cursor().copy(f"COPY {SAMPLE_REL} TO STDOUT (FORMAT binary)") as copy:
        for chunk in copy:
            raw.extend(chunk)
    return bytes(raw), conn.execute(f"SELECT * FROM {SAMPLE_REL} ORDER BY ctid").fetchall()


def sample_digest(rows: list[tuple[Any, ...]], dataset: dict[str, Any]) -> str:
    h = hashlib.sha256()
    header = json.dumps(
        {"schema": dataset["ordered_column_schema"], "row_count": len(rows), "order": "ctid"},
        sort_keys=True,
        separators=(",", ":"),
    ).encode()
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


def ordinary_summary(conn: psycopg.Connection[Any]) -> dict[str, Any]:
    rows = conn.execute(
        "SELECT a.attnum,a.attname,s.stanullfrac,s.stawidth,s.stadistinct,"
        "s.stakind1,s.stakind2,s.stakind3,s.stakind4,s.stakind5,"
        "cardinality(s.stavalues1),cardinality(s.stavalues2),cardinality(s.stavalues3),"
        "cardinality(s.stavalues4),cardinality(s.stavalues5),"
        "cardinality(s.stanumbers1),cardinality(s.stanumbers2),cardinality(s.stanumbers3),"
        "cardinality(s.stanumbers4),cardinality(s.stanumbers5),"
        "s.stavalues1::text,s.stavalues2::text,s.stavalues3::text,s.stavalues4::text,s.stavalues5::text,"
        "s.stanumbers1::text,s.stanumbers2::text,s.stanumbers3::text,s.stanumbers4::text,s.stanumbers5::text "
        "FROM pg_attribute a LEFT JOIN pg_statistic s ON s.starelid=a.attrelid AND s.staattnum=a.attnum "
        "AND s.stainherit=false WHERE a.attrelid='public.dmv'::regclass AND a.attnum>0 "
        "AND NOT a.attisdropped ORDER BY a.attnum"
    ).fetchall()
    fields = (
        "attnum", "attname", "nullfrac", "width", "distinct", "kind1", "kind2", "kind3",
        "kind4", "kind5", "mcv_count", "hist_count", "num3_count", "num4_count", "num5_count",
        "freq_count1", "freq_count2", "freq_count3", "freq_count4", "freq_count5",
        "values1", "values2", "values3", "values4", "values5", "numbers1", "numbers2",
        "numbers3", "numbers4", "numbers5",
    )
    records = [dict(zip(fields, row, strict=True)) for row in rows]
    for record in records:
        record["values_digest"] = digest([record[f"values{i}"] for i in range(1, 6)])
        record["numbers_digest"] = digest([record[f"numbers{i}"] for i in range(1, 6)])
        record.pop("values1", None); record.pop("values2", None); record.pop("values3", None)
        record.pop("values4", None); record.pop("values5", None)
        record.pop("numbers1", None); record.pop("numbers2", None); record.pop("numbers3", None)
        record.pop("numbers4", None); record.pop("numbers5", None)
    return {"digest": digest(records), "columns": records}


def extstats_summary(conn: psycopg.Connection[Any]) -> dict[str, Any]:
    rows = conn.execute(
        "SELECT e.stxname,e.stxstattarget,e.stxkeys::text,e.stxkind,"
        "d.stxdmcv IS NOT NULL,d.stxddependencies IS NOT NULL,"
        "CASE WHEN d.stxdmcv IS NULL THEN NULL ELSE octet_length(pg_mcv_list_send(d.stxdmcv)) END,"
        "CASE WHEN d.stxddependencies IS NULL THEN NULL ELSE octet_length(pg_dependencies_send(d.stxddependencies)) END "
        "FROM pg_statistic_ext e LEFT JOIN pg_statistic_ext_data d ON d.stxoid=e.oid "
        "WHERE e.stxname LIKE 'pgextadv_acq_%' ORDER BY e.stxname"
    ).fetchall()
    records = [
        {
            "name": str(r[0]), "target": int(r[1]), "stxkeys": r[2], "kind": str(r[3]),
            "mcv_present": bool(r[4]), "fd_present": bool(r[5]), "mcv_bytes": r[6], "fd_bytes": r[7],
        }
        for r in rows
    ]
    return {"digest": digest(records), "count": len(records), "records": records,
            "state_counts": dict(Counter("mcv_present" if r["mcv_present"] else "mcv_absent" if "m" in r["kind"] else "fd_present" if r["fd_present"] else "fd_absent" for r in records))}


def vector_digest(state: Any) -> str:
    return digest([{"query_id": str(x.query_id), "estimate": x.estimate, "truth": x.truth,
                    "contribution": x.contribution, "provenance": x.provenance} for x in state.query_evaluations])


def vector_records(state: Any) -> list[dict[str, Any]]:
    return [{"query_id": str(x.query_id), "estimate": x.estimate, "truth": x.truth,
             "q_error": max(x.estimate / x.truth, x.truth / x.estimate) if x.estimate > 0 else None,
             "contribution": x.contribution, "provenance": x.provenance} for x in state.query_evaluations]


def rank(values: list[float]) -> list[float]:
    order = sorted(enumerate(values), key=lambda item: item[1])
    output = [0.0] * len(values)
    i = 0
    while i < len(order):
        j = i
        while j + 1 < len(order) and order[j + 1][1] == order[i][1]:
            j += 1
        value = (i + j + 2) / 2.0
        for k in range(i, j + 1):
            output[order[k][0]] = value
        i = j + 1
    return output


def spearman(left: list[float], right: list[float]) -> float | None:
    if len(left) != len(right) or not left:
        return None
    a, b = rank(left), rank(right)
    am, bm = statistics.mean(a), statistics.mean(b)
    numerator = sum((x - am) * (y - bm) for x, y in zip(a, b, strict=True))
    denominator = (sum((x - am) ** 2 for x in a) * sum((y - bm) ** 2 for y in b)) ** 0.5
    return numerator / denominator if denominator else None


def singleton_summary(rows: list[dict[str, Any]]) -> dict[str, Any]:
    ordered = sorted(rows, key=lambda x: (-float(x["singleton_improvement"]), int(x["precedence_rank"]), x["candidate_id"]))
    signs = Counter("positive" if float(x["singleton_improvement"]) > 0 else "negative" if float(x["singleton_improvement"]) < 0 else "zero" for x in rows)
    return {"sign_counts": dict(signs), "top5": [x["candidate_id"] for x in ordered[:5]], "top10": [x["candidate_id"] for x in ordered[:10]], "top20": [x["candidate_id"] for x in ordered[:20]], "best_improvement": float(ordered[0]["singleton_improvement"]), "worst_improvement": float(ordered[-1]["singleton_improvement"])}


def create_shells(conn: psycopg.Connection[Any], repository: PayloadRepository, target: int) -> None:
    for item in repository.payloads:
        c = item.candidate
        definition = dict(c.definition)
        schema, relation = c.relation_name.split(".", 1)
        name = str(definition["statistics_name"])
        kind = "mcv" if c.mechanism.value == "mcv" else "dependencies"
        attrs = ", ".join(qident(a) for a in c.attributes)
        conn.execute(f"CREATE STATISTICS {qident(schema)}.{qident(name)} ({kind}) ON {attrs} FROM {qident(schema)}.{qident(relation)}")
        conn.execute(f"ALTER STATISTICS {qident(schema)}.{qident(name)} SET STATISTICS {target}")


def validate_target_lineage(info: dict[str, Any], repository: PayloadRepository, target: int) -> None:
    if int(info["target"]) != target:
        raise ValueError("sample target does not match replay target")
    raw = json.loads((repository.root / "manifest.json").read_text())
    provenance = raw.get("acquisition_provenance", {})
    recorded = provenance.get("global_statistics_target", provenance.get("statistics_target"))
    if recorded is None or int(recorded) != target:
        raise ValueError("repository target does not match replay target")


def verify_target(conn: psycopg.Connection[Any], target: int) -> dict[str, Any]:
    default = int(conn.execute("SHOW default_statistics_target").fetchone()[0])
    columns = conn.execute("SELECT attname,attstattarget FROM pg_attribute WHERE attrelid='public.dmv'::regclass AND attnum>0 AND NOT attisdropped ORDER BY attnum").fetchall()
    ext = conn.execute("SELECT min(stxstattarget),max(stxstattarget),count(*) FROM pg_statistic_ext WHERE stxname LIKE 'pgextadv_acq_%'").fetchone()
    if default != target or any(int(row[1]) != target for row in columns) or (ext[2] and (int(ext[0]) != target or int(ext[1]) != target)):
        raise RuntimeError(f"global target propagation failed: default={default}, columns={columns}, ext={ext}")
    return {"default_statistics_target": default, "column_targets": [{"column": str(a), "attstattarget": int(b)} for a, b in columns], "ext_target_min": int(ext[0]) if ext[2] else None, "ext_target_max": int(ext[1]) if ext[2] else None, "ext_count": int(ext[2])}


def capture_one(conn: psycopg.Connection[Any], prepared: Any, metadata: RelationMetadata, dataset: dict[str, Any], target: int, name: str) -> dict[str, Any]:
    sample_dir = CACHE / name
    repo_path = sample_dir / "repository"
    manifest_path = OUT / "samples" / name / "manifest.json"
    if manifest_path.exists() and repo_path.exists() and (sample_dir / "sample.copy.bin").exists():
        return json.loads(manifest_path.read_text())
    sample_dir.mkdir(parents=True, exist_ok=True)
    (sample_dir / "sample.copy.bin").unlink(missing_ok=True)
    shutil.rmtree(repo_path, ignore_errors=True)
    clean_stats(conn)
    conn.execute(f"CREATE UNLOGGED TABLE {SAMPLE_REL} (LIKE {TARGET_REL} INCLUDING DEFAULTS)")
    set_target(conn, target)
    conn.execute(f"TRUNCATE {SAMPLE_REL}")
    set_config(conn, "pg_extstats.frozen_sample_mode", "capture")
    set_config(conn, "pg_extstats.frozen_sample_relation", SAMPLE_REL)
    set_config(conn, "pg_extstats.frozen_totalrows", "-1")
    started = time.perf_counter()
    result = acquire_payloads(conn, prepared.catalog, repo_path, statistics_target=target, global_statistics_target=target, upstream_sha256=UPSTREAM_SHA, patch_commit=PATCH_SHA, repository_id=f"dmv-m2-26-{name}", source_relations=(metadata,))
    elapsed = time.perf_counter() - started
    binary, rows = sample_copy(conn)
    (sample_dir / "sample.copy.bin").write_bytes(binary)
    source_rows = int(conn.execute(f"SELECT count(*) FROM {TARGET_REL}").fetchone()[0])
    if source_rows != SOURCE_ROWS:
        raise RuntimeError(f"source row count changed: {source_rows}")
    repository = PayloadRepository.load(repo_path)
    if [str(c.candidate_id) for c in repository.catalog.candidates] != [str(c.candidate_id) for c in prepared.catalog.candidates]:
        raise RuntimeError("candidate logical IDs changed across statistics targets")
    record = {
        "sample_name": name, "target": target, "requested_target": target,
        "native_requested_sample_capacity": len(rows), "requested_native_sample_rows": 300 * target,
        "actual_native_sample_rows": len(rows), "sample_rows": len(rows),
        "capacity_source": "PostgreSQL 16.14 ComputeExtStatisticsRows; observed persisted native rows",
        "expected_capacity_from_source_formula": 300 * target,
        "source_relation_rows": source_rows, "totalrows_controlled": SOURCE_ROWS,
        "observed_native_reltuples": float(conn.execute("SELECT reltuples FROM pg_class WHERE oid='public.dmv'::regclass").fetchone()[0]),
        "source_dataset_sha256": dataset["source_sha256"],
        "postgres_build_recipe_digest": json.loads(BUILD_ENV.read_text())["build_recipe_digest"],
        "postgres_version": str(conn.execute("SHOW server_version").fetchone()[0]),
        "random_provenance": "fresh backend native pg_global_prng_state; PostgreSQL exposes no user seed for ANALYZE reservoir",
        "sample_sha256": hashlib.sha256(binary).hexdigest(), "sample_semantic_digest": sample_digest(rows, dataset),
        "repository_semantic_digest": repository_semantic_digest(repository), "repository_path": str(repo_path),
        "catalog_digest": prepared.candidate_catalog_digest, "elapsed_seconds": elapsed,
        "native_capture": True, "sampling": "PostgreSQL 16.14 native block sampler + native reservoir + patched capture hook",
        "captured_at": datetime.now(UTC).isoformat(), "target_propagation": verify_target(conn, target),
    }
    write_json(sample_dir / "cache-manifest.json", {"cache_schema_version": 1, "identity": {"statistics_target": target, "sample_name": name, "sample_sha256": record["sample_sha256"], "candidate_catalog_digest": prepared.candidate_catalog_digest}, "repository_semantic_digest": record["repository_semantic_digest"]})
    write_json(manifest_path, record)
    cleanup_acquisition(conn, result)
    clean_stats(conn)
    reset_target(conn)
    conn.execute(f"DROP TABLE IF EXISTS {SAMPLE_REL}")
    conn.commit()
    return record


def replay_one(conn: psycopg.Connection[Any], prepared: Any, dataset: dict[str, Any], info: dict[str, Any], repeat: int = 1) -> dict[str, Any]:
    target = int(info["target"])
    sample_path = CACHE / info["sample_name"] / "sample.copy.bin"
    clean_stats(conn)
    conn.execute(f"CREATE UNLOGGED TABLE {SAMPLE_REL} (LIKE {TARGET_REL} INCLUDING DEFAULTS)")
    with conn.cursor().copy(f"COPY {SAMPLE_REL} FROM STDIN (FORMAT binary)") as copy:
        copy.write(sample_path.read_bytes())
    set_target(conn, target)
    repository = PayloadRepository.load(CACHE / info["sample_name"] / "repository")
    validate_target_lineage(info, repository, target)
    create_shells(conn, repository, target)
    set_config(conn, "pg_extstats.frozen_sample_mode", "replay")
    set_config(conn, "pg_extstats.frozen_sample_relation", SAMPLE_REL)
    set_config(conn, "pg_extstats.frozen_totalrows", str(SOURCE_ROWS))
    started = time.perf_counter()
    conn.execute(f"ANALYZE {TARGET_REL}")
    conn.commit()
    elapsed = time.perf_counter() - started
    target_state = verify_target(conn, target)
    ordinary = ordinary_summary(conn)
    ext = extstats_summary(conn)
    evaluator = NativeEvaluator(prepared.workload, repository, prepared.incidence, PostgresAdapter(conn, repository))
    baseline = evaluator.evaluate_design(Design(()))
    singleton = run_profile(prepared, repository, DSN, f"{info['sample_name']}-r{repeat}")
    singleton_profile_digest = digest(
        [{k: v for k, v in row.items() if k != "elapsed_seconds"} for row in singleton["rows"]]
    )
    result = {
        "sample_name": info["sample_name"], "target": target, "repeat": repeat,
        "ordinary_statistics": ordinary, "extstats": ext, "target_state": target_state,
        "baseline_objective": baseline.aggregate_objective, "baseline_vector_digest": vector_digest(baseline),
        "baseline_vector": vector_records(baseline),
        "singleton": singleton, "repository_semantic_digest": repository_semantic_digest(repository),
        "singleton_profile_digest": singleton_profile_digest,
        "shared_sample_audit": {"sample_rows": int(info["sample_rows"]), "ordinary_statistics_consumed_same_sample": True, "extended_statistics_consumed_same_sample": True, "secondary_sampling_or_downsampling": False},
        "replay_seconds": elapsed, "cache_identity": {"statistics_target": target, "sample_sha256": info["sample_sha256"], "repository_semantic_digest": info["repository_semantic_digest"]},
    }
    return result


def cleanup_replay(conn: psycopg.Connection[Any]) -> None:
    clean_stats(conn)
    reset_target(conn)
    conn.execute(f"DROP TABLE IF EXISTS {SAMPLE_REL}")
    conn.commit()


def main() -> int:
    OUT.mkdir(parents=True, exist_ok=True); CACHE.mkdir(parents=True, exist_ok=True)
    prepared = load_prepared_run(PREPARED); dataset = json.loads(DATASET.read_text()); metadata = relation_metadata(dataset)
    ids = [str(c.candidate_id) for c in prepared.catalog.candidates]
    protocol = {"milestone": "M2.26", "status": "running", "targets": list(TARGETS), "realizations_per_target": 3, "sample_names": [f"T{t}-{r.upper()}" for t in TARGETS for r in REALIZATIONS], "global_statistics_target": "single shared T for ordinary columns and all 72 extstats", "source_relation": TARGET_REL, "source_rows": SOURCE_ROWS, "controlled_replay_statistics_population_rows": SOURCE_ROWS, "source_dataset_sha256": dataset["source_sha256"], "postgres_build_recipe_digest": json.loads(BUILD_ENV.read_text())["build_recipe_digest"], "candidate_catalog_digest": prepared.candidate_catalog_digest, "candidate_count": len(ids), "workload_digest": prepared.workload.digest, "effective_workload_digest": prepared.effective_workload_digest, "search_started": False, "budget_search_started": False, "sampling_method": "native PostgreSQL 16.14 block sampler + native reservoir + patched capture hook", "copy_client_reservoir_used": False, "patch_sha256": PATCH_SHA, "upstream_sha256": UPSTREAM_SHA}
    write_json(OUT / "protocol.json", protocol)
    all_infos: list[dict[str, Any]] = []
    with psycopg.connect(DSN) as conn:
        for target in TARGETS:
            for realization in REALIZATIONS:
                name = f"T{target}-{realization.upper()}"
                info = capture_one(conn, prepared, metadata, dataset, target, name)
                cache_manifest = CACHE / name / "cache-manifest.json"
                if not cache_manifest.exists():
                    write_json(cache_manifest, {"cache_schema_version": 1, "identity": {"statistics_target": target, "sample_name": name, "sample_sha256": info["sample_sha256"], "candidate_catalog_digest": prepared.candidate_catalog_digest}, "repository_semantic_digest": info["repository_semantic_digest"]})
                all_infos.append(info)
    write_json(OUT / "samples.json", {"samples": all_infos, "candidate_ids": ids})
    replay_results: list[dict[str, Any]] = []
    for info in all_infos:
        with psycopg.connect(DSN) as conn:
            replay_results.append(replay_one(conn, prepared, dataset, info))
            cleanup_replay(conn)
    # Fresh-backend same-sample determinism gate for the first realization at every target.
    repeats: dict[str, Any] = {}
    for info in all_infos[::3]:
        with psycopg.connect(DSN) as conn:
            first = replay_one(conn, prepared, dataset, info, 1); cleanup_replay(conn)
        with psycopg.connect(DSN) as conn:
            second = replay_one(conn, prepared, dataset, info, 2); cleanup_replay(conn)
        comparable = {"ordinary_digest": (first["ordinary_statistics"]["digest"], second["ordinary_statistics"]["digest"]), "extstats_digest": (first["extstats"]["digest"], second["extstats"]["digest"]), "baseline_vector_digest": (first["baseline_vector_digest"], second["baseline_vector_digest"]), "baseline_objective": (first["baseline_objective"], second["baseline_objective"]), "singleton_profile_digest": (first["singleton_profile_digest"], second["singleton_profile_digest"])}
        repeats[info["sample_name"]] = {"exact": all(a == b for a, b in comparable.values()), "comparisons": comparable}
    ordinary_samples = [{"sample_name": r["sample_name"], "target": r["target"], "digest": r["ordinary_statistics"]["digest"], "columns": r["ordinary_statistics"]["columns"]} for r in replay_results]
    ordinary_dispersion = []
    for column in {x["attname"] for r in replay_results for x in r["ordinary_statistics"]["columns"]}:
        records = [next(x for x in r["ordinary_statistics"]["columns"] if x["attname"] == column) for r in replay_results]
        ordinary_dispersion.append({"column": column, "nullfrac_stdev": statistics.stdev(float(x["nullfrac"] or 0.0) for x in records), "distinct_stdev": statistics.stdev(float(x["distinct"] or 0.0) for x in records), "mcv_count_stdev": statistics.stdev(int(x["mcv_count"] or 0) for x in records), "hist_count_stdev": statistics.stdev(int(x["hist_count"] or 0) for x in records)})
    write_json(OUT / "ordinary-stats-stability.json", {"samples": ordinary_samples, "cross_realization_dispersion": ordinary_dispersion})
    ext_samples = [{"sample_name": r["sample_name"], "target": r["target"], "digest": r["extstats"]["digest"], "summary": r["extstats"]} for r in replay_results]
    ext_pairwise = []
    for i, left in enumerate(ext_samples):
        left_by_name = {x["name"]: (x["mcv_present"], x["fd_present"]) for x in left["summary"]["records"]}
        for right in ext_samples[i + 1:]:
            right_by_name = {x["name"]: (x["mcv_present"], x["fd_present"]) for x in right["summary"]["records"]}
            flips = sum(left_by_name.get(k) != right_by_name.get(k) for k in left_by_name)
            ext_pairwise.append({"left": left["sample_name"], "right": right["sample_name"], "state_flip_count": flips})
    write_json(OUT / "extstats-stability.json", {"samples": ext_samples, "pairwise_state_flips": ext_pairwise})
    baseline_samples = [{"sample_name": r["sample_name"], "target": r["target"], "objective": r["baseline_objective"], "vector_digest": r["baseline_vector_digest"], "vector": r["baseline_vector"]} for r in replay_results]
    query_dispersion = []
    for query_id in {x["query_id"] for r in replay_results for x in r["baseline_vector"]}:
        records = [next(x for x in r["baseline_vector"] if x["query_id"] == query_id) for r in replay_results]
        values = [x["estimate"] for x in records]
        q_errors = [x["q_error"] for x in records if x["q_error"] is not None]
        query_dispersion.append({"query_id": query_id, "estimate_stdev": statistics.stdev(values), "estimate_min": min(values), "estimate_max": max(values), "q_error_stdev": statistics.stdev(q_errors) if len(q_errors) > 1 else None})
    query_dispersion.sort(key=lambda x: -x["estimate_stdev"])
    write_json(OUT / "baseline-stability.json", {"samples": baseline_samples, "per_query_dispersion": query_dispersion[:50], "top_unstable_queries": query_dispersion[:20]})
    singleton_profiles = [{"sample_name": r["sample_name"], "target": r["target"], "profile_digest": r["singleton_profile_digest"], "summary": singleton_summary(r["singleton"]["rows"]), "rows": r["singleton"]["rows"]} for r in replay_results]
    pairwise = []
    for i, left in enumerate(singleton_profiles):
        left_values = {x["candidate_id"]: float(x["singleton_improvement"]) for x in left["rows"]}
        for right in singleton_profiles[i + 1:]:
            right_values = {x["candidate_id"]: float(x["singleton_improvement"]) for x in right["rows"]}
            ids_common = sorted(set(left_values) & set(right_values))
            pairwise.append({"left": left["sample_name"], "right": right["sample_name"], "spearman": spearman([left_values[x] for x in ids_common], [right_values[x] for x in ids_common]), "top5_overlap": len(set(left["summary"]["top5"]) & set(right["summary"]["top5"])), "top10_overlap": len(set(left["summary"]["top10"]) & set(right["summary"]["top10"]))})
    sign_flips = []
    for candidate_id in sorted({x["candidate_id"] for p in singleton_profiles for x in p["rows"]}):
        signs = {"positive" if float(next(x for x in p["rows"] if x["candidate_id"] == candidate_id)["singleton_improvement"]) > 0 else "negative" if float(next(x for x in p["rows"] if x["candidate_id"] == candidate_id)["singleton_improvement"]) < 0 else "zero" for p in singleton_profiles}
        if len(signs) > 1:
            sign_flips.append({"candidate_id": candidate_id, "signs": sorted(signs)})
    write_json(OUT / "singleton-stability.json", {"samples": singleton_profiles, "pairwise_rank_stability": pairwise, "sign_flips": sign_flips, "determinism": repeats})
    grouped: dict[int, list[dict[str, Any]]] = {t: [r for r in replay_results if r["target"] == t] for t in TARGETS}
    trends = []
    for target, rows in grouped.items():
        objectives = [float(r["baseline_objective"]) for r in rows]
        trends.append({"target": target, "baseline_mean": statistics.mean(objectives), "baseline_stdev": statistics.stdev(objectives) if len(objectives) > 1 else 0.0, "sample_rows": [int(x["sample_name"].split("-")[1]) if False else next(i["sample_rows"] for i in all_infos if i["sample_name"] == x["sample_name"]) for x in rows]})
    write_json(OUT / "runtime-summary.json", {"capture_seconds": {i["sample_name"]: i["elapsed_seconds"] for i in all_infos}, "replay_seconds": {r["sample_name"]: r["replay_seconds"] for r in replay_results}, "singleton_seconds": {r["sample_name"]: r["singleton"]["elapsed_seconds"] for r in replay_results}, "cache_bytes": {i["sample_name"]: sum(p.stat().st_size for p in (CACHE / i["sample_name"]).rglob("*") if p.is_file()) for i in all_infos}})
    write_json(OUT / "summary.json", {**protocol, "status": "complete", "trends": trends, "determinism": repeats, "replay_count": len(replay_results), "native_sample_rows": {i["sample_name"]: i["sample_rows"] for i in all_infos}, "target_capacity_formula": "ComputeExtStatisticsRows yields approximately 300*T for this 72-object DMV catalog; persisted row count is authoritative", "target_cache_identity_distinct": len({(i["target"], i["sample_sha256"]) for i in all_infos}) == len(all_infos)})
    report = ["# M2.26 — DMV Statistics-Target / Planner-State Stability", "", "No search, budget, recommendation, or patch modification was performed.", "", "## Samples", "", "```json", json.dumps(trends, indent=2), "```", "", "## Gates", "", f"- all persisted native samples: {len(all_infos)}", f"- source relation rows: {SOURCE_ROWS}", f"- replay population rows: {SOURCE_ROWS}", f"- same-sample fresh-backend determinism: {all(x['exact'] for x in repeats.values())}", f"- candidate catalog preserved: {len(ids)} IDs", "", "The experiment characterizes one shared target T; it does not tune T or run physical-design search."]
    (OUT / "report.md").write_text("\n".join(report) + "\n")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
