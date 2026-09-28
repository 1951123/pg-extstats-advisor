"""Profile all frozen Census candidates in the empty-design context.

This is a diagnostic pre-study.  It evaluates every singleton with the frozen
native evaluator, but never invokes the budget search or changes acquisition
artifacts.  The analysis phase consumes the persisted CSV and the M2.7-v2
partial checkpoint to produce descriptive ranking/retrospective artifacts.
"""

from __future__ import annotations

import argparse
import csv
import hashlib
import json
import math
import statistics
import time
from collections import Counter, defaultdict
from pathlib import Path
from typing import Any

import psycopg

from pg_extstats_advisor.analysis.singleton import (
    classify_improvement,
    deterministic_order,
    linear_quantile,
    percentile_from_rank,
    recall_at,
)
from pg_extstats_advisor.evaluator.native import NativeEvaluator
from pg_extstats_advisor.models import Design, Move
from pg_extstats_advisor.orchestration import load_maintenance_model, load_prepared_run
from pg_extstats_advisor.postgres.adapter import PostgresAdapter
from pg_extstats_advisor.search.deterministic import optimistic_objective_lower_bound

ROOT = Path(__file__).resolve().parents[1]
M27_ROOT = ROOT / "experiments/census-m2-7/budgets/b010-run1"
OUT_ROOT = ROOT / "experiments/census-m2-9-singletons"
PROTOCOL_PATH = OUT_ROOT / "protocol.json"
RESULTS_PATH = OUT_ROOT / "singleton-results.csv"
CHECKPOINT_PATH = OUT_ROOT / "profile-checkpoint.json"
RETROSPECTIVE_PATH = OUT_ROOT / "partial-run-retrospective.csv"
SUMMARY_PATH = OUT_ROOT / "singleton-summary.json"
RANKING_PATH = OUT_ROOT / "ranking-summary.json"
PERFORMANCE_PATH = OUT_ROOT / "performance.json"
REPORT_PATH = OUT_ROOT / "report.md"
PARTIAL_CHECKPOINT = ROOT / "experiments/census-m2-7/v2/budgets/b100-run1/checkpoint.json"
EXPECTED_BASELINE = 5586.930692724469
EXPECTED = {
    "workload": "796ab606e825969ad91801c9795cd830ef40686568feef857d856921a8d42215",
    "catalog": "74727a871b3a601977885cbf96d25cda6e5ac38ef448d1815561bdc5988a425a",
    "incidence": "0cd8466f654e21080914cf8444ad3ce3a093d9a7700d370b5ac6a4b9a28f66b4",
    "repository": "c0167aa2a48d2c9cccf1249643dccffd737356ae0d97dcab84ea9ec8a97ad7fe",
    "model": "dbad23611afa778a7ff6aab6ad39e519f7bfc51387874aa07dd33e37a5d3a11e",
}
DEFAULT_DSN = (
    "host=/root/projects/pg-extstats-advisor/.build/pg16.14-experiment-socket "
    "port=55436 dbname=pgextadv_exp16_census_m27_acq user=postgres"
)
CSV_FIELDS = [
    "candidate_id",
    "precedence_rank",
    "mechanism",
    "relation",
    "column_1",
    "column_2",
    "attnum_1",
    "attnum_2",
    "realization_state",
    "payload_digest",
    "maintenance_cost",
    "maintenance_cost_numeric",
    "incidence_query_count",
    "singleton_objective",
    "singleton_improvement",
    "relative_improvement_vs_baseline",
    "improvement_class",
    "improvement_per_maintenance_cost",
    "optimistic_improvement_bound",
    "realized_fraction_of_bound",
    "improved_query_count",
    "unchanged_query_count",
    "worsened_query_count",
    "max_qerror_reduction",
    "max_qerror_increase",
    "affected_query_count",
    "planner_calls",
    "elapsed_seconds",
]


