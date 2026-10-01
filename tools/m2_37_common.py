"""Shared, measurement-only helpers for the M2.37 performance suites."""

from __future__ import annotations

import csv
import hashlib
import json
import os
import platform
import random
import statistics
import subprocess
import time
from collections.abc import Iterable
from pathlib import Path
from typing import Any

import psycopg

from pg_extstats_advisor.models import Candidate, CandidateId, QueryEvaluation
from pg_extstats_advisor.objective.qerror import aggregate_objective, q_error
from pg_extstats_advisor.payloads.repository import NativePayloadState, PayloadRepository
from pg_extstats_advisor.postgres.extraction import extract_target_estimate

ROOT = Path(__file__).resolve().parents[1]
DMV_PREPARED = ROOT / "experiments/dmv-m2-15-singletons/prepared-run"
DMV_REPOSITORY = ROOT / ".build/artifact-cache/dmv-m2-17b-frozen-sample-v1/repository"
DMV_FROZEN = ROOT / "datasets/dmv-frozen-acquisition-sample-v1"
DMV_SEARCH = ROOT / "experiments/dmv-m2-17d-frozen-full72-add/final-result.json"
CENSUS_PREPARED = ROOT / "experiments/census-m2-7/prepared-run"
CENSUS_REPOSITORY = CENSUS_PREPARED / "repository"
DMV_DSN = (
    f"host={ROOT}/.build/pg16.14-experiment-socket port=55436 "
    "dbname=pgextadv_exp16_dmv user=postgres"
)
CENSUS_DSN = (
    f"host={ROOT}/.build/pg16.14-experiment-socket port=55436 "
    "dbname=pgextadv_exp16_census_m27_acq user=postgres"
)
TARGET = "public.dmv"
SAMPLE = "public.pgextadv_frozen_sample"
TARGET_T = 100
DMV_ROWS = 11_687_702
DMV_SAMPLE_ROWS = 30_000
EXPECTED_PG = "16.14"


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


def set_replay(conn: psycopg.Connection[Any], mode: str) -> None:
    conn.execute("SELECT set_config(%s,%s,false)", ("pg_extstats.frozen_sample_mode", mode))
    conn.execute("SELECT set_config(%s,%s,false)", ("pg_extstats.frozen_sample_relation", SAMPLE))
    conn.execute("SELECT set_config(%s,%s,false)", ("pg_extstats.frozen_totalrows", str(DMV_ROWS)))


def stat_counts(conn: psycopg.Connection[Any], prefix: str = "pgextadv_%") -> tuple[int, int]:
    stats = int(conn.execute(
        "SELECT count(*) FROM pg_statistic_ext WHERE stxname LIKE %s", (prefix,)
    ).fetchone()[0])
    data = int(conn.execute(
        "SELECT count(*) FROM pg_statistic_ext_data d "
        "JOIN pg_statistic_ext e ON e.oid=d.stxoid WHERE e.stxname LIKE %s", (prefix,)
    ).fetchone()[0])
    return stats, data


def drop_definitions(conn: psycopg.Connection[Any], repository: PayloadRepository) -> None:
    for frozen in repository.payloads:
        candidate = frozen.candidate
        qname = f"{quote_identifier(relation_schema(candidate))}.{quote_identifier(statistic_name(candidate))}"
        conn.execute(f"DROP STATISTICS IF EXISTS {qname}")


def create_definitions(conn: psycopg.Connection[Any], candidates: Iterable[Candidate]) -> None:
    for candidate in candidates:
        mechanism = "mcv" if candidate.mechanism.value == "mcv" else "dependencies"
        qname = f"{quote_identifier(relation_schema(candidate))}.{quote_identifier(statistic_name(candidate))}"
        attrs = ", ".join(quote_identifier(item) for item in candidate.attributes)
        conn.execute(
            f"CREATE STATISTICS {qname} ({mechanism}) ON {attrs} "
            f"FROM {qualified_relation(candidate.relation_name)}"
        )
        conn.execute(f"ALTER STATISTICS {qname} SET STATISTICS {TARGET_T}")


def load_dmv_sample(conn: psycopg.Connection[Any]) -> None:
    conn.execute(f"DROP TABLE IF EXISTS {SAMPLE}")
    conn.execute(f"CREATE UNLOGGED TABLE {SAMPLE} (LIKE {TARGET} INCLUDING DEFAULTS)")
    with conn.cursor().copy(f"COPY {SAMPLE} FROM STDIN (FORMAT binary)") as copy:
        copy.write((DMV_FROZEN / "sample.copy.bin").read_bytes())


def explain_objective(conn: psycopg.Connection[Any], workload: Any) -> tuple[dict[str, Any], dict[str, float]]:
    started = time.perf_counter()
    raw: list[tuple[str, float, float]] = []
    for query in sorted(workload.queries, key=lambda item: item.query_id):
        row = conn.execute(f"EXPLAIN (FORMAT JSON) {query.sql}").fetchone()
        if row is None:
            raise RuntimeError(f"EXPLAIN returned no row for {query.query_id}")
        raw.append((str(query.query_id), extract_target_estimate(row[0], query.target_relation), query.truth))
    explain_seconds = time.perf_counter() - started
    evaluations = tuple(
        QueryEvaluation(query_id, estimate, truth, q_error(estimate, truth), "native-explain:16.14")
        for query_id, estimate, truth in raw
    )
    objective = aggregate_objective(evaluations)
    rows = [
        {"query_id": str(item.query_id), "estimate": item.estimate, "truth": item.truth,
         "q_error": item.contribution}
        for item in evaluations
    ]
    return ({
        "objective": objective,
        "estimate_vector_digest": digest(rows),
        "query_count": len(evaluations),
        "planner_calls": len(evaluations),
        "explain_elapsed_seconds": explain_seconds,
    }, {query_id: estimate for query_id, estimate, _truth in raw})


