"""Narrow supplied-workload ingestion and predicate inspection."""

from __future__ import annotations

import hashlib
import json
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


def ingest_workload(path: Path, connection: Connection[Any]) -> IngestedWorkload:
    raw = json.loads(path.read_text())
    if (
        raw.get("schema_version") != 1
        or not raw.get("workload_id")
        or not isinstance(raw.get("queries"), list)
    ):
        raise ValueError("invalid workload file")
    queries, inspections, seen = [], [], set()
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
    return IngestedWorkload(Workload(str(raw["workload_id"]), tuple(queries)), tuple(inspections))