def write_json(path: Path, value: Any) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    temporary = path.with_suffix(path.suffix + ".tmp")
    temporary.write_text(json.dumps(value, indent=2, sort_keys=True) + "\n")
    temporary.replace(path)


def write_csv(path: Path, rows: list[dict[str, Any]], fields: list[str]) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    temporary = path.with_suffix(path.suffix + ".tmp")
    with temporary.open("w", newline="") as handle:
        writer = csv.DictWriter(handle, fieldnames=fields, extrasaction="ignore")
        writer.writeheader()
        writer.writerows(rows)
    temporary.replace(path)


def digest_file(path: Path) -> str:
    return hashlib.sha256(path.read_bytes()).hexdigest()


def verify_lineage(prepared: Any, model: Any) -> None:
    from pg_extstats_advisor.search.model import candidate_catalog_digest

    actual = {
        "workload": prepared.workload.digest,
        "catalog": candidate_catalog_digest(prepared.catalog.candidates),
        "incidence": prepared.incidence_digest,
        "repository": prepared.repository.digest,
        "model": model.digest,
    }
    if actual != EXPECTED:
        raise RuntimeError(f"frozen lineage mismatch: expected={EXPECTED}, actual={actual}")
    if len(prepared.workload.queries) != 468 or len(prepared.catalog.candidates) != 4506:
        raise RuntimeError("frozen Census cardinality mismatch")
    if sum(1 for item in prepared.catalog.candidates if item.mechanism.value == "mcv") != 2253:
        raise RuntimeError("frozen MCV cardinality mismatch")
    if sum(1 for item in prepared.catalog.candidates if item.mechanism.value == "fd") != 2253:
        raise RuntimeError("frozen FD cardinality mismatch")


def candidate_row(
    candidate: Any,
    frozen: Any,
    state: Any,
    baseline: Any,
    model: Any,
    affected: set[Any],
    planner_calls: int,
    elapsed: float,
) -> dict[str, Any]:
    baseline_by_query = baseline.by_query()
    singleton_by_query = state.by_query()
    differences = [
        baseline_by_query[item].contribution - singleton_by_query[item].contribution
        for item in baseline_by_query
    ]
    improvements = sum(value > 0 for value in differences)
    worsened = sum(value < 0 for value in differences)
    unchanged = len(differences) - improvements - worsened
    maintenance = model.estimate_candidate(candidate)
    improvement = baseline.aggregate_objective - state.aggregate_objective
    bound = baseline.aggregate_objective - optimistic_objective_lower_bound(
        baseline, affected
    )
    attnums = list(dict(candidate.definition).get("attnums", ()))
    return {
        "candidate_id": str(candidate.candidate_id),
        "precedence_rank": candidate.precedence_rank,
        "mechanism": candidate.mechanism.value,
        "relation": candidate.relation_name,
        "column_1": candidate.attributes[0],
        "column_2": candidate.attributes[1],
        "attnum_1": attnums[0],
        "attnum_2": attnums[1],
        "realization_state": frozen.state.value,
        "payload_digest": frozen.payload_sha256 or "",
        "maintenance_cost": str(maintenance),
        "maintenance_cost_numeric": float(maintenance),
        "incidence_query_count": len(affected),
        "singleton_objective": state.aggregate_objective,
        "singleton_improvement": improvement,
        "relative_improvement_vs_baseline": improvement / baseline.aggregate_objective,
        "improvement_class": classify_improvement(
            baseline.aggregate_objective, state.aggregate_objective
        ),
        "improvement_per_maintenance_cost": improvement / float(maintenance),
        "optimistic_improvement_bound": bound,
        "realized_fraction_of_bound": improvement / bound if bound > 0 else None,
        "improved_query_count": improvements,
        "unchanged_query_count": unchanged,
        "worsened_query_count": worsened,
        "max_qerror_reduction": max(differences, default=0.0),
        "max_qerror_increase": max((-value for value in differences), default=0.0),
        "affected_query_count": len(state.affected_query_ids),
        "planner_calls": planner_calls,
        "elapsed_seconds": elapsed,
    }


