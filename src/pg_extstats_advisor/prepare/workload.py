"""Narrow supplied-workload ingestion and predicate inspection."""

from __future__ import annotations

import hashlib
import json
import math
from dataclasses import dataclass
from pathlib import Path
from typing import Any

from psycopg import Connection

from pg_extstats_advisor.models import QueryId, WorkloadQuery
from pg_extstats_advisor.sql.analysis import analyze_query
from pg_extstats_advisor.workload.model import Workload


@dataclass(frozen=True, slots=True)
class RelationMetadata:
    schema: str
    name: str
    oid: int
    columns: tuple[tuple[int, str, str, bool], ...]

    @property
    def qualified_name(self) -> str:
        return f"{self.schema}.{self.name}"

    @property
    def logical_descriptor(self) -> dict[str, Any]:
        return {"qualified_relation": self.qualified_name, "columns": self.columns}

    @property
    def logical_fingerprint(self) -> str:
        value = json.dumps(self.logical_descriptor, sort_keys=True, separators=(",", ":"))
        return hashlib.sha256(value.encode()).hexdigest()


@dataclass(frozen=True, slots=True)
class QueryInspection:
    query_id: QueryId
    relation: RelationMetadata
    predicate_columns: frozenset[str]
    derivation_mode: str
    parser: str = "pglast"
    parser_version: str = "unknown"
    analysis_version: str = "unknown"


@dataclass(frozen=True, slots=True)
class IngestedWorkload:
    workload: Workload
    inspections: tuple[QueryInspection, ...]
    raw_query_count: int = 0
    excluded_query_ids: tuple[str, ...] = ()
    objective_membership_policy: str = "require_all_positive"
    raw_source_path: str | None = None
    raw_source_sha256: str | None = None
    raw_workload_digest: str | None = None
    effective_workload_digest: str | None = None


OBJECTIVE_MEMBERSHIP_POLICIES = {"require_all_positive", "positive_truth_only"}


def _records_digest(records: list[dict[str, Any]], policy: str) -> str:
    value = {
        "objective_membership_policy": policy,
        "queries": [
            {
                "query_id": str(record["query_id"]),
                "sql": str(record["sql"]),
                "truth": float(record["truth"]),
                "target_relation": str(record["target_relation"]),
            }
            for record in records
        ],
    }
    return hashlib.sha256(
        json.dumps(value, sort_keys=True, separators=(",", ":")).encode()
    ).hexdigest()


def _relation(connection: Connection[Any], value: str) -> RelationMetadata:
    row = connection.execute(
        "SELECT n.nspname,c.relname,c.oid FROM pg_class c JOIN pg_namespace n ON n.oid=c.relnamespace "
        "WHERE c.oid=to_regclass(%s) AND c.relkind IN ('r','p')",
        (value,),
    ).fetchone()
    if row is None:
        raise ValueError(f"target relation does not exist: {value}")
    columns = connection.execute(
        "SELECT attnum,attname,atttypid::regtype::text,attnotnull FROM pg_attribute "
        "WHERE attrelid=%s AND attnum>0 AND NOT attisdropped ORDER BY attnum",
        (row[2],),
    ).fetchall()
    return RelationMetadata(
        str(row[0]),
        str(row[1]),
        int(row[2]),
        tuple((int(a), str(b), str(c), bool(d)) for a, b, c, d in columns),
    )


def _inspect_sql(
    sql: str, declared_relation: str, metadata: RelationMetadata
) -> tuple[frozenset[str], str]:
    analysis = analyze_query(sql, declared_relation, metadata)
    return analysis.predicate_columns, analysis.derivation_mode


def ingest_workload(
    path: Path,
    connection: Connection[Any],
    *,
    objective_membership_policy: str = "require_all_positive",
) -> IngestedWorkload:
    if objective_membership_policy not in OBJECTIVE_MEMBERSHIP_POLICIES:
        raise ValueError(
            "objective_membership_policy must be require_all_positive or positive_truth_only"
        )
    raw = json.loads(path.read_text())
    if (
        raw.get("schema_version") != 1
        or not raw.get("workload_id")
        or not isinstance(raw.get("queries"), list)
    ):
        raise ValueError("invalid workload file")
    queries, inspections, seen = [], [], set()
    excluded: list[str] = []
    records = [dict(record) for record in raw["queries"]]
    raw_source = raw.get("source_provenance", {})
    for record in raw["queries"]:
        query_id = str(record["query_id"])
        if query_id in seen:
            raise ValueError(f"duplicate query ID: {query_id}")
        seen.add(query_id)
        sql, truth, target = (
            str(record["sql"]),
            float(record["truth"]),
            str(record["target_relation"]),
        )
        if not math.isfinite(truth):
            raise ValueError(f"non-finite truth is invalid for objective query: {query_id}")
        if truth < 0:
            raise ValueError(f"negative truth is invalid for objective query: {query_id}")
        if truth == 0:
            if objective_membership_policy == "positive_truth_only":
                excluded.append(query_id)
                continue
            raise ValueError(
                f"non-positive truth requires explicit positive_truth_only policy: {query_id}"
            )
        metadata = _relation(connection, target)
        columns, mode = _inspect_sql(sql, target, metadata)
        query = WorkloadQuery(
            QueryId(query_id),
            sql,
            truth,
            metadata.name,
            frozenset({metadata.oid}),
            record.get("label"),
        )
        queries.append(query)
        analysis = analyze_query(sql, target, metadata)
        inspections.append(QueryInspection(query.query_id, metadata, columns, mode, analysis.parser, analysis.parser_version, analysis.analysis_version))
    workload = Workload(str(raw["workload_id"]), tuple(queries))
    excluded_set = set(excluded)
    effective_digest = (
        workload.digest
        if objective_membership_policy == "require_all_positive" and not excluded
        else _records_digest(
            [record for record in records if str(record["query_id"]) not in excluded_set],
            objective_membership_policy,
        )
    )
    return IngestedWorkload(
        workload,
        tuple(inspections),
        raw_query_count=len(records),
        excluded_query_ids=tuple(excluded),
        objective_membership_policy=objective_membership_policy,
        raw_source_path=(str(raw_source["path"]) if raw_source.get("path") else None),
        raw_source_sha256=(str(raw_source["sha256"]) if raw_source.get("sha256") else None),
        raw_workload_digest=_records_digest(records, "raw"),
        effective_workload_digest=effective_digest,
    )
