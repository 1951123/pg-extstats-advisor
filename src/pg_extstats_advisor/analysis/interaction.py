"""Pure helpers for contextual singleton-interaction diagnostics."""

from __future__ import annotations

import math
from collections.abc import Iterable


def classify_contextual(value: float) -> str:
    if value > 0:
        return "positive"
    if value < 0:
        return "negative"
    return "zero"


def interaction_gain(contextual_improvement: float, singleton_improvement: float) -> float:
    return contextual_improvement - singleton_improvement


def percentile_bucket(rank: int, total: int) -> str:
    if total <= 0 or not 1 <= rank <= total:
        raise ValueError("rank must be within a non-empty population")
    cutoffs = (
        (0.01, "top_1pct"),
        (0.05, "1_5pct"),
        (0.10, "5_10pct"),
        (0.20, "10_20pct"),
        (0.40, "20_40pct"),
        (0.60, "40_60pct"),
        (0.80, "60_80pct"),
    )
    for fraction, label in cutoffs:
        if rank <= math.ceil(total * fraction):
            return label
    return "bottom_20pct"


def gain_recall(
    retained_ids: set[str], accepted: Iterable[tuple[str, float]]
) -> float:
    values = list(accepted)
    total = sum(value for _, value in values)
    retained = sum(value for candidate_id, value in values if candidate_id in retained_ids)
    return retained / total if total else 0.0
