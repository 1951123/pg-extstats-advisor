from __future__ import annotations

import json
import os
from pathlib import Path

import psycopg
import pytest

from pg_extstats_advisor.evaluator.native import NativeEvaluator
from pg_extstats_advisor.models import Design
from pg_extstats_advisor.postgres.adapter import PostgresAdapter
from pg_extstats_advisor.prepare.acquisition import cleanup_acquisition
from pg_extstats_advisor.prepare.artifacts import prepare_mvp
from pg_extstats_advisor.prepare.config import PreparationConfig

DSN = os.environ.get("PG_EXTSTATS_TEST_DSN")
pytestmark = pytest.mark.skipif(not DSN, reason="requires patched PostgreSQL fixture")
UPSTREAM = "f6d077142737920858ce958ccdb75c6ee137a63b5b0853c70693d401ac7e3471"


def test_real_preparation_pipeline_feeds_native_evaluator(tmp_path: Path) -> None:
    assert DSN is not None
    with psycopg.connect(DSN) as source, psycopg.connect(DSN) as acquisition:
        source.execute("SET default_statistics_target=1000")
        source.execute("CREATE TABLE prep_t(a int,b int,c int)")
        source.execute(
            "INSERT INTO prep_t SELECT g%10,g%10,(g+1)%10 FROM generate_series(1,1000) g"
        )
        source.execute("ANALYZE prep_t")
        source.commit()
        workload_path = tmp_path / "workload-input.json"
        workload_path.write_text(
            json.dumps(
                {
                    "schema_version": 1,
                    "workload_id": "prep-integration-v1",
                    "queries": [
                        {
                            "query_id": "q_precise",
                            "sql": "SELECT * FROM public.prep_t WHERE a=1 AND b=1 AND c=2",
                            "truth": 100,
                            "target_relation": "public.prep_t",
                        },
                        {
                            "query_id": "q_fallback",
                            "sql": "SELECT * FROM public.prep_t WHERE a=1 AND (b + c)=3",
                            "truth": 100,
                            "target_relation": "public.prep_t",
                        },
                    ],
                }
            )
        )
        config = PreparationConfig(
            1,
            DSN,
            DSN,
            workload_path,
            tmp_path / "run",
            ("mcv", "fd"),
            2,
            None,
            (),
            1000,
            (("type", "preset-development"),),
        )
        prepared = prepare_mvp(
            config,
            source,
            acquisition,
            upstream_sha256=UPSTREAM,
            patch_commit="c052faa80abc9e3db5a9b6c035ef4d195f07436c",
        )
        assert len(prepared.workload.queries) == 2
        assert len(prepared.catalog.candidates) == 6
        assert prepared.acquisition.analyze_count == 1
        assert len(prepared.incidence.by_candidate) == 6
        summary = json.loads((prepared.artifact_root / "prepare-summary.json").read_text())
        assert summary["incidence_edge_count"] == 12
        assert summary["fallback_query_count"] == 1
        assert all(item.payload for item in prepared.repository.payloads)
        evaluator = NativeEvaluator(
            prepared.workload,
            prepared.repository,
            prepared.incidence,
            PostgresAdapter(acquisition, prepared.repository),
        )
        empty = evaluator.evaluate_design(Design(()))
        one = evaluator.evaluate_design(Design((prepared.catalog.candidates[0].candidate_id,)))
        assert len(empty.query_evaluations) == len(one.query_evaluations) == 2
        cleanup_acquisition(acquisition, prepared.acquisition)
        source.execute("DROP TABLE prep_t")
        source.commit()
        assert (
            acquisition.execute(
                "SELECT count(*) FROM pg_statistic_ext WHERE stxname LIKE 'pgextadv_acq_%'"
            ).fetchone()[0]
            == 0
        )