def load_rows() -> list[dict[str, Any]]:
    with RESULTS_PATH.open(newline="") as handle:
        return list(csv.DictReader(handle))


def profile(dsn: str, resume: bool) -> tuple[list[dict[str, Any]], dict[str, Any]]:
    prepared = load_prepared_run(M27_ROOT)
    model = load_maintenance_model(M27_ROOT)
    verify_lineage(prepared, model)
    OUT_ROOT.mkdir(parents=True, exist_ok=True)
    started = time.perf_counter()
    existing = load_rows() if resume and RESULTS_PATH.exists() else []
    done = {str(row["candidate_id"]) for row in existing}
    rows = list(existing)
    with psycopg.connect(dsn) as connection:
        adapter = PostgresAdapter(connection, prepared.repository)
        evaluator = NativeEvaluator(
            prepared.workload, prepared.repository, prepared.incidence, adapter
        )
        baseline = evaluator.evaluate_design(Design(()))
        if baseline.aggregate_objective != EXPECTED_BASELINE:
            raise RuntimeError(
                "empty-design objective mismatch: "
                f"expected={EXPECTED_BASELINE}, actual={baseline.aggregate_objective}"
            )
        empty = Design(())
        incidence = prepared.incidence.by_candidate
        by_id = prepared.repository.by_candidate
        for index, candidate in enumerate(prepared.catalog.candidates, start=1):
            candidate_id = str(candidate.candidate_id)
            if candidate_id in done:
                continue
            affected = set(incidence[candidate.candidate_id])
            before_calls = adapter.planner_calls_total
            candidate_started = time.perf_counter()
            state = evaluator.evaluate_move(
                empty, Move.add_candidate(candidate.candidate_id), baseline
            )
            elapsed = time.perf_counter() - candidate_started
            row = candidate_row(
                candidate,
                by_id[candidate.candidate_id],
                state,
                baseline,
                model,
                affected,
                adapter.planner_calls_total - before_calls,
                elapsed,
            )
            rows.append(row)
            done.add(candidate_id)
            if len(rows) % 100 == 0 or len(rows) == len(prepared.catalog.candidates):
                ordered = sorted(rows, key=lambda item: int(item["precedence_rank"]))
                write_csv(RESULTS_PATH, ordered, CSV_FIELDS)
                write_json(
                    CHECKPOINT_PATH,
                    {
                        "completed_candidates": len(rows),
                        "total_candidates": len(prepared.catalog.candidates),
                        "last_catalog_index": index,
                        "baseline_objective": baseline.aggregate_objective,
                    },
                )
                print(f"profiled {len(rows)}/{len(prepared.catalog.candidates)}", flush=True)
        if len(done) != len(prepared.catalog.candidates):
            raise RuntimeError("singleton profiling did not cover every candidate")
        rows = sorted(rows, key=lambda item: int(item["precedence_rank"]))
        write_csv(RESULTS_PATH, rows, CSV_FIELDS)
        total_elapsed = time.perf_counter() - started
        performance = {
            "profile_status": "complete",
            "candidate_count": len(rows),
            "query_count": len(prepared.workload.queries),
            "baseline_objective": baseline.aggregate_objective,
            "baseline_planner_calls": len(prepared.workload.queries),
            "total_native_evaluations": len(rows),
            "total_planner_calls": adapter.planner_calls_total,
            "total_affected_query_replans": sum(int(row["planner_calls"]) for row in rows),
            "total_elapsed_seconds": total_elapsed,
            "median_candidate_elapsed_seconds": statistics.median(
                float(row["elapsed_seconds"]) for row in rows
            ),
            "p95_candidate_elapsed_seconds": linear_quantile(
                (float(row["elapsed_seconds"]) for row in rows), 0.95
            ),
            "max_candidate_elapsed_seconds": max(
                float(row["elapsed_seconds"]) for row in rows
            ),
            "repository_digest": prepared.repository.digest,
            "no_analyze": True,
            "statistics_ddl": 0,
        }
    write_json(PERFORMANCE_PATH, performance)
    return rows, performance


