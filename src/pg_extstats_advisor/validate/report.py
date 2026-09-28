"""Canonical JSON rendering for M2 validation results."""

from __future__ import annotations

import json
from dataclasses import asdict
from decimal import Decimal
from pathlib import Path
from typing import Any

from pg_extstats_advisor.validate.model import ValidationResult


def _jsonable(value: Any) -> Any:
    if isinstance(value, Decimal):
        return str(value)
    if isinstance(value, dict):
        return {str(key): _jsonable(item) for key, item in value.items()}
    if isinstance(value, (list, tuple)):
        return [_jsonable(item) for item in value]
    if hasattr(value, "value"):
        return _jsonable(value.value)
    return value


def validation_report_dict(result: ValidationResult) -> dict[str, Any]:
    return _jsonable(asdict(result))


def render_validation_report(result: ValidationResult) -> str:
    return json.dumps(validation_report_dict(result), sort_keys=True, indent=2) + "\n"


def write_validation_report(result: ValidationResult, path: Path) -> None:
    path.write_text(render_validation_report(result), encoding="utf-8")
