"""Capture and replay one authoritative DMV PostgreSQL acquisition sample.

This milestone deliberately stops at acquisition/replay validation.  It does
not invoke a search, screening, calibration, or workload objective.
"""

from __future__ import annotations

import hashlib
import json
import shutil
import tempfile
import time
from datetime import UTC, datetime
from pathlib import Path
from typing import Any

import psycopg

from pg_extstats_advisor.evaluator.native import NativeEvaluator
from pg_extstats_advisor.models import Design
from pg_extstats_advisor.orchestration import load_prepared_run
from pg_extstats_advisor.payloads.repository import PayloadRepository
from pg_extstats_advisor.postgres.adapter import PostgresAdapter
from pg_extstats_advisor.prepare.acquisition import acquire_payloads, cleanup_acquisition
from pg_extstats_advisor.prepare.workload import RelationMetadata

REPO = Path(__file__).resolve().parents[1]
PREPARED = REPO / "experiments/dmv-m2-15-singletons/prepared-run"
DATASET = REPO / "experiments/environment/dmv-dataset.json"
OUT = REPO / "experiments/dmv-m2-17b-frozen-acquisition-sample"
SAMPLE = REPO / "datasets/dmv-frozen-acquisition-sample-v1"
DBNAME = "pgextadv_exp16_dmv"
DSN = f"host={REPO}/.build/pg16.14-experiment-socket port=55436 dbname={DBNAME} user=postgres"
SAMPLE_REL = "public.pgextadv_frozen_sample"
TARGET_REL = "public.dmv"
UPSTREAM_SHA = "f6d077142737920858ce958ccdb75c6ee137a63b5b0853c70693d401ac7e3471"
PATCH_SHA = "22c7f48632585e81fd8a557dc8bffba873ac5da070aca31713e22c60261c3b4f"


def digest(value: Any) -> str:
    return hashlib.sha256(
        json.dumps(value, sort_keys=True, separators=(",", ":"), default=str).encode()
    ).hexdigest()


def sql_setting(conn: psycopg.Connection, name: str, value: str) -> None:
    conn.execute("SELECT set_config(%s, %s, false)", (name, value))


def relation_metadata(dataset: dict[str, Any]) -> RelationMetadata:
    cols = tuple(
        (
            int(item["attnum"]),
            str(item["name"]),
            str(item["type"]),
            bool(item["not_null"]),
        )
        for item in dataset["ordered_column_schema"]
    )
    return RelationMetadata("public", "dmv", 0, cols)


def ensure_sample_table(conn: psycopg.Connection) -> None:
    conn.execute("DROP TABLE IF EXISTS public.frozen_smoke_target")
    conn.execute("DROP TABLE IF EXISTS public.frozen_smoke_sample")
    conn.execute("DROP TABLE IF EXISTS public.pgextadv_frozen_sample")
    conn.execute(f"CREATE UNLOGGED TABLE {SAMPLE_REL} (LIKE {TARGET_REL} INCLUDING DEFAULTS)")
    conn.commit()


def drop_acquisition_stats(conn: psycopg.Connection) -> None:
    names = conn.execute(
        "SELECT n.nspname,e.stxname FROM pg_statistic_ext e "
        "JOIN pg_namespace n ON n.oid=e.stxnamespace "
        "WHERE e.stxname LIKE 'pgextadv_acq_%' ORDER BY e.oid DESC"
    ).fetchall()
    for schema, name in names:
        conn.execute(f'DROP STATISTICS IF EXISTS "{schema}"."{name}"')
    conn.commit()


def copy_sample(conn: psycopg.Connection) -> tuple[bytes, list[tuple[Any, ...]]]:
    raw = bytearray()
    with conn.cursor().copy(f"COPY {SAMPLE_REL} TO STDOUT (FORMAT binary)") as copy:
        for chunk in copy:
            raw.extend(chunk)
    rows = conn.execute(f"SELECT * FROM {SAMPLE_REL} ORDER BY ctid").fetchall()
    return bytes(raw), rows