def numeric_rows(rows: list[dict[str, Any]]) -> list[dict[str, Any]]:
    converted = []
    for row in rows:
        item = dict(row)
        for key in (
            "singleton_objective",
            "singleton_improvement",
            "relative_improvement_vs_baseline",
            "improvement_per_maintenance_cost",
            "optimistic_improvement_bound",
            "max_qerror_reduction",
            "max_qerror_increase",
            "elapsed_seconds",
        ):
            item[key] = float(item[key])
        item["maintenance_cost_numeric"] = float(item["maintenance_cost_numeric"])
        item["precedence_rank"] = int(item["precedence_rank"])
        item["planner_calls"] = int(item["planner_calls"])
        item["incidence_query_count"] = int(item["incidence_query_count"])
        item["affected_query_count"] = int(item["affected_query_count"])
        item["improved_query_count"] = int(item["improved_query_count"])
        item["unchanged_query_count"] = int(item["unchanged_query_count"])
        item["worsened_query_count"] = int(item["worsened_query_count"])
        raw_fraction = item.get("realized_fraction_of_bound", "")
        item["realized_fraction_of_bound"] = (
            None if raw_fraction in ("", None, "null") else float(raw_fraction)
        )
        converted.append(item)
    return converted


def counts(rows: list[dict[str, Any]]) -> dict[str, int]:
    counter = Counter(str(row["improvement_class"]) for row in rows)
    return {key: counter.get(key, 0) for key in ("positive", "zero", "negative")}


def quantiles(rows: list[dict[str, Any]]) -> dict[str, float | None]:
    values = [float(row["singleton_improvement"]) for row in rows]
    probabilities = (("p10", 0.10), ("p25", 0.25), ("p50", 0.50), ("p75", 0.75),
                     ("p90", 0.90), ("p95", 0.95), ("p99", 0.99), ("max", 1.0))
    return {key: linear_quantile(values, probability) for key, probability in probabilities}


def concentration(rows: list[dict[str, Any]]) -> dict[str, Any]:
    positive = sorted(
        (row for row in rows if row["singleton_improvement"] > 0),
        key=lambda row: (-row["singleton_improvement"], row["precedence_rank"], row["candidate_id"]),
    )
    total = sum(row["singleton_improvement"] for row in positive)
    shares: dict[str, Any] = {}
    for fraction in (0.01, 0.05, 0.10, 0.20, 0.50):
        count = math.ceil(len(positive) * fraction)
        shares[f"top_{int(fraction * 100)}pct"] = {
            "candidate_count": count,
            "utility_share": sum(row["singleton_improvement"] for row in positive[:count]) / total
            if total else None,
        }
    needed = {}
    for threshold in (0.50, 0.80, 0.90, 0.95):
        cumulative = 0.0
        count = None
        for index, row in enumerate(positive, start=1):
            cumulative += row["singleton_improvement"]
            if total and cumulative / total >= threshold:
                count = index
                break
        needed[f"{int(threshold * 100)}pct"] = count
    return {
        "positive_candidate_count": len(positive),
        "positive_utility_total": total,
        "top_fraction_utility_share": shares,
        "candidates_needed_for_positive_utility_fraction": needed,
        "interpretation": "descriptive concentration of singleton utilities only",
    }


def composition(rows: list[dict[str, Any]], count: int) -> dict[str, Any]:
    subset = rows[:count]
    return {
        "count": len(subset),
        "mechanism": dict(Counter(row["mechanism"] for row in subset)),
        "realization_state": dict(Counter(row["realization_state"] for row in subset)),
    }


