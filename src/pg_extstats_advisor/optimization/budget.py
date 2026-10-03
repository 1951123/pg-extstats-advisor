"""Monotonic-clock wall-clock budget for advisor optimization work."""

from __future__ import annotations

import math
import time
from dataclasses import dataclass, field
from enum import StrEnum
from typing import Callable


class OptimizationStatus(StrEnum):
    """Structured outcomes for the bounded advisor optimization path."""

    LOCAL_OPTIMUM = "LOCAL_OPTIMUM"
    BUDGET_EXHAUSTED_DURING_BASELINE = "BUDGET_EXHAUSTED_DURING_BASELINE"
    BUDGET_EXHAUSTED_DURING_SINGLETON = "BUDGET_EXHAUSTED_DURING_SINGLETON"
    BUDGET_EXHAUSTED_DURING_GREEDY = "BUDGET_EXHAUSTED_DURING_GREEDY"
    BUDGET_EXHAUSTED_DURING_LOCAL = "BUDGET_EXHAUSTED_DURING_LOCAL"
    COMPLETED = "COMPLETED"
    ERROR = "ERROR"


class OptimizationBudgetExhausted(RuntimeError):
    """Internal control signal for a deadline reached during optimization."""

    def __init__(self, phase: str, elapsed_seconds: float) -> None:
        self.phase = phase
        self.elapsed_seconds = elapsed_seconds
        super().__init__(f"optimization budget exhausted during {phase}")


@dataclass(slots=True)
class OptimizationBudget:
    """A non-negative deadline backed by an injectable monotonic clock.

    Construction does not start the timer.  Callers start it immediately before
    the first workload-dependent optimization operation, which makes setup and
    artifact preparation explicitly outside ``B_opt``.
    """

    limit_seconds: float
    clock: Callable[[], float] = time.monotonic
    start_monotonic: float | None = field(default=None, init=False)

    def __post_init__(self) -> None:
        if not math.isfinite(self.limit_seconds) or self.limit_seconds < 0:
            raise ValueError("optimization budget must be finite and non-negative")

    def start(self) -> None:
        """Start the timer once; repeated calls preserve the original boundary."""

        if self.start_monotonic is None:
            self.start_monotonic = float(self.clock())

    @property
    def elapsed_seconds(self) -> float:
        if self.start_monotonic is None:
            return 0.0
        return max(0.0, float(self.clock()) - self.start_monotonic)

    @property
    def remaining_seconds(self) -> float:
        return max(0.0, self.limit_seconds - self.elapsed_seconds)

    @property
    def exhausted(self) -> bool:
        return self.start_monotonic is not None and self.elapsed_seconds >= self.limit_seconds

    def check(self, phase: str) -> None:
        """Raise the internal stop signal if the deadline has been reached."""

        if not phase:
            raise ValueError("optimization phase is required")
        if self.start_monotonic is None:
            self.start()
        if self.exhausted:
            raise OptimizationBudgetExhausted(phase, self.elapsed_seconds)
