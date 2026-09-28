"""Physical aggregate ANALYZE calibration runner and artifact writer."""

from __future__ import annotations

import csv
import hashlib
import json
import math
import random
import statistics
import subprocess
import time
import uuid
from dataclasses import asdict
from datetime import UTC, datetime
from pathlib import Path
from typing import Any

from psycopg import Connection, sql

from pg_extstats_advisor.calibration.config import CalibrationConfig
from pg_extstats_advisor.calibration.design import (
    CalibrationCandidate,
    CalibrationConfiguration,
    candidate_pool,
    configuration_design,
)
from pg_extstats_advisor.calibration.fit import (
    TimingRow,
    coefficient_intervals,
    fit_aggregate,
)
from pg_extstats_advisor.cost.empirical import MODEL_TYPE, MODEL_UNIT, artifact_digest


def _write_json(path: Path, value: Any) -> None:
    temporary = path.with_suffix(path.suffix + ".tmp")
    temporary.write_text(json.dumps(value, sort_keys=True, indent=2) + "\n")
    temporary.replace(path)


def _split_relation(relation: str) -> tuple[str, str]:
    parts = relation.split(".")
    if len(parts) == 1:
        return "public", parts[0]
    if len(parts) == 2 and all(parts):
        return parts[0], parts[1]
    raise ValueError("relation must be an unquoted schema-qualified name")


def _relation_metadata(connection: Connection[Any], relation: str) -> dict[str, Any]:
    row = connection.execute(
        """
        SELECT c.oid, n.nspname, c.relname, c.reltuples::double precision,
               c.relpages, c.relfilenode, pg_total_relation_size(c.oid),
               current_database(), current_setting('server_version'),
               current_setting('default_statistics_target')
        FROM pg_class c JOIN pg_namespace n ON n.oid = c.relnamespace
        WHERE c.oid = to_regclass(%s)
        """,
        (relation,),
    ).fetchone()
    if row is None:
        raise ValueError(f"calibration relation does not exist: {relation}")
    columns = connection.execute(
        """
        SELECT a.attnum, a.attname, format_type(a.atttypid, a.atttypmod), a.attnotnull
        FROM pg_attribute a
        WHERE a.attrelid = %s AND a.attnum > 0 AND NOT a.attisdropped
        ORDER BY a.attnum
        """,
        (row[0],),
    ).fetchall()
    count = connection.execute(
        sql.SQL("SELECT count(*) FROM {}.{}").format(sql.Identifier(row[1]), sql.Identifier(row[2]))
    ).fetchone()[0]
    schema = [
        {"attnum": item[0], "name": item[1], "type": item[2], "not_null": item[3]}
        for item in columns
    ]
    logical = hashlib.sha256(
        json.dumps({"relation": f"{row[1]}.{row[2]}", "rows": count, "schema": schema},
                   sort_keys=True, separators=(",", ":")).encode()
    ).hexdigest()
    physical = hashlib.sha256(
        json.dumps({"oid": row[0], "relfilenode": row[5], "bytes": row[6]},
                   sort_keys=True, separators=(",", ":")).encode()
    ).hexdigest()
    return {
        "oid": row[0],
        "relation": f"{row[1]}.{row[2]}",
        "reltuples": row[3],
        "relpages": row[4],
        "relfilenode": row[5],
        "total_relation_bytes": row[6],
        "database": row[7],
        "postgres_version": row[8],
        "default_statistics_target": int(row[9]),
        "row_count": count,
        "columns": schema,
        "logical_fingerprint": logical,
        "physical_fingerprint": physical,
    }


def _existing_statistics(connection: Connection[Any], relation_oid: int) -> tuple[str, ...]:
    return tuple(
        row[0]
        for row in connection.execute(
            "SELECT n.nspname || '.' || e.stxname FROM pg_statistic_ext e "
            "JOIN pg_namespace n ON n.oid=e.stxnamespace WHERE e.stxrelid=%s ORDER BY 1",
            (relation_oid,),
        ).fetchall()
    )