def pair_diagnostics(
    rows: list[dict[str, Any]], incidence: dict[str, frozenset[Any]], count: int
) -> dict[str, Any]:
    subset = rows[:count]
    pairs: dict[tuple[str, str, str], set[str]] = defaultdict(set)
    incidence_groups: dict[tuple[str, ...], list[str]] = defaultdict(list)
    for row in subset:
        pair = (str(row["relation"]), str(row["column_1"]), str(row["column_2"]))
        pairs[pair].add(str(row["mechanism"]))
        signature = tuple(sorted(str(item) for item in incidence[row["candidate_id"]]))
        incidence_groups[signature].append(str(row["candidate_id"]))
    overlap_pairs = sum(1 for values in pairs.values() if {"mcv", "fd"} <= values)
    duplicate_incidence = sum(max(0, len(values) - 1) for values in incidence_groups.values())
    return {
        "candidate_count": len(subset),
        "unique_column_pairs": len(pairs),
        "mcv_fd_same_pair_count": overlap_pairs,
        "duplicate_incidence_set_count": duplicate_incidence,
    }


def diversity(rows: list[dict[str, Any]], incidence: dict[str, frozenset[Any]], count: int) -> dict[str, Any]:
    subset = rows[:count]
    sets = [set(incidence[row["candidate_id"]]) for row in subset]
    unique = set().union(*sets) if sets else set()
    pairwise_intersections = 0
    pairwise_jaccard = []
    for index, left in enumerate(sets):
        for right in sets[index + 1 :]:
            pairwise_intersections += bool(left & right)
            union = left | right
            pairwise_jaccard.append(len(left & right) / len(union) if union else 0.0)
    return {
        "candidate_count": len(subset),
        "unique_affected_queries": len(unique),
        "total_coverage_count": sum(len(value) for value in sets),
        "mean_affected_queries": statistics.mean(len(value) for value in sets) if sets else 0.0,
        "overlapping_candidate_pairs": pairwise_intersections,
        "mean_pairwise_jaccard": statistics.mean(pairwise_jaccard) if pairwise_jaccard else 0.0,
    }


def retrospective(
    rows: list[dict[str, Any]], incidence: dict[str, frozenset[Any]]
) -> tuple[list[dict[str, Any]], dict[str, Any]]:
    checkpoint = json.loads(PARTIAL_CHECKPOINT.read_text())
    accepted = [item for item in checkpoint["rounds"] if item["accepted"]]
    by_id = {row["candidate_id"]: row for row in rows}
    all_ranked = deterministic_order(rows, "singleton_improvement")
    all_rank = {row["candidate_id"]: index for index, row in enumerate(all_ranked, start=1)}
    mechanism_rank: dict[str, dict[str, int]] = {}
    for mechanism in ("mcv", "fd"):
        ranked = deterministic_order(
            [row for row in rows if row["mechanism"] == mechanism], "singleton_improvement"
        )
        mechanism_rank[mechanism] = {
            row["candidate_id"]: index for index, row in enumerate(ranked, start=1)
        }
    cost_ranked = deterministic_order(rows, "improvement_per_maintenance_cost")
    cost_rank = {row["candidate_id"]: index for index, row in enumerate(cost_ranked, start=1)}
    output = []
    for item in accepted:
        candidate_id = str(item["accepted_move"]).split(":", 1)[1]
        row = by_id[candidate_id]
        rank = all_rank[candidate_id]
        output.append(
            {
                "accepted_order": item["round"],
                "candidate_id": candidate_id,
                "mechanism": row["mechanism"],
                "realization_state": row["realization_state"],
                "singleton_improvement": row["singleton_improvement"],
                "singleton_rank_all": rank,
                "singleton_percentile_all": percentile_from_rank(rank, len(rows)),
                "singleton_rank_mechanism": mechanism_rank[row["mechanism"]][candidate_id],
                "singleton_improvement_per_cost": row["improvement_per_maintenance_cost"],
                "cost_aware_rank_all": cost_rank[candidate_id],
                "move_phase": item["phase"],
                "contextual_objective_before": item["objective_before"],
                "contextual_objective_after": item["objective_after"],
                "contextual_improvement": item["objective_before"] - item["objective_after"],
                "neighboring_selected_design_size": item["selected_count_before"],
                "affected_query_count": len(incidence[candidate_id]),
            }
        )
    fields = list(output[0]) if output else ["candidate_id"]
    write_csv(RETROSPECTIVE_PATH, output, fields)
    summary = {
        "accepted_count": len(output),
        "singleton_distribution": counts(output) if output else {"positive": 0, "zero": 0, "negative": 0},
        "raw_recall": {str(k): recall_at((item["singleton_rank_all"] for item in output), k)
                        for k in (50, 100, 200, 500, 1000)},
        "cost_aware_recall": {str(k): recall_at((item["cost_aware_rank_all"] for item in output), k)
                              for k in (50, 100, 200, 500, 1000)},
    }
    surprises = [
        item for item in output
        if item["singleton_improvement"] <= 0 and item["contextual_improvement"] > 0
    ]
    summary["contextual_surprise_count"] = len(surprises)
    summary["contextual_surprises"] = surprises
    return output, summary


