# ANALYZE sample-cache provenance and determinism

This note records the experimental PostgreSQL-side ANALYZE sample cache. The
cache stores the acquisition sample (sampled `HeapTuple` values and estimated
live-row count), not `pg_statistic` or `pg_statistic_ext_data` payloads.

## Authoritative source

The source of truth is the clean `postgresql-pgextadv` Git repository, not a
patch file:

- upstream base: PostgreSQL `REL_16_14`,
  `0d1c00c624fa7367d4a895f44381887757289682`;
- authoritative commit:
  `6d7f5c9cd6cf1b0f73e84a4bacc45a31d1cb0cd6`;
- derived advisor patch:
  `pg/patches/postgresql-16.14-pgextadv.patch`;
- derived patch SHA256:
  `35736bbb40045394e7a2cc56405c7760392439014343c6ede3fffa490d2197dd`.

The old split patches are retired from the active tree. Their implementations
remain recoverable through advisor Git history. The canonical derivation is
`git diff --binary --full-index --no-ext-diff --no-renames` between the upstream
base and the frozen authoritative commit, implemented by
`scripts/export_postgres_patch.sh`.

The authoritative source history contains the sample-cache commits
`54d4d01c829dc895ec4577d8c7902c3931ff7de2` and
`960bfebc9c492edbcf733b26db60f988488f327f`, plus the hypothetical-statistics
commit `f1c50b9d5543933fc38f3493a571857c0c0de9d3`. `PGEXTADV.md` documents the
repository role.

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
estimated total rows, raw sampled heap-tuple bytes, and CRC32C.

The canonical identity is `length(schema):schema length(relation):relation`,
for example `6:public16:sample_cache_det`. OID is retained for diagnostics and
legacy compatibility. A new artifact can be imported by a relation with the
same schema/name and descriptor but a different OID. A legacy v1 artifact
containing only a bare relation name is accepted only when its OID still
matches. Bad magic/version/server version, identity or descriptor mismatch,
invalid tuple lengths/counts, checksum failures, and trailing bytes are
rejected.

## Build and validation evidence

A clean-room build from the authoritative commit used
`--enable-debug --enable-cassert --with-openssl`, passed `make -j$(nproc)`,
`make check` (222/222), and `make install`. Its PostgreSQL 16.14 binary SHA256
was `3859247d686d758c04d1f5ea899bab35894ab11941730010f5900d18d5fdfbf9`.

A disposable PostgreSQL 16.14 instance on port 55438 used a deterministic
20,000-row relation with two MCV and two functional-dependency candidates. The
exported artifact was 1,085,375 bytes with SHA256
`957b698b48145b329202acc4103508090fff17afe7836edf6b51ede6c46ad6e7`; its
header reported `PGEXTSC1`, format 1, PostgreSQL 16.14, and identity
`6:public16:sample_cache_det`.

The export run followed by three imports produced identical digests for:

- ordinary column statistics: `3b1df2fe8f4e1c92dd989769d46446a1`;
- both MCV payloads: `27e7a8f2e4ce675d329b4582ce464d74`;
- both dependency payloads: `257fa208b5c1b8237ab48c9944dd14ff`.

All four extended-statistics rows were present after each run, so one imported
sample fed multiple candidates in one ANALYZE. Recreating the relation under
the same `public.sample_cache_det` identity yielded a different OID and a
successful import. Separate negative checks rejected a wrong relation name,
wrong descriptor, corrupted checksum, and unsupported format version.

No benchmark data, workload, q-error experiment, planner comparison, or
extension-statistics experiment was run for this validation.

## Limitations

The cache is tied to PostgreSQL 16.14 binary and relation-descriptor
compatibility. A DDL change, incompatible server build, or different relation
identity requires a fresh export. It supports one relation per path, has no
locking or concurrent-writer coordination, and does not capture dead tuples,
planner estimates, query results, or final statistics payloads. The evidence
establishes deterministic replay for this controlled relation and these
statistics kinds; it does not claim workload-level or benchmark-level
reproducibility.