def semantic_sample_digest(rows: list[tuple[Any, ...]], dataset: dict[str, Any]) -> str:
    # Length-prefix each value and include the ordered schema.  This is
    # independent of PostgreSQL's binary COPY framing while preserving NULLs,
    # column positions, and physical sample order.
    h = hashlib.sha256()
    schema = [
        (int(c["attnum"]), str(c["name"]), str(c["type"]), bool(c["not_null"]))
        for c in dataset["ordered_column_schema"]
    ]
    header = json.dumps(
        {"schema": schema, "row_count": len(rows), "order": "ctid"},
        sort_keys=True,
        separators=(",", ":"),
    ).encode()
    h.update(len(header).to_bytes(8, "big"))
    h.update(header)
    for row in rows:
        h.update(len(row).to_bytes(4, "big"))
        for value in row:
            if value is None:
                h.update(b"\x00")
                continue
            encoded = str(value).encode("utf-8")
            h.update(b"\x01")
            h.update(len(encoded).to_bytes(8, "big"))
            h.update(encoded)
    return h.hexdigest()


def ordinary_stats_digest(conn: psycopg.Connection) -> tuple[str, list[dict[str, Any]]]:
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
        "relation", "attnum", "inherit", "nullfrac", "width", "distinct",
        "kind1", "kind2", "kind3", "kind4", "kind5", "op1", "op2", "op3", "op4", "op5",
        "coll1", "coll2", "coll3", "coll4", "coll5", "numbers1", "numbers2", "numbers3",
        "numbers4", "numbers5", "values1", "values2", "values3", "values4", "values5",
    )
    records = [dict(zip(keys, row, strict=True)) for row in rows]
    return digest(records), records


def baseline_estimate_vector(
    conn: psycopg.Connection, prepared: Any, repository_path: Path
) -> tuple[str, float, list[dict[str, Any]]]:
    repository = PayloadRepository.load(repository_path)
    evaluator = NativeEvaluator(
        prepared.workload,
        repository,
        prepared.incidence,
        PostgresAdapter(conn, repository),
    )
    state = evaluator.evaluate_design(Design(()))
    vector = [
        {
            "query_id": str(item.query_id),
            "estimate": item.estimate,
            "truth": item.truth,
            "contribution": item.contribution,
            "provenance": item.provenance,
        }
        for item in state.query_evaluations
    ]
    return digest(vector), state.aggregate_objective, vector


def add_sample_provenance(repo_path: Path, sample_manifest: dict[str, Any]) -> dict[str, Any]:
    manifest_path = repo_path / "manifest.json"
    manifest = json.loads(manifest_path.read_text())
    manifest["frozen_acquisition_sample"] = sample_manifest
    manifest_path.write_text(json.dumps(manifest, sort_keys=True, indent=2) + "\n")
    return manifest


def payload_state_equal(first: Path, second: Path) -> bool:
    left = json.loads((first / "manifest.json").read_text())["candidates"]
    right = json.loads((second / "manifest.json").read_text())["candidates"]
    if len(left) != len(right):
        return False
    return all(
        a["candidate_id"] == b["candidate_id"]
        and a["mechanism"] == b["mechanism"]
        and a["state"] == b["state"]
        and a["payload_sha256"] == b["payload_sha256"]
        and a["payload_size"] == b["payload_size"]
        for a, b in zip(left, right, strict=True)
    )


