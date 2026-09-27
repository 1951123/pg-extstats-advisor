from __future__ import annotations

import hashlib
import json
import os
from dataclasses import replace
from pathlib import Path

import psycopg
import pytest

from pg_extstats_advisor.evaluator.native import NativeEvaluator
from pg_extstats_advisor.incidence.index import IncidenceIndex
from pg_extstats_advisor.models import CandidateId, Design, Move, QueryId, WorkloadQuery
from pg_extstats_advisor.payloads.repository import PayloadRepository
from pg_extstats_advisor.postgres.adapter import PostgresAdapter
from pg_extstats_advisor.workload.model import Workload

DSN = os.environ.get("PG_EXTSTATS_TEST_DSN")
pytestmark = pytest.mark.skipif(not DSN, reason="requires patched PostgreSQL fixture")

UPSTREAM_SHA256 = "f6d077142737920858ce958ccdb75c6ee137a63b5b0853c70693d401ac7e3471"


def plan_rows(connection: psycopg.Connection, sql: str, relation: str) -> float:
    plan = connection.execute(f"EXPLAIN (FORMAT JSON) {sql}").fetchone()[0]
    nodes = []

    def visit(node: dict) -> None:
        if node.get("Relation Name") == relation:
            nodes.append(node)
        for child in node.get("Plans", []):
            visit(child)

    visit(plan[0]["Plan"])
    assert len(nodes) == 1
    return float(nodes[0]["Plan Rows"])


def setup_fixture(connection: psycopg.Connection) -> dict[str, float]:
    connection.execute("SET default_statistics_target=10000")
    connection.execute("CREATE TABLE m0_single(a int,b int)")
    connection.execute("INSERT INTO m0_single SELECT g%10,g%10 FROM generate_series(1,10000) g")
    connection.execute("CREATE TABLE m0_overlap(a int,b int,c int)")
    connection.execute(
        "INSERT INTO m0_overlap SELECT g%10,g%10,"
        "CASE WHEN g%10=1 THEN CASE WHEN g%5<4 THEN 1 ELSE 2 END "
        "WHEN g%8=0 THEN 1 ELSE (g+3)%10 END FROM generate_series(1,10000) g"
    )
    connection.execute("CREATE TABLE m0_fd(a int,b int)")
    connection.execute("INSERT INTO m0_fd SELECT g%100,g%100 FROM generate_series(1,10000) g")
    connection.execute("CREATE TABLE m0_mixed(a int,b int,c int)")
    connection.execute("INSERT INTO m0_mixed SELECT g%20,g%20,g%20 FROM generate_series(1,10000) g")
    connection.execute("CREATE TABLE m0_unaffected(a int,b int)")
    connection.execute("INSERT INTO m0_unaffected SELECT g%10,g%10 FROM generate_series(1,10000) g")
    for relation in ("m0_single", "m0_overlap", "m0_fd", "m0_mixed", "m0_unaffected"):
        connection.execute(f"ANALYZE {relation}")
    physical = {
        "empty_single": plan_rows(connection, "SELECT * FROM m0_single WHERE a=1 AND b=1", "m0_single")
    }
    connection.execute("CREATE STATISTICS m0_single_ab (mcv) ON a,b FROM m0_single")
    connection.execute("CREATE STATISTICS m0_overlap_ab (mcv) ON a,b FROM m0_overlap")
    connection.execute("CREATE STATISTICS m0_overlap_bc (mcv) ON b,c FROM m0_overlap")
    connection.execute("CREATE STATISTICS m0_fd_ab (dependencies) ON a,b FROM m0_fd")
    connection.execute("CREATE STATISTICS m0_mixed_ab (mcv) ON a,b FROM m0_mixed")
    connection.execute("CREATE STATISTICS m0_mixed_bc (dependencies) ON b,c FROM m0_mixed")
    for relation in ("m0_single", "m0_overlap", "m0_fd", "m0_mixed"):
        connection.execute(f"ANALYZE {relation}")
    physical.update(
        single=plan_rows(connection, "SELECT * FROM m0_single WHERE a=1 AND b=1", "m0_single"),
        overlap=plan_rows(
            connection, "SELECT * FROM m0_overlap WHERE a=1 AND b=1 AND c=1", "m0_overlap"
        ),
        fd=plan_rows(connection, "SELECT * FROM m0_fd WHERE a=1 AND b=1", "m0_fd"),
        mixed=plan_rows(
            connection, "SELECT * FROM m0_mixed WHERE a=1 AND b=1 AND c=1", "m0_mixed"
        ),
    )
    connection.commit()
    return physical