def analyse(rows: list[dict[str, Any]], performance: dict[str, Any]) -> None:
    rows = numeric_rows(rows)
    prepared = load_prepared_run(M27_ROOT)
    incidence = {str(key): value for key, value in prepared.incidence.by_candidate.items()}
    groups = {
        "all": rows,
        "mcv": [row for row in rows if row["mechanism"] == "mcv"],
        "fd": [row for row in rows if row["mechanism"] == "fd"],
        "PRESENT": [row for row in rows if row["realization_state"] == "PRESENT"],
        "ABSENT_NATIVE": [row for row in rows if row["realization_state"] == "ABSENT_NATIVE"],
    }
    summary: dict[str, Any] = {
        "format_version": 1,
        "experiment": "M2.9 Candidate Singleton Utility Profiling",
        "candidate_count": len(rows),
        "baseline_objective": EXPECTED_BASELINE,
        "distribution": {name: counts(group) for name, group in groups.items()},
        "quantiles": {name: quantiles(group) for name, group in groups.items()},
        "concentration": concentration(rows),
        "mechanism_comparison": {
            "positive_rate": {
                name: counts(groups[name])["positive"] / len(groups[name])
                for name in ("mcv", "fd")
            },
            "median_positive_improvement": {
                name: linear_quantile(
                    (row["singleton_improvement"] for row in groups[name]
                     if row["singleton_improvement"] > 0), 0.5
                )
                for name in ("mcv", "fd")
            },
            "max_improvement": {
                name: max((row["singleton_improvement"] for row in groups[name]), default=None)
                for name in ("mcv", "fd")
            },
        },
        "performance": performance,
        "interpretation_boundary": (
            "singleton utility is evidence about candidate usefulness in the empty-design "
            "context; singleton-zero is not proof of global contextual uselessness"
        ),
    }
    ranked_raw = deterministic_order(rows, "singleton_improvement")
    ranked_cost = deterministic_order(rows, "improvement_per_maintenance_cost")
    ranking = {
        "tie_break": ["higher value", "lower maintenance cost", "lower precedence rank", "candidate ID"],
        "raw_top_composition": {str(k): composition(ranked_raw, k) for k in (100, 500)},
        "cost_aware_top_composition": {str(k): composition(ranked_cost, k) for k in (100, 500)},
        "raw_top_diversity": {str(k): diversity(ranked_raw, incidence, k) for k in (100, 500)},
        "cost_aware_top_diversity": {str(k): diversity(ranked_cost, incidence, k) for k in (100, 500)},
        "raw_pair_diagnostics": {str(k): pair_diagnostics(ranked_raw, incidence, k) for k in (100, 500)},
        "cost_aware_pair_diagnostics": {
            str(k): pair_diagnostics(ranked_cost, incidence, k) for k in (100, 500)
        },
    }
    retrospective_rows, retrospective_summary = retrospective(rows, incidence)
    summary["partial_run_retrospective"] = retrospective_summary
    summary["absent_native"] = {
        "total": len(groups["ABSENT_NATIVE"]),
        "positive": counts(groups["ABSENT_NATIVE"])["positive"],
        "zero": counts(groups["ABSENT_NATIVE"])["zero"],
        "negative": counts(groups["ABSENT_NATIVE"])["negative"],
        "nonzero_candidates": [
            {
                "candidate_id": row["candidate_id"],
                "mechanism": row["mechanism"],
                "improvement": row["singleton_improvement"],
                "relation": row["relation"],
                "column_1": row["column_1"],
                "column_2": row["column_2"],
            }
            for row in groups["ABSENT_NATIVE"]
            if row["singleton_improvement"] != 0
        ],
    }
    write_json(SUMMARY_PATH, summary)
    write_json(RANKING_PATH, ranking)
    write_report(summary, ranking, retrospective_rows)


