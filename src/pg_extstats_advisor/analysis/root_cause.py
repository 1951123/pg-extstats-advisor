"""Pure helpers for M2.28 ordinary-statistics root-cause diagnostics."""

from __future__ import annotations

import math
from collections.abc import Iterable, Mapping
from dataclasses import dataclass
from typing import Any

from pglast import parse_sql
from pglast.ast import A_Const, A_Expr, BoolExpr, ColumnRef, NullTest, RangeVar, SelectStmt
from pglast.enums import A_Expr_Kind, BoolExprType


@dataclass(frozen=True, slots=True)
class Predicate:
    """One equality/IN predicate recovered from the single-table workload."""

    column: str
    operator: str
    constants: tuple[str, ...]


def _name(node: Any) -> str:
    return str(getattr(node, "sval", node))


def _constant(node: Any) -> str | None:
    if not isinstance(node, A_Const) or bool(getattr(node, "isnull", False)):
        return None
    value = getattr(node, "val", None)
    for attribute in ("sval", "ival", "fval", "boolval"):
        if hasattr(value, attribute):
            return str(getattr(value, attribute))
    return str(value)


def _column(node: Any) -> str | None:
    if not isinstance(node, ColumnRef) or not node.fields:
        return None
    return _name(node.fields[-1])


def parse_predicates(sql: str) -> tuple[str, tuple[Predicate, ...], str]:
    """Return relation, predicates, and a compact conjunction classification."""

    statements = parse_sql(sql)
    if len(statements) != 1 or not isinstance(statements[0].stmt, SelectStmt):
        raise ValueError("M2.28 expects one SELECT statement")
    statement = statements[0].stmt
    if (
        not statement.fromClause
        or len(statement.fromClause) != 1
        or not isinstance(statement.fromClause[0], RangeVar)
    ):
        raise ValueError("M2.28 expects one base relation")
    relation = ".".join(
        part
        for part in (statement.fromClause[0].schemaname, statement.fromClause[0].relname)
        if part
    )
    predicates: list[Predicate] = []
    structure = "NO_WHERE"

    def walk(node: Any) -> None:
        nonlocal structure
        if isinstance(node, BoolExpr):
            if node.boolop == BoolExprType.AND_EXPR:
                structure = "AND" if structure in {"NO_WHERE", "AND"} else "MIXED"
                for child in node.args:
                    walk(child)
                return
            structure = "OR" if node.boolop == BoolExprType.OR_EXPR else "NOT"
            return
        if isinstance(node, NullTest):
            column = _column(node.arg)
            if column:
                predicates.append(Predicate(column, "IS NULL", ()))
            return
        if not isinstance(node, A_Expr):
            return
        column = _column(node.lexpr)
        if column is None:
            return
        if node.kind == A_Expr_Kind.AEXPR_IN:
            values = node.rexpr if isinstance(node.rexpr, tuple) else ()
            constants = tuple(
                value for value in (_constant(item) for item in values) if value is not None
            )
            if constants and len(constants) == len(values):
                predicates.append(Predicate(column, "IN", constants))
            return
        if node.kind == A_Expr_Kind.AEXPR_OP:
            operator = _name(node.name[0]) if node.name else "?"
            value = _constant(node.rexpr)
            if value is not None:
                predicates.append(Predicate(column, operator, (value,)))

    if statement.whereClause is not None:
        walk(statement.whereClause)
        if structure == "NO_WHERE":
            structure = "LEAF"
        if len(predicates) > 1 and structure == "LEAF":
            structure = "AND"
    return relation, tuple(predicates), structure


def normalize_ndistinct(raw: float | None, source_rows: int) -> float | None:
    """Normalize PG's negative n_distinct ratio into an implied cardinality."""

    if raw is None:
        return None
    value = float(raw)
    return -value * source_rows if value < 0 else value


def _norm(value: Any) -> str:
    return str(value)


def mcv_membership(stat: Mapping[str, Any], constant: str) -> dict[str, Any]:
    """Classify one constant against a canonical stats row."""

    values = list(stat.get("mcv_values") or [])
    frequencies = list(stat.get("mcv_frequencies") or [])
    key = _norm(constant)
    try:
        index = [_norm(value) for value in values].index(key)
    except ValueError:
        index = -1
    frequency = frequencies[index] if 0 <= index < len(frequencies) else None
    ndistinct = stat.get("n_distinct")
    if frequency is not None:
        path, selectivity = "MCV_EXACT", float(frequency)
    elif ndistinct not in (None, 0):
        path, selectivity = "MCV_MISS_NDISTINCT_FALLBACK", 1.0 / abs(float(ndistinct))
    elif stat.get("histogram_bounds"):
        path, selectivity = "HISTOGRAM", None
    else:
        path, selectivity = "OTHER", None
    return {
        "in_mcv": index >= 0,
        "mcv_frequency": None if frequency is None else float(frequency),
        "selectivity_path": path,
        "selectivity": selectivity,
    }


def jaccard(left: Iterable[Any], right: Iterable[Any]) -> float:
    a, b = {_norm(item) for item in left}, {_norm(item) for item in right}
    union = a | b
    return 1.0 if not union else len(a & b) / len(union)


def frequency_differences(left: Mapping[str, Any], right: Mapping[str, Any]) -> list[float]:
    """Absolute frequency deltas over MCV values common to two states."""

    a = {
        _norm(value): float(freq)
        for value, freq in zip(left.get("mcv_values") or (), left.get("mcv_frequencies") or ())
    }
    b = {
        _norm(value): float(freq)
        for value, freq in zip(right.get("mcv_values") or (), right.get("mcv_frequencies") or ())
    }
    return [abs(a[key] - b[key]) for key in sorted(a.keys() & b.keys())]


def quantiles(values: Iterable[float]) -> dict[str, float | None]:
    ordered = sorted(float(value) for value in values)
    if not ordered:
        return {name: None for name in ("p50", "p75", "p90", "p95", "p99", "max")}

    def at(probability: float) -> float:
        return ordered[min(len(ordered) - 1, max(0, math.ceil(probability * len(ordered)) - 1))]

    return {
        "p50": at(0.50),
        "p75": at(0.75),
        "p90": at(0.90),
        "p95": at(0.95),
        "p99": at(0.99),
        "max": ordered[-1],
    }


def top_contributors(rows: Iterable[Mapping[str, Any]], limit: int = 20) -> list[Mapping[str, Any]]:
    """Deterministically retain the largest reduction rows."""

    return sorted(rows, key=lambda row: (-float(row["qerror_reduction"]), str(row["query_id"])))[
        :limit
    ]


def trimmed_aggregate(rows: Iterable[Mapping[str, Any]], excluded_ids: set[str]) -> float:
    """Sum q-errors after removing one fixed query set."""

    return math.fsum(
        float(row["q_error"]) for row in rows if str(row["query_id"]) not in excluded_ids
    )


def adjacent_target_gains(values: Mapping[int, float]) -> list[float]:
    """Return relative 100→300 and 300→1000 objective gains."""

    return [
        (float(values[100]) - float(values[300])) / float(values[100]),
        (float(values[300]) - float(values[1000])) / float(values[300]),
    ]