def candidate_identity(candidate: Candidate) -> dict[str, Any]:
    return {
        "candidate_id": str(candidate.candidate_id),
        "relation_name": candidate.relation_name,
        "mechanism": candidate.mechanism.value,
        "attributes": list(candidate.attributes),
        "precedence_rank": candidate.precedence_rank,
    }


def ordered_candidate_digest(candidates: Iterable[Candidate]) -> str:
    return digest([candidate_identity(candidate) for candidate in candidates])


def config_record(repository: PayloadRepository, design_id: str, source: str, ids: Iterable[CandidateId]) -> dict[str, Any]:
    design = repository.catalog.normalize_design(set(ids))
    selected = tuple(repository.catalog.by_id[item] for item in design.candidate_ids)
    return {
        "config_id": design_id,
        "source": source,
        "selected_design": [str(item.candidate_id) for item in selected],
        "design_size": len(selected),
        "mcv_count": sum(item.mechanism.value == "mcv" for item in selected),
        "fd_count": sum(item.mechanism.value == "fd" for item in selected),
        "absent_native_count": sum(
            repository.by_candidate[item.candidate_id].state is NativePayloadState.ABSENT_NATIVE
            for item in selected
        ),
        "ordered_candidate_digest": ordered_candidate_digest(selected),
    }


def build_m2_36_suite(repository: PayloadRepository) -> list[dict[str, Any]]:
    search = json.loads(DMV_SEARCH.read_text())
    accepted_ids = [CandidateId(str(item["candidate_id"])) for item in search["accepted_sequence"]]
    states = [
        ("empty", "empty design", ()),
        ("prefix-01", "M2.17d accepted ADD trajectory prefix 1", accepted_ids[:1]),
        ("prefix-05", "M2.17d accepted ADD trajectory prefix 5", accepted_ids[:5]),
        ("prefix-16", "M2.17d accepted ADD trajectory prefix 16", accepted_ids[:16]),
        ("prefix-30", "M2.17d accepted ADD trajectory prefix 30", accepted_ids[:30]),
        ("final-31", "M2.17d final 31-object recommendation", accepted_ids),
    ]
    return [config_record(repository, name, source, ids) for name, source, ids in states]


def build_configuration_pool(repository: PayloadRepository, count: int = 256) -> list[dict[str, Any]]:
    """Build a deterministic, non-prefix-heavy pool of valid designs."""
    candidates = list(repository.catalog.candidates)
    by_mechanism = {
        "mcv": [item.candidate_id for item in candidates if item.mechanism.value == "mcv"],
        "fd": [item.candidate_id for item in candidates if item.mechanism.value == "fd"],
    }
    configs: list[dict[str, Any]] = []
    seen: set[tuple[str, ...]] = set()

    def add(name: str, source: str, ids: Iterable[CandidateId]) -> None:
        record = config_record(repository, name, source, ids)
        key = tuple(record["selected_design"])
        if key not in seen:
            seen.add(key)
            configs.append(record)

    add("cfg-0000", "empty", ())
    add("cfg-0001", "first MCV singleton", by_mechanism["mcv"][:1])
    add("cfg-0002", "first FD singleton", by_mechanism["fd"][:1])
    add("cfg-0003", "all MCV candidates", by_mechanism["mcv"])
    add("cfg-0004", "all FD candidates", by_mechanism["fd"])
    add("cfg-0005", "mixed mechanism prefix", by_mechanism["mcv"][:4] + by_mechanism["fd"][:4])
    rng = random.Random(2037)
    while len(configs) < count:
        size = rng.choice((1, 2, 3, 4, 8, 12, 16, 24, 32, 40, 48, 56, 64, 72))
        ids = [item.candidate_id for item in rng.sample(candidates, k=min(size, len(candidates)))]
        add(f"cfg-{len(configs):04d}", f"deterministic seed-2037 subset size {size}", ids)
    # Renumber after duplicate elimination so IDs are stable and contiguous.
    for index, record in enumerate(configs):
        record["config_id"] = f"cfg-{index:04d}"
    return configs[:count]


def environment_metadata(conn: psycopg.Connection[Any], database: str) -> dict[str, Any]:
    settings = {
        name: str(conn.execute(f"SHOW {name}").fetchone()[0])
        for name in ("shared_buffers", "work_mem", "random_page_cost", "jit", "max_parallel_workers_per_gather")
    }
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
        "database": database,
        "dsn_socket": str(ROOT / ".build/pg16.14-experiment-socket"),
        "settings": settings,
        "cache_protocol": "one untimed warmup plus five measured repetitions; no OS/PostgreSQL cache flushing",
        "cache_flushed": False,
        "postgres_startup_measured": False,
    }


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


def median_stats(values: Iterable[float]) -> dict[str, float]:
    vals = [float(item) for item in values]
    return {
        "median": statistics.median(vals),
        "min": min(vals),
        "max": max(vals),
        "p25": statistics.quantiles(vals, n=4, method="inclusive")[0] if len(vals) > 1 else vals[0],
        "p75": statistics.quantiles(vals, n=4, method="inclusive")[2] if len(vals) > 1 else vals[0],
    }
