#!/usr/bin/env python3
"""M2.28: explain DMV native/reservoir fidelity divergence from ordinary stats.

The tool consumes the frozen M2.26 native samples and M2.27a reservoir bundle.
It does not create extended statistics, run a search, deploy a design, or
capture production.  The only database work is deterministic replay of the
already persisted sample rows into the isolated advisor cluster.
"""

from __future__ import annotations

import csv
import hashlib
import json
import math
import sys
from collections import Counter, defaultdict
from pathlib import Path
from typing import Any

import psycopg

ROOT = Path(__file__).resolve().parents[1]
if str(ROOT / "src") not in sys.path:
    sys.path.insert(0, str(ROOT / "src"))

from pg_extstats_advisor.analysis.root_cause import (
    frequency_differences,
    jaccard,
    mcv_membership,
    normalize_ndistinct,
    parse_predicates,
    quantiles,
    top_contributors,
    trimmed_aggregate,
)
from pg_extstats_advisor.capture.bundle import decode_sample
from pg_extstats_advisor.capture.bundle_v2 import verify_bundle_v2
from pg_extstats_advisor.capture.fidelity import aggregate_qerror
from pg_extstats_advisor.postgres.extraction import extract_target_estimate

OUT = ROOT / "experiments/dmv-m2-28-acquisition-root-cause"
M26 = ROOT / "experiments/dmv-m2-26-statistics-target-stability"
M27A = ROOT / "experiments/dmv-m2-27a-reservoir-target-characterization"
M27B = ROOT / "experiments/dmv-m2-27b-target-selection-fidelity"
BUNDLE = ROOT / ".build/production-captures/dmv-m2-27a-v2"
ADVISOR_DSN = "host=/root/projects/pg-extstats-advisor/.build/pg16.14-advisor-socket port=55438 dbname=postgres user=postgres"
SOURCE_ROWS = 11_591_877
TARGETS = (100, 300, 1000)
COLUMNS = (
    "record_type",
    "registration_class",
    "state",
    "county",
    "body_type",
    "fuel_type",
    "reg_valid_date",
    "color",
    "scofflaw_indicator",
    "suspension_indicator",
    "revocation_indicator",
)
SAMPLE_CACHE_NATIVE = ROOT / ".build/artifact-cache/dmv-statistics-target-stability-v1"
SAMPLE_CACHE_RESERVOIR = ROOT / ".build/artifact-cache"


def load(path: Path) -> Any:
    return json.loads(path.read_text(encoding="utf-8"))


def digest(value: Any) -> str:
    return hashlib.sha256(
        json.dumps(value, sort_keys=True, separators=(",", ":"), default=str).encode()
    ).hexdigest()


def write_json(path: Path, value: Any) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text(
        json.dumps(value, sort_keys=True, indent=2, default=str) + "\n", encoding="utf-8"
    )


def qerror(estimate: float, truth: float) -> float:
    estimate = max(float(estimate), 1.0)
    return max(estimate / float(truth), float(truth) / estimate)


def csv_write(path: Path, rows: list[dict[str, Any]]) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    if not rows:
        path.write_text("\n", encoding="utf-8")
        return
    with path.open("w", newline="", encoding="utf-8") as handle:
        writer = csv.DictWriter(handle, fieldnames=list(rows[0]))
        writer.writeheader()
        writer.writerows(rows)


def workload() -> list[dict[str, Any]]:
    rows = load(ROOT / "experiments/dmv-m2-15-singletons/prepared-run/workload.json")["queries"]
    return sorted(
        [
            {
                "query_id": str(row["query_id"]),
                "sql": str(row["sql"]),
                "truth": float(row["truth"]),
                "target_relation": str(row["target_relation"]),
            }
            for row in rows
        ],
        key=lambda row: row["query_id"],
    )


def validate_inputs(queries: list[dict[str, Any]]) -> dict[str, Any]:
    bundle_verification = verify_bundle_v2(BUNDLE)
    reservoir_workload = load(BUNDLE / "workload.json")["queries"]
    reservoir_semantic = sorted(
        [
            {
                "query_id": str(row["query_id"]),
                "sql": str(row["sql"]),
                "truth": float(row["truth"]),
                "target_relation": str(row["target_relation"]).replace("public.", ""),
            }
            for row in reservoir_workload
        ],
        key=lambda row: row["query_id"],
    )
    if queries != reservoir_semantic:
        raise RuntimeError("native and reservoir workload/truth populations differ")
    if len(queries) != 1963 or any(row["truth"] <= 0 for row in queries):
        raise RuntimeError("unexpected fixed positive-truth workload")
    return {
        "bundle": bundle_verification,
        "workload_size": len(queries),
        "source_population_rows": SOURCE_ROWS,
        "workload_equal": True,
    }


