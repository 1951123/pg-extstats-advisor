"""Portable capture-time relation identity used by deployment preflight."""

from __future__ import annotations

from collections.abc import Mapping
from typing import Any

from pg_extstats_advisor.capture.manifest import canonical_digest


def schema_binding_from_record(relation: Mapping[str, Any]) -> dict[str, Any]:
    """Return the stable, non-OID schema identity for one captured relation.

    Statistics payload/target fields are deliberately excluded: they are
    deployment configuration, not relation schema.  Historical captures did
    not record ``relation_kind``; those remain usable with the conservative
    regular/partitioned-table compatibility set.
    """

    columns = []
    for column in relation.get("columns", []):
        columns.append(
            {
                "attnum": int(column["attnum"]),
                "name": str(column["name"]),
                "nullable": bool(column["nullable"]),
                "typmod": str(column.get("typmod", "-1")),
                "type": dict(column["type"]),
                "collation": dict(column["collation"]),
            }
        )
    raw_kind = relation.get("relation_kind")
    relation_kinds = [str(raw_kind)] if raw_kind in {"r", "p"} else ["r", "p"]
    value: dict[str, Any] = {
        "relation_id": str(relation["relation_id"]),
        "relation_kind": relation_kinds,
        "columns": columns,
    }
    value["schema_digest"] = canonical_digest(value)
    return value


__all__ = ["schema_binding_from_record"]
