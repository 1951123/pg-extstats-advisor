"""One-time physical acquisition of native MCV/FD payloads."""

from __future__ import annotations

import hashlib
import json
import os
import shutil
import tempfile
from dataclasses import dataclass
from datetime import UTC, datetime
from pathlib import Path
from typing import Any

from psycopg import Connection

from pg_extstats_advisor.candidates.model import CandidateCatalog
from pg_extstats_advisor.deploy.sql import qualified_relation_name, quote_identifier
from pg_extstats_advisor.models import MechanismKind
from pg_extstats_advisor.payloads.repository import PayloadRepository
from pg_extstats_advisor.prepare.workload import RelationMetadata


@dataclass(frozen=True, slots=True)
class AcquisitionResult:
    repository: PayloadRepository
    analyzed_relations: tuple[str, ...]
    analyze_count: int
    created_statistics_names: tuple[str, ...]
    compatibility: tuple[tuple[str, str, str, bool], ...]


def _acquisition_name(candidate_id: str) -> str:
    return f"pgextadv_acq_{hashlib.sha256(candidate_id.encode()).hexdigest()[:24]}"


def _relation_fingerprint(connection: Connection[Any], relation: str) -> str:
    row = connection.execute(
        "SELECT current_database(),n.nspname,c.relname,c.oid,c.relfilenode,c.reltuples::bigint "
        "FROM pg_class c JOIN pg_namespace n ON n.oid=c.relnamespace WHERE c.oid=to_regclass(%s)",
        (relation,),
    ).fetchone()
    if row is None:
        raise ValueError(f"acquisition relation missing: {relation}")
    columns = connection.execute(
        "SELECT attnum,attname,atttypid::regtype::text,attnotnull FROM pg_attribute "
        "WHERE attrelid=%s AND attnum>0 AND NOT attisdropped ORDER BY attnum",
        (row[3],),
    ).fetchall()
    exact_count = connection.execute(
        f"SELECT count(*) FROM {qualified_relation_name(relation)}"
    ).fetchone()[0]
    value = {
        "database": row[0],
        "schema": row[1],
        "relation": row[2],
        "oid": row[3],
        "relfilenode": row[4],
        "reltuples": row[5],
        "exact_rows": exact_count,
        "columns": columns,
    }
    return hashlib.sha256(
        json.dumps(value, sort_keys=True, default=str, separators=(",", ":")).encode()
    ).hexdigest()


def _acquisition_logical_metadata(connection: Connection[Any], relation: str) -> RelationMetadata:
    row = connection.execute(
        "SELECT n.nspname,c.relname,c.oid FROM pg_class c JOIN pg_namespace n ON n.oid=c.relnamespace "
        "WHERE c.oid=to_regclass(%s) AND c.relkind IN ('r','p')",
        (relation,),
    ).fetchone()
    if row is None:
        raise ValueError(f"acquisition relation missing: {relation}")
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


