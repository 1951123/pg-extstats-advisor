"""Frozen positive-truth q-error objective."""

import math
from collections.abc import Iterable

from pg_extstats_advisor.models import QueryEvaluation


def q_error(estimate: float, truth: float) -> float:
    if truth <= 0:
        raise ValueError("q-error requires positive truth")
    if not math.isfinite(estimate) or estimate < 0:
        raise ValueError("estimate must be finite and non-negative")
    floored_estimate = max(float(estimate), 1.0)
    return max(floored_estimate / float(truth), float(truth) / floored_estimate)


def aggregate_objective(evaluations: Iterable[QueryEvaluation]) -> float:
    ordered = sorted(evaluations, key=lambda item: item.query_id)
    return math.fsum(item.contribution for item in ordered)
