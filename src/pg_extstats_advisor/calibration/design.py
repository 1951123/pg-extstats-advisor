"""Deterministic arity-two candidate pools and calibration configurations."""

from __future__ import annotations

import hashlib
import itertools
import random
from dataclasses import dataclass


@dataclass(frozen=True, slots=True)
class CalibrationCandidate:
    candidate_id: str
    object_name: str
    mechanism: str
    columns: tuple[str, str]


@dataclass(frozen=True, slots=True)
class CalibrationConfiguration:
    configuration_id: str
    kind: str
    role: str
    mcv: tuple[CalibrationCandidate, ...]
    fd: tuple[CalibrationCandidate, ...]
    subset_id: str = "s0"

    @property
    def n_mcv(self) -> int:
        return len(self.mcv)

    @property
    def n_fd(self) -> int:
        return len(self.fd)


def candidate_pool(columns: tuple[str, ...]) -> tuple[CalibrationCandidate, ...]:
    if len(columns) < 3 or len(columns) != len(set(columns)):
        raise ValueError("calibration requires at least three unique columns")
    result = []
    for mechanism in ("mcv", "fd"):
        for pair in itertools.combinations(columns, 2):
            identity = f"{mechanism}|{pair[0]}|{pair[1]}"
            digest = hashlib.sha256(identity.encode()).hexdigest()[:16]
            result.append(
                CalibrationCandidate(
                    f"cal_{digest}", f"pgextadv_cal_{digest}", mechanism, pair
                )
            )
    return tuple(result)


def configuration_design(
    pool: tuple[CalibrationCandidate, ...],
    count_levels: tuple[int, ...] = (),
    subsets_per_count: int = 1,
    seed: int = 0,
) -> tuple[CalibrationConfiguration, ...]:
    mcv = tuple(item for item in pool if item.mechanism == "mcv")
    fd = tuple(item for item in pool if item.mechanism == "fd")
    available = min(len(mcv), len(fd))
    if available < 3:
        raise ValueError("calibration candidate pool is too small")
    if count_levels:
        if count_levels[-1] > available:
            raise ValueError("count level exceeds candidate pool")
        levels = count_levels
    else:
        proposed = (max(1, available // 4), max(2, available // 2), available)
        levels = tuple(sorted(set(proposed)))
    if len(levels) < 2:
        raise ValueError("calibration needs at least two distinct count levels")
    if subsets_per_count < 1:
        raise ValueError("subsets_per_count must be positive")
    configurations = [CalibrationConfiguration("empty", "empty", "fit", (), ())]
    for count in levels:
        for subset_index in range(subsets_per_count):
            subset_id = f"s{subset_index}"
            mcv_order = list(mcv)
            fd_order = list(fd)
            if subset_index:
                random.Random(f"{seed}|mcv|{count}|{subset_index}").shuffle(mcv_order)
                random.Random(f"{seed}|fd|{count}|{subset_index}").shuffle(fd_order)
            configurations.append(CalibrationConfiguration(
                f"mcv-{count}-{subset_id}", "mcv-only", "fit",
                tuple(mcv_order[:count]), (), subset_id
            ))
            configurations.append(CalibrationConfiguration(
                f"fd-{count}-{subset_id}", "fd-only", "fit",
                (), tuple(fd_order[:count]), subset_id
            ))
    small, large = levels[0], levels[-1]
    middle = levels[len(levels) // 2]
    mixed = ((small, small), (middle, large), (large, middle), (large, large))
    for n_mcv, n_fd in tuple(dict.fromkeys(mixed)):
        configurations.append(
            CalibrationConfiguration(
                f"mixed-{n_mcv}-{n_fd}",
                "mixed",
                "held-out",
                mcv[:n_mcv],
                fd[:n_fd],
                "heldout",
            )
        )
    return tuple(configurations)
