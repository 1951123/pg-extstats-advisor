"""Narrow supplied-workload ingestion and predicate inspection."""

from __future__ import annotations

import hashlib
import json
import re
from dataclasses import dataclass
from pathlib import Path
from typing import Any

from psycopg import Connection

from pg_extstats_advisor.models import QueryId, WorkloadQuery
from pg_extstats_advisor.workload.model import Workload

_FORBIDDEN = re.compile(
    r"\b(join|group\s+by|having|union|intersect|except|with|over)\b", re.IGNORECASE
)
_AGGREGATE = re.compile(r"\b(count|sum|avg|min|max)\s*\(", re.IGNORECASE)
_SHAPE = re.compile(
    r"^\s*select\s+.+?\s+from\s+([A-Za-z_][\w$]*)(?:\.([A-Za-z_][\w$]*))?"
    r"(?:\s+(?:as\s+)?[A-Za-z_][\w$]*)?\s+where\s+(.+?)\s*;?\s*$",
    re.IGNORECASE | re.DOTALL,
)
_COLUMN = r"(?:[A-Za-z_][\w$]*\.)?([A-Za-z_][\w$]*)"
_LITERAL = r"(?:[-+]?\d+(?:\.\d+)?|'(?:[^']|'')*'|true|false|null)"
_SCALAR = re.compile(rf"{_COLUMN}\s*(?:=|<=|>=|<|>)\s*({_LITERAL})", re.IGNORECASE)
_IN = re.compile(rf"{_COLUMN}\s+in\s*\(\s*{_LITERAL}(?:\s*,\s*{_LITERAL})*\s*\)", re.IGNORECASE)
_IS_NULL = re.compile(rf"{_COLUMN}\s+is\s+null", re.IGNORECASE)


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


def _split_top_level_and(where: str) -> list[str] | None:
    clauses: list[str] = []
    start = 0
    depth = 0
    quoted = False
    index = 0
    while index < len(where):
        char = where[index]
        if char == "'":
            if quoted and index + 1 < len(where) and where[index + 1] == "'":
                index += 2
                continue
            quoted = not quoted
        elif not quoted:
            if char == "(":
                depth += 1
            elif char == ")":
                depth -= 1
                if depth < 0:
                    return None
            elif depth == 0:
                token = re.match(r"(?i)(and|or|not)\b", where[index:])
                if token:
                    word = token.group(1).lower()
                    if word != "and":
                        return None
                    clauses.append(where[start:index].strip())
                    index += len(token.group(0))
                    start = index
                    continue
        index += 1
    if quoted or depth != 0:
        return None
    clauses.append(where[start:].strip())
    return clauses


def _inspect_sql(
    sql: str, declared_relation: str, metadata: RelationMetadata
) -> tuple[frozenset[str], str]:
    if (
        not re.match(r"^\s*select\b", sql, re.IGNORECASE)
        or _FORBIDDEN.search(sql)
        or _AGGREGATE.search(sql)
    ):
        raise ValueError("query is outside MVP single-relation SELECT scope")
    if re.search(r"\(\s*select\b", sql, re.IGNORECASE):
        raise ValueError("subqueries are outside MVP scope")
    match = _SHAPE.match(sql)
    if match is None:
        raise ValueError("query must be one base relation SELECT with WHERE")
    parsed = match.group(1) if match.group(2) is None else f"{match.group(1)}.{match.group(2)}"
    if parsed not in {metadata.name, metadata.qualified_name, declared_relation}:
        raise ValueError("declared target relation does not match query FROM relation")
    known = {name for _, name, _, _ in metadata.columns}
    found: set[str] = set()
    clauses = _split_top_level_and(match.group(3))
    if clauses is None:
        return frozenset(), "conservative-fallback"
    for clause in clauses:
        predicate = next(
            (
                pattern.fullmatch(clause)
                for pattern in (_SCALAR, _IN, _IS_NULL)
                if pattern.fullmatch(clause)
            ),
            None,
        )
        if predicate is None or predicate.group(1) not in known:
            return frozenset(), "conservative-fallback"
        found.add(predicate.group(1))
    return frozenset(found), "precise-structural"


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
        inspections.append(QueryInspection(query.query_id, metadata, columns, mode))
    return IngestedWorkload(Workload(str(raw["workload_id"]), tuple(queries)), tuple(inspections))
