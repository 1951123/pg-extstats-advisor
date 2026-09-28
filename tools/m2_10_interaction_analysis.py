"""Analyse the M2.10 36-round contextual replay without running search."""

from __future__ import annotations

import argparse
import csv
import hashlib
import json
import math
import statistics
from collections import Counter, defaultdict
from pathlib import Path
from typing import Any

from pg_extstats_advisor.analysis.interaction import (
    classify_contextual,
    gain_recall,
    percentile_bucket,
)

ROOT = Path(__file__).resolve().parents[1]
OUT = ROOT / "experiments/census-m2-10-interactions"
M29 = ROOT / "experiments/census-m2-9-singletons/singleton-results.csv"
EVALS = OUT / "contextual-evaluations.csv"
SUMMARY = OUT / "summary.json"
REPORT = OUT / "report.md"
CANDIDATE_SUMMARY = OUT / "candidate-context-summary.csv"
SCREENING = OUT / "screening-recall.csv"
PERCENTILE = OUT / "singleton-percentile-context-summary.csv"
ACCEPTED = OUT / "accepted-sequence.csv"
SURPRISE = OUT / "surprise-candidates.csv"


def quantile(values: list[float], probability: float) -> float | None:
    ordered = sorted(values)
    if not ordered:
        return None
    if len(ordered) == 1:
        return ordered[0]
    position = probability * (len(ordered) - 1)
    lower = math.floor(position)
    upper = math.ceil(position)
    if lower == upper:
        return ordered[lower]
    return ordered[lower] + (position - lower) * (ordered[upper] - ordered[lower])


def write_json(path: Path, value: Any) -> None:
    temporary = path.with_suffix(path.suffix + ".tmp")
    temporary.write_text(json.dumps(value, indent=2, sort_keys=True) + "\n")
    temporary.replace(path)


def sha256(path: Path) -> str:
    return hashlib.sha256(path.read_bytes()).hexdigest()


def write_csv(path: Path, rows: list[dict[str, Any]], fields: list[str] | None = None) -> None:
    if fields is None:
        fields = list(rows[0]) if rows else ["candidate_id"]
    temporary = path.with_suffix(path.suffix + ".tmp")
    with temporary.open("w", newline="") as handle:
        writer = csv.DictWriter(handle, fieldnames=fields, extrasaction="ignore")
        writer.writeheader()
        writer.writerows(rows)
    temporary.replace(path)


def load_singletons() -> dict[str, dict[str, Any]]:
    with M29.open(newline="") as handle:
        rows = list(csv.DictReader(handle))
    if len(rows) != 4506:
        raise RuntimeError("M2.9 singleton result coverage is not 4506")
    for row in rows:
        row["singleton_improvement"] = float(row["singleton_improvement"])
        row["maintenance_cost_numeric"] = float(row["maintenance_cost_numeric"])
        row["precedence_rank"] = int(row["precedence_rank"])
    raw = sorted(
        rows,
        key=lambda row: (
            -row["singleton_improvement"],
            row["maintenance_cost_numeric"],
            row["precedence_rank"],
            row["candidate_id"],
        ),
    )
    cost = sorted(
        rows,
        key=lambda row: (
            -(row["singleton_improvement"] / row["maintenance_cost_numeric"]),
            row["maintenance_cost_numeric"],
            row["precedence_rank"],
            row["candidate_id"],
        ),
    )
    for rank, row in enumerate(raw, start=1):
        row["singleton_rank"] = rank
        row["singleton_percentile"] = 100.0 * (len(raw) - rank + 1) / len(raw)
    for rank, row in enumerate(cost, start=1):
        row["cost_aware_rank"] = rank
    return {row["candidate_id"]: row for row in raw}


def load_evaluations(singletons: dict[str, dict[str, Any]]) -> list[dict[str, Any]]:
    with EVALS.open(newline="") as handle:
        rows = list(csv.DictReader(handle))
    if len(rows) != 86098:
        raise RuntimeError(f"unexpected contextual evaluation count: {len(rows)}")
    for row in rows:
        singleton = singletons[row["candidate_id"]]
        row["round"] = int(row["round"])
        row["selected_count_before"] = int(row["selected_count_before"])
        row["singleton_improvement"] = float(row["singleton_improvement"])
        row["singleton_rank"] = int(row["singleton_rank"])
        row["singleton_percentile"] = float(row["singleton_percentile"])
        row["objective_before"] = float(row["objective_before"])
        row["objective_after"] = float(row["objective_after"])
        row["contextual_improvement"] = float(row["contextual_improvement"])
        row["interaction_gain"] = float(row["interaction_gain"])
        row["accepted"] = row["accepted"] == "True"
        row["affected_query_count"] = int(row["affected_query_count"])
        row["maintenance_cost"] = float(row["maintenance_cost"])
        row["improvement_class"] = classify_contextual(row["contextual_improvement"])
        row["percentile_bucket"] = percentile_bucket(row["singleton_rank"], 4506)
        row["cost_aware_rank"] = singleton["cost_aware_rank"]
    return rows