def _drop_owned(connection: Connection[Any], schema: str) -> None:
    rows = connection.execute(
        "SELECT e.stxname FROM pg_statistic_ext e JOIN pg_namespace n ON n.oid=e.stxnamespace "
        "WHERE n.nspname=%s AND e.stxname LIKE 'pgextadv_cal_%%' ORDER BY e.stxname",
        (schema,),
    ).fetchall()
    for (name,) in rows:
        connection.execute(
            sql.SQL("DROP STATISTICS {}.{}").format(sql.Identifier(schema), sql.Identifier(name))
        )


def _activate(
    connection: Connection[Any],
    relation: str,
    configuration: CalibrationConfiguration,
    target: int,
) -> None:
    schema, table = _split_relation(relation)
    _drop_owned(connection, schema)
    for candidate in configuration.mcv + configuration.fd:
        kind = sql.SQL("mcv") if candidate.mechanism == "mcv" else sql.SQL("dependencies")
        connection.execute(
            sql.SQL("CREATE STATISTICS {}.{} ({}) ON {}, {} FROM {}.{}").format(
                sql.Identifier(schema),
                sql.Identifier(candidate.object_name),
                kind,
                sql.Identifier(candidate.columns[0]),
                sql.Identifier(candidate.columns[1]),
                sql.Identifier(schema),
                sql.Identifier(table),
            )
        )
        connection.execute(
            sql.SQL("ALTER STATISTICS {}.{} SET STATISTICS {}").format(
                sql.Identifier(schema), sql.Identifier(candidate.object_name), sql.Literal(target)
            )
        )


def _summary(values: list[float]) -> dict[str, float]:
    mean = statistics.fmean(values)
    stddev = statistics.stdev(values) if len(values) > 1 else 0.0
    return {
        "mean_seconds": mean,
        "median_seconds": statistics.median(values),
        "stddev_seconds": stddev,
        "cv": stddev / mean if mean else math.inf,
        "min_seconds": min(values),
        "max_seconds": max(values),
    }


def _repo_commit() -> str:
    return subprocess.run(
        ["git", "rev-parse", "HEAD"], check=True, text=True, capture_output=True
    ).stdout.strip()


def _candidate_dict(candidate: CalibrationCandidate) -> dict[str, Any]:
    return asdict(candidate)


