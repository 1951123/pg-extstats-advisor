from __future__ import annotations

import pytest

from pg_extstats_advisor.capture.target_policy import (
    TargetGridPolicy,
    TargetPolicyError,
    inspect_override_rows,
    validate_override_rows,
)


def test_frozen_grid_and_target_aware_seeds_are_distinct() -> None:
    policy = TargetGridPolicy()
    assert policy.expected_cells() == ((100, "A"), (100, "B"), (100, "C"), (300, "A"), (300, "B"), (300, "C"), (1000, "A"), (1000, "B"), (1000, "C"))
    assert len({policy.seed(*cell) for cell in policy.expected_cells()}) == 9
    assert policy.native_analyze_equivalent is False


@pytest.mark.parametrize("targets", [(300, 100), (100, 100), (0, 100)])
def test_target_grid_rejects_invalid_order_or_values(targets: tuple[int, ...]) -> None:
    with pytest.raises(TargetPolicyError):
        TargetGridPolicy(allowed_targets=targets)


def test_override_scope_is_fail_closed_but_unrelated_relation_is_ignored() -> None:
    columns = [
        ("public", "other", "x", 500),
        ("public", "dmv", "state", -1),
        ("public", "dmv", "county", 300),
    ]
    extstats = [("public", "dmv", "dmv_state_fd", 100)]
    relevant = {"public.dmv"}
    assert [item.as_dict() for item in inspect_override_rows(columns, extstats, relevant, {"public.dmv": {"state", "county"}})] == [
        {"kind": "column", "relation": "public.dmv", "name": "county", "value": 300},
        {"kind": "extended_statistics", "relation": "public.dmv", "name": "dmv_state_fd", "value": 100},
    ]
    with pytest.raises(TargetPolicyError, match="dmv.county=300"):
        validate_override_rows(columns, extstats, relevant, {"public.dmv": {"state", "county"}})


def test_default_and_unrelated_overrides_pass() -> None:
    validate_override_rows(
        [("public", "dmv", "state", -1), ("public", "other", "x", 500)],
        [("public", "other", "other_fd", 100)],
        {"public.dmv"},
        {"public.dmv": {"state"}},
    )
