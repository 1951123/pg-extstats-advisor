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
    candidate_pool_from_catalog,
    configuration_design,
)
from pg_extstats_advisor.calibration.fit import (
    TimingRow,
    assess_gates,
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
               c.relpages, c.relfilenode, c.relpersistence, pg_total_relation_size(c.oid),
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
        json.dumps({"oid": row[0], "relfilenode": row[5], "bytes": row[7]},
                   sort_keys=True, separators=(",", ":")).encode()
    ).hexdigest()
    return {
        "oid": row[0],
        "relation": f"{row[1]}.{row[2]}",
        "reltuples": row[3],
        "relpages": row[4],
        "relfilenode": row[5],
        "relation_persistence": row[6],
        "total_relation_bytes": row[7],
        "database": row[8],
        "postgres_version": row[9],
        "default_statistics_target": int(row[10]),
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


def _dataset_provenance(config: CalibrationConfig, metadata: dict[str, Any]) -> dict[str, Any] | None:
    if config.dataset_provenance_path is None:
        return None
    artifact = json.loads(config.dataset_provenance_path.read_text())
    if artifact.get("relation") != metadata["relation"]:
        raise ValueError("dataset provenance relation mismatch")
    if int(artifact.get("row_count")) != int(metadata["row_count"]):
        raise ValueError("dataset provenance row-count mismatch")
    if artifact.get("relation_persistence") != metadata["relation_persistence"]:
        raise ValueError("dataset provenance persistence mismatch")
    expected_columns = [
        {
            "attnum": int(item["attnum"]),
            "name": str(item["name"]),
            "not_null": bool(item["not_null"]),
            "type": str(item["type"]),
        }
        for item in metadata["columns"]
    ]
    if artifact.get("ordered_column_schema") != expected_columns:
        raise ValueError("dataset provenance schema mismatch")
    source_path = artifact.get("source_path")
    source_sha = artifact.get("source_sha256")
    if source_path and source_sha and hashlib.sha256(Path(source_path).read_bytes()).hexdigest() != source_sha:
        raise ValueError("dataset source SHA256 mismatch")
    return artifact


def _verify_authoritative_environment(
    config: CalibrationConfig, connection: Connection[Any]
) -> dict[str, Any] | None:
    actual_version = str(connection.execute("SHOW server_version").fetchone()[0])
    if config.require_postgres_version and actual_version != config.require_postgres_version:
        raise ValueError(
            f"PostgreSQL version {actual_version} does not match required "
            f"{config.require_postgres_version}"
        )
    if not config.build_provenance_path:
        return None
    build = json.loads(config.build_provenance_path.read_text())
    if build.get("postgres_version") != actual_version:
        raise ValueError("server version does not match build provenance")
    if build.get("build_recipe_digest") != config.expected_build_recipe_digest:
        raise ValueError("PostgreSQL build recipe digest mismatch")
    if config.postgres_binary_path is None:
        raise ValueError("authoritative calibration requires postgres_binary_path")
    expected_path = Path(str(build["install_prefix"])) / "bin" / "postgres"
    if config.postgres_binary_path.resolve() != expected_path.resolve():
        raise ValueError("postgres binary path is outside the authoritative install prefix")
    actual_binary_digest = hashlib.sha256(config.postgres_binary_path.read_bytes()).hexdigest()
    if actual_binary_digest != build.get("postgres_binary_sha256"):
        raise ValueError("postgres binary digest does not match build provenance")
    return build


def run_calibration(config: CalibrationConfig, connection: Connection[Any]) -> dict[str, Any]:
    build_provenance = _verify_authoritative_environment(config, connection)
    root = config.output_path
    if root.exists():
        raise FileExistsError(f"calibration output directory exists: {root}")
    root.mkdir(parents=True)
    metadata = _relation_metadata(connection, config.relation)
    existing = _existing_statistics(connection, int(metadata["oid"]))
    if existing:
        raise ValueError(f"calibration relation already has extended statistics: {existing}")
    dataset_provenance = _dataset_provenance(config, metadata)
    available_columns = tuple(item["name"] for item in metadata["columns"])
    columns = config.columns or available_columns
    unknown = set(columns) - set(available_columns)
    if unknown:
        raise ValueError(f"unknown calibration columns: {sorted(unknown)}")
    pool = (
        candidate_pool_from_catalog(config.candidate_catalog_path)
        if config.candidate_catalog_path is not None
        else candidate_pool(columns)
    )
    pool_mechanisms = {item.mechanism for item in pool}
    if pool_mechanisms != {"mcv", "fd"}:
        raise ValueError("calibration candidate pool must contain both mcv and fd")
    configurations = configuration_design(
        pool, config.count_levels, config.subsets_per_count, config.seed
    )
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
            repetitions = (
                config.stability_repetitions
                if configuration.role == "stability" and config.stability_repetitions is not None
                else config.repetitions
            )
            for repetition in range(1, repetitions + 1):
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

    metadata_after = _relation_metadata(connection, config.relation)
    if metadata_after["logical_fingerprint"] != metadata["logical_fingerprint"]:
        raise RuntimeError("relation logical fingerprint changed during calibration")
    if metadata_after["relation_persistence"] != metadata["relation_persistence"]:
        raise RuntimeError("relation persistence changed during calibration")
    if metadata_after["default_statistics_target"] != metadata["default_statistics_target"]:
        raise RuntimeError("default statistics target changed during calibration")
    remaining = _existing_statistics(connection, int(metadata["oid"]))
    if remaining:
        raise RuntimeError(f"calibration statistics remain after cleanup: {remaining}")

    with (root / "raw-timings.csv").open("w", newline="") as stream:
        writer = csv.DictWriter(stream, fieldnames=list(rows[0]), lineterminator="\n")
        writer.writeheader()
        writer.writerows(rows)
    (root / "measurements.csv").write_bytes((root / "raw-timings.csv").read_bytes())
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
            "subset_id": item.subset_id,
            **_summary(by_configuration[configuration_id]),
        })
    with (root / "configuration-summary.csv").open("w", newline="") as stream:
        writer = csv.DictWriter(stream, fieldnames=list(summaries[0]), lineterminator="\n")
        writer.writeheader()
        writer.writerows(summaries)
    (root / "configurations.csv").write_bytes((root / "configuration-summary.csv").read_bytes())
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
    subset_group_cvs = []
    for kind in ("mcv-only", "fd-only", "mixed-stability"):
        counts = sorted({
            (item["n_mcv"] + item["n_fd"] if kind == "mixed-stability" else item["n_mcv"] or item["n_fd"])
            for item in summaries if item["configuration_kind"] == kind
        })
        for count in counts:
            means = [
                item["mean_seconds"] for item in summaries
                if item["configuration_kind"] == kind
                and (item["n_mcv"] + item["n_fd"] if kind == "mixed-stability" else item["n_mcv"] or item["n_fd"]) == count
            ]
            if len(means) > 1:
                subset_group_cvs.append({
                    "mechanism": "mcv" if kind == "mcv-only" else "fd" if kind == "fd-only" else "mixed",
                    "count": count,
                    "subset_count": len(means),
                    "mean_seconds": statistics.fmean(means),
                    "subset_mean_cv": statistics.stdev(means) / statistics.fmean(means),
                })
    max_subset_cv = max((item["subset_mean_cv"] for item in subset_group_cvs), default=None)
    gates = assess_gates(
        fit,
        max_cv=max_cv,
        cv_threshold=config.gates.max_cv,
        max_heldout_relative_error=max_heldout,
        heldout_threshold=config.gates.max_heldout_relative_error,
        r_squared_threshold=config.gates.min_r_squared,
        same_count_subset_cv=max_subset_cv,
        same_count_subset_threshold=config.gates.max_same_count_subset_cv,
    )
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
        "same_count_subset_variability": subset_group_cvs,
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
        "relation_persistence": metadata["relation_persistence"],
        "total_relation_bytes": metadata["total_relation_bytes"],
        "row_count": metadata["row_count"],
        "reltuples": metadata["reltuples"],
        "column_schema": metadata["columns"],
        "statistics_target": config.statistics_target,
        "candidate_arity": 2,
        "mechanisms": ["mcv", "fd"],
        "mcv_candidate_pool_size": len(pool) // 2,
        "fd_candidate_pool_size": len(pool) // 2,
        "candidate_catalog_path": (
            str(config.candidate_catalog_path) if config.candidate_catalog_path else None
        ),
        "candidate_catalog_digest": (
            json.loads(config.candidate_catalog_path.read_text()).get("digest")
            if config.candidate_catalog_path else None
        ),
        "dataset_provenance": dataset_provenance,
        "repetitions": config.repetitions,
        "stability_repetitions": config.stability_repetitions,
        "seed": config.seed,
        "timing_clock": "time.perf_counter",
        "cache_policy": "long-lived server; no cache flush; one untimed warmup per configuration",
        "config_digest": config.digest,
        "environment_description": config.environment_description,
        "hardware_provenance_complete": False,
        "build_recipe_digest": (
            build_provenance.get("build_recipe_digest") if build_provenance else None
        ),
        "postgres_binary_path": (
            str(config.postgres_binary_path) if config.postgres_binary_path else None
        ),
        "environment_authoritative": build_provenance is not None,
        "server_settings": {
            name: connection.execute(sql.SQL("SHOW {}").format(sql.Identifier(name))).fetchone()[0]
            for name in (
                "jit", "max_parallel_workers_per_gather", "effective_cache_size", "work_mem",
                "random_page_cost", "cpu_tuple_cost", "cpu_index_tuple_cost",
                "cpu_operator_cost", "default_statistics_target",
            )
        },
    }
    _write_json(root / "config.json", json.loads(config.canonical_json()))
    _write_json(root / "protocol.json", {
        "dependent_variable": "full relation ANALYZE elapsed wall-clock seconds",
        "ddl_included": False,
        "warmup_count": 1,
        "measured_repetitions": config.repetitions,
        "stability_repetitions": config.stability_repetitions,
        "execution_order": "deterministic seeded configuration shuffle",
        "seed": config.seed,
        "gates": asdict(config.gates),
        "benchmark": config.benchmark,
        "candidate_catalog_digest": provenance["candidate_catalog_digest"],
        "configuration_count": len(configurations),
        "fit_configuration_count": sum(item.role == "fit" for item in configurations),
        "heldout_configuration_count": sum(item.role == "held-out" for item in configurations),
        "stability_configuration_count": sum(item.role == "stability" for item in configurations),
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
                "subset_id": item.subset_id,
                "candidate_ids": [candidate.candidate_id for candidate in item.mcv + item.fd],
            }
            for item in configurations
        ],
        "execution_order": [item.configuration_id for item in order],
    })
    _write_json(root / "fit.json", fit_dict)
    _write_json(root / "heldout.json", {"configurations": heldout})
    accepted = all(bool(item["passed"]) for item in gates.values())
    gate_failures = [name for name, item in gates.items() if not item["passed"]]
    report = {
        "format_version": 1,
        "status": "accepted" if accepted else "rejected",
        "model_type": MODEL_TYPE,
        "fit": fit_dict,
        "stability": stability,
        "heldout": heldout,
        "calibration_provenance": provenance,
        "authority_status": (
            "authoritative" if accepted and build_provenance else
            "rejected-authoritative-environment" if build_provenance else "diagnostic"
        ),
        "gate_failures": gate_failures,
        "completed_at": datetime.now(UTC).isoformat(),
    }
    _write_json(root / "calibration-report.json", report)
    model = {
        "format_version": 1,
        "model_type": MODEL_TYPE,
        "status": "accepted" if accepted else "rejected",
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
        "acceptance_gates": gates,
        "calibration_provenance": provenance,
    }
    model["digest"] = artifact_digest(model)
    _write_json(root / "maintenance-model.json", model)
    report["maintenance_model_digest"] = model["digest"]
    _write_json(root / "calibration-report.json", report)
    return report
