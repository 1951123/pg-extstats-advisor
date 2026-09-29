# M2.23 — Production/advisor separation report

The prototype establishes two physically independent PostgreSQL 16.14
installations.  Stock PostgreSQL was extracted from the immutable upstream
archive and built without the tracked advisor patch.  The stock binary and
patched advisor binary differ (`154e0346…` versus `a54fa02a…`), and stock
source contains no hypothetical-extstats symbols.  Resolving the three
advisor hypothetical functions on stock returned no functions.

The stock production simulator held the complete canonical DMV relation:
11,591,877 rows, eleven nullable `text` columns, canonical `btrim` import, and
UNLOGGED persistence.  The capture role had only CONNECT, schema USAGE, and
table SELECT.  Capture used read-only metadata/catalog SELECTs, exact COUNT
queries, and a COPY SELECT stream; CREATE TABLE, CREATE STATISTICS, and INSERT
were rejected.  The 1,965 exact truths matched the existing authoritative
workload and retained the two known zero-truth IDs.

The sample is a deliberately non-authoritative prototype: a persisted 30,000
row, all-column, deterministic reservoir from a stock COPY stream.  It is not
claimed equivalent to native PostgreSQL ANALYZE sampling.  Truth and sample
were collected in separate exported repeatable-read snapshots, so the artifact
claims best-effort/multi-snapshot consistency rather than a single global
snapshot.

After sealing root digest `4ab35dc7…aba8`, production was stopped and
`pg_isready` on port 55437 reported no response.  The advisor reconstructed two
30,000-row disposable staging relations from the sealed artifact only, with
`frozen_totalrows=11,591,877`.  It produced all 72 candidate realizations (70
PRESENT and 2 ABSENT_NATIVE), ordinary statistics, a baseline objective, and
an estimate vector.  No search was run.  The prototype values differ from
research-oracle sample A, as expected; exact sample, repository, and objective
equality is not a gate.

## Dependency inventory

Definitely required inputs are PG major/version, relation schema and
collations, original cardinality, workload/effective membership, exact truth,
and an acquisition sample.  Relation statistics and selected planner GUCs are
captured conservatively.  The persisted native-ANALYZE sample A is
research-oracle-only.  A stock acquisition protocol with native-ANALYZE
fidelity and the final minimal Bundle v1 schema remain unresolved.

## Exit answers

1. Yes: stock production and patched advisor have separate binaries, ports,
   sockets, and data directories.
2. Yes: production capture used no advisor patch or hypothetical API.
3. Yes: the full DMV base relation exists only on production; advisor saw only
   30,000-row staging/sample inputs.
4. Yes: after production shutdown, advisor reconstructed stats/repository and
   baseline from the sealed artifact.
5. Yes: capture was read-only and mutation attempts were rejected.
6. Yes: stock-compatible versus native-ANALYZE sample fidelity remains an
   unresolved research question.
7. Yes: the boundary and required dependency classes are clear enough to start
   defining a formal Production Capture Bundle v1 contract, but this milestone
   does not implement that CLI.
