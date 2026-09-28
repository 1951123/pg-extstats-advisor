import json
from decimal import Decimal
from pathlib import Path

import pytest

from pg_extstats_advisor.calibration.config import CalibrationConfig
from pg_extstats_advisor.calibration.design import candidate_pool, configuration_design
from pg_extstats_advisor.calibration.fit import TimingRow, assess_gates, fit_aggregate
from pg_extstats_advisor.cost.empirical import EmpiricalMechanismCountCostModel, artifact_digest
from pg_extstats_advisor.models import Candidate, CandidateId, MechanismKind


def test_configuration_generation_is_deterministic_and_counted() -> None:
    first = candidate_pool(("a", "b", "c", "d", "e"))
    second = candidate_pool(("a", "b", "c", "d", "e"))
    assert first == second
    assert len(first) == 20
    configurations = configuration_design(first, (2, 5, 10))
    assert configurations == configuration_design(second, (2, 5, 10))
    assert [(item.n_mcv, item.n_fd) for item in configurations if item.role == "fit"] == [
        (0, 0), (2, 0), (0, 2), (5, 0), (0, 5), (10, 0), (0, 10)
    ]
    assert all(item.n_mcv and item.n_fd for item in configurations if item.role == "held-out")


def test_exact_and_noisy_aggregate_ols() -> None:
    exact = tuple(
        TimingRow(f"c-{m}-{f}", "fit", m, f, 0.2 + 0.003 * m + 0.005 * f)
        for m, f in ((0, 0), (0, 0), (2, 0), (2, 0), (5, 0), (5, 0),
                     (0, 2), (0, 2), (0, 5), (0, 5))
    )
    fit = fit_aggregate(exact)
    assert fit.intercept_seconds == pytest.approx(0.2)
    assert fit.mcv_seconds_per_object == pytest.approx(0.003)
    assert fit.fd_seconds_per_object == pytest.approx(0.005)
    assert fit.r_squared == pytest.approx(1.0)
    noisy = tuple(
        TimingRow(row.configuration_id, row.role, row.n_mcv, row.n_fd,
                  row.elapsed_seconds + (-0.0001 if index % 2 else 0.0001))
        for index, row in enumerate(exact)
    )
    assert fit_aggregate(noisy).r_squared > 0.99


def test_calibration_config_and_empirical_decimal_round_trip(tmp_path: Path) -> None:
    path = tmp_path / "config.json"
    path.write_text(json.dumps({
        "schema_version": 1,
        "database": {"calibration_dsn": "dbname=test"},
        "relation": "public.t",
        "columns": ["a", "b", "c"],
        "statistics_target": 100,
        "repetitions": 2,
        "seed": 7,
        "output_path": str(tmp_path / "out"),
        "count_levels": [1, 3],
        "gates": {"max_cv": 0.1, "min_r_squared": 0.95,
                  "max_heldout_relative_error": 0.15},
    }))
    config = CalibrationConfig.load(path)
    assert config.statistics_target == 100
    raw = {
        "format_version": 1,
        "model_type": "empirical-mechanism-count-v1",
        "model_version": "v",
        "unit": "milliseconds-per-analyze",
        "statistics_target": 100,
        "candidate_arity": 2,
        "parameters": {"mcv_ms_per_object": "1.234567890123",
                       "fd_ms_per_object": "2.345678901234"},
        "fit": {}, "stability": {}, "calibration_provenance": {},
    }
    raw["digest"] = artifact_digest(raw)
    model = EmpiricalMechanismCountCostModel.from_artifact(raw)
    candidate = Candidate(CandidateId("m"), 1, "public.t", MechanismKind.MCV,
                          ("a", "b"), (), 0, 0)
    assert model.estimate_candidate(candidate) == Decimal("1.234567890123")


def test_negative_slope_is_visible_for_rejection() -> None:
    rows = tuple(
        TimingRow(str((m, f, repeat)), "fit", m, f, 1.0 - 0.01 * m + 0.02 * f)
        for m, f in ((0, 0), (2, 0), (4, 0), (0, 2), (0, 4))
        for repeat in range(2)
    )
    fit = fit_aggregate(rows)
    assert fit.mcv_seconds_per_object < 0
    gates = assess_gates(
        fit,
        max_cv=0.11,
        cv_threshold=0.10,
        max_heldout_relative_error=0.16,
        heldout_threshold=0.15,
        r_squared_threshold=0.95,
    )
    assert not gates["within_configuration_cv"]["passed"]
    assert not gates["heldout_max_relative_error"]["passed"]
    assert not gates["nonnegative_slopes"]["passed"]
