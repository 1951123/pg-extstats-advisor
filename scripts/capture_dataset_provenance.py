#!/usr/bin/env python3
"""Capture validated dataset provenance from the authoritative PG16 cluster."""

from __future__ import annotations

import argparse
import hashlib
import json
from datetime import UTC, datetime
from pathlib import Path

import psycopg


def main() -> None:
    parser = argparse.ArgumentParser()
    parser.add_argument("--dataset", choices=("census", "dmv"), required=True)
    parser.add_argument("--dsn", required=True)
    parser.add_argument("--output", type=Path, required=True)
    args = parser.parse_args()
    specifications = {
        "census": {
            "relation": "public.climate",
            "source": Path("/root/projects/pg-extstats-advisor/.build/datasets/census-climate.csv"),
            "rows": 2458285,
            "loader": "scripts/load_census_pg16.sh",
            "source_kind": "logical CSV export from the fixed legacy Census database",
        },
        "dmv": {
            "relation": "public.dmv",
            "source": Path(
                "/root/projects/extended-stats-optim-v2/benchmarks/DMV/data/original.csv"
            ),
            "rows": 11591877,
            "loader": "scripts/load_dmv_pg16.sh",
            "source_kind": "canonical benchmark CSV with deterministic btrim transformation",
        },
    }
    specification = specifications[args.dataset]
    with psycopg.connect(args.dsn) as connection:
        version = connection.execute("SHOW server_version").fetchone()[0]
        if version != "16.14":
            raise RuntimeError(f"refusing non-authoritative server version {version}")
        oid, persistence, size = connection.execute(
            "SELECT %s::regclass::oid, c.relpersistence::text, pg_total_relation_size(c.oid) "
            "FROM pg_class c WHERE c.oid=%s::regclass",
            (specification["relation"], specification["relation"]),
        ).fetchone()
        rows = connection.execute(
            f"SELECT count(*) FROM {specification['relation']}"  # controlled identifiers above
        ).fetchone()[0]
        columns = [
            {"attnum": row[0], "name": row[1], "type": row[2], "not_null": row[3]}
            for row in connection.execute(
                "SELECT attnum,attname,format_type(atttypid,atttypmod),attnotnull "
                "FROM pg_attribute WHERE attrelid=%s AND attnum>0 AND NOT attisdropped "
                "ORDER BY attnum",
                (oid,),
            ).fetchall()
        ]
        database = connection.execute("SELECT current_database()").fetchone()[0]
    if rows != specification["rows"]:
        raise RuntimeError(f"row-count mismatch: {rows} != {specification['rows']}")
    schema_signature = hashlib.sha256(
        json.dumps(columns, sort_keys=True, separators=(",", ":")).encode()
    ).hexdigest()
    logical_fingerprint = hashlib.sha256(
        json.dumps(
            {"relation": specification["relation"], "rows": rows, "columns": columns},
            sort_keys=True,
            separators=(",", ":"),
        ).encode()
    ).hexdigest()
    build = json.loads(
        Path("experiments/environment/postgresql-16.14-build.json").read_text()
    )
    source = specification["source"]
    value = {
        "format_version": 1,
        "dataset_id": f"{args.dataset}-pg16.14-v1",
        "source_path": str(source),
        "source_kind": specification["source_kind"],
        "source_sha256": hashlib.sha256(source.read_bytes()).hexdigest(),
        "preprocessing_import_script": specification["loader"],
        "database_name": database,
        "relation": specification["relation"],
        "row_count": rows,
        "relation_persistence": persistence,
        "relation_size_bytes": size,
        "ordered_column_schema": columns,
        "schema_signature": schema_signature,
        "logical_relation_fingerprint": logical_fingerprint,
        "postgres_version": version,
        "postgres_build_recipe_digest": build["build_recipe_digest"],
        "import_completed_at": datetime.now(UTC).isoformat(),
    }
    args.output.parent.mkdir(parents=True, exist_ok=True)
    temporary = args.output.with_suffix(args.output.suffix + ".tmp")
    temporary.write_text(json.dumps(value, sort_keys=True, indent=2) + "\n")
    temporary.replace(args.output)


if __name__ == "__main__":
    main()