def run_calibration(config: CalibrationConfig, connection: Connection[Any]) -> dict[str, Any]:
    root = config.output_path
    if root.exists():
        raise FileExistsError(f"calibration output directory exists: {root}")
    root.mkdir(parents=True)
    metadata = _relation_metadata(connection, config.relation)
    existing = _existing_statistics(connection, int(metadata["oid"]))
    if existing:
        raise ValueError(f"calibration relation already has extended statistics: {existing}")
    available_columns = tuple(item["name"] for item in metadata["columns"])
    columns = config.columns or available_columns
    unknown = set(columns) - set(available_columns)
    if unknown:
        raise ValueError(f"unknown calibration columns: {sorted(unknown)}")
    pool = candidate_pool(columns)
    configurations = configuration_design(pool, config.count_levels)
    run_id = f"cal-{uuid.uuid4().hex[:12]}"
    order = list(configurations)
    random.Random(config.seed).shuffle(order)
    rows: list[dict[str, Any]] = []
    schema, table = _split_relation(metadata["relation"])
    analyze = sql.SQL("ANALYZE {}.{}").format(sql.Identifier(schema), sql.Identifier(table))
    try:
        for order_index, configuration in enumerate(order):
            _activate(connection, metadata["relation"], configuration, config.statistics_target)
            started = time.perf_counter()
            connection.execute(analyze)
            rows.append({
                "run_id": run_id,
                "configuration_id": configuration.configuration_id,
                "configuration_kind": configuration.kind,
                "role": configuration.role,
                "n_mcv": configuration.n_mcv,
                "n_fd": configuration.n_fd,
                "statistics_target": config.statistics_target,
                "repetition": 0,
                "elapsed_seconds": time.perf_counter() - started,
                "is_warmup": True,
                "order_index": order_index,
            })
            for repetition in range(1, config.repetitions + 1):
                started = time.perf_counter()
                connection.execute(analyze)
                rows.append({
                    **{key: value for key, value in rows[-1].items() if key not in {"repetition", "elapsed_seconds", "is_warmup"}},
                    "repetition": repetition,
                    "elapsed_seconds": time.perf_counter() - started,
                    "is_warmup": False,
                })
    finally:
        _drop_owned(connection, schema)

    with (root / "raw-timings.csv").open("w", newline="") as stream:
        writer = csv.DictWriter(stream, fieldnames=list(rows[0]))
        writer.writeheader()
        writer.writerows(rows)
    measured = [row for row in rows if not row["is_warmup"]]
    by_configuration: dict[str, list[float]] = {}
    for row in measured:
        by_configuration.setdefault(str(row["configuration_id"]), []).append(
            float(row["elapsed_seconds"])
        )
    summaries = []
    by_id = {item.configuration_id: item for item in configurations}
    for configuration_id in sorted(by_configuration):
        item = by_id[configuration_id]
        summaries.append({
            "configuration_id": configuration_id,
            "configuration_kind": item.kind,
            "role": item.role,
            "n_mcv": item.n_mcv,
            "n_fd": item.n_fd,
            **_summary(by_configuration[configuration_id]),
        })
    with (root / "configuration-summary.csv").open("w", newline="") as stream:
        writer = csv.DictWriter(stream, fieldnames=list(summaries[0]))
        writer.writeheader()
        writer.writerows(summaries)
    timing_rows = tuple(
        TimingRow(
            str(row["configuration_id"]),
            str(row["role"]),
            int(row["n_mcv"]),
            int(row["n_fd"]),
            float(row["elapsed_seconds"]),
        )
        for row in measured
    )
    fit = fit_aggregate(timing_rows)
    intervals = coefficient_intervals(fit)
    heldout = []
    for item in summaries:
        if item["role"] != "held-out":
            continue
        predicted = (
            fit.intercept_seconds
            + fit.mcv_seconds_per_object * item["n_mcv"]
            + fit.fd_seconds_per_object * item["n_fd"]
        )
        absolute = abs(item["mean_seconds"] - predicted)
        heldout.append({
            "configuration_id": item["configuration_id"],
            "n_mcv": item["n_mcv"],
            "n_fd": item["n_fd"],
            "observed_mean_seconds": item["mean_seconds"],
            "predicted_seconds": predicted,
            "absolute_error_seconds": absolute,
            "relative_error": absolute / item["mean_seconds"] if item["mean_seconds"] else math.inf,
        })
    max_cv = max(item["cv"] for item in summaries)
    max_heldout = max((item["relative_error"] for item in heldout), default=math.inf)
    slopes_positive = fit.mcv_seconds_per_object >= 0 and fit.fd_seconds_per_object >= 0
    gates = {
        "within_configuration_cv": {
            "passed": max_cv <= config.gates.max_cv,
            "observed": max_cv,
            "threshold": config.gates.max_cv,
            "operator": "<=",
        },
        "fit_r_squared": {
            "passed": fit.r_squared >= config.gates.min_r_squared,
            "observed": fit.r_squared,
            "threshold": config.gates.min_r_squared,
            "operator": ">=",
        },
        "heldout_max_relative_error": {
            "passed": max_heldout <= config.gates.max_heldout_relative_error,
            "observed": max_heldout,
            "threshold": config.gates.max_heldout_relative_error,
            "operator": "<=",
        },
        "nonnegative_slopes": {
            "passed": slopes_positive,
            "observed": {
                "mcv": fit.mcv_seconds_per_object,
                "fd": fit.fd_seconds_per_object,
            },
            "threshold": 0,
            "operator": ">=",
        },
    }
    fit_dict = {
        "model": "T=intercept+beta_mcv*n_mcv+beta_fd*n_fd",
        "training_observations": sum(item.role == "fit" for item in timing_rows),
        "intercept_seconds": fit.intercept_seconds,
        "mcv_seconds_per_object": fit.mcv_seconds_per_object,
        "fd_seconds_per_object": fit.fd_seconds_per_object,
        "r_squared": fit.r_squared,
        "rmse_seconds": fit.rmse_seconds,
        "median_relative_error": fit.median_relative_error,
        "max_relative_error": fit.max_relative_error,
        "coefficient_standard_errors": fit.coefficient_standard_errors,
        "normal_approximation_95_percent_intervals": intervals,
        "ci_caveat": "ordinary OLS approximation; repeated observations are not independent runs",
    }
    max_level = max(item.n_mcv for item in configurations)
    median_stddev = statistics.median(item["stddev_seconds"] for item in summaries)
    stability = {
        "gates": gates,
        "same_count_subset_gate": "not_applicable_one_deterministic_subset_per_count",
        "mcv_aggregate_signal_seconds": fit.mcv_seconds_per_object * max_level,
        "fd_aggregate_signal_seconds": fit.fd_seconds_per_object * max_level,
        "median_configuration_stddev_seconds": median_stddev,
    }
    repo_commit = _repo_commit()
    patch_path = Path(__file__).parents[3] / "pg" / "patches" / "postgresql-16.14-hypothetical-extstats.patch"
    patch_digest = hashlib.sha256(patch_path.read_bytes()).hexdigest()
    sums = (Path(__file__).parents[3] / "pg" / "SHA256SUMS").read_text().split()
    upstream_sha256 = sums[0] if sums else "unresolved"
    provenance = {
        "repo_commit": repo_commit,
        "postgres_version": metadata["postgres_version"],
        "upstream_tarball_sha256": upstream_sha256,
        "pg_patch_sha256": patch_digest,
        "database_identity": metadata["database"],
        "relation_identity": metadata["relation"],
        "relation_logical_fingerprint": metadata["logical_fingerprint"],
        "relation_physical_fingerprint": metadata["physical_fingerprint"],
        "row_count": metadata["row_count"],
        "reltuples": metadata["reltuples"],
        "column_schema": metadata["columns"],
        "statistics_target": config.statistics_target,
        "candidate_arity": 2,
        "mechanisms": ["mcv", "fd"],
        "mcv_candidate_pool_size": len(pool) // 2,
        "fd_candidate_pool_size": len(pool) // 2,
        "repetitions": config.repetitions,
        "seed": config.seed,
        "timing_clock": "time.perf_counter",
        "cache_policy": "long-lived server; no cache flush; one untimed warmup per configuration",
        "config_digest": config.digest,
        "environment_description": config.environment_description,
        "hardware_provenance_complete": False,
    }
    _write_json(root / "config.json", json.loads(config.canonical_json()))
    _write_json(root / "protocol.json", {
        "dependent_variable": "full relation ANALYZE elapsed wall-clock seconds",
        "ddl_included": False,
        "warmup_count": 1,
        "measured_repetitions": config.repetitions,
        "execution_order": "deterministic seeded configuration shuffle",
        "seed": config.seed,
        "gates": asdict(config.gates),
    })
    _write_json(root / "candidate-pool.json", {"candidate_arity": 2, "candidates": [_candidate_dict(item) for item in pool]})
    _write_json(root / "configurations.json", {
        "configurations": [
            {
                "configuration_id": item.configuration_id,
                "kind": item.kind,
                "role": item.role,
                "n_mcv": item.n_mcv,
                "n_fd": item.n_fd,
                "candidate_ids": [candidate.candidate_id for candidate in item.mcv + item.fd],
            }
            for item in configurations
        ],
        "execution_order": [item.configuration_id for item in order],
    })
    _write_json(root / "fit.json", fit_dict)
    _write_json(root / "heldout.json", {"configurations": heldout})
    accepted = all(bool(item["passed"]) for item in gates.values())
    report = {
        "format_version": 1,
        "status": "accepted" if accepted else "rejected",
        "model_type": MODEL_TYPE,
        "fit": fit_dict,
        "stability": stability,
        "heldout": heldout,
        "calibration_provenance": provenance,
        "completed_at": datetime.now(UTC).isoformat(),
    }
    _write_json(root / "calibration-report.json", report)
    if accepted:
        model = {
            "format_version": 1,
            "model_type": MODEL_TYPE,
            "model_version": f"cal-{config.digest[:16]}",
            "unit": MODEL_UNIT,
            "statistics_target": config.statistics_target,
            "candidate_arity": 2,
            "parameters": {
                "mcv_ms_per_object": str(fit.mcv_seconds_per_object * 1000),
                "fd_ms_per_object": str(fit.fd_seconds_per_object * 1000),
            },
            "fit": fit_dict,
            "stability": stability,
            "calibration_provenance": provenance,
        }
        model["digest"] = artifact_digest(model)
        _write_json(root / "maintenance-model.json", model)
        report["maintenance_model_digest"] = model["digest"]
        _write_json(root / "calibration-report.json", report)
    return report