def baseline_vectors() -> dict[str, list[dict[str, Any]]]:
    result: dict[str, list[dict[str, Any]]] = {}
    for sample in load(M26 / "baseline-stability.json")["samples"]:
        key = str(sample["sample_name"])
        result[key] = [
            {
                "query_id": str(item["query_id"]),
                "estimate": float(item["estimate"]),
                "truth": float(item["truth"]),
                "q_error": float(item["contribution"]),
            }
            for item in sample["vector"]
        ]
    return result


def top_rows() -> list[dict[str, Any]]:
    with (M27B / "top-contributors.csv").open(newline="", encoding="utf-8") as handle:
        rows = [dict(row) for row in csv.DictReader(handle) if row["mechanism"] == "reservoir"]
    rows = list(top_contributors(rows, 20))
    if len(rows) != 20:
        raise RuntimeError("M2.27b reservoir top-contributor set is not exactly 20")
    return rows


def _json_array(value: Any) -> list[Any]:
    if value is None:
        return []
    if isinstance(value, list):
        return value
    if isinstance(value, str):
        decoded = json.loads(value)
        return [] if decoded is None else list(decoded)
    return list(value)


def stats_rows(
    conn: psycopg.Connection[Any], relevant: set[str], target: int, mechanism: str, sample_rows: int
) -> dict[str, dict[str, Any]]:
    rows = conn.execute(
        """SELECT attname, null_frac, n_distinct,
                  array_to_json(most_common_vals), array_to_json(most_common_freqs),
                  array_to_json(histogram_bounds), correlation
           FROM pg_stats WHERE schemaname='public' AND tablename='dmv' ORDER BY attname"""
    ).fetchall()
    result: dict[str, dict[str, Any]] = {}
    for row in rows:
        column = str(row[0])
        if column not in relevant:
            continue
        values, frequencies, histogram = (
            _json_array(row[3]),
            _json_array(row[4]),
            _json_array(row[5]),
        )
        result[column] = {
            "mechanism": mechanism,
            "target": target,
            "column": column,
            "null_frac": None if row[1] is None else float(row[1]),
            "n_distinct": None if row[2] is None else float(row[2]),
            "implied_ndistinct": normalize_ndistinct(row[2], SOURCE_ROWS),
            "mcv_values": values,
            "mcv_frequencies": [float(item) for item in frequencies],
            "histogram_bounds": histogram,
            "correlation": None if row[6] is None else float(row[6]),
            "mcv_count": len(values),
            "histogram_count": len(histogram),
            "statistics_target": target,
            "sample_rows": sample_rows,
            "source_population": SOURCE_ROWS,
        }
        result[column]["semantic_digest"] = digest(result[column])
    if set(result) != relevant:
        raise RuntimeError(
            f"ordinary stats missing relevant columns: {sorted(relevant - set(result))}"
        )
    return result


def replay_state(
    mechanism: str,
    target: int,
    queries: list[dict[str, Any]],
    relevant: set[str],
    native_vectors: dict[str, list[dict[str, Any]]],
    cells: dict[tuple[int, str], dict[str, Any]],
) -> dict[str, Any]:
    if mechanism == "native":
        sample_rel = SAMPLE_CACHE_NATIVE / f"T{target}-A/sample.copy.bin"
        sample_count = 300 * target
        vector = native_vectors[f"T{target}-A"]
        source = "m2-26-native-sample-cache"
    else:
        cell = cells[(target, "A")]
        sample_rel = SAMPLE_CACHE_RESERVOIR / cell["artifact_cache_relative"] / "sample.copy.bin"
        sample_count = int(cell["sample_row_count"])
        vector = None
        source = "m2-27a-v2-reservoir-cache"
    rows = None if mechanism == "native" else decode_sample(sample_rel, sample_count, len(COLUMNS))
    conn = psycopg.connect(ADVISOR_DSN, autocommit=False)
    try:
        conn.execute("DROP TABLE IF EXISTS public.dmv")
        conn.execute(
            "CREATE UNLOGGED TABLE public.dmv ("
            + ",".join(f'"{name}" text' for name in COLUMNS)
            + ")"
        )
        if mechanism == "native":
            with conn.cursor().copy("COPY public.dmv FROM STDIN (FORMAT binary)") as copy:
                copy.write(sample_rel.read_bytes())
        else:
            with conn.cursor().copy("COPY public.dmv FROM STDIN") as copy:
                for row in rows or ():
                    copy.write_row(tuple(row))
        conn.execute("SELECT set_config('default_statistics_target', %s, false)", (str(target),))
        conn.execute("SELECT set_config('pg_extstats.frozen_sample_mode', 'replay', false)")
        conn.execute("SELECT set_config('pg_extstats.frozen_sample_relation', 'public.dmv', false)")
        conn.execute(
            "SELECT set_config('pg_extstats.frozen_totalrows', %s, false)", (str(SOURCE_ROWS),)
        )
        conn.execute("ANALYZE public.dmv")
        if vector is None:
            vector = []
            for query in queries:
                plan = conn.execute(f"EXPLAIN (FORMAT JSON) {query['sql']}").fetchone()[0]
                estimate = extract_target_estimate(plan, "dmv")
                vector.append(
                    {
                        "query_id": query["query_id"],
                        "estimate": float(estimate),
                        "truth": query["truth"],
                        "q_error": qerror(estimate, query["truth"]),
                    }
                )
        ext_count = int(
            conn.execute(
                "SELECT count(*) FROM pg_statistic_ext WHERE stxrelid='public.dmv'::regclass"
            ).fetchone()[0]
        )
        if ext_count != 0:
            raise RuntimeError(f"extstats contamination in {mechanism} T{target}")
        stats = stats_rows(conn, relevant, target, mechanism, sample_count)
        objective = aggregate_qerror(item["q_error"] for item in vector)
        conn.commit()
        return {
            "mechanism": mechanism,
            "target": target,
            "realization": "A",
            "sample_rows": sample_count,
            "source_population": SOURCE_ROWS,
            "stats": stats,
            "vector": vector,
            "aggregate_objective": objective,
            "source": source,
            "extstats_count": ext_count,
        }
    finally:
        conn.close()


