"""Small, explicit metrics for replicated target-grid observations."""

from __future__ import annotations

import statistics
from typing import Any


def _ratio(numerator: float, denominator: float) -> float | None:
    return None if denominator == 0 else numerator / denominator


def summarize_objectives(values: list[float]) -> dict[str, Any]:
    if not values:
        raise ValueError("at least one objective is required")
    mean = statistics.fmean(values)
    stdev = statistics.stdev(values) if len(values) > 1 else 0.0
    minimum, maximum = min(values), max(values)
    return {
        "values": list(values),
        "mean": mean,
        "stddev": stdev,
        "stddev_definition": "sample",
        "min": minimum,
        "max": maximum,
        "normalized_range": _ratio(maximum - minimum, mean),
        "coefficient_of_variation": _ratio(stdev, mean),
    }

def marginal_metrics(previous: dict[str, Any], current: dict[str, Any], capacity_multiplier: float) -> dict[str, Any]:
    old_mean = float(previous["mean"])
    old_range = previous.get("normalized_range")
    new_range = current.get("normalized_range")
    return {
        "capacity_multiplier": capacity_multiplier,
        "quality_gain": None if old_mean == 0 else (old_mean - float(current["mean"])) / old_mean,
        "stability_gain": None if not old_range else (old_range - new_range) / old_range,
        "denominator_policy": "null when denominator is zero",
    }