def candidate_summaries(
    rows: list[dict[str, Any]], singletons: dict[str, dict[str, Any]]
) -> list[dict[str, Any]]:
    by_id: dict[str, list[dict[str, Any]]] = defaultdict(list)
    for row in rows:
        by_id[row["candidate_id"]].append(row)
    output = []
    for candidate_id, evaluations in by_id.items():
        base = singletons[candidate_id]
        best = max(evaluations, key=lambda row: (row["contextual_improvement"], -row["round"]))
        accepted = [row for row in evaluations if row["accepted"]]
        improvements = [row["contextual_improvement"] for row in evaluations]
        positive = sum(value > 0 for value in improvements)
        zero = sum(value == 0 for value in improvements)
        negative = sum(value < 0 for value in improvements)
        output.append(
            {
                "candidate_id": candidate_id,
                "mechanism": base["mechanism"],
                "realization_state": base["realization_state"],
                "singleton_improvement": base["singleton_improvement"],
                "singleton_rank": base["singleton_rank"],
                "singleton_percentile": base["singleton_percentile"],
                "contextual_evaluation_count": len(evaluations),
                "contextual_positive_count": positive,
                "contextual_zero_count": zero,
                "contextual_negative_count": negative,
                "positive_contextual_rate": positive / len(evaluations),
                "max_contextual_improvement": max(improvements),
                "median_contextual_improvement": quantile(improvements, 0.50),
                "p90_contextual_improvement": quantile(improvements, 0.90),
                "p95_contextual_improvement": quantile(improvements, 0.95),
                "max_interaction_gain": max(row["interaction_gain"] for row in evaluations),
                "round_of_max_contextual_improvement": best["round"],
                "ever_accepted": bool(accepted),
                "accepted_round": accepted[0]["round"] if accepted else "",
                "cost_aware_rank": base["cost_aware_rank"],
            }
        )
    return sorted(output, key=lambda row: row["singleton_rank"])


def distribution(rows: list[dict[str, Any]]) -> dict[str, Any]:
    counter = Counter(row["improvement_class"] for row in rows)
    values = [row["contextual_improvement"] for row in rows]
    return {
        "evaluations": len(rows),
        "positive": counter.get("positive", 0),
        "zero": counter.get("zero", 0),
        "negative": counter.get("negative", 0),
        "positive_rate": counter.get("positive", 0) / len(rows) if rows else 0.0,
        "zero_rate": counter.get("zero", 0) / len(rows) if rows else 0.0,
        "negative_rate": counter.get("negative", 0) / len(rows) if rows else 0.0,
        "max_contextual_improvement": max(values) if values else None,
        "max_interaction_gain": max((row["interaction_gain"] for row in rows), default=None),
        "p50_contextual_improvement": quantile(values, 0.50),
        "p90_contextual_improvement": quantile(values, 0.90),
        "p95_contextual_improvement": quantile(values, 0.95),
    }


def region_metrics(
    name: str,
    summaries: list[dict[str, Any]],
    rows: list[dict[str, Any]],
    predicate: Any,
    thresholds: dict[str, float],
    population: list[dict[str, Any]] | None = None,
) -> dict[str, Any]:
    population_rows = population if population is not None else summaries
    candidate_ids = {row["candidate_id"] for row in population_rows if predicate(row)}
    observed_rows = [row for row in rows if row["candidate_id"] in candidate_ids]
    observed_summary = [row for row in summaries if row["candidate_id"] in candidate_ids]
    return {
        "name": name,
        "candidate_population": len(candidate_ids),
        "unique_candidates_observed": len(observed_summary),
        "contextual_evaluations": len(observed_rows),
        "positive_contextual_evaluations": sum(row["contextual_improvement"] > 0 for row in observed_rows),
        "positive_contextual_rate": (
            sum(row["contextual_improvement"] > 0 for row in observed_rows) / len(observed_rows)
            if observed_rows else 0.0
        ),
        "large_contextual_counts": {
            label: sum(row["contextual_improvement"] > threshold for row in observed_rows)
            for label, threshold in thresholds.items()
        },
        "large_contextual_candidate_counts": {
            label: sum(row["max_contextual_improvement"] > threshold for row in observed_summary)
            for label, threshold in thresholds.items()
        },
        "accepted_count": sum(row["ever_accepted"] for row in observed_summary),
        "distribution": distribution(observed_rows),
    }


