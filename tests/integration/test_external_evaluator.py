from __future__ import annotations

import hashlib
import json
import os
from dataclasses import replace
from pathlib import Path

import psycopg
import pytest

from pg_extstats_advisor.cost.model import MaintenanceBudget
from pg_extstats_advisor.cost.preset import PresetMaintenanceCostModel
from pg_extstats_advisor.deploy.physical import PhysicalDeployer
from pg_extstats_advisor.deploy.sql import (
    build_deployment_plan,
    build_search_deployment_plan,
    quote_identifier,
)
from pg_extstats_advisor.evaluator.native import NativeEvaluator
from pg_extstats_advisor.incidence.index import IncidenceIndex
from pg_extstats_advisor.models import CandidateId, Design, Move, QueryId, WorkloadQuery
from pg_extstats_advisor.payloads.repository import PayloadRepository
from pg_extstats_advisor.postgres.adapter import PostgresAdapter
from pg_extstats_advisor.search.deterministic import DeterministicBudgetSearch
from pg_extstats_advisor.search.model import SearchConfig
from pg_extstats_advisor.validate.deployment import (
    build_validation_result,
    collect_fresh_payload_fingerprints,
    evaluate_physical,
    frozen_payload_fingerprints,
)
from pg_extstats_advisor.validate.model import ValidationProvenance
from pg_extstats_advisor.validate.report import write_validation_report
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

        cost_model = PresetMaintenanceCostModel(0, 1, 1, 1)
        registrations = adapter.registration_calls
        planner_before = adapter.planner_calls_total
        zero = DeterministicBudgetSearch(
            evaluator, repository.catalog, cost_model, MaintenanceBudget(0, cost_model.unit)
        ).run()
        assert zero.final_design == Design(())
        assert zero.evaluated_moves_count == 0
        assert adapter.planner_calls_total - planner_before == len(workload.queries)
        assert adapter.registration_calls == registrations

        def signature(result):
            return tuple(
                (record.move, record.after_design, record.after_objective)
                for record in result.trajectory
                if record.accepted
            )

        local_planner_before = adapter.planner_calls_total
        local = DeterministicBudgetSearch(
            evaluator, repository.catalog, cost_model, MaintenanceBudget(4, cost_model.unit)
        ).run()
        local_planner_calls = adapter.planner_calls_total - local_planner_before
        reference = DeterministicBudgetSearch(
            evaluator,
            repository.catalog,
            cost_model,
            MaintenanceBudget(4, cost_model.unit),
            SearchConfig(full_reference=True),
        ).run()
        assert local.infeasible_moves_skipped_count > 0
        assert signature(local) == signature(reference)
        assert local.final_design == reference.final_design
        assert local.selected_objective == reference.selected_objective

        loose_planner_before = adapter.planner_calls_total
        loose1 = DeterministicBudgetSearch(
            evaluator, repository.catalog, cost_model, MaintenanceBudget(20, cost_model.unit)
        ).run()
        loose_planner_calls = adapter.planner_calls_total - loose_planner_before
        loose2 = DeterministicBudgetSearch(
            evaluator, repository.catalog, cost_model, MaintenanceBudget(20, cost_model.unit)
        ).run()
        assert loose1 == loose2
        assert adapter.registration_calls == registrations
        assert catalog_fingerprint(connection) == fingerprint

        # M2 layer B: retain only the selected physical definitions acquired in R1.
        selected_ids = set(local.selected_design.candidate_ids)
        connection.execute("SELECT pg_hypothetical_extstats_reset()")
        for frozen in repository.payloads:
            if frozen.candidate.candidate_id not in selected_ids:
                name = str(dict(frozen.candidate.definition)["statistics_name"])
                connection.execute(f"DROP STATISTICS public.{quote_identifier(name)}")
        connection.commit()
        same_realization = evaluate_physical(
            connection, workload, local.selected_design, repository.digest
        )
        assert same_realization.design == local.selected_state.design
        for query_id, physical_evaluation in same_realization.by_query().items():
            hypothetical_evaluation = local.selected_state.by_query()[query_id]
            assert physical_evaluation.truth == hypothetical_evaluation.truth
            assert physical_evaluation.estimate == hypothetical_evaluation.estimate
            assert physical_evaluation.contribution == hypothetical_evaluation.contribution
        assert same_realization.aggregate_objective == local.selected_state.aggregate_objective

        # Remove R1 definitions. Empty physical planning still performs fresh ANALYZE.
        for frozen in repository.payloads:
            if frozen.candidate.candidate_id in selected_ids:
                name = str(dict(frozen.candidate.definition)["statistics_name"])
                connection.execute(f"DROP STATISTICS public.{quote_identifier(name)}")
        connection.commit()
        validation_relations = tuple(query.target_relation for query in workload.queries)
        empty_plan = build_deployment_plan(
            Design(()),
            repository.catalog,
            repository_digest=repository.digest,
            workload_digest=workload.digest,
            statistics_target=10000,
            validation_relations=validation_relations,
        )
        empty_deployment = PhysicalDeployer(connection, "disposable-m2-cluster").deploy(
            empty_plan
        )
        assert empty_deployment.created_statistics == ()
        assert len(empty_deployment.analyze_commands) == len(validation_relations)
        empty_physical = evaluate_physical(connection, workload, Design(()), repository.digest)
        assert len(empty_physical.query_evaluations) == len(workload.queries)

        # Exercise a single candidate, then explicit cleanup/recreate behavior.
        single_design = repository.catalog.normalize_design({CandidateId("single_mcv")})
        single_plan = build_deployment_plan(
            single_design,
            repository.catalog,
            repository_digest=repository.digest,
            workload_digest=workload.digest,
            statistics_target=10000,
        )
        single_deployment = PhysicalDeployer(connection, "disposable-m2-cluster").deploy(
            single_plan
        )
        assert len(collect_fresh_payload_fingerprints(connection, single_deployment)) == 1
        with pytest.raises(psycopg.errors.DuplicateObject):
            PhysicalDeployer(connection, "disposable-m2-cluster").deploy(single_plan)
        for name in single_plan.statistics_names:
            connection.execute(f"DROP STATISTICS public.{quote_identifier(name)}")
        connection.commit()

        # Exercise an MCV+FD physical design and verify both native payloads exist.
        mixed_design = repository.catalog.normalize_design(
            {CandidateId("mixed_mcv"), CandidateId("mixed_fd")}
        )
        mixed_plan = build_deployment_plan(
            mixed_design,
            repository.catalog,
            repository_digest=repository.digest,
            workload_digest=workload.digest,
            statistics_target=10000,
        )
        mixed_deployment = PhysicalDeployer(connection, "disposable-m2-cluster").deploy(
            mixed_plan
        )
        mixed_fingerprints = collect_fresh_payload_fingerprints(connection, mixed_deployment)
        assert {item.mechanism for item in mixed_fingerprints} == {"mcv", "fd"}
        for name in mixed_plan.statistics_names:
            connection.execute(f"DROP STATISTICS public.{quote_identifier(name)}")
        connection.commit()

        # Deploy the actual M1-selected design, produce R2, and serialize drift evidence.
        selected_plan = build_search_deployment_plan(
            local,
            repository.catalog,
            statistics_target=10000,
            validation_relations=validation_relations,
        )
        selected_deployment = PhysicalDeployer(
            connection, "disposable-m2-cluster"
        ).deploy(selected_plan)
        fresh = evaluate_physical(
            connection, workload, local.selected_design, repository.digest
        )
        fresh_fingerprints = collect_fresh_payload_fingerprints(
            connection, selected_deployment
        )
        provenance = ValidationProvenance(
            "working-tree-integration",
            repository.upstream_sha256,
            repository.patch_commit,
            workload.digest,
            repository.digest,
            local.cost_model_digest,
            str(local.budget.value),
            local.budget.unit,
            local.config.algorithm_version,
            selected_plan.sql_digest,
            selected_deployment.environment_identity,
            selected_plan.statistics_target,
            selected_deployment.postgres_version,
            tuple(
                (
                    relation,
                    connection.execute(
                        f"SELECT count(*) FROM public.{quote_identifier(relation)}"
                    ).fetchone()[0],
                )
                for relation in sorted(set(validation_relations))
            ),
            (("default_statistics_target", "10000"),),
        )
        validation = build_validation_result(
            frozen=local.selected_state,
            fresh=fresh,
            frozen_fingerprints=frozen_payload_fingerprints(
                repository, local.selected_design
            ),
            fresh_fingerprints=fresh_fingerprints,
            deployment=selected_deployment,
            provenance=provenance,
            same_realization_control=same_realization,
        )
        report_path = tmp_path / "validation-report.json"
        write_validation_report(validation, report_path)
        assert json.loads(report_path.read_text())["aggregate"]["fresh_objective"] == (
            fresh.aggregate_objective
        )
        print(
            "M2_VALIDATION="
            + json.dumps(
                {
                    "design": local.selected_design.candidate_ids,
                    "frozen_objective": validation.aggregate.frozen_objective,
                    "fresh_objective": validation.aggregate.fresh_objective,
                    "objective_drift": validation.aggregate.absolute_objective_drift,
                    "payload_same": sum(
                        item.same_realization for item in validation.payload_comparisons
                    ),
                    "payload_changed": sum(
                        not item.same_realization for item in validation.payload_comparisons
                    ),
                    "per_query": [
                        {
                            "query_id": item.query_id,
                            "frozen": item.frozen_estimate,
                            "fresh": item.fresh_estimate,
                        }
                        for item in validation.per_query
                    ],
                },
                sort_keys=True,
            )
        )
        for name in selected_plan.statistics_names:
            connection.execute(f"DROP STATISTICS public.{quote_identifier(name)}")
        for relation in (
            "m0_single",
            "m0_overlap",
            "m0_fd",
            "m0_mixed",
            "m0_unaffected",
        ):
            connection.execute(f"DROP TABLE public.{quote_identifier(relation)}")
        connection.commit()
        assert connection.execute(
            "SELECT count(*) FROM pg_statistic_ext WHERE stxname LIKE 'pgextadv_%'"
        ).fetchone()[0] == 0
        print(
            json.dumps(
                {
                    "zero": {
                        "design": zero.final_design.candidate_ids,
                        "objective": zero.selected_objective,
                        "skipped": zero.infeasible_moves_skipped_count,
                        "evaluator_calls": zero.evaluator_calls_count,
                    },
                    "intermediate": {
                        "design": local.final_design.candidate_ids,
                        "objective": local.selected_objective,
                        "cost": str(local.selected_maintenance_cost),
                        "accepted": [record.move.kind.value for record in local.trajectory if record.accepted],
                        "skipped": local.infeasible_moves_skipped_count,
                        "evaluator_calls": local.evaluator_calls_count,
                        "planner_calls": local_planner_calls,
                    },
                    "loose": {
                        "design": loose1.final_design.candidate_ids,
                        "objective": loose1.selected_objective,
                        "cost": str(loose1.selected_maintenance_cost),
                        "accepted": [record.move.kind.value for record in loose1.trajectory if record.accepted],
                        "skipped": loose1.infeasible_moves_skipped_count,
                        "evaluator_calls": loose1.evaluator_calls_count,
                        "planner_calls": loose_planner_calls,
                    },
                },
                sort_keys=True,
            )
        )
