# M0-A PostgreSQL hypothetical overlay

## Source boundaries

`RelationGetStatExtList()` returns a caller-owned OID-sorted list. Immediately
after that call, `get_relation_statistics()` now filters and reorders the list;
its loop constructs `StatisticExtInfo` nodes in that order and does not reorder
them later. `statext_mcv_load()` and `statext_dependencies_load()` are intercepted
before catalog payload lookup and continue to use the existing native
deserializers.

Both deserializers treat the serialized `bytea` as read-only and allocate their
result in the caller's current context. The repository therefore owns one copied,
session-lifetime serialized value; reset releases it, while each planner call owns
and releases its ordinary decoded estimator object.

## Backend state and ownership

All state is backend-local, with no shared memory. A named
`HypotheticalExtStatsContext` under `TopMemoryContext` owns a repository child and
one replaceable active-design child. Reset deletes the root and clears every
static pointer. Registration detoasts and copies bytes into the repository child.
Activation validates its complete input before replacing the old active child.

The repository is a PostgreSQL dynahash keyed by `(statistics OID, mechanism
kind)` with relation OID and native serialized payload as the value. Separate MCV
and FD entries may share an OID. Duplicate keys, bad relation identity, unsupported
kinds, empty/malformed payloads, unknown activation OIDs, duplicate active OIDs,
and selected missing kinds fail loudly.

## Ordered design and visibility

The active design consists of an ordered OID vector plus an OID membership hash.
The input order is effective precedence. For a relation containing registered
candidates, filtering emits selected candidates in this order, hides registered
but unselected candidates, and then retains catalog definitions outside the
registered candidate universe. Relations with no registered candidates retain
the original list unchanged. An explicitly activated empty array is distinct
from default-off state and hides registered candidates.

The verified order-sensitive fixture had catalog/active `A,B` produce 300 plan
rows and reversed active `B,A` produce 100 rows. The debug function returned the
same requested OID order.

## Loader behavior

For a selected, registered non-inherited object, the MCV and FD loaders pass the
repository bytes directly to the corresponding native deserializer. A missing
selected kind is an error, never a catalog fallback. Unselected, inherited, or
default-off calls follow the original catalog path. M0-A is limited to ordinary
non-inherited base-relation payloads.

## SQL test/control surface

Four volatile built-ins exist solely as the narrow M0 control boundary:

- `pg_hypothetical_extstats_reset()`
- `pg_hypothetical_extstats_register(oid, oid, "char", bytea)`
- `pg_hypothetical_extstats_activate(oid[])`
- `pg_hypothetical_extstats_active()`

The kind codes are PostgreSQL's `m` (MCV) and `f` (dependencies). Tests obtain
authoritative bytes through the native send functions after fixture `ANALYZE`.
No comma-separated GUC or decoded execution format is used.

## Correctness evidence

`tests/integration/m0_postgres_overlay.sql` covers default-off before activation,
explicit empty design, single MCV, two overlapping MCVs in both orders, FD-only,
mixed MCV+FD, relation isolation, repeated `Y1/Y2/empty/Y1`, reset, catalog
fingerprints, and eight validation/fail-loudly paths. Physical and hypothetical
plan rows are exact for the single-MCV (1000), FD-only (100), mixed (25), and
catalog-order overlapping-MCV (300) cases. Empty design restores the no-extstats
estimate (100). Catalog definition and payload fingerprints are unchanged across
the hot path, which contains no `CREATE/DROP/UPDATE` or `ANALYZE`.

## Known limitations

- M0-A extracts exact `Plan Rows`; it does not expose a raw-selectivity probe.
- The built-ins are an internal test/control surface, not a stable public API or
  extension package.
- Registered candidates must retain catalog definitions; the overlay changes
  visibility and payloads but does not synthesize definition metadata.
- Inherited-statistics payload interception is not implemented.