def spearman(xs: list[float], ys: list[float]) -> float | None:
    if len(xs) != len(ys) or len(xs) < 2:
        return None
    def ranks(values: list[float]) -> list[float]:
        indexed = sorted(enumerate(values), key=lambda item: item[1])
        result = [0.0] * len(values)
        index = 0
        while index < len(indexed):
            end = index + 1
            while end < len(indexed) and indexed[end][1] == indexed[index][1]:
                end += 1
            rank = (index + 1 + end) / 2
            for position in range(index, end):
                result[indexed[position][0]] = rank
            index = end
        return result
    return pearson(ranks(xs), ranks(ys))


def pearson(xs: list[float], ys: list[float]) -> float | None:
    if len(xs) != len(ys) or len(xs) < 2:
        return None
    x_mean = statistics.mean(xs)
    y_mean = statistics.mean(ys)
    numerator = sum((x - x_mean) * (y - y_mean) for x, y in zip(xs, ys))
    x_den = math.sqrt(sum((x - x_mean) ** 2 for x in xs))
    y_den = math.sqrt(sum((y - y_mean) ** 2 for y in ys))
    return numerator / (x_den * y_den) if x_den and y_den else None


def screening_rows(
    summaries: list[dict[str, Any]],
    accepted: list[dict[str, Any]],
    thresholds: dict[str, float],
) -> list[dict[str, Any]]:
    accepted_ids = {row["candidate_id"] for row in accepted}
    accepted_gain = [(row["candidate_id"], row["contextual_improvement"]) for row in accepted]
    high_ids = {
        label: {
            row["candidate_id"]
            for row in summaries
            if row["max_contextual_improvement"] > threshold
        }
        for label, threshold in thresholds.items()
    }
    output = []
    for ranking in ("raw", "cost-aware"):
        rank_key = "singleton_rank" if ranking == "raw" else "cost_aware_rank"
        for fraction in (0.01, 0.02, 0.05, 0.10, 0.20, 0.30, 0.40, 0.50, 0.75, 1.0):
            retained_count = math.ceil(4506 * fraction)
            retained = {
                row["candidate_id"] for row in summaries if row[rank_key] <= retained_count
            }
            output_row = {
                "ranking": ranking,
                "retained_fraction": fraction,
                "retained_count": retained_count,
                "accepted_candidate_recall": len(retained & accepted_ids) / len(accepted_ids),
                "accepted_gain_recall": gain_recall(retained, accepted_gain),
            }
            for label, ids in high_ids.items():
                output_row[f"high_{label}_candidate_count"] = len(ids)
                output_row[f"high_{label}_recall"] = len(retained & ids) / len(ids) if ids else None
            output.append(output_row)
    return output


