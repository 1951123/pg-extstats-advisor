"""Strict extraction of one target base-relation estimate."""

from typing import Any


def extract_target_estimate(plan_json: Any, target_relation: str) -> float:
    if not isinstance(plan_json, list) or len(plan_json) != 1 or "Plan" not in plan_json[0]:
        raise ValueError("unexpected EXPLAIN JSON shape")
    matches: list[dict[str, Any]] = []

    def visit(node: dict[str, Any]) -> None:
        if node.get("Relation Name") == target_relation:
            matches.append(node)
        for child in node.get("Plans", []):
            visit(child)

    visit(plan_json[0]["Plan"])
    if len(matches) != 1:
        raise ValueError(f"target relation {target_relation!r} matched {len(matches)} plan nodes")
    estimate = matches[0].get("Plan Rows")
    if not isinstance(estimate, (int, float)):
        raise TypeError("target plan node has no numeric Plan Rows")
    return float(estimate)
