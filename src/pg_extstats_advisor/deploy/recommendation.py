"""Deployment recommendation identity for the global-target MVP scope."""

from __future__ import annotations

from dataclasses import dataclass
from typing import Any

from pg_extstats_advisor.capture.manifest import canonical_digest


@dataclass(frozen=True, slots=True)
class StatisticsConfiguration:
    """A global target plus the selected extstats design.

    The target is intentionally outside per-candidate design selection.  A
    future deployer may render it as an ALTER DATABASE/role setting, but this
    model does not perform deployment.
    """

    global_statistics_target: int
    selected_candidate_ids: tuple[str, ...] = ()
    scope: str = "database/advisor_run"

    def __post_init__(self) -> None:
        if not 1 <= int(self.global_statistics_target) <= 10000:
            raise ValueError("global_statistics_target must be between 1 and 10000")
        if self.scope != "database/advisor_run":
            raise ValueError("MVP target scope must be database/advisor_run")

    def as_dict(self) -> dict[str, Any]:
        return {
            "global_statistics_target": int(self.global_statistics_target),
            "selected_candidate_ids": list(self.selected_candidate_ids),
            "scope": self.scope,
            "target_optimization": "outside_current_scope",
        }

    @property
    def digest(self) -> str:
        return canonical_digest(self.as_dict())