def main() -> None:
    parser = argparse.ArgumentParser()
    parser.add_argument("--write", action="store_true")
    args = parser.parse_args()
    del args
    singletons = load_singletons()
    all_singleton_rows = list(singletons.values())
    rows = load_evaluations(singletons)
    summaries = candidate_summaries(rows, singletons)
    accepted = [row for row in rows if row["accepted"]]
    if len(accepted) != 36:
        raise RuntimeError(f"expected 36 accepted rows, found {len(accepted)}")
    accepted = sorted(accepted, key=lambda row: row["round"])
    accepted_values = [row["contextual_improvement"] for row in accepted]
    thresholds = {
        "accepted_median": quantile(accepted_values, 0.50) or 0.0,
        "accepted_p75": quantile(accepted_values, 0.75) or 0.0,
        "accepted_p90": quantile(accepted_values, 0.90) or 0.0,
    }
    write_csv(
        CANDIDATE_SUMMARY,
        summaries,
        list(summaries[0]) if summaries else ["candidate_id"],
    )
    percentile_rows = []
    for bucket in (
        "top_1pct", "1_5pct", "5_10pct", "10_20pct", "20_40pct", "40_60pct",
        "60_80pct", "bottom_20pct",
    ):
        bucket_summaries = [row for row in summaries if percentile_bucket(row["singleton_rank"], 4506) == bucket]
        bucket_rows = [row for row in rows if row["percentile_bucket"] == bucket]
        item = distribution(bucket_rows)
        item.update({
            "bucket": bucket,
            "unique_candidates_observed": len(bucket_summaries),
            "accepted_move_count": sum(row["accepted"] for row in bucket_rows),
            "median_contextual_improvement": quantile(
                [row["contextual_improvement"] for row in bucket_rows], 0.50
            ),
            "p90_contextual_improvement": quantile(
                [row["contextual_improvement"] for row in bucket_rows], 0.90
            ),
            "p95_contextual_improvement": quantile(
                [row["contextual_improvement"] for row in bucket_rows], 0.95
            ),
        })
        percentile_rows.append(item)
    write_csv(PERCENTILE, percentile_rows)
    accepted_sequence = []
    for row in accepted:
        accepted_sequence.append({
            "accepted_round": row["round"],
            "candidate_id": row["candidate_id"],
            "mechanism": row["mechanism"],
            "realization_state": row["realization_state"],
            "singleton_improvement": row["singleton_improvement"],
            "singleton_rank": row["singleton_rank"],
            "singleton_percentile": row["singleton_percentile"],
            "contextual_improvement": row["contextual_improvement"],
            "interaction_gain": row["interaction_gain"],
            "selected_count_before": row["selected_count_before"],
        })
    write_csv(ACCEPTED, accepted_sequence)
    screening = screening_rows(summaries, accepted, thresholds)
    write_csv(SCREENING, screening)
    surprise_rows = []
    surprise_specs = {
        "bottom_50pct": lambda row: row["singleton_rank"] > 2253,
        "bottom_80pct": lambda row: row["singleton_rank"] > 901,
        "singleton_nonpositive": lambda row: row["singleton_improvement"] <= 0,
    }
    for name, predicate in surprise_specs.items():
        selected = [row for row in summaries if predicate(row)]
        selected.sort(key=lambda row: (-row["max_contextual_improvement"], row["singleton_rank"], row["candidate_id"]))
        for row in selected[:20]:
            surprise_rows.append({
                "region": name,
                "candidate_id": row["candidate_id"],
                "mechanism": row["mechanism"],
                "realization_state": row["realization_state"],
                "singleton_improvement": row["singleton_improvement"],
                "singleton_rank": row["singleton_rank"],
                "max_contextual_improvement": row["max_contextual_improvement"],
                "max_interaction_gain": row["max_interaction_gain"],
                "round_of_max_contextual_improvement": row["round_of_max_contextual_improvement"],
                "ever_accepted": row["ever_accepted"],
            })
    write_csv(SURPRISE, surprise_rows)
    regions = {}
    for label, fraction in (("bottom_50pct", 0.50), ("bottom_70pct", 0.70),
                            ("bottom_80pct", 0.80), ("bottom_90pct", 0.90), ("bottom_95pct", 0.95)):
        cutoff = math.floor(4506 * (1 - fraction))
        regions[label] = region_metrics(
            label, summaries, rows, lambda row, cutoff=cutoff: row["singleton_rank"] > cutoff,
            thresholds,
            all_singleton_rows,
        )
    positive_values = [row["singleton_improvement"] for row in singletons.values() if row["singleton_improvement"] > 0]
    median_positive = quantile(positive_values, 0.50) or 0.0
    regions["singleton_nonpositive"] = region_metrics(
        "singleton_nonpositive", summaries, rows, lambda row: row["singleton_improvement"] <= 0,
        thresholds,
        all_singleton_rows,
    )
    regions["positive_below_median"] = region_metrics(
        "positive_below_median", summaries, rows,
        lambda row: 0 < row["singleton_improvement"] < median_positive, thresholds,
        all_singleton_rows,
    )
    regions["positive_at_or_above_median"] = region_metrics(
        "positive_at_or_above_median", summaries, rows,
        lambda row: row["singleton_improvement"] >= median_positive, thresholds,
        all_singleton_rows,
    )
    mechanisms = {}
    for mechanism in ("mcv", "fd"):
        mech_rows = [row for row in rows if row["mechanism"] == mechanism]
        mech_summaries = [row for row in summaries if row["mechanism"] == mechanism]
        mechanisms[mechanism] = region_metrics(
            mechanism, mech_summaries, mech_rows, lambda row: True, thresholds,
            [row for row in all_singleton_rows if row["mechanism"] == mechanism],
        )
        mechanisms[mechanism]["low_singleton_rescue"] = region_metrics(
            f"{mechanism}_bottom_50pct", mech_summaries, mech_rows,
            lambda row: row["singleton_rank"] > 2253, thresholds,
            [row for row in all_singleton_rows if row["mechanism"] == mechanism],
        )
    absent_population = [row for row in all_singleton_rows if row["realization_state"] == "ABSENT_NATIVE"]
    absent = [row for row in summaries if row["realization_state"] == "ABSENT_NATIVE"]
    absent_rows = [row for row in rows if row["realization_state"] == "ABSENT_NATIVE"]
    round_segments = {}
    for label, low, high in (("early_1_12", 1, 12), ("middle_13_24", 13, 24), ("late_25_36", 25, 36)):
        segment = [row for row in rows if low <= row["round"] <= high]
        round_segments[label] = distribution(segment)
        round_segments[label]["accepted_count"] = sum(row["accepted"] for row in segment)
    observed_ids = {row["candidate_id"] for row in summaries}
    candidate_max = {row["candidate_id"]: row["max_contextual_improvement"] for row in summaries}
    correlation_rows = [singletons[candidate_id] for candidate_id in observed_ids]
    correlation = {
        "observed_candidate_count": len(correlation_rows),
        "pearson_singleton_vs_max_contextual": pearson(
            [row["singleton_improvement"] for row in correlation_rows],
            [candidate_max[row["candidate_id"]] for row in correlation_rows],
        ),
        "spearman_singleton_vs_max_contextual": spearman(
            [row["singleton_improvement"] for row in correlation_rows],
            [candidate_max[row["candidate_id"]] for row in correlation_rows],
        ),
    }
    boundary = [row for row in accepted_sequence if 500 < row["singleton_rank"] <= 1000]
    accepted_ranks = [row["singleton_rank"] for row in accepted_sequence]
    rank_trend = {
        "accepted_ranks": accepted_ranks,
        "spearman_round_vs_rank": spearman(
            [row["accepted_round"] for row in accepted_sequence], accepted_ranks
        ),
        "first_half_median_rank": quantile(accepted_ranks[:18], 0.50),
        "second_half_median_rank": quantile(accepted_ranks[18:], 0.50),
    }
    summary = {
        "format_version": 1,
        "experiment": "M2.10 Singleton Screening Interaction-Rescue Analysis",
        "protocol_sha256": sha256(OUT / "protocol.json"),
        "provenance": json.loads((OUT / "protocol.json").read_text())["source_artifacts"],
        "lineage": json.loads((OUT / "protocol.json").read_text())["lineage"],
        "replay": json.loads((OUT / "replay-checkpoint.json").read_text()),
        "coverage": {
            "contextual_native_evaluations": len(rows),
            "unique_candidates_observed": len(observed_ids),
            "candidates_never_natively_observed": 4506 - len(observed_ids),
            "bound_pruned_moves_not_included": 75488,
            "observational_trajectory_only": True,
        },
        "overall": distribution(rows),
        "accepted_reference": {
            "count": len(accepted),
            "improvement_quantiles": {
                "median": thresholds["accepted_median"],
                "p75": thresholds["accepted_p75"],
                "p90": thresholds["accepted_p90"],
                "max": max(accepted_values),
            },
        },
        "singleton_percentile_buckets": percentile_rows,
        "low_singleton_regions": regions,
        "screening_recall": screening,
        "nonpositive_singleton": regions["singleton_nonpositive"],
        "absent_native": {
            "candidate_count": len(absent_population),
            "observed_count": len(absent),
            "contextual_evaluations": len(absent_rows),
            "positive_contextual_count": sum(row["contextual_improvement"] > 0 for row in absent_rows),
            "accepted_count": sum(row["accepted"] for row in absent_rows),
            "max_contextual_improvement": max((row["contextual_improvement"] for row in absent_rows), default=None),
        },
        "mechanisms": mechanisms,
        "correlation": correlation,
        "round_segments": round_segments,
        "accepted_sequence": rank_trend,
        "boundary_case_rank_501_1000": boundary,
        "surprise_counts": dict(Counter(row["region"] for row in surprise_rows)),
        "interpretation": {
            "definition": "contextual improvement and interaction gain are observed-only descriptive quantities",
            "no_screening_rule": True,
            "no_search_resume": True,
            "no_m3": True,
            "low_singleton_not_globally_useless": True,
        },
    }
    write_json(SUMMARY, summary)
    write_report(summary, thresholds, boundary, surprise_rows)
    print(json.dumps({"status": "complete", "evaluations": len(rows)}, sort_keys=True))