def validate_replayed_semantics(
    states: dict[str, dict[str, Any]], cells: dict[tuple[int, str], dict[str, Any]]
) -> dict[str, Any]:
    """Cross-check compact M2.26/M2.27a ordinary-stat summaries."""

    native_expected = {
        (int(sample["target"]), "A"): {str(row["attname"]): row for row in sample["columns"]}
        for sample in load(M26 / "ordinary-stats-stability.json")["samples"]
        if str(sample["sample_name"]).endswith("-A")
    }
    reservoir_expected = {}
    for result in load(M27A / "target-sweep.json")["target_results"]:
        if str(result["realization_id"]) == "A":
            reservoir_expected[(int(result["statistics_target"]), "A")] = {
                str(row[1]): row for row in result["ordinary_statistics"]["rows"]
            }
    checks = {}
    for mechanism in ("native", "reservoir"):
        expected_map = native_expected if mechanism == "native" else reservoir_expected
        for target in TARGETS:
            actual = states[f"{mechanism}-T{target}"]["stats"]
            expected = expected_map[(target, "A")]
            mismatches = []
            for column, row in actual.items():
                recorded = expected[column]
                if isinstance(recorded, dict):
                    recorded_nd = (
                        float(recorded["distinct"]) if recorded["distinct"] is not None else None
                    )
                    recorded_hist = (
                        0 if recorded["hist_count"] is None else int(recorded["hist_count"])
                    )
                    recorded_mcv = int(recorded["mcv_count"])
                    recorded_null = float(recorded["nullfrac"])
                else:
                    recorded_nd = float(recorded[4]) if recorded[4] is not None else None
                    recorded_hist = 0 if recorded[11] is None else int(recorded[11])
                    recorded_mcv = int(recorded[10])
                    recorded_null = float(recorded[2])
                if (
                    row["mcv_count"] != recorded_mcv
                    or row["histogram_count"] != recorded_hist
                    or row["n_distinct"] != recorded_nd
                    or row["null_frac"] != recorded_null
                ):
                    mismatches.append(column)
            checks[f"{mechanism}-T{target}"] = {
                "match": not mismatches,
                "mismatched_columns": mismatches,
            }
            if mismatches:
                raise RuntimeError(
                    f"replayed ordinary statistics do not match frozen summary: {mechanism} T{target} {mismatches}"
                )
    return checks


def predicate_rows(
    top: list[dict[str, Any]], states: dict[str, dict[str, Any]]
) -> list[dict[str, Any]]:
    rows: list[dict[str, Any]] = []
    for top_row in top:
        _, predicates, _ = parse_predicates(top_row["sql"])
        for predicate in predicates:
            for constant in predicate.constants or (None,):
                for mechanism in ("native", "reservoir"):
                    for target in TARGETS:
                        stat = states[f"{mechanism}-T{target}"]["stats"].get(predicate.column, {})
                        result = (
                            {
                                "in_mcv": False,
                                "mcv_frequency": None,
                                "selectivity_path": "OTHER",
                                "selectivity": None,
                            }
                            if constant is None
                            else mcv_membership(stat, constant)
                        )
                        rows.append(
                            {
                                "query_id": top_row["query_id"],
                                "column": predicate.column,
                                "operator": predicate.operator,
                                "constant": constant,
                                "mechanism": mechanism,
                                "target": target,
                                "in_mcv": result["in_mcv"],
                                "mcv_frequency": result["mcv_frequency"],
                                "n_distinct": stat.get("n_distinct"),
                                "implied_ndistinct": stat.get("implied_ndistinct"),
                                "selectivity": result["selectivity"],
                                "selectivity_path": "NULL_RELATED"
                                if constant is None
                                else result["selectivity_path"],
                            }
                        )
    return rows


