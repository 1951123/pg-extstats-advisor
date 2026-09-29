from __future__ import annotations

from dataclasses import FrozenInstanceError
from pathlib import Path

import pytest

from pg_extstats_advisor.capture.bundle import BundleCompatibilityError, check_advisor_compatibility
from pg_extstats_advisor.deploy.recommendation import StatisticsConfiguration
from pg_extstats_advisor.models import AdvisorProblem
from pg_extstats_advisor.payloads.cache import bind_statistics_target
from pg_extstats_advisor.prepare.config import PreparationConfig, StatisticsTargetConfig
from pg_extstats_advisor.search.model import SearchConfig


def _config(target: int = 100) -> PreparationConfig:
    return PreparationConfig(
        1,
        "source",
        "acquisition",
        Path("workload.json"),
        Path("run"),
        ("mcv", "fd"),
        2,
        None,
        (),
        target,
        (),
    )


def test_default_and_explicit_target_are_fixed_external_configuration() -> None:
    assert StatisticsTargetConfig().global_statistics_target == 100
    assert _config().global_statistics_target == 100
    assert _config(300).global_statistics_target == 300
    assert _config(300).evaluated_statistics_target == 300
    assert SearchConfig().global_statistics_target == 100
    assert SearchConfig(global_statistics_target=300).global_statistics_target == 300
    recommendation = StatisticsConfiguration(300, ("candidate",))
    assert recommendation.evaluated_statistics_target == 300
    assert recommendation.as_dict()["target_optimization"] == "outside_current_scope"
    with pytest.raises(ValueError, match="positive"):
        StatisticsTargetConfig(0)
    with pytest.raises(ValueError, match="positive"):
        _config(0)


def test_problem_identity_and_search_config_are_immutable_and_target_bound() -> None:
    problem = AdvisorProblem(
        global_statistics_target=100,
        workload_digest="w",
        candidate_catalog_digest="c",
        realization_digest="r",
        acquisition_identity="a",
    )
    assert problem.identity["global_statistics_target"] == 100
    assert problem.digest != AdvisorProblem(global_statistics_target=300).digest
    with pytest.raises(FrozenInstanceError):
        problem.global_statistics_target = 300  # type: ignore[misc]
    with pytest.raises(FrozenInstanceError):
        SearchConfig().global_statistics_target = 300  # type: ignore[misc]


def test_cache_identity_distinguishes_target_and_rejects_conflicting_alias() -> None:
    assert bind_statistics_target({"candidate": "c"}, 100) != bind_statistics_target(
        {"candidate": "c"}, 300
    )
    assert bind_statistics_target({"statistics_target": 100}, 100)["global_statistics_target"] == 100
    with pytest.raises(ValueError, match="mismatch"):
        bind_statistics_target({"global_statistics_target": 100}, 300)


def test_bundle_target_mismatch_fails_closed(tmp_path: Path) -> None:
    # Build the smallest valid v1 fixture through the existing contract helper.
    from tests.unit.test_production_capture_bundle import _make_bundle

    _make_bundle(tmp_path)
    with pytest.raises(BundleCompatibilityError, match="target mismatch"):
        check_advisor_compatibility(
            tmp_path, {"postgres_version": "16.14", "global_statistics_target": 300}
        )