def acquire_payloads(
    connection: Connection[Any],
    catalog: CandidateCatalog,
    output_path: Path,
    *,
    statistics_target: int = 100,
    global_statistics_target: int | None = None,
    upstream_sha256: str,
    patch_commit: str,
    repository_id: str,
    source_relations: tuple[RelationMetadata, ...],
) -> AcquisitionResult:
    if (
        global_statistics_target is not None
        and statistics_target != 100
        and statistics_target != global_statistics_target
    ):
        raise ValueError(
            "statistics_target and global_statistics_target disagree; "
            "the MVP uses one global target"
        )
    target = statistics_target if global_statistics_target is None else global_statistics_target
    if not 0 <= target <= 10000:
        raise ValueError("global_statistics_target must be between 0 and 10000")
    if output_path.exists():
        raise FileExistsError(f"repository output already exists: {output_path}")
    output_path.parent.mkdir(parents=True, exist_ok=True)
    temporary = Path(tempfile.mkdtemp(prefix=f".{output_path.name}.", dir=output_path.parent))
    created: list[tuple[str, str]] = []
    started = datetime.now(UTC).isoformat()
    try:
        # Ordinary column statistics use this session target.  Extended
        # statistics definitions below receive the identical value explicitly.
        connection.execute(
            "SELECT set_config('default_statistics_target', %s, false)", (str(target),)
        )
        version = str(connection.execute("SHOW server_version").fetchone()[0])
        source_by_name = {item.qualified_name: item for item in source_relations}
        relation_names = tuple(sorted({item.relation_name for item in catalog.candidates}))
        compatibility = []
        for relation in relation_names:
            source = source_by_name.get(relation)
            if source is None:
                raise ValueError(f"source compatibility metadata missing: {relation}")
            acquisition = _acquisition_logical_metadata(connection, relation)
            compatible = source.logical_descriptor == acquisition.logical_descriptor
            compatibility.append(
                (relation, source.logical_fingerprint, acquisition.logical_fingerprint, compatible)
            )
            if not compatible:
                raise ValueError(f"source/acquisition relation incompatible: {relation}")
        actual: dict[str, tuple[int, int, str, str]] = {}
        for candidate in catalog.candidates:
            name = _acquisition_name(str(candidate.candidate_id))
            schema = candidate.relation_name.split(".")[0]
            mechanism = "mcv" if candidate.mechanism is MechanismKind.MCV else "dependencies"
            attributes = ", ".join(quote_identifier(item) for item in candidate.attributes)
            connection.execute(
                f"CREATE STATISTICS {quote_identifier(schema)}.{quote_identifier(name)} ({mechanism}) "
                f"ON {attributes} FROM {qualified_relation_name(candidate.relation_name)}"
            )
            connection.execute(
                f"ALTER STATISTICS {quote_identifier(schema)}.{quote_identifier(name)} SET STATISTICS {target}"
            )
            row = connection.execute(
                "SELECT e.oid,e.stxrelid,e.stxkind FROM pg_statistic_ext e JOIN pg_namespace n ON n.oid=e.stxnamespace "
                "WHERE n.nspname=%s AND e.stxname=%s",
                (schema, name),
            ).fetchone()
            if row is None or candidate.mechanism.postgres_code not in row[2]:
                raise RuntimeError(
                    f"acquisition definition validation failed: {candidate.candidate_id}"
                )
            actual[str(candidate.candidate_id)] = (int(row[0]), int(row[1]), str(row[2]), name)
            created.append((schema, name))
        relations = relation_names
        for relation in relations:
            connection.execute(f"ANALYZE {qualified_relation_name(relation)}")
        payload_dir = temporary / "payloads"
        payload_dir.mkdir()
        records = []
        fingerprints = {
            relation: _relation_fingerprint(connection, relation) for relation in relations
        }
        for candidate in catalog.candidates:
            oid, relation_oid, _kinds, name = actual[str(candidate.candidate_id)]
            if candidate.mechanism is MechanismKind.MCV:
                expression, extension = "pg_mcv_list_send(d.stxdmcv)", "mcv"
                interpretation = {"sender": "pg_mcv_list_send", "postgres_type": "stxdmcv"}
            else:
                expression, extension = "pg_dependencies_send(d.stxddependencies)", "fd"
                interpretation = {
                    "sender": "pg_dependencies_send",
                    "postgres_type": "stxddependencies",
                }
            row = connection.execute(
                f"SELECT {expression} FROM pg_statistic_ext_data d WHERE d.stxoid=%s", (oid,)
            ).fetchone()
            if row is None:
                raise RuntimeError(
                    f"missing pg_statistic_ext_data row for {candidate.candidate_id}"
                )
            if row[0] is None:
                payload = None
                state = "ABSENT_NATIVE"
                relative = None
                payload_size = None
                payload_sha256 = None
            else:
                payload = bytes(row[0])
                if not payload:
                    raise RuntimeError(f"empty serialized native payload for {candidate.candidate_id}")
                state = "PRESENT"
                relative = Path("payloads") / f"{candidate.candidate_id}.{extension}.bin"
                (temporary / relative).write_bytes(payload)
                payload_size = len(payload)
                payload_sha256 = hashlib.sha256(payload).hexdigest()
            records.append(
                {
                    "candidate_id": candidate.candidate_id,
                    "relation_oid": relation_oid,
                    "relation_name": candidate.relation_name,
                    "mechanism": candidate.mechanism.value,
                    "attributes": candidate.attributes,
                    "definition": {
                        "statistics_name": name,
                        "stxkind": candidate.mechanism.postgres_code,
                        "attnums": dict(candidate.definition).get("attnums"),
                    },
                    "precedence_rank": candidate.precedence_rank,
                    "backend_oid": oid,
                    "state": state,
                    "payload_path": str(relative) if relative else None,
                    "payload_size": payload_size,
                    "payload_sha256": payload_sha256,
                    "relation_fingerprint": fingerprints[candidate.relation_name],
                    "interpretation": interpretation,
                }
            )
        manifest = {
            "format_version": 2,
            "repository_id": repository_id,
            "postgres_version": version,
            "upstream_tarball_sha256": upstream_sha256,
            "patch_commit": patch_commit,
            "acquisition_provenance": {
                "method": "physical CREATE STATISTICS plus relation-grouped ANALYZE",
                "statistics_target": target,
                "global_statistics_target": target,
                "analyzed_relations": relations,
                "analyze_count": len(relations),
                "started_at": started,
                "completed_at": datetime.now(UTC).isoformat(),
                "relation_compatibility": [
                    {
                        "relation": relation,
                        "source_logical_fingerprint": source,
                        "acquisition_logical_fingerprint": acquisition,
                        "compatible": compatible,
                    }
                    for relation, source, acquisition, compatible in compatibility
                ],
            },
            "candidates": records,
        }
        (temporary / "manifest.json").write_text(
            json.dumps(manifest, sort_keys=True, indent=2) + "\n"
        )
        PayloadRepository.load(temporary)
        connection.commit()
        os.replace(temporary, output_path)
        repository = PayloadRepository.load(output_path)
        return AcquisitionResult(
            repository,
            relations,
            len(relations),
            tuple(name for _, name in created),
            tuple(compatibility),
        )
    except Exception:
        connection.rollback()
        for schema, name in reversed(created):
            connection.execute(
                f"DROP STATISTICS IF EXISTS {quote_identifier(schema)}.{quote_identifier(name)}"
            )
        connection.commit()
        shutil.rmtree(temporary, ignore_errors=True)
        raise


def cleanup_acquisition(connection: Connection[Any], result: AcquisitionResult) -> None:
    for frozen in result.repository.payloads:
        name = str(dict(frozen.candidate.definition)["statistics_name"])
        schema = frozen.candidate.relation_name.split(".")[0]
        connection.execute(
            f"DROP STATISTICS IF EXISTS {quote_identifier(schema)}.{quote_identifier(name)}"
        )
    connection.commit()