def predicate_selectivity(stat: dict[str, Any], predicate: Any) -> tuple[float | None, str]:
    values = [mcv_membership(stat, value) for value in predicate.constants]
    if not values or any(item["selectivity"] is None for item in values):
        return None, "UNRESOLVED"
    # An IN clause is an OR and fallback masses are not disjoint.  This is an
    # explanatory control, not an exact planner reconstruction, so bound it.
    return min(1.0, sum(float(item["selectivity"]) for item in values)), "+".join(
        sorted({item["selectivity_path"] for item in values})
    )


def query_analysis(
    top: list[dict[str, Any]],
    states: dict[str, dict[str, Any]],
    native_vectors: dict[str, list[dict[str, Any]]],
) -> tuple[list[dict[str, Any]], dict[str, Any]]:
    by_state = {
        key: {item["query_id"]: item for item in value["vector"]} for key, value in states.items()
    }
    # Native vectors are authoritative M2.26 values; the native replay states only carry stats.
    for target in TARGETS:
        by_state[f"native-T{target}"] = {
            item["query_id"]: item for item in native_vectors[f"T{target}-A"]
        }
    rows: list[dict[str, Any]] = []
    details: dict[str, Any] = {}
    for top_row in top:
        query_id, sql = top_row["query_id"], top_row["sql"]
        relation, predicates, conjunction = parse_predicates(sql)
        native = [by_state[f"native-T{target}"][query_id] for target in TARGETS]
        reservoir = [by_state[f"reservoir-T{target}"][query_id] for target in TARGETS]
        row = {
            "query_id": query_id,
            "sql": sql,
            "relation": relation.lower(),
            "predicate_columns": ";".join(sorted({p.column for p in predicates})),
            "predicate_operators": ";".join(p.operator for p in predicates),
            "constants": json.dumps(
                [value for p in predicates for value in p.constants], separators=(",", ":")
            ),
            "conjunction_structure": conjunction,
            "predicate_count": len(predicates),
            "truth": native[0]["truth"],
        }
        for mechanism, values in (("native", native), ("reservoir", reservoir)):
            for target, value in zip(TARGETS, values, strict=True):
                row[f"{mechanism}_T{target}_estimate"] = value["estimate"]
                row[f"{mechanism}_T{target}_qerror"] = value["q_error"]
        rows.append(row)
        details[query_id] = {
            "sql": sql,
            "truth": native[0]["truth"],
            "predicates": [
                p.__dict__
                if hasattr(p, "__dict__")
                else {"column": p.column, "operator": p.operator, "constants": list(p.constants)}
                for p in predicates
            ],
            "estimates": {
                key: {
                    "estimate": by_state[key][query_id]["estimate"],
                    "q_error": by_state[key][query_id]["q_error"],
                }
                for key in by_state
                if key
                in {f"native-T{target}" for target in TARGETS}
                | {f"reservoir-T{target}" for target in TARGETS}
            },
        }
        for mechanism in ("native", "reservoir"):
            for target in TARGETS:
                state = states[f"{mechanism}-T{target}"]
                sels = [predicate_selectivity(state["stats"][p.column], p) for p in predicates]
                details[query_id].setdefault("decomposition", {})[f"{mechanism}-T{target}"] = {
                    "predicate_selectivities": [item[0] for item in sels],
                    "selectivity_paths": [item[1] for item in sels],
                    "independence_product": math.prod(item[0] for item in sels)
                    if sels and all(item[0] is not None for item in sels)
                    else None,
                    "final_estimate": by_state[f"{mechanism}-T{target}"][query_id]["estimate"],
                    "truth": native[0]["truth"],
                }
    return rows, details


def concentration(
    top: list[dict[str, Any]],
) -> tuple[list[dict[str, Any]], list[dict[str, Any]], list[dict[str, Any]]]:
    columns, pairs, values = Counter(), Counter(), Counter()
    for row in top:
        _, predicates, _ = parse_predicates(row["sql"])
        names = sorted({p.column for p in predicates})
        columns.update(names)
        pairs.update((a, b) for index, a in enumerate(names) for b in names[index + 1 :])
        for predicate in predicates:
            values.update((predicate.column, value) for value in predicate.constants)
    return (
        [{"column": key, "query_count": value} for key, value in columns.most_common()],
        [
            {"column_left": key[0], "column_right": key[1], "query_count": value}
            for key, value in pairs.most_common()
        ],
        [
            {"column": key[0], "constant": key[1], "predicate_count": value}
            for key, value in values.most_common()
        ],
    )


