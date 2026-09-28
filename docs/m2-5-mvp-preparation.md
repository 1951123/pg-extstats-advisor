# M2.5-A/B MVP preparation

`prepare_mvp(config, source_connection, acquisition_connection)` converts a supplied fixed workload into the existing `Workload`, `CandidateCatalog`, `PayloadRepository`, and `IncidenceIndex` models. It does not run search or provide a CLI.

The version-1 JSON config requires distinct source/acquisition DSN roles, a version-1 workload JSON path, artifact output directory, enabled `mcv`/`fd` mechanisms, maximum arity, optional per-relation cap, optional explicit candidates, one statistics target, and maintenance-model metadata. Configuration has canonical JSON and SHA256 identity; there are no per-candidate targets.

Workload records contain `query_id`, SQL, positive `truth`, `target_relation`, and optional label. Ingestion resolves schema, relation OID, and ordered column metadata in the read-only source connection. The deliberately narrow inspector accepts one-relation SELECT/WHERE queries and common conjunctive comparisons, IN, and IS NULL. Joins, subqueries, CTEs, aggregates, GROUP BY, windows, and set operations fail loudly. An otherwise valid selection with an unclassified clause is retained in conservative-fallback mode.

Automatic generation forms every arity 2..max combination appearing together in a precisely inspected query, for each enabled mechanism. Explicit mode accepts `{relation, mechanism, columns}` records instead. Columns canonicalize by `attnum`; stable IDs are SHA256 of qualified relation, mechanism, and canonical names and never contain an OID. Precedence sorts by qualified relation, MCV before FD, arity, attnums, then stable ID. Caps truncate this order deterministically without scoring.

Acquisition writes only to the caller-supplied isolated acquisition connection. It creates `pgextadv_acq_<candidate-id-sha256>` objects, applies one uniform target, creates all objects before running exactly one ANALYZE per relation, and persists native `pg_mcv_list_send` or `pg_dependencies_send` bytes. The manifest records OIDs, kind, target, timestamps, analyzed relations/count, hashes, and metadata, then is reloaded through `PayloadRepository.load`. Failure rolls back and explicitly removes partial definitions. Successful definitions remain in the disposable acquisition database because the PG16 overlay uses their OIDs as registration handles; `cleanup_acquisition` removes them after evaluation.

Relation fingerprints hash database/schema/relation identity, OID, relfilenode, reltuples, exact row count, and ordered column/type/nullability signature. Workload digest hashes query content, truth, target identity/OID and metadata. Candidate digest binds ID, relation identity/OID, mechanism, attributes, definition, and precedence.

Artifacts are `config.json`, `workload.json`, `candidates.json`, `repository/manifest.json` plus native blobs, `incidence.json`, and `prepare-summary.json`. Writes outside the repository acquisition directory use temporary-file replacement. The summary records all lineage digests, versions, counts, target, ANALYZE count, and completion time.

Limitations are PostgreSQL 16.14, fixed offline workloads, base selection CE, plain columns, MCV/FD, simple SQL inspection, a uniform target, and a pre-existing maintenance model. There is no CLI, join/ndistinct/expression support, workload capture, fitting, M3 runtime work, or general SQL parser.
