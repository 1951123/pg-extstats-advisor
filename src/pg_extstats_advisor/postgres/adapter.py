"""One-session adapter for the patched PostgreSQL overlay."""

from __future__ import annotations

from collections.abc import Iterable
from typing import Any

from psycopg import Connection
from psycopg.rows import tuple_row

from pg_extstats_advisor.models import Design, QueryId, WorkloadQuery
from pg_extstats_advisor.payloads.repository import PayloadRepository
from pg_extstats_advisor.postgres.extraction import extract_target_estimate


class PostgresAdapter:
    def __init__(self, connection: Connection[Any], repository: PayloadRepository):
        self.connection = connection
        self.repository = repository
        self.registered = False
        self.registration_calls = 0
        self.planner_called_query_ids: list[QueryId] = []
        self.planner_calls_total = 0
        self.activation_calls = 0
        with self.connection.cursor(row_factory=tuple_row) as cursor:
            cursor.execute("SHOW server_version")
            self.postgres_version = str(cursor.fetchone()[0])

    def reset_overlay(self) -> None:
        self.connection.execute("SELECT pg_hypothetical_extstats_reset()")
        self.registered = False

    def register_repository(self) -> None:
        if self.registered:
            return
        self.reset_overlay()
        for frozen in self.repository.payloads:
            candidate = frozen.candidate
            row = self.connection.execute(
                "SELECT stxrelid, stxkind FROM pg_statistic_ext WHERE oid=%s",
                (candidate.backend_oid,),
            ).fetchone()
            if row is None:
                raise ValueError(f"missing catalog definition for {candidate.candidate_id}")
            relid, kinds = row
            if int(relid) != candidate.relation_oid:
                raise ValueError(f"relation mismatch for {candidate.candidate_id}")
            if candidate.mechanism.postgres_code not in kinds:
                raise ValueError(f"mechanism kind mismatch for {candidate.candidate_id}")
            self.connection.execute(
                "SELECT pg_hypothetical_extstats_register(%s,%s,%s,%s)",
                (
                    candidate.backend_oid,
                    candidate.relation_oid,
                    candidate.mechanism.postgres_code,
                    frozen.payload,
                ),
            )
            self.registration_calls += 1
        self.registered = True

    def activate_design(self, design: Design) -> None:
        if not self.registered:
            raise RuntimeError("payload repository is not registered")
        by_id = self.repository.by_candidate
        try:
            oids = [by_id[item].candidate.backend_oid for item in design.candidate_ids]
        except KeyError as error:
            raise ValueError(f"design references missing payload candidate {error.args[0]}") from error
        self.connection.execute("SELECT pg_hypothetical_extstats_activate(%s::oid[])", (oids,))
        active = self.connection.execute(
            "SELECT pg_hypothetical_extstats_active()"
        ).fetchone()[0]
        if list(active) != oids:
            raise RuntimeError("backend active design does not match requested design")
        self.activation_calls += 1

    def start_measurement(self) -> None:
        self.planner_called_query_ids.clear()

    def estimate_query(self, query: WorkloadQuery) -> float:
        self.planner_called_query_ids.append(query.query_id)
        self.planner_calls_total += 1
        row = self.connection.execute(f"EXPLAIN (FORMAT JSON) {query.sql}").fetchone()
        if row is None:
            raise RuntimeError("EXPLAIN returned no row")
        return extract_target_estimate(row[0], query.target_relation)

    def estimate_queries(self, queries: Iterable[WorkloadQuery]) -> dict[QueryId, float]:
        return {query.query_id: self.estimate_query(query) for query in queries}
