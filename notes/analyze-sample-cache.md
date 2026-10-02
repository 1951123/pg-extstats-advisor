# ANALYZE sample-cache provenance and determinism

This note closes the provenance boundary for the experimental PostgreSQL-side
ANALYZE sample cache. The cache stores the acquisition sample (sampled
`HeapTuple` values and the estimated live-row count), not `pg_statistic` or
`pg_statistic_ext_data` payloads.

## Reproducible source stack

The clean-room build starts from vanilla PostgreSQL `REL_16_14`, commit
`0d1c00c624fa7367d4a895f44381887757289682`, and applies these tracked advisor
patches in order:

1. `pg/patches/postgresql-16.14-hypothetical-extstats.patch` — SHA256
   `22c7f48632585e81fd8a557dc8bffba873ac5da070aca31713e22c60261c3b4f`.
2. `pg/patches/postgresql-16.14-analyze-sample-cache.patch` — SHA256
   `0d8c3fb24d59c52e2548875b04403d81c3fc1dfe22c1cfd12c935fb7f1691bc5`.

The sample-cache source history is based on commit
`54d4d01c829dc895ec4577d8c7902c3931ff7de2`; commit
`960bfebc9c492edbcf733b26db60f988488f327f` adds the portable relation-identity fix. The clean-room source is
an archive of the clean upstream checkout plus the two patches; the mutable
`postgresql-src-pgextadv` development checkout is not used as build input.

The build used:

```text
scripts/build_postgres16_cleanroom.sh \
  /home/wqts/projects/postgresql-src \
  /home/wqts/projects/postgresql-build-pgextadv-cleanroom-20261002c \
  /home/wqts/projects/postgresql-install-pgextadv-cleanroom-20261002c
```

Configure arguments were `--enable-debug --enable-cassert --with-openssl` with
an explicit user-owned prefix. `make -j$(nproc)`, `make check` (221/221), and
`make install` passed. The resulting `postgres` binary SHA256 is
`ee6be55da350662007c229d81701a4b7fb1637255e38fb06bfeab951e1384f73`.

## Controls and format

- `pgextadv.analyze_sample_export`: normal reservoir sampling runs; the
  resulting sample is written to the server-side path.
- `pgextadv.analyze_sample_import`: random sampling is skipped; the validated
  sample at the path is passed into the existing statistics-generation path.

Empty values preserve ordinary PostgreSQL behavior. The controls cannot be
used together.

The binary format is `PGEXTSC1`, format version 1. It records the exact
PostgreSQL version number/string, a diagnostic relation OID, a canonical
length-prefixed schema/relation identity, ordered tuple-descriptor metadata
(column name, type OID, typmod, collation, and dropped flag), tuple count,
estimated total rows, and raw sampled heap-tuple bytes. CRC32C covers the
header and payload. Import rejects wrong magic/version/server version,
identity or descriptor mismatch, invalid tuple lengths/counts, checksum
failures, and trailing bytes.

The canonical identity is `length(schema):schema length(relation):relation`;
for example `6:public16:sample_cache_det`. OID is retained for diagnostics and
legacy compatibility. A new artifact can therefore be imported by a relation
with the same schema/name and descriptor but a different OID. A legacy v1
artifact containing only a bare relation name is accepted only when its OID
still matches. This keeps old files safe while making new files portable across
recreated catalogs.

## Validation evidence

A disposable PostgreSQL 16.14 instance on port 55438 used a deterministic
20,000-row relation with two MCV and two functional-dependency candidates.
The exported artifact was 1,085,375 bytes with SHA256
`957b698b48145b329202acc4103508090fff17afe7836edf6b51ede6c46ad6e7`; its
header reported `PGEXTSC1`, format 1, PostgreSQL 16.14, and identity
`6:public16:sample_cache_det`.

The export run followed by three imports produced identical digests for:

- ordinary column statistics (`3b1df2fe8f4e1c92dd989769d46446a1`),
- both MCV payloads (`27e7a8f2e4ce675d329b4582ce464d74`), and
- both dependency payloads (`257fa208b5c1b8237ab48c9944dd14ff`).

All four extended-statistics rows were present after each run, so one imported
sample fed multiple candidates in one `ANALYZE`. Recreating the relation under
the same `public.sample_cache_det` identity yielded a different OID and a
successful import. Separate negative checks rejected a wrong relation name,
wrong descriptor, corrupted checksum, and unsupported format version.

The repository regression test also passed its export/import ordinary-statistics
comparison. No benchmark data, workload, q-error experiment, planner
comparison, or extension-statistics experiment was run as part of this task.

## Limitations

The cache is tied to this PostgreSQL 16.14 binary format and relation descriptor; a DDL change, incompatible server build, or different relation
identity requires a fresh export. It supports one relation per path, has no
locking or concurrent-writer coordination, and does not capture dead tuples,
planner estimates, query results, or final statistics payloads. The evidence
above establishes deterministic replay for this controlled relation and these
statistics kinds; it does not claim workload-level or benchmark-level
reproducibility.