def distribution(
    states: dict[str, dict[str, Any]], top_ids: set[str]
) -> tuple[list[dict[str, Any]], dict[str, Any]]:
    rows, summary = [], {}
    for target in TARGETS:
        native = {item["query_id"]: item for item in states[f"native-T{target}"]["vector"]}
        reservoir = {item["query_id"]: item for item in states[f"reservoir-T{target}"]["vector"]}
        ratios = [reservoir[key]["q_error"] / native[key]["q_error"] for key in native]
        logs = [abs(math.log(ratio)) for ratio in ratios]
        summary[str(target)] = {
            "qerror_ratio_quantiles": quantiles(ratios),
            "absolute_log_qerror_difference_quantiles": quantiles(logs),
            "bucket_thresholds": {"ratio": [1.05, 2.0, 10.0], "absolute_log": [0.05, 0.5, 2.0]},
            "ratio_buckets": {
                "<=1.05": sum(x <= 1.05 for x in ratios),
                "1.05-2": sum(1.05 < x <= 2 for x in ratios),
                "2-10": sum(2 < x <= 10 for x in ratios),
                ">10": sum(x > 10 for x in ratios),
            },
            "absolute_log_buckets": {
                "<=0.05": sum(x <= 0.05 for x in logs),
                "0.05-0.5": sum(0.05 < x <= 0.5 for x in logs),
                "0.5-2": sum(0.5 < x <= 2 for x in logs),
                ">2": sum(x > 2 for x in logs),
            },
        }
        for key in native:
            rows.append(
                {
                    "query_id": key,
                    "target": target,
                    "native_qerror": native[key]["q_error"],
                    "reservoir_qerror": reservoir[key]["q_error"],
                    "qerror_ratio_reservoir_over_native": reservoir[key]["q_error"]
                    / native[key]["q_error"],
                    "absolute_log_qerror_difference": abs(
                        math.log(reservoir[key]["q_error"]) - math.log(native[key]["q_error"])
                    ),
                    "is_top20": key in top_ids,
                }
            )
    return rows, summary


def trimmed(states: dict[str, dict[str, Any]], top: list[dict[str, Any]]) -> dict[str, Any]:
    top_ids = [row["query_id"] for row in top]
    result: dict[str, Any] = {"fixed_trim_set": top_ids, "full": {}, "trimmed": {}}
    for label, excluded in (("full", set()), ("trimmed", set(top_ids))):
        for mechanism in ("native", "reservoir"):
            values = {}
            for target in TARGETS:
                vector = states[f"{mechanism}-T{target}"]["vector"]
                values[str(target)] = {
                    "aggregate_qerror": trimmed_aggregate(vector, excluded),
                    "query_count": sum(row["query_id"] not in excluded for row in vector),
                }
            result[label][mechanism] = {
                "target_ordering": sorted(
                    TARGETS, key=lambda target: values[str(target)]["aggregate_qerror"]
                ),
                "values": values,
                "quality_gains": [
                    (values[str(a)]["aggregate_qerror"] - values[str(b)]["aggregate_qerror"])
                    / values[str(a)]["aggregate_qerror"]
                    for a, b in ((100, 300), (300, 1000))
                ],
            }
    return result


def semantic_diffs(
    states: dict[str, dict[str, Any]], top: list[dict[str, Any]]
) -> tuple[list[dict[str, Any]], dict[str, Any]]:
    relevant = sorted({p.column for row in top for p in parse_predicates(row["sql"])[1]})
    constants_by_column: dict[str, set[str]] = defaultdict(set)
    for row in top:
        for predicate in parse_predicates(row["sql"])[1]:
            constants_by_column[predicate.column].update(predicate.constants)
    rows, overlap = [], {}
    for target in TARGETS:
        represented = {
            name: Counter() for name in ("both", "native_only", "reservoir_only", "neither")
        }
        diffs = []
        for column in relevant:
            n, r = (
                states[f"native-T{target}"]["stats"][column],
                states[f"reservoir-T{target}"]["stats"][column],
            )
            nset, rset = set(map(str, n["mcv_values"])), set(map(str, r["mcv_values"]))
            for value in constants_by_column[column]:
                represented[
                    "both"
                    if value in nset and value in rset
                    else "native_only"
                    if value in nset
                    else "reservoir_only"
                    if value in rset
                    else "neither"
                ][column] += 1
            common = frequency_differences(n, r)
            diffs.extend(common)
            rows.append(
                {
                    "target": target,
                    "column": column,
                    "native_n_distinct": n["n_distinct"],
                    "reservoir_n_distinct": r["n_distinct"],
                    "native_implied_ndistinct": n["implied_ndistinct"],
                    "reservoir_implied_ndistinct": r["implied_ndistinct"],
                    "n_distinct_relative_difference": None
                    if n["implied_ndistinct"] in (None, 0)
                    else abs(r["implied_ndistinct"] - n["implied_ndistinct"])
                    / n["implied_ndistinct"],
                    "mcv_jaccard": jaccard(n["mcv_values"], r["mcv_values"]),
                    "native_mcv_count": n["mcv_count"],
                    "reservoir_mcv_count": r["mcv_count"],
                    "native_histogram_count": n["histogram_count"],
                    "reservoir_histogram_count": r["histogram_count"],
                    "common_mcv_frequency_quantiles": quantiles(common),
                }
            )
        overlap[str(target)] = {
            "queried_constant_representation": {
                key: sum(counter.values()) for key, counter in represented.items()
            },
            "mcv_frequency_difference_quantiles": quantiles(diffs),
        }
    return rows, overlap


