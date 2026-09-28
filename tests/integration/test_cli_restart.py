from __future__ import annotations

import json
import os
import shutil
import subprocess
import sys
from pathlib import Path

import psycopg
import pytest

from pg_extstats_advisor.orchestration import execute_search_stage, load_search_result

DSN = os.environ.get("PG_EXTSTATS_TEST_DSN")
pytestmark = pytest.mark.skipif(not DSN, reason="requires patched PostgreSQL fixture")


def invoke(*arguments: str, env: dict[str, str]) -> subprocess.CompletedProcess[str]:
    return subprocess.run(
        [sys.executable, "-m", "pg_extstats_advisor.cli", *arguments],
        text=True,
        capture_output=True,
        env=env,
        check=False,
    )


def tamper_copy(source: Path, target: Path, relative: str, field: str) -> Path:
    shutil.copytree(source, target)
    path = target / relative
    value = json.loads(path.read_text())
    value[field] = "tampered"
    path.write_text(json.dumps(value))
    return target


def test_cli_stages_restart_and_cleanup(tmp_path: Path) -> None:
    assert DSN is not None
    with psycopg.connect(DSN) as connection:
        connection.execute("CREATE TABLE cli_t(a int,b int)")
        connection.execute("INSERT INTO cli_t SELECT g%10,g%10 FROM generate_series(1,1000) g")
        connection.execute("ANALYZE cli_t")
        connection.commit()
    workload = tmp_path / "workload.json"
    workload.write_text(
        json.dumps(
            {
                "schema_version": 1,
                "workload_id": "cli-v1",
                "queries": [
                    {
                        "query_id": "q",
                        "sql": "SELECT * FROM public.cli_t WHERE a=1 AND b=1",
                        "truth": 100,
                        "target_relation": "public.cli_t",
                    }
                ],
            }
        )
    )
    run_dir = tmp_path / "run"
    config = tmp_path / "config.json"
    config.write_text(
        json.dumps(
            {
                "schema_version": 1,
                "database": {
                    "source_dsn_env": "PGEXT_SOURCE_DSN",
                    "acquisition_dsn_env": "PGEXT_ACQUISITION_DSN",
                },
                "workload": {"path": str(workload)},
                "candidates": {
                    "mechanisms": ["mcv", "fd"],
                    "max_candidate_arity": 2,
                    "max_candidates_per_relation": None,
                    "explicit": [],
                },
                "acquisition": {"statistics_target": 1000, "output_path": str(run_dir)},
                "maintenance": {
                    "type": "preset-development",
                    "base_mcv": "0",
                    "per_column_mcv": "1",
                    "base_fd": "1",
                    "per_column_fd": "1",
                },
            }
        )
    )
    secret_dsn = DSN + " password=secret"
    env = dict(os.environ, PGEXT_SOURCE_DSN=secret_dsn, PGEXT_ACQUISITION_DSN=secret_dsn)
    prepared = invoke("prepare", str(config), env=env)
    assert prepared.returncode == 0, prepared.stderr
    searched = invoke("search", str(run_dir), "--budget", "2", env=env)
    assert searched.returncode == 0, searched.stderr
    cli_result = load_search_result(run_dir)
    recommended = invoke("recommend", str(run_dir), env=env)
    assert recommended.returncode == 0, recommended.stderr
    assert (run_dir / "recommendation" / "deployment.sql").exists()
    with psycopg.connect(DSN) as connection:
        direct = execute_search_stage(run_dir, connection, "2")
    assert direct == cli_result
    for index, (relative, field, command) in enumerate(
        (
            ("workload.json", "digest", "search"),
            ("candidates.json", "digest", "search"),
            ("incidence.json", "digest", "search"),
            ("repository/manifest.json", "repository_id", "search"),
            ("maintenance-model.json", "digest", "search"),
            ("search/result.json", "repository_digest", "recommend"),
        )
    ):
        damaged = tamper_copy(run_dir, tmp_path / f"tampered-{index}", relative, field)
        arguments = (
            (command, str(damaged), "--budget", "2")
            if command == "search"
            else (command, str(damaged))
        )
        assert invoke(*arguments, env=env).returncode != 0
    cleaned = invoke("cleanup-acquisition", str(run_dir), env=env)
    assert cleaned.returncode == 0, cleaned.stderr
    assert invoke("recommend", str(run_dir), env=env).returncode == 0
    failed_search = invoke("search", str(run_dir), "--budget", "2", env=env)
    assert failed_search.returncode != 0
    assert "missing catalog definition" in failed_search.stderr
    artifacts = "\n".join(path.read_text(errors="ignore") for path in run_dir.rglob("*.json"))
    assert "password=secret" not in artifacts
    with psycopg.connect(DSN) as connection:
        connection.execute("DROP TABLE cli_t")
        connection.commit()
