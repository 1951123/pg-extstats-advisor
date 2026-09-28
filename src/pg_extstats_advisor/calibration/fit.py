"""Transparent three-parameter OLS and calibration gates."""

from __future__ import annotations

import math
import statistics
from dataclasses import dataclass


@dataclass(frozen=True, slots=True)
class TimingRow:
    configuration_id: str
    role: str
    n_mcv: int
    n_fd: int
    elapsed_seconds: float


@dataclass(frozen=True, slots=True)
class FitResult:
    intercept_seconds: float
    mcv_seconds_per_object: float
    fd_seconds_per_object: float
    r_squared: float
    rmse_seconds: float
    median_relative_error: float
    max_relative_error: float
    coefficient_standard_errors: tuple[float, float, float] | None


def assess_gates(
    fit: FitResult,
    *,
    max_cv: float,
    cv_threshold: float,
    max_heldout_relative_error: float,
    heldout_threshold: float,
    r_squared_threshold: float,
) -> dict[str, dict[str, object]]:
    """Apply predeclared acceptance thresholds without modifying observations."""
    return {
        "within_configuration_cv": {
            "passed": max_cv <= cv_threshold,
            "observed": max_cv,
            "threshold": cv_threshold,
            "operator": "<=",
        },
        "fit_r_squared": {
            "passed": fit.r_squared >= r_squared_threshold,
            "observed": fit.r_squared,
            "threshold": r_squared_threshold,
            "operator": ">=",
        },
        "heldout_max_relative_error": {
            "passed": max_heldout_relative_error <= heldout_threshold,
            "observed": max_heldout_relative_error,
            "threshold": heldout_threshold,
            "operator": "<=",
        },
        "nonnegative_slopes": {
            "passed": fit.mcv_seconds_per_object >= 0 and fit.fd_seconds_per_object >= 0,
            "observed": {
                "mcv": fit.mcv_seconds_per_object,
                "fd": fit.fd_seconds_per_object,
            },
            "threshold": 0,
            "operator": ">=",
        },
    }


def _inverse3(matrix: list[list[float]]) -> list[list[float]]:
    augmented = [row[:] + [float(i == j) for j in range(3)] for i, row in enumerate(matrix)]
    for column in range(3):
        pivot = max(range(column, 3), key=lambda row: abs(augmented[row][column]))
        if abs(augmented[pivot][column]) < 1e-15:
            raise ValueError("calibration design matrix is singular")
        augmented[column], augmented[pivot] = augmented[pivot], augmented[column]
        scale = augmented[column][column]
        augmented[column] = [value / scale for value in augmented[column]]
        for row in range(3):
            if row == column:
                continue
            factor = augmented[row][column]
            augmented[row] = [
                value - factor * pivot_value
                for value, pivot_value in zip(augmented[row], augmented[column], strict=True)
            ]
    return [row[3:] for row in augmented]


def fit_aggregate(rows: tuple[TimingRow, ...]) -> FitResult:
    training = tuple(row for row in rows if row.role == "fit")
    if len(training) <= 3:
        raise ValueError("OLS requires more than three fitting observations")
    x = [(1.0, float(row.n_mcv), float(row.n_fd)) for row in training]
    y = [row.elapsed_seconds for row in training]
    xtx = [[sum(row[i] * row[j] for row in x) for j in range(3)] for i in range(3)]
    inverse = _inverse3(xtx)
    xty = [sum(row[i] * value for row, value in zip(x, y, strict=True)) for i in range(3)]
    beta = [sum(inverse[i][j] * xty[j] for j in range(3)) for i in range(3)]
    predicted = [sum(a * b for a, b in zip(row, beta, strict=True)) for row in x]
    residuals = [actual - estimate for actual, estimate in zip(y, predicted, strict=True)]
    sse = sum(value * value for value in residuals)
    mean = statistics.fmean(y)
    total = sum((value - mean) ** 2 for value in y)
    relative = [abs(error) / actual for error, actual in zip(residuals, y, strict=True) if actual]
    variance = sse / (len(y) - 3)
    standard_errors = tuple(math.sqrt(max(0.0, variance * inverse[i][i])) for i in range(3))
    return FitResult(
        beta[0],
        beta[1],
        beta[2],
        1.0 - sse / total if total else 1.0,
        math.sqrt(sse / len(y)),
        statistics.median(relative) if relative else 0.0,
        max(relative, default=0.0),
        standard_errors,
    )


def coefficient_intervals(fit: FitResult) -> tuple[tuple[float, float], ...] | None:
    if fit.coefficient_standard_errors is None:
        return None
    coefficients = (
        fit.intercept_seconds,
        fit.mcv_seconds_per_object,
        fit.fd_seconds_per_object,
    )
    return tuple(
        (coefficient - 1.96 * error, coefficient + 1.96 * error)
        for coefficient, error in zip(coefficients, fit.coefficient_standard_errors, strict=True)
    )
