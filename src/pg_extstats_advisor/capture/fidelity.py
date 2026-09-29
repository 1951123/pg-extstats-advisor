"""Metric normalization and conservative target-selection fidelity gates."""

from __future__ import annotations

import math
import statistics
from collections.abc import Iterable, Mapping
from typing import Any


def aggregate_qerror(values: Iterable[float]) -> float:
    return math.fsum(float(value) for value in values)


def normalize_mean(mean_qerror: float, workload_size: int) -> float:
    if workload_size <= 0:
        raise ValueError("workload size must be positive")
    return float(mean_qerror) * workload_size


def target_summary(values: list[float]) -> dict[str, Any]:
    if not values:
        raise ValueError("target summary needs at least one realization")
    mean = statistics.fmean(values)
    stdev = statistics.stdev(values) if len(values) > 1 else 0.0
    minimum, maximum = min(values), max(values)
    return {
        "values": list(values),
        "mean_aggregate_qerror": mean,
        "stddev": stdev,
        "stddev_definition": "sample",
        "min": minimum,
        "max": maximum,
        "range": maximum - minimum,
        "normalized_range": None if mean == 0 else (maximum - minimum) / mean,
        "cv": None if mean == 0 else stdev / mean,
    }


def adjacent_quality(left: Mapping[str, Any], right: Mapping[str, Any]) -> float | None:
    denominator = float(left["mean_aggregate_qerror"])
    return None if denominator == 0 else (denominator - float(right["mean_aggregate_qerror"])) / denominator


def adjacent_stability(left: Mapping[str, Any], right: Mapping[str, Any]) -> float | None:
    denominator = left.get("normalized_range")
    if denominator in (None, 0):
        return None
    return (float(denominator) - float(right["normalized_range"])) / float(denominator)


def ordering(summary: Mapping[str, Mapping[str, Any]], value: str = "mean_aggregate_qerror") -> list[int]:
    return [int(target) for target, _ in sorted(summary.items(), key=lambda item: float(item[1][value]))]


def _rank(values: list[float]) -> list[float]:
    ordered = sorted(enumerate(values), key=lambda item: item[1])
    result = [0.0] * len(values)
    index = 0
    while index < len(ordered):
        end = index
        while end + 1 < len(ordered) and ordered[end + 1][1] == ordered[index][1]:
            end += 1
        rank = (index + end + 2) / 2.0
        for position in range(index, end + 1):
            result[ordered[position][0]] = rank
        index = end + 1
    return result


def spearman(left: list[float], right: list[float]) -> float | None:
    if len(left) != len(right) or not left:
        return None
    a, b = _rank(left), _rank(right)
    am, bm = statistics.fmean(a), statistics.fmean(b)
    numerator = math.fsum((x - am) * (y - bm) for x, y in zip(a, b, strict=True))
    denominator = math.sqrt(
        math.fsum((x - am) ** 2 for x in a) * math.fsum((y - bm) ** 2 for y in b)
    )
    return None if denominator == 0 else numerator / denominator


def knee_structure(quality_gains: list[float], stability_gains: list[float]) -> dict[str, Any]:
    if len(quality_gains) != 2 or len(stability_gains) != 2:
        raise ValueError("knee classification expects two adjacent transitions")
    q0, q1 = quality_gains
    s0, s1 = stability_gains
    if q1 > q0 and s1 > s0:
        return {"pattern": "B", "knee_candidate": 1000, "description": "second step dominates first"}
    if q0 > q1 and s0 > s1:
        return {"pattern": "A", "knee_candidate": 300, "description": "first step dominates or second step diminishes"}
    if abs(q0) < abs(q1) and abs(s0) < abs(s1):
        return {"pattern": "B", "knee_candidate": 1000, "description": "second step dominates first"}
    if abs(q1) < abs(q0) and abs(s1) < abs(s0):
        return {"pattern": "A", "knee_candidate": 300, "description": "first step dominates first"}
    return {"pattern": "C", "knee_candidate": 100, "description": "neither transition dominates consistently"}


def gate(native: Mapping[str, Any], reservoir: Mapping[str, Any]) -> dict[str, Any]:
    checks = {
        "quality_target_ordering": native["quality_ordering"] == reservoir["quality_ordering"],
        "adjacent_quality_gain_ordering": native["quality_gain_ordering"] == reservoir["quality_gain_ordering"],
        "normalized_range_trend": native["normalized_range_trend"] == reservoir["normalized_range_trend"],
        "cv_trend": native["cv_trend"] == reservoir["cv_trend"],
        "qualitative_knee_structure": native["knee"]["pattern"] == reservoir["knee"]["pattern"],
    }
    return {"target_selection_fidelity_qualified": all(checks.values()), "checks": checks, "failed_conditions": [name for name, passed in checks.items() if not passed]}


def per_query_diagnostics(native: list[Mapping[str, Any]], reservoir: list[Mapping[str, Any]]) -> dict[str, Any]:
    by_native = {str(item["query_id"]): float(item["q_error"]) for item in native}
    by_reservoir = {str(item["query_id"]): float(item["q_error"]) for item in reservoir}
    if set(by_native) != set(by_reservoir):
        raise ValueError("per-query populations differ")
    deltas = [abs(math.log(by_reservoir[key]) - math.log(by_native[key])) for key in sorted(by_native)]
    return {
        "log_base": "natural",
        "count": len(deltas),
        "spearman_qerror": spearman([by_native[key] for key in sorted(by_native)], [by_reservoir[key] for key in sorted(by_native)]),
        "median_absolute_log_qerror_difference": statistics.median(deltas),
        "p90_absolute_log_qerror_difference": sorted(deltas)[math.ceil(0.9 * len(deltas)) - 1],
        "fraction_reservoir_qerror_greater": sum(by_reservoir[key] > by_native[key] for key in by_native) / len(deltas),
        "fraction_reservoir_qerror_less": sum(by_reservoir[key] < by_native[key] for key in by_native) / len(deltas),
    }