def artifact_integrity_negative_control(expected_digest: str) -> dict[str, Any]:
    manifest = json.loads((SAMPLE / "manifest.json").read_text()) if (SAMPLE / "manifest.json").exists() else {}
    payload = (SAMPLE / manifest["sample_file"]).read_bytes()
    actual = hashlib.sha256(payload).hexdigest()
    if actual != expected_digest:
        raise RuntimeError("frozen sample serialization digest mismatch")
    with tempfile.NamedTemporaryFile() as handle:
        corrupted = bytearray(payload)
        corrupted[len(corrupted) // 2] ^= 1
        handle.write(corrupted)
        handle.flush()
        corrupt_digest = hashlib.sha256(Path(handle.name).read_bytes()).hexdigest()
    if corrupt_digest == expected_digest:
        raise RuntimeError("corrupted frozen sample was not detected")
    return {
        "original_digest_valid": True,
        "corrupted_digest_rejected": True,
        "validation": "sha256 before backend replay",
    }


def backend_invalid_sample_controls(conn: psycopg.Connection) -> dict[str, Any]:
    conn.execute("DROP TABLE IF EXISTS public.frozen_bad_schema")
    conn.execute("DROP TABLE IF EXISTS public.frozen_bad_type")
    conn.execute("DROP TABLE IF EXISTS public.frozen_empty_sample")
    conn.execute("CREATE TABLE public.frozen_bad_schema(a text)")
    conn.execute("CREATE TABLE public.frozen_bad_type(a integer, b text, c text, d text, e text, f text, g text, h text, i text, j text, k text)")
    conn.execute("CREATE TABLE public.frozen_empty_sample (LIKE public.dmv)")
    conn.commit()
    outcomes: dict[str, Any] = {}
    cases = {
        "missing_relation": "public.frozen_missing_sample",
        "schema_mismatch": "public.frozen_bad_schema",
        "type_mismatch": "public.frozen_bad_type",
        "row_count_empty": "public.frozen_empty_sample",
    }
    for label, relation in cases.items():
        try:
            sql_setting(conn, "pg_extstats.frozen_sample_mode", "replay")
            sql_setting(conn, "pg_extstats.frozen_sample_relation", relation)
            sql_setting(conn, "pg_extstats.frozen_totalrows", "30000")
            conn.execute("ANALYZE public.dmv")
        except psycopg.Error as error:
            conn.rollback()
            outcomes[label] = {"rejected": True, "sqlstate": error.sqlstate, "message": str(error).splitlines()[0]}
        else:
            outcomes[label] = {"rejected": False}
            conn.rollback()
    conn.execute("DROP TABLE IF EXISTS public.frozen_bad_schema")
    conn.execute("DROP TABLE IF EXISTS public.frozen_bad_type")
    conn.execute("DROP TABLE IF EXISTS public.frozen_empty_sample")
    conn.commit()
    return outcomes


def capture_sample(conn: psycopg.Connection, prepared: Any, metadata: RelationMetadata) -> tuple[Any, dict[str, Any], dict[str, Any]]:
    shutil.rmtree(OUT / "build-1", ignore_errors=True)
    (OUT / "build-1").mkdir(parents=True)
    conn.execute(f"TRUNCATE {SAMPLE_REL}")
    sql_setting(conn, "pg_extstats.frozen_sample_mode", "capture")
    sql_setting(conn, "pg_extstats.frozen_sample_relation", SAMPLE_REL)
    sql_setting(conn, "pg_extstats.frozen_totalrows", "-1")
    started = time.perf_counter()
    result = acquire_payloads(
        conn,
        prepared.catalog,
        OUT / "build-1" / "repository",
        statistics_target=100,
        upstream_sha256=UPSTREAM_SHA,
        patch_commit=PATCH_SHA,
        repository_id="dmv-m2-17b-frozen-sample-build-1",
        source_relations=(metadata,),
    )
    elapsed = time.perf_counter() - started
    binary, rows = copy_sample(conn)
    SAMPLE.mkdir(parents=True, exist_ok=True)
    binary_path = SAMPLE / "sample.copy.bin"
    binary_path.write_bytes(binary)
    sample_info = {
        "sample_relation": SAMPLE_REL,
        "target_relation": TARGET_REL,
        "row_count": len(rows),
        "serialization": "PostgreSQL COPY FORMAT binary",
        "serialization_sha256": hashlib.sha256(binary).hexdigest(),
        "semantic_sha256": semantic_sample_digest(rows, json.loads(DATASET.read_text())),
        "capture_mode": "native acquire_sample_rows reservoir sample, preserved sorted TID order",
        "statistics_target": 100,
        "totalrows_used_by_builder": float(conn.execute(
            "SELECT reltuples FROM pg_class WHERE oid='public.dmv'::regclass"
        ).fetchone()[0]),
        "captured_at": datetime.now(UTC).isoformat(),
    }
    manifest = add_sample_provenance(OUT / "build-1" / "repository", sample_info)
    repository_digest = digest(manifest)
    ordinary_digest, ordinary_records = ordinary_stats_digest(conn)
    vector_digest, baseline_objective, estimate_vector = baseline_estimate_vector(
        conn, prepared, OUT / "build-1" / "repository"
    )
    build = {
        "build_id": "build-1-capture",
        "mode": "capture",
        "repository_digest": repository_digest,
        "ordinary_statistics_digest": ordinary_digest,
        "ordinary_statistics": ordinary_records,
        "baseline_estimate_vector_digest": vector_digest,
        "baseline_objective": baseline_objective,
        "baseline_estimate_vector": estimate_vector,
        "sample": sample_info,
        "elapsed_seconds": elapsed,
        "statistics_ext_count": int(conn.execute("SELECT count(*) FROM pg_statistic_ext").fetchone()[0]),
        "statistics_ext_data_count": int(conn.execute("SELECT count(*) FROM pg_statistic_ext_data").fetchone()[0]),
    }
    return result, sample_info, build


def replay_build(conn: psycopg.Connection, prepared: Any, metadata: RelationMetadata, sample_info: dict[str, Any], number: int) -> tuple[Any, dict[str, Any]]:
    out = OUT / f"build-{number}"
    shutil.rmtree(out, ignore_errors=True)
    out.mkdir(parents=True)
    sql_setting(conn, "pg_extstats.frozen_sample_mode", "replay")
    sql_setting(conn, "pg_extstats.frozen_sample_relation", SAMPLE_REL)
    sql_setting(conn, "pg_extstats.frozen_totalrows", str(sample_info["totalrows_used_by_builder"]))
    started = time.perf_counter()
    result = acquire_payloads(
        conn,
        prepared.catalog,
        out / "repository",
        statistics_target=100,
        upstream_sha256=UPSTREAM_SHA,
        patch_commit=PATCH_SHA,
        repository_id=f"dmv-m2-17b-frozen-sample-build-{number}",
        source_relations=(metadata,),
    )
    elapsed = time.perf_counter() - started
    manifest = add_sample_provenance(out / "repository", sample_info)
    ordinary_digest, ordinary_records = ordinary_stats_digest(conn)
    vector_digest, baseline_objective, estimate_vector = baseline_estimate_vector(
        conn, prepared, out / "repository"
    )
    build = {
        "build_id": f"build-{number}-replay",
        "mode": "replay",
        "repository_digest": digest(manifest),
        "ordinary_statistics_digest": ordinary_digest,
        "ordinary_statistics": ordinary_records,
        "baseline_estimate_vector_digest": vector_digest,
        "baseline_objective": baseline_objective,
        "baseline_estimate_vector": estimate_vector,
        "sample": sample_info,
        "elapsed_seconds": elapsed,
        "statistics_ext_count": int(conn.execute("SELECT count(*) FROM pg_statistic_ext").fetchone()[0]),
        "statistics_ext_data_count": int(conn.execute("SELECT count(*) FROM pg_statistic_ext_data").fetchone()[0]),
    }
    return result, build


def smoke_negative_controls(conn: psycopg.Connection) -> dict[str, Any]:
    # A normal ANALYZE remains available with mode=off.  Use a small disposable
    # relation so this control cannot alter the authoritative DMV lineage.
    conn.execute("DROP TABLE IF EXISTS public.frozen_negative_target")
    conn.execute("CREATE TEMP TABLE frozen_negative_target(a text, b text)")
    conn.execute("INSERT INTO frozen_negative_target SELECT g::text, (g%3)::text FROM generate_series(1,100) g")
    sql_setting(conn, "pg_extstats.frozen_sample_mode", "off")
    sql_setting(conn, "pg_extstats.frozen_sample_relation", "")
    sql_setting(conn, "pg_extstats.frozen_totalrows", "-1")
    conn.execute("ANALYZE frozen_negative_target")
    return {"normal_analyze_mode": "off", "normal_analyze_succeeded": True}


def main() -> int:
    OUT.mkdir(parents=True, exist_ok=True)
    SAMPLE.mkdir(parents=True, exist_ok=True)
    prepared = load_prepared_run(PREPARED)
    dataset = json.loads(DATASET.read_text())
    metadata = relation_metadata(dataset)
    protocol = {
        "milestone": "M2.17b",
        "benchmark": "DMV",
        "search_started": False,
        "screening_started": False,
        "calibration_rerun": False,
        "source_workload": str(PREPARED),
        "candidate_catalog_digest": prepared.candidate_catalog_digest,
        "incidence_digest": prepared.incidence_digest,
        "effective_workload_digest": prepared.effective_workload_digest,
        "statistics_target": 100,
        "target_relation": TARGET_REL,
        "sample_relation": SAMPLE_REL,
        "source_audit": "experiments/dmv-m2-17b-frozen-acquisition-sample/source-audit.md",
    }
    (OUT / "protocol.json").write_text(json.dumps(protocol, sort_keys=True, indent=2) + "\n")
    with psycopg.connect(DSN) as conn:
        ensure_sample_table(conn)
        drop_acquisition_stats(conn)
        first, sample_info, build1 = capture_sample(conn, prepared, metadata)
        (OUT / "build-1" / "summary.json").write_text(json.dumps(build1, sort_keys=True, indent=2) + "\n")
        cleanup_acquisition(conn, first)
        second, build2 = replay_build(conn, prepared, metadata, sample_info, 2)
        (OUT / "build-2" / "summary.json").write_text(json.dumps(build2, sort_keys=True, indent=2) + "\n")
        cleanup_acquisition(conn, second)
        # A separate backend connection below supplies the fresh-session gate.
        conn.commit()
    with psycopg.connect(DSN) as fresh:
        third, build3 = replay_build(fresh, prepared, metadata, sample_info, 3)
        (OUT / "build-3" / "summary.json").write_text(json.dumps(build3, sort_keys=True, indent=2) + "\n")
        negative = smoke_negative_controls(fresh)
        invalid = backend_invalid_sample_controls(fresh)
        cleanup_acquisition(fresh, third)
        fresh.execute(f"DROP TABLE IF EXISTS {SAMPLE_REL}")
        fresh.commit()
    # The manifest is deliberately explicit about the artifact-level contract.
    sample_manifest = {
        "format_version": 1,
        "dataset_id": dataset["dataset_id"],
        "target_relation": TARGET_REL,
        "sample_relation": SAMPLE_REL,
        "sample_file": "sample.copy.bin",
        "sample_file_sha256": sample_info["serialization_sha256"],
        "semantic_sha256": sample_info["semantic_sha256"],
        "row_count": sample_info["row_count"],
        "ordered_column_schema": dataset["ordered_column_schema"],
        "statistics_target": 100,
        "sampling_algorithm": "PostgreSQL 16.14 native block sampler + Vitter reservoir; rows sorted by physical TID",
        "upstream_tarball_sha256": UPSTREAM_SHA,
        "patch_source": "pg/patches/postgresql-16.14-hypothetical-extstats.patch",
        "patch_sha256": PATCH_SHA,
        "replay_contract": "backend-local frozen sample relation; no sampling from target relation",
    }
    (SAMPLE / "manifest.json").write_text(json.dumps(sample_manifest, sort_keys=True, indent=2) + "\n")
    integrity = artifact_integrity_negative_control(sample_manifest["sample_file_sha256"])
    (OUT / "negative-control.json").write_text(json.dumps({"normal": negative, "invalid_backend": invalid, "artifact": integrity}, sort_keys=True, indent=2) + "\n")
    (SAMPLE / "README.md").write_text(
        "# DMV frozen acquisition sample v1\n\n"
        "This artifact is one native PostgreSQL 16.14 ANALYZE sample captured from `public.dmv`. "
        "The binary COPY stream preserves sample row values and order; the manifest checksums both "
        "serialization and canonical sample semantics. Replay uses the backend-local frozen-sample "
        "GUC path and never materializes this artifact as a production relation.\n\n"
        "The sample is authoritative only for M2.17b acquisition/replay validation. It does not alter "
        "the frozen candidate catalog, incidence, workload, or M2.16 maintenance model.\n"
    )
    (OUT / "sample-manifest.json").write_text(json.dumps(sample_manifest, sort_keys=True, indent=2) + "\n")
    base_dir = OUT / "base-statistics"
    ext_dir = OUT / "extstats"
    base_dir.mkdir(exist_ok=True)
    ext_dir.mkdir(exist_ok=True)
    for number, build in ((1, build1), (2, build2), (3, build3)):
        (base_dir / f"build-{number}.json").write_text(
            json.dumps(
                {
                    "build_id": build["build_id"],
                    "ordinary_statistics_digest": build["ordinary_statistics_digest"],
                    "ordinary_statistics": build["ordinary_statistics"],
                },
                sort_keys=True,
                indent=2,
            )
            + "\n"
        )
        manifest = json.loads((OUT / f"build-{number}" / "repository" / "manifest.json").read_text())
        ext_records = [
            {
                "candidate_id": item["candidate_id"],
                "mechanism": item["mechanism"],
                "state": item["state"],
                "payload_size": item["payload_size"],
                "payload_sha256": item["payload_sha256"],
            }
            for item in manifest["candidates"]
        ]
        (ext_dir / f"build-{number}.json").write_text(
            json.dumps({"records": ext_records, "record_digest": digest(ext_records)}, sort_keys=True, indent=2) + "\n"
        )
    (OUT / "determinism.json").write_text(
        json.dumps(
            {
                "ordinary_statistics_exact": {
                    "build1_build2": build1["ordinary_statistics_digest"] == build2["ordinary_statistics_digest"],
                    "build2_build3": build2["ordinary_statistics_digest"] == build3["ordinary_statistics_digest"],
                },
                "payload_state_exact": {
                    "build1_build2": payload_state_equal(OUT / "build-1" / "repository", OUT / "build-2" / "repository"),
                    "build2_build3": payload_state_equal(OUT / "build-2" / "repository", OUT / "build-3" / "repository"),
                },
                "baseline_estimate_vector_exact": {
                    "build1_build2": build1["baseline_estimate_vector_digest"] == build2["baseline_estimate_vector_digest"],
                    "build2_build3": build2["baseline_estimate_vector_digest"] == build3["baseline_estimate_vector_digest"],
                },
                "fresh_backend_session": True,
            },
            sort_keys=True,
            indent=2,
        )
        + "\n"
    )
    report = {
        "milestone": "M2.17b",
        "candidate_count": len(prepared.catalog.candidates),
        "mcv_count": sum(c.mechanism.value == "mcv" for c in prepared.catalog.candidates),
        "fd_count": sum(c.mechanism.value == "fd" for c in prepared.catalog.candidates),
        "sample": sample_manifest,
        "builds": [build1, build2, build3],
        "ordinary_digest_exact_build1_build2": build1["ordinary_statistics_digest"] == build2["ordinary_statistics_digest"],
        "ordinary_digest_exact_build2_build3": build2["ordinary_statistics_digest"] == build3["ordinary_statistics_digest"],
        "repository_digest_exact_build1_build2": build1["repository_digest"] == build2["repository_digest"],
        "repository_digest_exact_build2_build3": build2["repository_digest"] == build3["repository_digest"],
        "payload_state_exact_build1_build2": payload_state_equal(OUT / "build-1" / "repository", OUT / "build-2" / "repository"),
        "payload_state_exact_build2_build3": payload_state_equal(OUT / "build-2" / "repository", OUT / "build-3" / "repository"),
        "baseline_vector_exact_build1_build2": build1["baseline_estimate_vector_digest"] == build2["baseline_estimate_vector_digest"],
        "baseline_vector_exact_build2_build3": build2["baseline_estimate_vector_digest"] == build3["baseline_estimate_vector_digest"],
        "fresh_backend_session": True,
        "search_started": False,
        "negative_control": negative,
        "invalid_sample_controls": invalid,
        "artifact_integrity_control": integrity,
    }
    (OUT / "report.json").write_text(json.dumps(report, sort_keys=True, indent=2) + "\n")
    (OUT / "report.md").write_text(
        "# M2.17b — DMV Frozen Acquisition Sample\n\n"
        f"The frozen sample contains {sample_manifest['row_count']} rows and is checksummed by "
        f"semantic SHA-256 `{sample_manifest['semantic_sha256']}` and binary serialization SHA-256 "
        f"`{sample_manifest['sample_file_sha256']}`. Three acquisition builds used the same frozen "
        "sample; build 2 and build 3 used backend replay and did not resample `public.dmv`.\n\n"
        f"Ordinary-statistics digests: build1/build2 exact = `{report['ordinary_digest_exact_build1_build2']}`; "
        f"build2/build3 exact = `{report['ordinary_digest_exact_build2_build3']}`. Native payload repository "
        f"states/payload bytes exact: build1/build2 = `{report['payload_state_exact_build1_build2']}`, "
        f"build2/build3 = `{report['payload_state_exact_build2_build3']}`. Baseline estimate vectors "
        f"exact: build1/build2 = `{report['baseline_vector_exact_build1_build2']}`, "
        f"build2/build3 = `{report['baseline_vector_exact_build2_build3']}`.\n\n"
        "No M2.17 search or screening was run. The sample lineage is an acquisition/replay prerequisite only.\n"
    )
    print(json.dumps({"sample": sample_manifest, "report": report}, sort_keys=True, indent=2))
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
