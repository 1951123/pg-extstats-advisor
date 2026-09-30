from __future__ import annotations

from pg_extstats_advisor.capture.schema import schema_binding_from_record
from pg_extstats_advisor.deploy.preflight import _schema_match


def _relation(kind: str = "r") -> dict:
    return {
        "relation_id": "public.t",
        "relation_kind": kind,
        "columns": [
            {
                "attnum": 1,
                "name": "a",
                "nullable": True,
                "typmod": "-1",
                "type": {
                    "kind": "builtin",
                    "name": "text",
                    "namespace": "pg_catalog",
                    "portable_name": "pg_catalog.text",
                },
                "collation": {
                    "name": "default",
                    "namespace": "pg_catalog",
                    "portable_name": "pg_catalog.default",
                },
                "statistics": {"target": 100},
            }
        ],
    }


def test_schema_binding_excludes_statistics_target_and_is_stable() -> None:
    relation = _relation()
    binding = schema_binding_from_record(relation)
    relation["columns"][0]["statistics"]["target"] = 300
    assert binding["relation_id"] == "public.t"
    assert binding["relation_kind"] == ["r"]
    assert "statistics" not in binding["columns"][0]
    assert binding["schema_digest"] == schema_binding_from_record(_relation())["schema_digest"]


def test_schema_match_accepts_same_semantics_and_rejects_drift() -> None:
    binding = schema_binding_from_record(_relation())
    observed = [dict(binding["columns"][0]) | {"attstattarget": -1}]
    matched, _ = _schema_match(binding, observed, "r", "public.t")
    assert matched
    renamed = [dict(observed[0]) | {"name": "renamed"}]
    matched, _ = _schema_match(binding, renamed, "r", "public.t")
    assert not matched
    matched, _ = _schema_match(binding, observed, "v", "public.t")
    assert not matched
