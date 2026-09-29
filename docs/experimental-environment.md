# Authoritative experimental environment

All paper-authoritative PostgreSQL empirical experiments use the source-built
PostgreSQL 16.14 profile owned by this repository. Ubuntu/distro PostgreSQL,
unverified installations, and bare PostgreSQL commands are not authoritative
experiment environments.

## Environment roles

1. `/root/projects/postgresql-reference/postgresql-16.14` is a read-only source
   reference for audit, grep, and line citations. It is never patched or built.
2. `.build/postgresql-16.14-install` is recreated from the immutable 16.14 tarball,
   the tracked patch, and `scripts/build_postgres16.sh`. This release-like profile
   uses the configure defaults plus `--without-readline --without-zlib
   --without-icu`, effective `-O2`, no assertions, no debug build, and no LLVM/JIT
   build. Build provenance is tracked in
   `experiments/environment/postgresql-16.14-build.json`.
3. `.build/pg16.14-experiment-cluster` is the persistent, reproducible experiment
   cluster. It is ignored by Git. Every lifecycle/import script invokes tools by
   absolute path from the authoritative install prefix and verifies server version
   16.14. The cluster uses port 55436 and a repo-local Unix socket.

The frozen upstream archive SHA256 is
`f6d077142737920858ce958ccdb75c6ee137a63b5b0853c70693d401ac7e3471`.
The build recipe digest excludes its creation timestamp. It is a composite build-input
identity over the upstream tarball, tracked patch, configure arguments, compiler
identity, and CFLAGS (including effective PostgreSQL CFLAGS); the binary digest is
recorded separately. The upstream and patch identities are also recorded as separate
fields, so historical artifacts can distinguish source/patch changes from compiler
or configuration changes. The current timing build remains release-like.

## Dataset rebuild discipline

Database files are never copied from a distro cluster. Census is restored through
a logical CSV export of the fixed legacy relation because no original Census raw
file/import script survives in the legacy repositories. The export checksum and
this limitation are explicit provenance. DMV is loaded from the canonical benchmark
CSV and receives the legacy deterministic `btrim` transformation. The loaders
validate exact row count, ordered schema, column types, nullability, and persistence.

The source-built databases are named `pgextadv_exp16_census` and
`pgextadv_exp16_dmv`. Dataset provenance is tracked in
`experiments/environment/census-dataset.json` and `dmv-dataset.json`; raw database
files and the large Census logical CSV stay under ignored `.build/` paths.

## Frozen settings and future policy

The cluster records JIT, parallelism, cache/cost settings, work memory, and ordinary
statistics target in each calibration artifact. Extended-statistics target remains
explicit per experiment. Calibration additionally binds the exact server version,
build recipe digest, authoritative binary path, and binary SHA256.

Maintenance calibration, benchmark search, deployment validation, future plan
experiments, and future execution/runtime work must use this same profile unless a
new versioned experiment campaign is explicitly declared. Integration fixtures may
use another environment only when marked non-authoritative.

Authoritative CE experiments may not depend on an unpersisted `ANALYZE` sample.
They must replay a persisted, checksummed acquisition sample or declare a new
versioned acquisition campaign with its own lineage before results are accepted.

For the Census M2.21 campaign, the native 30,000-row acquisition sample is
persisted under `datasets/census-frozen-acquisition-sample-v1/` and all replay,
singleton, search, and physical-validation stages consume that sample. During
the initial capture preflight, the pre-existing Census database was repaired by
registering the already compiled backend-local
`pg_hypothetical_extstats_register_absent` internal function. This was a
database-local registration repair only: no PostgreSQL source, tracked patch,
binary, build input, candidate catalog, or CE semantics changed. The repair is
recorded in the M2.21 protocol and lineage manifests.