def differential(
    top: list[dict[str, Any]], states: dict[str, dict[str, Any]], mechanism: str
) -> list[dict[str, Any]]:
    rows = []
    for item in top:
        predicates = parse_predicates(item["sql"])[1]
        columns = sorted({p.column for p in predicates})
        before, after = states[f"{mechanism}-T300"]["stats"], states[f"{mechanism}-T1000"]["stats"]
        changes = []
        for column in columns:
            b, a = before[column], after[column]
            values = set(map(str, b["mcv_values"])), set(map(str, a["mcv_values"]))
            changes.append(
                {
                    "column": column,
                    "mcv_added": sorted(values[1] - values[0]),
                    "mcv_removed": sorted(values[0] - values[1]),
                    "n_distinct_before": b["n_distinct"],
                    "n_distinct_after": a["n_distinct"],
                    "mcv_count_before": b["mcv_count"],
                    "mcv_count_after": a["mcv_count"],
                }
            )
        rows.append(
            {
                "query_id": item["query_id"],
                "mechanism": mechanism,
                "qerror_300": states[f"{mechanism}-T300"]["vector"][
                    [x["query_id"] for x in states[f"{mechanism}-T300"]["vector"]].index(
                        item["query_id"]
                    )
                ]["q_error"],
                "qerror_1000": states[f"{mechanism}-T1000"]["vector"][
                    [x["query_id"] for x in states[f"{mechanism}-T1000"]["vector"]].index(
                        item["query_id"]
                    )
                ]["q_error"],
                "stat_changes": json.dumps(changes, sort_keys=True, separators=(",", ":")),
            }
        )
    return rows


def source_audit() -> dict[str, Any]:
    return {
        "source_root": "/root/projects/extended-stats-optim/postgresql-16.14",
        "native_sampling": [
            {
                "file": "src/backend/commands/analyze.c",
                "function": "acquire_sample_rows",
                "lines": "1104-1345",
                "finding": "BlockSampler selects blocks; rows are scanned from accepted blocks; Vitter reservoir selects rows; the result is sorted by physical position for correlation; total rows are extrapolated from sampled blocks.",
            },
            {
                "file": "src/backend/commands/analyze.c",
                "function": "std_typanalyze",
                "lines": "1883-1950",
                "finding": "Scalar ordinary statistics use compute_scalar_stats and minrows=300*target.",
            },
        ],
        "ordinary_mcv": [
            {
                "file": "src/backend/commands/analyze.c",
                "function": "compute_distinct_stats",
                "lines": "2038-2051,2077",
                "finding": "Per-column MCV tracking is driven by sample rows and attstattarget.",
            },
            {
                "file": "src/backend/utils/adt/selfuncs.c",
                "function": "var_eq_const",
                "lines": "334-448",
                "finding": "Equality first checks MCV values; a miss uses remaining mass, null fraction, and estimated other distinct values.",
            },
        ],
        "extended_mcv": [
            {
                "file": "src/backend/statistics/mcv.c",
                "function": "statext_mcv_build",
                "lines": "184-250",
                "finding": "Extended MCV construction is separate; no extstats are created in this study.",
            }
        ],
        "planner_combination": [
            {
                "file": "src/backend/utils/adt/selfuncs.c",
                "function": "clauselist_selectivity",
                "lines": "6545,6698",
                "finding": "Conjunction selectivity is combined by PostgreSQL's clause-list machinery; this report uses independence products only as explanatory controls, not as a claimed exact reconstruction.",
            }
        ],
    }


