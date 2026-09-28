"""Validate component boundary."""

"""Fresh physical validation and realization-drift reporting."""

from pg_extstats_advisor.validate.deployment import build_validation_result, evaluate_physical
from pg_extstats_advisor.validate.model import ValidationResult
from pg_extstats_advisor.validate.report import render_validation_report

__all__ = [
    "ValidationResult",
    "build_validation_result",
    "evaluate_physical",
    "render_validation_report",
]
