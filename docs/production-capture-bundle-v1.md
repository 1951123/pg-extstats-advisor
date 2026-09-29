# Production Capture Bundle v1 contract

`production-capture-bundle-v1` is the portable, sealed directory contract
between a production PostgreSQL capture process and the offline advisor. It
contains the inputs needed to reconstruct the current base-relation CE state;
it never contains a full base table or extended-statistics payload bytes.

## Layout

```text
production-capture-bundle-v1/
  bundle.json
  environment.json
  schema.json
  workload.json
  truth.json
  acquisition/relations/<relation-id>/
    manifest.json
    sample.copy.bin
```

The relation directory makes the contract multi-relation-ready even though the
current advisor scope is single-relation base-count queries. Multi-relation
storage does not imply join CE support.

## Authority and exclusions

Production remains authoritative for full relation data, exact full-data truth,
the captured sample input, and environment/schema compatibility metadata. The
sample is authoritative for the statistics realization of this bundle, but
that does not make its sampling method native-`ANALYZE` equivalent. Ordinary
`pg_statistic` rows, extstats payload bytes, payload repositories, search state,
and recommendations are derived advisor artifacts and are explicitly excluded
from the bundle.

The sensitivity declaration says that the bundle contains sampled production
row values and exact truth, does not contain the full base table, is not
anonymized, and is not encrypted. Digests provide integrity/tamper detection
only, not confidentiality.

## Required fields and identities

`bundle.json` requires `bundle_schema_version`, `sealed`, `semantic_digest`,
`production_identity`, `snapshot_consistency`, `components`,
`relation_inventory`, `workload_identity`, `truth_identity`,
`acquisition_identity`, `compatibility`, `capture_mode`, `created_timestamp`,
and `sensitivity`. `sealed=true` is valid only when every required component,
semantic digest, binary digest, root digest, and verifier check succeeds.

Stable relation identity is a portable `schema.name` identifier plus relation
kind, persistence, and schema digest. Production OIDs are provenance only and
never part of the portable identity. Columns use portable namespace/type names,
typmods, collations, nullability, and fixed statistics targets.

Workload identity binds query ID, SQL, effective membership, weight, target
relation ID, CE target ID, and parser-support metadata. Query display order is
not semantic; query identity, SQL, target, membership, and weight are. Truth
binds IDs rather than only positional arrays and uses explicit
`exact_full_data_count` quality. Approximate truth is not authoritative v1.

## Snapshot and sampling semantics

The contract supports `strong_single_snapshot` and
`best_effort_multi_snapshot`. M2.24 DMV uses the latter: metadata/schema,
truth, and acquisition identify their own repeatable-read snapshots, while the
workload is an external static input. The verifier never upgrades this to a
strong claim and no cryptographic database-state hash is implied.

The DMV prototype uses `deterministic_reservoir_v1`, version 1, with a
length-prefixed UTF-8 binary serialization (`pgextstats_m223_length_prefixed_v1`,
version 1). Its capability declaration is:

```text
production_compatible       = true
stock_postgresql_only       = true
deterministic_replay        = true
authoritative_for_bundle    = true
native_analyze_equivalent   = false
native_analyze_distribution = none
```

`sample_row_count`, `source_population_rows`, and
`statistics_population_rows` are separate fields. Sample size is never used
to infer production cardinality; current DMV values are 30,000 and 11,591,877.

## Canonicalization, sealing, and verification

Semantic JSON is UTF-8, sorted-key, compact JSON hashed with SHA-256. Runtime
timestamps, absolute local paths, hostnames, temporary directories, backend
PIDs, and non-portable production OIDs are excluded from semantic identity.
Binary SHA-256 is recorded separately. A root digest binds the schema version,
production identity, snapshot descriptor, component digests, relation
inventory, workload/truth/acquisition identities, compatibility declaration,
capture mode, and sensitivity declaration.

`verify_production_capture_bundle(path)` fails closed on unknown schema versions,
missing components, malformed or duplicate IDs, inconsistent relation/truth/
sample references, unsupported method or serialization versions, incoherent
population counts, binary or semantic tampering, and root mismatch.
Offline reconstruction writes its derived repository and comparison report
beside the bundle directory; they are not bundle components.

## Advisor compatibility

`check_advisor_compatibility()` currently requires exact PostgreSQL 16.14,
supported portable types (`pg_catalog.text` for the DMV MVP), matching database
collation, known statistics target, the supported sample method/serialization,
the `pg16-mvp-v2` workload analysis version, and the
`single_relation_base_count` CE scope. PG17, type/collation mismatch, unknown
sampling/serialization versions, unsupported workload analysis, and joins fail
closed. Same-major portability is deliberately not claimed.

## Optional and unresolved areas

Capture timing, operational notes, index metadata, frequency sources, and
broader planner settings are optional/advisory. Still unresolved are stock
sample fidelity versus native `ANALYZE`, sample-size sufficiency, production
truth cost at scale, strong-snapshot ergonomics, portability beyond exact
16.14, multi-relation execution, privacy/encryption, and live-production
workload capture. These are outside the v1 contract and are not experimental
claims of this milestone.