def fmt(value: Any, digits: int = 6) -> str:
    if value is None:
        return "n/a"
    if isinstance(value, float):
        return f"{value:.{digits}f}"
    return str(value)


def write_report(summary: dict[str, Any], ranking: dict[str, Any], retrospective_rows: list[dict[str, Any]]) -> None:
    dist = summary["distribution"]
    mechanism = summary["mechanism_comparison"]
    absent = summary["absent_native"]
    retro = summary["partial_run_retrospective"]
    concentration_data = summary["concentration"]["top_fraction_utility_share"]
    needed = summary["concentration"]["candidates_needed_for_positive_utility_fraction"]
    lines = [
        "# M2.9 Candidate Singleton Utility Profiling",
        "",
        "## Scope and semantics",
        "",
        (
            "This independent pre-study evaluates all 4,506 frozen Census candidates as "
            "singletons `{c}` against the empty design. It reuses the frozen native repository "
            "and query-local PostgreSQL 16.14 replay. No ANALYZE, statistics DDL, search resume, "
            "screening rule, or M3 work was performed."
        ),
        "",
        (
            "The singleton improvement is `F(empty) - F({c})`, with strict floating comparison "
            "and no epsilon or clipping. Ranking ties use higher improvement, lower maintenance "
            "cost, lower precedence rank, then candidate ID. Percentile is top-oriented: rank "
            "one is 100%."
        ),
        "",
        "## Baseline and coverage",
        "",
        (
            f"The empty-design objective is `{fmt(summary['baseline_objective'], 12)}`, matching "
            "the M2.7 baseline. All 2,253 MCV and all 2,253 FD candidates were evaluated; "
            f"positive/zero/negative counts are {dist['all']}."
        ),
        "",
        "## Distribution",
        "",
        (
            f"MCV: {dist['mcv']}; FD: {dist['fd']}. PRESENT: {dist['PRESENT']}; "
            f"ABSENT_NATIVE: {dist['ABSENT_NATIVE']}."
        ),
        "",
        "Quantiles of singleton improvement (linear interpolation):",
        "",
        "| group | p10 | p25 | p50 | p75 | p90 | p95 | p99 | max |",
        "|---|---:|---:|---:|---:|---:|---:|---:|---:|",
    ]
    for name in ("all", "mcv", "fd", "PRESENT", "ABSENT_NATIVE"):
        q = summary["quantiles"][name]
        lines.append("| " + name + " | " + " | ".join(fmt(q[key]) for key in ("p10", "p25", "p50", "p75", "p90", "p95", "p99", "max")) + " |")
    lines += [
        "",
        "## Cost-aware diagnostic and concentration",
        "",
        (
            f"Positive-rate MCV/FD: {fmt(mechanism['positive_rate']['mcv'] * 100, 3)}% / "
            f"{fmt(mechanism['positive_rate']['fd'] * 100, 3)}%. Median positive improvement "
            f"MCV/FD: {fmt(mechanism['median_positive_improvement']['mcv'])} / "
            f"{fmt(mechanism['median_positive_improvement']['fd'])}; maximum MCV/FD: "
            f"{fmt(mechanism['max_improvement']['mcv'])} / {fmt(mechanism['max_improvement']['fd'])}."
        ),
        "",
        (
            "The concentration values below are descriptive singleton-utility shares only; "
            "they are not cumulative achievable CE gains for a multi-candidate design."
        ),
        "",
        "| top positive candidates | utility share |",
        "|---|---:|",
    ]
    for key in ("top_1pct", "top_5pct", "top_10pct", "top_20pct", "top_50pct"):
        lines.append(f"| {key} | {fmt(concentration_data[key]['utility_share'] * 100 if concentration_data[key]['utility_share'] is not None else None, 3)}% |")
    lines += [
        "",
        (
            f"Candidates needed for 50/80/90/95% of positive singleton utility: "
            f"{needed['50pct']} / {needed['80pct']} / {needed['90pct']} / {needed['95pct']}."
        ),
        "",
        (
            "Top-100 and top-500 mechanism/state compositions are in `ranking-summary.json`; "
            "the same artifact contains query diversity and pair-redundancy diagnostics for raw "
            "and cost-aware rankings."
        ),
        "",
        "## ABSENT_NATIVE",
        "",
        (
            f"There are {absent['total']} ABSENT_NATIVE candidates: positive {absent['positive']}, "
            f"zero {absent['zero']}, negative {absent['negative']}. No ABSENT_NATIVE candidate "
            "was filtered or assigned a fabricated payload."
        ),
        "",
        "## Retrospective against M2.7 protocol-v2 partial run",
        "",
        (
            f"The exact 36-candidate accepted sequence from the frozen checkpoint was mapped to "
            f"singleton ranks. Singleton positive/zero/negative: {retro['singleton_distribution']}. "
            f"Raw ranking recall at top 50/100/200/500/1000: "
            f"{retro['raw_recall']}. Cost-aware recall: {retro['cost_aware_recall']}."
        ),
        "",
        (
            f"Contextual-surprise candidates (singleton improvement <= 0 but contextual strict "
            f"improvement > 0): {retro['contextual_surprise_count']}. Details are in "
            "`partial-run-retrospective.csv` and its `contextual_surprises` summary."
        ),
        "",
        "## Interpretation boundary",
        "",
        (
            "Singleton utility is evidence about usefulness in the empty-design context. A "
            "singleton-zero candidate is not thereby globally useless; contextual interactions "
            "remain possible. This pre-study chooses no screening rule and does not resume M2.7."
        ),
        "",
        "## Performance and limitations",
        "",
        (
            f"The profile made {summary['performance']['total_native_evaluations']} native singleton "
            f"evaluations and {summary['performance']['total_planner_calls']} planner calls, with "
            f"{summary['performance']['total_affected_query_replans']} affected-query replans in "
            f"{fmt(summary['performance']['total_elapsed_seconds'], 3)} seconds. It is tied to the "
            "frozen workload, repository, source-built PostgreSQL 16.14, and current incidence "
            "contract; it does not establish a global utility decomposition or a production "
            "screening policy."
        ),
    ]
    REPORT_PATH.write_text("\n".join(lines) + "\n")


def main() -> None:
    parser = argparse.ArgumentParser()
    parser.add_argument("--dsn", default=DEFAULT_DSN)
    parser.add_argument("--phase", choices=("profile", "all"), default="all")
    parser.add_argument("--resume", action="store_true")
    args = parser.parse_args()
    rows, performance = profile(args.dsn, args.resume)
    if args.phase == "all":
        analyse(rows, performance)
    print(json.dumps({"status": "complete", "candidates": len(rows)}, sort_keys=True))


if __name__ == "__main__":
    main()