def write_report(summary: dict[str, Any], thresholds: dict[str, float], boundary: list[dict[str, Any]], surprise_rows: list[dict[str, Any]]) -> None:
    overall = summary["overall"]
    coverage = summary["coverage"]
    absent = summary["absent_native"]
    nonpositive = summary["nonpositive_singleton"]
    lines = [
        "# M2.10 Singleton Screening Interaction-Rescue Analysis",
        "",
        "## Scope",
        "",
        (
            "This pre-study uses the 4,506 M2.9 singleton results and a strict replay of "
            "the 36 completed M2.7-v2 greedy rounds. It captures only actual native-evaluated ADD "
            "moves. Bound-pruned moves are excluded because they have no observed native objective. "
            "The replay stopped before round 37; no search, screening threshold, ANALYZE, DDL, or M3 "
            "work was performed."
        ),
        "",
        (
            "For each observed move, contextual improvement is `F(Y_t) - F(Y_t ∪ {c})`, and "
            "interaction gain is contextual improvement minus the frozen M2.9 singleton improvement. "
            "All conclusions are observational and trajectory-dependent."
        ),
        "",
        "## Coverage and overall contextual distribution",
        "",
        (
            f"The replay analyzed {coverage['contextual_native_evaluations']} native evaluations "
            f"over {coverage['unique_candidates_observed']} unique candidates; "
            f"{coverage['candidates_never_natively_observed']} candidates were never natively observed. "
            f"Positive/zero/negative contextual evaluations: {overall['positive']} / {overall['zero']} / "
            f"{overall['negative']} (positive rate {overall['positive_rate']:.4%})."
        ),
        "",
        (
            f"Maximum contextual improvement: {overall['max_contextual_improvement']:.6f}; "
            f"maximum interaction gain: {overall['max_interaction_gain']:.6f}."
        ),
        "",
        "## Singleton percentile behavior",
        "",
        (
            "See `singleton-percentile-context-summary.csv` for unique-candidate and evaluation "
            "denominators, contextual rates, quantiles, and accepted counts by raw singleton rank bucket."
        ),
        "",
        "## Rescue regions",
        "",
        (
            f"Accepted contextual-improvement reference levels are median={thresholds['accepted_median']:.6f}, "
            f"p75={thresholds['accepted_p75']:.6f}, p90={thresholds['accepted_p90']:.6f}."
        ),
        "",
        (
            f"For singleton-nonpositive candidates: observed {nonpositive['unique_candidates_observed']}, "
            f"positive contextual evaluations {nonpositive['positive_contextual_evaluations']}, "
            f"accepted moves {nonpositive['accepted_count']}, and maximum contextual improvement "
            f"{nonpositive['distribution']['max_contextual_improvement']}."
        ),
        "",
        (
            f"ABSENT_NATIVE: observed {absent['observed_count']}, positive contextual evaluations "
            f"{absent['positive_contextual_count']}, accepted {absent['accepted_count']}, maximum "
            f"contextual improvement {absent['max_contextual_improvement']}. This is evidence for "
            "this trajectory only, not a pruning theorem."
        ),
        "",
        "## Screening recall (retrospective only)",
        "",
        (
            "`screening-recall.csv` reports raw and cost-aware singleton-ranking retention at the "
            "requested fractions. Accepted-gain recall is the fraction of the 36 accepted contextual "
            "improvement sum retained; it is not a rerun objective guarantee."
        ),
        "",
        f"The accepted candidate outside raw top-500 but inside top-1000: {boundary}.",
        "",
        "## Contextual surprise and limitations",
        "",
        (
            f"Surprise rows are stored in `surprise-candidates.csv` ({len(surprise_rows)} rows across "
            "the requested regions). Candidates never observed were bound-pruned or otherwise absent "
            "from native evaluation; never-observed-positive is not evidence of contextual uselessness."
        ),
        "",
        (
            f"Accepted-rank trend: {summary['accepted_sequence']}. Mechanism-specific rescue, "
            "correlation, round-segment, and cost-aware recall details are in `summary.json` and "
            "the CSV artifacts."
        ),
        "",
        (
            "No screening rule is selected. The evidence is sufficient to quantify the observed "
            "trajectory but not to claim that singleton utility is globally contextual or to finalize "
            "a production cutoff."
        ),
    ]
    REPORT.write_text("\n".join(lines) + "\n")


if __name__ == "__main__":
    main()