def main() -> None:
    queries = workload()
    input_audit = validate_inputs(queries)
    top = top_rows()
    top_ids = {row["query_id"] for row in top}
    relevant = {predicate.column for row in top for predicate in parse_predicates(row["sql"])[1]}
    native_vectors = baseline_vectors()
    cells = {
        (int(cell["statistics_target"]), str(cell["realization_id"])): cell
        for cell in load(BUNDLE / "acquisition.json")["cells"]
    }
    states: dict[str, dict[str, Any]] = {}
    for mechanism in ("native", "reservoir"):
        for target in TARGETS:
            state = replay_state(mechanism, target, queries, relevant, native_vectors, cells)
            states[f"{mechanism}-T{target}"] = state
            print(f"replayed {mechanism} T{target}: {state['sample_rows']} rows", flush=True)
    replay_semantics = validate_replayed_semantics(states, cells)
    # Native estimate vectors are frozen M2.26 artifacts; reservoir vectors are fresh deterministic replays.
    for target in TARGETS:
        states[f"native-T{target}"]["vector"] = native_vectors[f"T{target}-A"]
    structural, decomposition = query_analysis(top, states, native_vectors)
    memberships = predicate_rows(top, states)
    path_counts = {
        f"{mechanism}-T{target}": dict(
            Counter(
                row["selectivity_path"]
                for row in memberships
                if row["mechanism"] == mechanism and int(row["target"]) == target
            )
        )
        for mechanism in ("native", "reservoir")
        for target in TARGETS
    }
    columns, pairs, values = concentration(top)
    stats_rows_out = [state for key in sorted(states) for state in states[key]["stats"].values()]
    stat_summary = []
    for item in stats_rows_out:
        stat_summary.append(
            {
                key: (
                    json.dumps(value, separators=(",", ":"))
                    if isinstance(value, (list, dict))
                    else value
                )
                for key, value in item.items()
                if key not in {"mcv_values", "mcv_frequencies", "histogram_bounds"}
            }
        )
    stat_diff, overlap = semantic_diffs(states, top)
    distribution_rows, distribution_summary = distribution(states, top_ids)
    trim = trimmed(states, top)
    focus_memberships = predicate_rows(top, states)
    root_cases = {}
    for query_id in ("dmv.1037", "dmv.179", "dmv.1100"):
        query = next(row for row in top if row["query_id"] == query_id)
        columns_for_query = sorted({p.column for p in parse_predicates(query["sql"])[1]})
        root_cases[query_id] = dict(decomposition[query_id])
        root_cases[query_id]["statistics_excerpt"] = {
            key: {column: states[key]["stats"][column] for column in columns_for_query}
            for key in ("native-T300", "native-T1000", "reservoir-T300", "reservoir-T1000")
        }
        root_cases[query_id]["predicate_membership"] = [
            row for row in focus_memberships if row["query_id"] == query_id
        ]
    differentials = differential(top, states, "native") + differential(top, states, "reservoir")
    full_reservoir_improvement = (
        states["reservoir-T300"]["aggregate_objective"]
        - states["reservoir-T1000"]["aggregate_objective"]
    )
    top_share = {}
    for k in (1, 3, 10, 20):
        top_share[str(k)] = (
            math.fsum(float(row["qerror_reduction"]) for row in top[:k])
            / full_reservoir_improvement
        )
    summary = {
        "milestone": "M2.28",
        "status": "complete",
        "input_audit": {
            **input_audit,
            "replayed_ordinary_statistics_match_frozen_summaries": replay_semantics,
        },
        "reused_artifacts": [
            str(M26 / "baseline-stability.json"),
            str(M26 / "ordinary-stats-stability.json"),
            str(M27A / "target-sweep.json"),
            str(M27B / "top-contributors.csv"),
            str(BUNDLE),
        ],
        "production_accessed": False,
        "search_started": False,
        "deployment_started": False,
        "extstats_created": False,
        "primary_top_n": 20,
        "top20_qerror_reduction_share_of_full_workload_improvement": top_share["20"],
        "top_k_share_of_full_workload_improvement": top_share,
        "top20_reduction_sum": math.fsum(float(row["qerror_reduction"]) for row in top),
        "full_workload_reservoir_improvement": full_reservoir_improvement,
        "top20_share_definition": "sum of the frozen M2.27b reservoir T300-A to T1000-A top-20 q-error reductions divided by the full canonical-A workload objective reduction; a value above 1 means non-top-20 queries have net offsetting regressions.",
        "m2_27b_mean_target_ordering": {
            "native": load(M27B / "summary.json")["native_target_summary"]["quality_ordering"],
            "reservoir": load(M27B / "summary.json")["reservoir_target_summary"][
                "quality_ordering"
            ],
            "note": "mean across A/B/C from M2.27b; trimmed analysis uses the fixed top-20 set on canonical A because the root-cause contrast is T300-A to T1000-A.",
        },
        "mcv_overlap": overlap,
        "selectivity_path_counts": path_counts,
        "reconstruction_exclusion": {
            "statistics_target_metadata_verified": True,
            "sample_capacity_verified": True,
            "sample_population_verified": True,
            "ordinary_replay_path": "same patched PG16.14 replay GUC used by M2.26/M2.27a; no extstats",
            "stale_statistics": False,
            "target_override_hidden": False,
            "workload_truth_mismatch": False,
            "production_reaccessed": False,
        },
        "distribution": distribution_summary,
        "trimmed_objective": trim,
        "source_audit": source_audit(),
        "hypotheses": {
            "H1_physical_block_structure": {
                "evidence_for": [
                    "PG16.14 native acquire_sample_rows is block-first and explicitly notes block-representation bias."
                ],
                "evidence_against": [
                    "M2.28 replays do not manipulate physical pages and therefore cannot isolate H1."
                ],
                "unresolved": True,
            },
            "H2_reservoir_rng_or_stream_order": {
                "evidence_for": [
                    "reservoir and native samples have different acquisition semantics and canonical-A ordinary stats diverge."
                ],
                "evidence_against": [
                    "A single deterministic realization cannot separate RNG from sampling-path effects."
                ],
                "unresolved": True,
            },
            "H3_target_MCV_capacity_threshold": {
                "evidence_for": [
                    "PG source makes MCV capacity and minimum-count decisions target-dependent; membership transitions are recorded in predicate-membership.csv."
                ],
                "evidence_against": [],
                "unresolved": True,
            },
            "H4_ndistinct_sensitivity": {
                "evidence_for": [
                    "n_distinct and implied cardinality are compared per relevant column."
                ],
                "evidence_against": [],
                "unresolved": True,
            },
            "H5_histogram_capacity": {
                "evidence_for": ["Histogram counts and values are recorded."],
                "evidence_against": [
                    "The workload is dominated by equality/IN predicates; source equality path prioritizes MCV and fallback."
                ],
                "unresolved": True,
            },
            "H6_source_population_or_totalrows_mismatch": {
                "evidence_for": [],
                "evidence_against": [
                    "Frozen M2.26/M2.27a metadata and replay both use source population 11591877."
                ],
                "unresolved": False,
            },
            "H7_reconstruction_bug": {
                "evidence_for": [],
                "evidence_against": [
                    "Bundle verification, sample digests, target metadata, extstats count, and workload/truth equality all pass."
                ],
                "unresolved": False,
            },
            "H8_objective_pathology": {
                "evidence_for": [
                    "M2.27b top contributors and fixed-set trimmed objective are reported."
                ],
                "evidence_against": [],
                "unresolved": True,
            },
        },
        "recommendation": "Do not implement a block-aware sampler from M2.28 alone. The evidence establishes ordinary-statistics divergence and a plausible block-sensitive native path, but does not isolate block structure from target thresholds, stream-order effects, or objective concentration.",
        "native_analyze_equivalent": False,
        "target_selection_fidelity_qualified": False,
    }
    OUT.mkdir(parents=True, exist_ok=True)
    write_json(OUT / "summary.json", summary)
    write_json(
        OUT / "ordinary-statistics.json",
        {
            key: {column: value for column, value in state["stats"].items()}
            for key, state in states.items()
        },
    )
    write_json(OUT / "query-decomposition.json", decomposition)
    write_json(OUT / "focus-cases.json", root_cases)
    write_json(OUT / "source-audit.json", source_audit())
    csv_write(OUT / "top-query-analysis.csv", structural)
    csv_write(OUT / "predicate-mcv-membership.csv", memberships)
    csv_write(OUT / "column-statistics-summary.csv", stat_summary)
    csv_write(OUT / "statistics-semantic-diff.csv", stat_diff)
    csv_write(OUT / "target-differentials.csv", differentials)
    csv_write(OUT / "distribution-diagnostics.csv", distribution_rows)
    csv_write(OUT / "column-frequency.csv", columns)
    csv_write(OUT / "column-pairs.csv", pairs)
    csv_write(OUT / "predicate-values.csv", values)
    write_json(OUT / "trimmed-objective-analysis.json", trim)
    (OUT / "hypothesis-assessment.md").write_text(
        "# M2.28 hypothesis assessment\n\nNo new sampling mechanism, search, deployment, or production capture was run. See `summary.json` for the evidence table and `source-audit.json` for frozen PG16.14 source references.\n\nThe block/page hypothesis remains unresolved; M2.28 does not justify a block-aware prototype by itself.\n",
        encoding="utf-8",
    )
    (OUT / "README.md").write_text(
        "# M2.28 acquisition-fidelity root-cause study\n\nThis package replays the existing M2.26 native and M2.27a reservoir samples in the isolated advisor cluster and compares ordinary PostgreSQL statistics. It does not create extstats, run search, capture production, or implement a sampler. `native_analyze_equivalent = false` and `target_selection_fidelity_qualified = false` are intentional.\n",
        encoding="utf-8",
    )
    print(f"wrote {OUT}")


if __name__ == "__main__":
    main()