def build_repository(connection: psycopg.Connection, root: Path) -> PayloadRepository:
    specifications = [
        ("single_mcv", "m0_single_ab", "mcv", ["a", "b"]),
        ("overlap_ab", "m0_overlap_ab", "mcv", ["a", "b"]),
        ("overlap_bc", "m0_overlap_bc", "mcv", ["b", "c"]),
        ("fd_ab", "m0_fd_ab", "fd", ["a", "b"]),
        ("mixed_mcv", "m0_mixed_ab", "mcv", ["a", "b"]),
        ("mixed_fd", "m0_mixed_bc", "fd", ["b", "c"]),
    ]
    payload_dir = root / "payloads"
    payload_dir.mkdir(parents=True)
    records = []
    for rank, (candidate_id, name, mechanism, attributes) in enumerate(specifications):
        column = "stxdmcv" if mechanism == "mcv" else "stxddependencies"
        sender = "pg_mcv_list_send" if mechanism == "mcv" else "pg_dependencies_send"
        row = connection.execute(
            f"SELECT e.oid,e.stxrelid,e.stxkind,{sender}(d.{column}),e.stxrelid::regclass::text "
            "FROM pg_statistic_ext e JOIN pg_statistic_ext_data d ON d.stxoid=e.oid "
            "WHERE e.stxname=%s",
            (name,),
        ).fetchone()
        assert row is not None
        oid, relid, kinds, payload, relation_name = row
        required_kind = "m" if mechanism == "mcv" else "f"
        assert required_kind in kinds
        path = payload_dir / f"{candidate_id}.{'mcv' if mechanism == 'mcv' else 'fd'}.bin"
        path.write_bytes(payload)
        records.append(
            {
                "candidate_id": candidate_id,
                "relation_oid": relid,
                "relation_name": relation_name,
                "mechanism": mechanism,
                "attributes": attributes,
                "definition": {"statistics_name": name, "stxkind": required_kind},
                "precedence_rank": rank,
                "backend_oid": oid,
                "payload_path": str(path.relative_to(root)),
                "payload_size": len(payload),
                "payload_sha256": hashlib.sha256(payload).hexdigest(),
                "relation_fingerprint": f"{relation_name}:10000",
                "interpretation": {"sender": sender, "postgres_type": column},
            }
        )
    manifest = {
        "format_version": 1,
        "repository_id": "m0-b-fixture-v1",
        "postgres_version": "16.14",
        "upstream_tarball_sha256": UPSTREAM_SHA256,
        "patch_commit": "c052faa80abc9e3db5a9b6c035ef4d195f07436c",
        "acquisition_provenance": {
            "method": "fixture ANALYZE once before evaluator construction",
            "statistics_target": 10000,
        },
        "candidates": records,
    }
    (root / "manifest.json").write_text(json.dumps(manifest, sort_keys=True, indent=2))
    return PayloadRepository.load(root)


def catalog_fingerprint(connection: psycopg.Connection) -> tuple[str, str]:
    definitions = connection.execute(
        "SELECT md5(string_agg(oid::text||':'||stxrelid::text||':'||stxname,',' ORDER BY oid)) "
        "FROM pg_statistic_ext"
    ).fetchone()[0]
    payloads = connection.execute(
        "SELECT md5(string_agg(stxoid::text||':'||stxdinherit::text||':'||"
        "coalesce(pg_mcv_list_send(stxdmcv)::text,'')||':'||"
        "coalesce(pg_dependencies_send(stxddependencies)::text,''),',' ORDER BY stxoid)) "
        "FROM pg_statistic_ext_data"
    ).fetchone()[0]
    return definitions, payloads


