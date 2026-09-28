from types import SimpleNamespace

import pytest

from pg_extstats_advisor.sql.analysis import analyze_query


def metadata():
    return SimpleNamespace(
        name="t", qualified_name="public.t",
        columns=tuple((i, n, "integer", False) for i, n in enumerate(("a", "b", "irlabor", "or_col", "not_flag"), 1)),
    )


@pytest.mark.parametrize("where", ["irlabor = 1 AND or_col = 2", "not_flag = 1", "a = 'x OR y' AND b = 2", "((a = 1)) AND (b = 2)"])
def test_ast_keywords_and_parentheses_are_precise(where):
    result = analyze_query(f"SELECT * FROM public.t WHERE {where}", "public.t", metadata())
    assert result.derivation_mode == "precise-structural"


@pytest.mark.parametrize("where, reason", [("a = 1 OR b = 2", "boolean-or"), ("NOT a = 1", "boolean-not"), ("a = b", "column-to-column"), ("lower(a) = 'x'", "unresolved-column-reference")])
def test_ast_fallback_reasons(where, reason):
    result = analyze_query(f"SELECT * FROM public.t WHERE {where}", "public.t", metadata())
    assert result.derivation_mode == "conservative-fallback"
    assert reason in result.fallback_reasons


def test_shape_rejection_and_multiple_statements():
    with pytest.raises(ValueError):
        analyze_query("SELECT count(*) FROM public.t WHERE a = 1", "public.t", metadata())
    with pytest.raises(ValueError, match="exactly one"):
        analyze_query("SELECT * FROM public.t WHERE a = 1; SELECT * FROM public.t", "public.t", metadata())
