# ANALYZE sample cache

This experiment adds an internal PostgreSQL-side cache for the tuple sample
acquired by `ANALYZE`. It is deliberately separate from `pg_statistic`: the
cache stores the sampled `HeapTuple` values and the estimated live-row count,
then passes those tuples to the existing statistics generation code.

## Controls

- `pgextadv.analyze_sample_export`: a server-side path. When non-empty,
  normal reservoir sampling runs and the resulting sample is written there.
- `pgextadv.analyze_sample_import`: a server-side path. When non-empty,
  random sampling is skipped and the validated sample at that path is used.

The controls are mutually exclusive. Empty values preserve ordinary
PostgreSQL ANALYZE behavior.

## File format

The file is a PostgreSQL-native binary format (`PGEXTSC1`, format version 1).
It contains the format and server version, relation OID and name, tuple
descriptor metadata for every column (name, type OID, typmod, collation, and
dropped status), the sampled tuple count, the estimated total row count, and
the raw sampled heap tuple bytes. A CRC32C covers the header and payload.
Import rejects a bad magic value, unsupported format or server version,
relation/OID or descriptor mismatch, invalid tuple lengths, row counts larger
than the ANALYZE target, and checksum failures.

The file is an intermediate experiment artifact. It is not a portable table
backup, SQL dump, CSV format, or statistics payload. It is tied to the same
PostgreSQL major/minor server build and relation descriptor; a table rewrite,
DDL change, or different server build requires a fresh export.

## Validation and limitations

The regression test exports a sample, confirms that the file is non-empty,
imports it on a subsequent `ANALYZE`, and checks that the resulting `pg_stats`
rows are identical. The cache currently supports one relation per path and
writes the path supplied by the GUC. It does not cache dead tuples, planner
estimates, query results, or extension statistics payloads. It does not
provide locking or concurrent-writer coordination for a shared path.

## Build record

- PostgreSQL base tag: `REL_16_14`
- PostgreSQL base commit: `0d1c00c624fa7367d4a895f44381887757289682`
- Sample-cache patch commit: `54d4d01c829dc895ec4577d8c7902c3931ff7de2`
- Configure: `--prefix=$HOME/projects/postgresql-install-pgextadv-16.14 --enable-debug --enable-cassert --with-openssl`
- Installed binary: `$HOME/projects/postgresql-install-pgextadv-16.14/bin/postgres`
- Installed `postgres` SHA256: `ed76483e1e572f7a9f3b104da47a1c77998c6d493d4c4f6fe20cf57ee4775bcb`
- Validation: `make check` passed all 221 regression tests.