def test_external_evaluator_vertical_slice(tmp_path: Path) -> None:
    assert DSN is not None
    with psycopg.connect(DSN) as connection:
        physical = setup_fixture(connection)
        repository = build_repository(connection, tmp_path / "repository")
        relation_oids = {
            name: connection.execute("SELECT %s::regclass::oid", (name,)).fetchone()[0]
            for name in ("m0_single", "m0_overlap", "m0_fd", "m0_mixed", "m0_unaffected")
        }
        queries = (
            WorkloadQuery(QueryId("q_single"), "SELECT * FROM m0_single WHERE a=1 AND b=1", 1000, "m0_single", frozenset({relation_oids["m0_single"]})),
            WorkloadQuery(QueryId("q_overlap"), "SELECT * FROM m0_overlap WHERE a=1 AND b=1 AND c=1", 800, "m0_overlap", frozenset({relation_oids["m0_overlap"]})),
            WorkloadQuery(QueryId("q_fd"), "SELECT * FROM m0_fd WHERE a=1 AND b=1", 100, "m0_fd", frozenset({relation_oids["m0_fd"]})),
            WorkloadQuery(QueryId("q_mixed"), "SELECT * FROM m0_mixed WHERE a=1 AND b=1 AND c=1", 500, "m0_mixed", frozenset({relation_oids["m0_mixed"]})),
            WorkloadQuery(QueryId("q_unaffected"), "SELECT * FROM m0_unaffected WHERE a=1 AND b=1", 1000, "m0_unaffected", frozenset({relation_oids["m0_unaffected"]})),
        )
        workload = Workload("m0-b-workload-v1", queries)
        incidence = IncidenceIndex(
            (
                (CandidateId("single_mcv"), frozenset({QueryId("q_single")})),
                (CandidateId("overlap_ab"), frozenset({QueryId("q_overlap")})),
                (CandidateId("overlap_bc"), frozenset({QueryId("q_overlap")})),
                (CandidateId("fd_ab"), frozenset({QueryId("q_fd")})),
                (CandidateId("mixed_mcv"), frozenset({QueryId("q_mixed")})),
                (CandidateId("mixed_fd"), frozenset({QueryId("q_mixed")})),
            ),
            frozenset(workload.by_id),
        )
        adapter = PostgresAdapter(connection, repository)
        evaluator = NativeEvaluator(workload, repository, incidence, adapter)
        assert adapter.registration_calls == 6
        fingerprint = catalog_fingerprint(connection)

        empty = evaluator.evaluate_design(Design(()))
        assert empty.by_query()[QueryId("q_single")].estimate == physical["empty_single"]
        initial = repository.catalog.normalize_design(
            {CandidateId("single_mcv"), CandidateId("overlap_ab"), CandidateId("fd_ab"), CandidateId("mixed_mcv")}
        )
        current = evaluator.evaluate_design(initial)
        assert current.by_query()[QueryId("q_single")].estimate == physical["single"]
        assert current.by_query()[QueryId("q_fd")].estimate == physical["fd"]

        moves = (
            Move.add_candidate(CandidateId("overlap_bc")),
            Move.drop_candidate(CandidateId("fd_ab")),
            Move.swap(CandidateId("single_mcv"), CandidateId("mixed_fd")),
        )
        expected_counts = ((1, 4), (1, 4), (2, 3))
        for move, counts in zip(moves, expected_counts, strict=True):
            registrations_before = adapter.registration_calls
            local = evaluator.evaluate_move(current.design, move, current)
            called = tuple(adapter.planner_called_query_ids)
            assert called == local.affected_query_ids
            assert (len(local.affected_query_ids), len(local.reused_query_ids)) == counts
            for query_id in local.reused_query_ids:
                assert local.by_query()[query_id] is current.by_query()[query_id]
            full = evaluator.evaluate_design(local.design)
            assert local.design == full.design
            assert local.by_query() == full.by_query()
            assert local.aggregate_objective == full.aggregate_objective
            assert adapter.registration_calls == registrations_before
            current = local

        mixed_design = repository.catalog.normalize_design({CandidateId("mixed_mcv"), CandidateId("mixed_fd")})
        mixed = evaluator.evaluate_design(mixed_design)
        assert mixed.by_query()[QueryId("q_mixed")].estimate == physical["mixed"]
        overlap_design = repository.catalog.normalize_design({CandidateId("overlap_ab"), CandidateId("overlap_bc")})
        overlap = evaluator.evaluate_design(overlap_design)
        assert overlap.by_query()[QueryId("q_overlap")].estimate == physical["overlap"]
        assert catalog_fingerprint(connection) == fingerprint

        with pytest.raises(ValueError, match="state/design mismatch"):
            evaluator.evaluate_move(Design(()), Move.add_candidate(CandidateId("single_mcv")), current)
        bad_lineage = replace(current, repository_digest="wrong")
        with pytest.raises(ValueError, match="state/repository mismatch"):
            evaluator.evaluate_move(current.design, Move.add_candidate(CandidateId("single_mcv")), bad_lineage)
