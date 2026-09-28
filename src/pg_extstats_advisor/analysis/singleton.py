"""Deterministic, side-effect-free singleton profiling helpers."""

from __future__ import annotations

import math
from collections.abc import Iterable, Mapping, Sequence
from typing import Any


def classify_improvement(baseline: float, singleton: float) -> str:
    """Classify ``baseline - singleton`` with exact floating comparison."""

    improvement = baseline - singleton
    if improvement > 0:
        return "positive"
    if improvement < 0:
        return "negative"
    return "zero"


def linear_quantile(values: Iterable[float], probability: float) -> float | None:
    """Return the linearly interpolated quantile used by the M2.9 report."""

    ordered = sorted(float(value) for value in values)
    if not ordered:
        return None
    if not 0 <= probability <= 1:
        raise ValueError("probability must be between zero and one")
    if len(ordered) == 1:
        return ordered[0]
    position = probability * (len(ordered) - 1)
    lower = math.floor(position)
    upper = math.ceil(position)
    if lower == upper:
        return ordered[lower]
    fraction = position - lower
    return ordered[lower] + fraction * (ordered[upper] - ordered[lower])


def deterministic_order(
    rows: Sequence[Mapping[str, Any]],
    value_key: str,
    cost_key: str = "maintenance_cost_numeric",
) -> list[Mapping[str, Any]]:
    """Rank rows by value, cost, precedence, then candidate identity."""

    return sorted(
        rows,
        key=lambda row: (
            -float(row[value_key]),
            float(row[cost_key]),
            int(row["precedence_rank"]),
            str(row["candidate_id"]),
        ),
    )


def percentile_from_rank(rank: int, total: int) -> float:
    """Return a top-oriented percentile: rank one is 100, last is near zero."""

    if total <= 0 or not 1 <= rank <= total:
        raise ValueError("rank must be within a non-empty population")
    return 100.0 * (total - rank + 1) / total


def recall_at(ranks: Iterable[int], cutoff: int) -> int:
    """Count ranked items whose one-based rank is at most ``cutoff``."""

    if cutoff < 0:
        raise ValueError("cutoff must be non-negative")
    return sum(1 for rank in ranks if rank <= cutoff)


def top_fraction_count(total: int, fraction: float) -> int:
    """Return the deterministic ceiling count for a top-fraction screen."""

    if total < 0 or not math.isfinite(fraction) or not 0 < fraction <= 1:
        raise ValueError("total must be non-negative and fraction must be in (0, 1]")
    return math.ceil(total * fraction)


def screen_singleton_rows(
    rows: Sequence[Mapping[str, Any]], fraction: float
) -> list[dict[str, Any]]:
    """Retain a deterministic top singleton-utility fraction.

    Ranking is exactly the M2.9 raw ordering: descending singleton improvement,
    ascending maintenance cost, precedence rank, then candidate identity.
    """

    ordered = deterministic_order(rows, "singleton_improvement")
    count = top_fraction_count(len(ordered), fraction)
    return [
        {
            **dict(row),
            "screening_rank": rank,
            "singleton_percentile": percentile_from_rank(rank, len(ordered)),
        }
        for rank, row in enumerate(ordered[:count], start=1)
    ]
