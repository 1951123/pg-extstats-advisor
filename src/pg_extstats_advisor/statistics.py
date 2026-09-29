"""Shared semantics for the advisor-wide PostgreSQL statistics target.

The target is an external database/DBA configuration.  It is deliberately
not part of the extended-statistics design search space.
"""

from __future__ import annotations

DEFAULT_GLOBAL_STATISTICS_TARGET = 100
MAX_GLOBAL_STATISTICS_TARGET = 10000


def validate_global_statistics_target(value: int, *, field: str = "global_statistics_target") -> int:
    """Return a canonical positive target or fail closed.

    ``bool`` is rejected explicitly even though it is an ``int`` subclass in
    Python; accepting ``True`` as target one would make configuration identity
    surprisingly depend on JSON/Python coercion.
    """

    if isinstance(value, bool) or not isinstance(value, int):
        raise ValueError(f"{field} must be a positive integer")  # noqa: TRY004
    if not 1 <= value <= MAX_GLOBAL_STATISTICS_TARGET:
        raise ValueError(
            f"{field} must be a positive integer between 1 and {MAX_GLOBAL_STATISTICS_TARGET}"
        )
    return value
