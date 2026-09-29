"""One-session adapter for the patched PostgreSQL overlay."""

from __future__ import annotations

from collections.abc import Iterable
from typing import Any

from psycopg import Connection
from psycopg.rows import tuple_row

from pg_extstats_advisor.models import CandidateId, Design, QueryId, WorkloadQuery
from pg_extstats_advisor.payloads.repository import NativePayloadState, PayloadRepository
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
        self._resolved_backend_oids: dict[CandidateId, int] = {}
        self._resolved_relation_oids: dict[CandidateId, int] = {}
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
        self._resolved_backend_oids.clear()
        self._resolved_relation_oids.clear()
        for frozen in self.repository.payloads:
            candidate = frozen.candidate
            definition = dict(candidate.definition)
            statistics_name = definition.get("statistics_name")
            if not statistics_name:
                raise ValueError(f"missing catalog definition for {candidate.candidate_id}")
            if "." in candidate.relation_name:
                schema, relation_name = candidate.relation_name.split(".", 1)
            else:
                schema, relation_name = "public", candidate.relation_name
            relation_row = self.connection.execute(
                "SELECT c.oid FROM pg_class c JOIN pg_namespace n "
                "ON n.oid=c.relnamespace "
                "WHERE n.nspname=%s AND c.relname=%s",
                (schema, relation_name),
            ).fetchone()
            if relation_row is None:
                raise ValueError(f"missing relation for {candidate.candidate_id}")
            resolved_relation_oid = int(relation_row[0])
            row = self.connection.execute(
                "SELECT stxrelid, stxkind FROM pg_statistic_ext WHERE oid=%s",
                (candidate.backend_oid,),
            ).fetchone()
            resolved_oid = candidate.backend_oid
            if row is None:
                row_with_oid = self.connection.execute(
                    "SELECT e.oid, e.stxrelid, e.stxkind "
                    "FROM pg_statistic_ext e JOIN pg_namespace n "
                    "ON n.oid=e.stxnamespace "
                    "WHERE n.nspname=%s AND e.stxname=%s",
                    (schema, statistics_name),
                ).fetchone()
                if row_with_oid is not None:
                    resolved_oid, relid, kinds = row_with_oid
                    row = (relid, kinds)
            if row is None:
                raise ValueError(f"missing catalog definition for {candidate.candidate_id}")
            relid, kinds = row
            if int(relid) != resolved_relation_oid:
                raise ValueError(f"relation mismatch for {candidate.candidate_id}")
            if candidate.mechanism.postgres_code not in kinds:
                raise ValueError(f"mechanism kind mismatch for {candidate.candidate_id}")
            self._resolved_backend_oids[candidate.candidate_id] = int(resolved_oid)
            self._resolved_relation_oids[candidate.candidate_id] = resolved_relation_oid
            if frozen.state is NativePayloadState.PRESENT:
                self.connection.execute(
                    "SELECT pg_hypothetical_extstats_register(%s,%s,%s,%s)",
                    (resolved_oid, resolved_relation_oid,
                     candidate.mechanism.postgres_code, frozen.payload),
                )
            elif frozen.state is NativePayloadState.ABSENT_NATIVE:
                self.connection.execute(
                    "SELECT pg_hypothetical_extstats_register_absent(%s::oid,%s::oid,%s::\"char\")",
                    (resolved_oid, resolved_relation_oid,
                     candidate.mechanism.postgres_code),
                )
            else:
                raise ValueError(f"unsupported realization state for {candidate.candidate_id}")
            self.registration_calls += 1
        self.registered = True

    def activate_design(self, design: Design) -> None:
        if not self.registered:
            raise RuntimeError("payload repository is not registered")
        by_id = self.repository.by_candidate
        try:
            oids = [self._resolved_backend_oids[item] for item in design.candidate_ids]
        except KeyError as error:
            if error.args[0] in by_id:
                raise ValueError(f"candidate was not registered: {error.args[0]}") from error
            raise ValueError(f"design references missing payload candidate {error.args[0]}") from error
        self.connection.execute("SELECT pg_hypothetical_extstats_activate(%s::oid[])", (oids,))
        active = self.connection.execute("SELECT pg_hypothetical_extstats_active()").fetchone()[0]
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
