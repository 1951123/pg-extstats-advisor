"""AST-based, deliberately narrow PostgreSQL workload analysis."""
from __future__ import annotations

from dataclasses import dataclass
from typing import Any

from pglast import parse_sql
from pglast.ast import (
    A_Const,
    A_Expr,
    BoolExpr,
    ColumnRef,
    FuncCall,
    NullTest,
    RangeVar,
    SelectStmt,
    SubLink,
)
from pglast.enums import A_Expr_Kind, BoolExprType, NullTestType, SetOperation
from pglast.parser import ParseError

PARSER = "pglast"
PARSER_VERSION = __import__("pglast").__version__
ANALYSIS_VERSION = "pg16-mvp-v2"

@dataclass(frozen=True, slots=True)
class QueryAnalysis:
    relation: str
    predicate_columns: frozenset[str]
    derivation_mode: str
    fallback_reasons: tuple[str, ...] = ()
    parser: str = PARSER
    parser_version: str = PARSER_VERSION
    analysis_version: str = ANALYSIS_VERSION

def _name(node: Any) -> str:
    return str(getattr(node, "sval", node))

def _column(node: Any, relation: RangeVar, aliases: set[str], known: set[str]) -> str | None:
    if not isinstance(node, ColumnRef): return None
    fields = tuple(_name(x) for x in node.fields)
    if not fields: return None
    col = fields[-1]
    if len(fields) > 1 and fields[-2] not in aliases | {relation.relname}: return None
    return col if col in known else None

def _constant(node: Any) -> bool:
    return isinstance(node, A_Const) and not bool(getattr(node, "isnull", False))

def _contains(node: Any, kind: type) -> bool:
    if isinstance(node, kind):
        return True
    if isinstance(node, tuple):
        return any(_contains(item, kind) for item in node)
    if hasattr(node, "__slots__"):
        return any(_contains(getattr(node, slot), kind) for slot in node.__slots__)
    return False

def _leaf(node: Any, relation: RangeVar, aliases: set[str], known: set[str]) -> tuple[str | None, str | None]:
    if isinstance(node, NullTest):
        if node.nulltesttype != NullTestType.IS_NULL: return None, "unsupported-null-test"
        return _column(node.arg, relation, aliases, known), None
    if not isinstance(node, A_Expr): return None, "unsupported-expression"
    col = _column(node.lexpr, relation, aliases, known)
    if col is None: return None, "unresolved-column-reference"
    if node.kind == A_Expr_Kind.AEXPR_IN:
        vals = node.rexpr if isinstance(node.rexpr, tuple) else ()
        return (col, None) if vals and all(_constant(v) for v in vals) else (None, "unsupported-expression")
    if node.kind != A_Expr_Kind.AEXPR_OP: return None, "unsupported-operator"
    op = _name(node.name[0]) if node.name else ""
    if op not in {"=", "<", "<=", ">", ">="}: return None, "unsupported-operator"
    if not _constant(node.rexpr): return None, "column-to-column" if isinstance(node.rexpr, ColumnRef) else "unsupported-expression"
    return col, None

def analyze_query(sql: str, declared_relation: str, metadata: Any) -> QueryAnalysis:
    try:
        statements = parse_sql(sql)
    except ParseError as error:
        raise ValueError(f"SQL parse error: {error}") from error
    if len(statements) != 1: raise ValueError("query must contain exactly one SQL statement")
    stmt = statements[0].stmt
    if not isinstance(stmt, SelectStmt): raise ValueError("query must be SELECT")  # noqa: TRY004
    if stmt.withClause or stmt.op != SetOperation.SETOP_NONE or stmt.groupClause or stmt.havingClause or stmt.windowClause or stmt.distinctClause:
        raise ValueError("query is outside MVP single-relation SELECT scope")
    for target in stmt.targetList:
        value = target.val
        if isinstance(value, FuncCall):
            names = tuple(_name(item) for item in value.funcname)
            if names != ("count",) or not value.agg_star:
                raise ValueError("aggregate/function target is outside MVP scope")
    if _contains(stmt.targetList, SubLink) or _contains(stmt.whereClause, SubLink):
        raise ValueError("subqueries are outside MVP scope")
    if not stmt.fromClause or len(stmt.fromClause) != 1 or not isinstance(stmt.fromClause[0], RangeVar):
        raise ValueError("query must reference exactly one base relation")
    rv = stmt.fromClause[0]
    parsed = f"{rv.schemaname}.{rv.relname}" if rv.schemaname else rv.relname
    if parsed not in {metadata.name, metadata.qualified_name, declared_relation}:
        raise ValueError("declared target relation does not match query FROM relation")
    aliases = {rv.alias.aliasname} if rv.alias else set()
    known = {name for _, name, _, _ in metadata.columns}
    if stmt.whereClause is None: return QueryAnalysis(parsed, frozenset(), "conservative-fallback", ("no-where",))
    reasons: set[str] = set(); cols: set[str] = set()
    def walk(node: Any) -> None:
        if isinstance(node, BoolExpr):
            if node.boolop != BoolExprType.AND_EXPR: reasons.add("boolean-or" if node.boolop == BoolExprType.OR_EXPR else "boolean-not"); return
            for child in node.args: walk(child)
            return
        col, reason = _leaf(node, rv, aliases, known)
        if reason: reasons.add(reason)
        elif col: cols.add(col)
    walk(stmt.whereClause)
    return QueryAnalysis(parsed, frozenset(cols) if not reasons else frozenset(), "conservative-fallback" if reasons else "precise-structural", tuple(sorted(reasons)))
