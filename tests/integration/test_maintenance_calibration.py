import json
import os
from pathlib import Path

import psycopg
import pytest

from pg_extstats_advisor.calibration import CalibrationConfig, run_calibration


@pytest.mark.skipif(
    not os.environ.get("PGEXT_CALIBRATION_TEST_DSN"),
    reason="requires disposable PostgreSQL calibration fixture",
)
def test_physical_calibration_pipeline_and_cleanup(tmp_path: Path) -> None:
    dsn = os.environ["PGEXT_CALIBRATION_TEST_DSN"]
    with psycopg.connect(dsn, autocommit=True) as connection:
        connection.execute("DROP SCHEMA IF EXISTS pgextadv_cal_test CASCADE")
        connection.execute("CREATE SCHEMA pgextadv_cal_test")
        connection.execute(
            "CREATE TABLE pgextadv_cal_test.t AS SELECT i, i%5 a, i%7 b, i%11 c, i%13 d "
            "FROM generate_series(1, 20000) i"
        )
        config_path = tmp_path / "config.json"
        config_path.write_text(json.dumps({
            "schema_version": 1,
            "database": {"calibration_dsn": dsn},
            "relation": "pgextadv_cal_test.t",
            "columns": ["a", "b", "c", "d"],
            "statistics_target": 100,
            "repetitions": 2,
            "seed": 19,
            "output_path": str(tmp_path / "artifacts"),
            "count_levels": [2, 6],
            "gates": {
                "max_cv": 10.0,
                "min_r_squared": 0.000001,
                "max_heldout_relative_error": 10.0,
            },
            "environment_description": "functional integration fixture, not empirical evidence",
        }))
        report = run_calibration(CalibrationConfig.load(config_path), connection)
        assert report["status"] in {"accepted", "rejected"}
        root = tmp_path / "artifacts"
        assert (root / "raw-timings.csv").exists()
        assert (root / "configuration-summary.csv").exists()
        assert (root / "fit.json").exists()
        assert connection.execute(
            "SELECT count(*) FROM pg_statistic_ext WHERE stxname LIKE 'pgextadv_cal_%'"
        ).fetchone()[0] == 0
        connection.execute("DROP SCHEMA pgextadv_cal_test CASCADE")
