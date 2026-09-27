# Legacy hypothetical-overlay audit

## Artifact reviewed

The historical input is
`/root/projects/extended-stats-optim-v3/patches/pg16_hypothetical_extstats_overlay_v0.patch`.
It is evidence and implementation reference only. It is not copied as the new
system patch and is not an upstream baseline.

## What the prototype established

The prototype demonstrated three sufficient PostgreSQL 16.14 interception
points:

1. filter and order the list returned by `RelationGetStatExtList()` before
   `get_relation_statistics()` constructs `StatisticExtInfo` nodes;
2. intercept `statext_mcv_load()` before its catalog lookup and pass frozen
   serialized MCV bytes to PostgreSQL's native deserializer;
3. intercept `statext_dependencies_load()` analogously for FD payloads.

This preserves PostgreSQL's estimator, GreedyCover, clause-consumption,
cross-mechanism, and payload-deserialization semantics. The prototype also
showed that backend-local switching can avoid per-design catalog mutation,
`ANALYZE`, rollback, and relcache invalidation.

## Retain

- The three narrow interception locations above.
- PostgreSQL-native serialized `bytea` payloads as the execution authority.
- Native MCV and dependency deserializers.
- Backend-local state and default-off behavior.
- An explicit ordered selected-candidate sequence as effective precedence.
- The invariant that ordinary catalog behavior is unchanged when the overlay is
  inactive.

## Drop or rework

- **String GUC control plane:** comma-separated repository/design OID lists are
  parsing-heavy, weakly typed, and not a production evaluator API. Replace them
  with a compact backend-local API exposed through a narrow test/control surface.
- **Linear repository lookup:** replace `List` scans with an OID-keyed hash table;
  preserve a separate ordered design vector for precedence.
- **Lazy catalog capture:** loading candidate bytes on first estimator access
  blurs acquisition and evaluation. Registration must explicitly copy already
  frozen payloads into a dedicated memory context before design evaluation.
- **TopMemoryContext ownership:** use one named child context whose reset has an
  explicit session/repository lifecycle. A design switch must not leak payload or
  parsed-list allocations.
- **Implicit identity:** catalog OID is a backend-local handle, not the external
  stable candidate identity. Registration records both and validates relation,
  kind, and definition metadata.
- **Prototype naming and benchmark harness:** `ce_replay_*` identifiers and any
  experiment-only instrumentation do not belong in the production system.
- **Unvalidated relation scope:** visibility and payload lookup must reject a
  candidate outside its registered relation and mechanism.

## Minimal migration

The first implementation patch should remain small:

1. add a backend-local repository memory context, OID-keyed payload table, and
   ordered active-design vector;
2. add explicit reset, register, and activate operations with validation;
3. filter/reorder definition visibility after `RelationGetStatExtList()`;
4. supply registered bytes at the MCV and FD native loader boundaries;
5. expose the smallest SQL-callable control functions needed by integration
   tests and the Python adapter;
6. add PostgreSQL regression tests for disabled behavior, ordering, reset, and
   malformed registration.

No estimator logic, search logic, workload cache, or semantic incremental replay
belongs in the PostgreSQL patch.

## Bootstrap patch status

The bootstrap patch adds only the internal API contract header. It deliberately
does not yet hook planner or payload-loader code. This keeps the authoritative
delta present and reproducible without representing M0 as implemented. The
implementation and regression tests are the next dedicated commit.
