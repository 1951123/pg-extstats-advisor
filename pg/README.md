# PostgreSQL source discipline

The sole upstream source is the immutable archive:

`/root/projects/extended-stats-optim/postgresql-16.14.tar.bz2`

Its expected digest is recorded in `SHA256SUMS`. Do not edit, repack, or silently
replace that archive.

## Three separate trees

1. `/root/projects/postgresql-reference/postgresql-16.14` is a read-only source
   navigation tree prepared directly from the archive. Never patch or build it.
2. `.build/postgresql-16.14-src` is a disposable patched source tree.
3. `.build/postgresql-16.14-install` is a generated install prefix.

The only authoritative PostgreSQL delta is
`patches/postgresql-16.14-hypothetical-extstats.patch`. A binary is attributable
to this project only when the clean build script verified the archive, extracted
a new disposable tree, applied that tracked patch, and built it.

## Patch workflow

- Read upstream code in the reference tree.
- Design changes against pristine 16.14 paths.
- Develop only in a disposable extraction.
- Regenerate the tracked patch relative to pristine upstream.
- Verify forward apply and reverse dry-run.
- Rebuild from scratch with `scripts/build_postgres16.sh`.

The old checkout under `/root/projects/extended-stats-optim/` and its prototype
patch are historical references, not upstream baselines.
