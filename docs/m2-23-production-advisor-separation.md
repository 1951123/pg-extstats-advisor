# M2.23 — Pristine production / patched advisor separation

M2.23 establishes a physical separation between a stock PostgreSQL 16.14
production simulator and a patched PostgreSQL 16.14 advisor backend.  The
production simulator was built directly from the checked upstream archive
(`f6d077142737920858ce958ccdb75c6ee137a63b5b0853c70693d401ac7e3471`) with no
advisor patch.  It ran on port 55437 and the patched advisor ran independently
on port 55438, with separate data and Unix-socket directories.  Their server
binary digests were respectively
`154e03469ee1e0cdbf54db9325877df0146384bdf9e553611809aa4be9d84d40` and
`a54fa02a96906b3853ee68d80f86e02cdaaa6bd2031822026ac943abcb171ba1`.

The stock side loaded the canonical 11,591,877-row, 11-column nullable-text
UNLOGGED DMV relation using the existing `btrim` import semantics.  A
non-superuser `pgextadv_m223_capture` role had CONNECT, schema USAGE, and table
SELECT only; `CREATE TABLE`, `CREATE STATISTICS`, and INSERT were rejected in
read-only transactions.  Capture operations were metadata SELECTs, exact
`SELECT COUNT(*)` queries, and a stock COPY SELECT stream.  No ANALYZE or
statistics DDL was part of the capture path.

The prototype capture is sealed under the ignored path
`.build/production-captures/dmv-m2-23-v1/`.  Its root semantic digest is
`4ab35dc7ecb21d5707ce4e384329118633f907fd6ed58fa7652268fdb649aba8`.
Timestamps and local paths are excluded from the root digest.  The truth
component contains all 1,965 exact counts from the full production relation;
the two zero-truth queries remain excluded from the 1,963-query positive-truth
objective, whose digest is unchanged (`e790933cfc4f0f42d92807170b76cec080621c5b463dab83e23474a0a51151d8`).

The production simulator was stopped before advisor reconstruction and
`pg_isready` reported no response on port 55437.  The patched advisor then
loaded only the sealed artifact.  It materialized two 30,000-row disposable
staging relations, replayed `frozen_totalrows=11,591,877`, and derived all 72
candidate realizations: 70 `PRESENT` and 2 `ABSENT_NATIVE`.  The resulting
prototype repository digest was
`893705417f1f4c134c0e96f8a4022d76c6ac8f82c9b7c22c359dc73c06194f51`, with
baseline objective `88053.61940613187` and estimate-vector digest
`6be99060002f3be7c49070e20ba57ecf520a5970f01f42053707dcf02bad1274`.
These are prototype-lineage values, not a replacement for the persisted native
ANALYZE sample A or its authoritative results.

## Dependency inventory

**Definitely required:** PostgreSQL major/version identity; relation identity,
ordered column type/collation/nullability schema; original relation cardinality;
workload SQL and effective membership; exact truth vector; acquisition sample;
statistics target and planner settings that affect replay.

**Captured conservatively:** selected relation statistics, `reltuples`/`relpages`,
server version, selected planner GUCs, snapshot identifiers, role and privilege
metadata.

**Research-oracle-only:** persisted native-ANALYZE sample A, its payload
repository and ordinary-statistics digest.  These are comparison controls, not
inputs to the production capture contract.

**Missing/unresolved:** a stock production acquisition method proven equivalent
to the patched native-ANALYZE sample semantics; a final minimal capture schema;
cross-major compatibility.

The prototype therefore demonstrates capture sufficiency and hard separation,
but does not claim sampling equivalence or begin the formal Production Capture
Bundle v1 CLI.
