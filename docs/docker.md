# Docker clean-room reproduction

This repository contains the release-qualified local-only clean-room path for reproducing the
fixed-target MVP without mounting the host virtual environment, PostgreSQL
source tree, PostgreSQL data directories, or `/root/projects` into a runtime
container. It is an operational reproduction harness, not a new evaluator or
search implementation.

The three images are built from Ubuntu 24.04:

- `pg-extstats-advisor/capture:cleanroom` contains a wheel installed into a
  fresh virtual environment and stock PostgreSQL client tools. It performs
  capture, validation, preflight, inspection, and deployment verification.
- `pg-extstats-advisor/advisor:cleanroom` contains the same wheel and a
  source-built, patched PostgreSQL 16.14 backend. The backend is private to
  the container and is used only to acquire/replay native payloads for
  `advise`; it never connects to production.
- `pg-extstats-advisor/stock-postgres:16.14` is an unpatched source-built
  PostgreSQL 16.14 image used for the disposable production and validation
  roles.

The advisor image build uses the derived aggregate patch
`pg/patches/postgresql-16.14-pgextadv.patch`, whose source of truth is the
frozen `postgresql-pgextadv` Git commit recorded in `pg/postgresql-source.json`.
It verifies the upstream tarball, records the configure flags, compiler, and
resulting `postgres` checksum, and does not use the host `.build/` tree. The stock image is compiled from the same tarball without the
patch. No image is pushed to a registry and no image tarball is checked in.

## Run the tracked fixture

Docker and its local daemon must be available. The runner uses the tarball
path below by default; set `PG_TARBALL` when it is stored elsewhere:

```bash
scripts/docker_cleanroom_m233.sh
```

The runner builds from a temporary context made from the committed checkout,
loads `examples/docker-cleanroom/fixture.sql` into disposable stock
PostgreSQL containers, captures a sealed fixed-`T=100` bundle, stops the
capture source, and runs the advisor offline. It then runs validation,
preflight, manual deployment/`ANALYZE`, verification, rollback, and negative
checks for a corrupt bundle, unavailable capture source, wrong target,
and missing `ANALYZE`. Runtime logs and caches remain ignored under
`experiments/m2-33-docker-cleanroom/runtime/` and `cache/`.

The committed evidence files are:

- `experiments/m2-33-docker-cleanroom/build-provenance.json`
- `experiments/m2-33-docker-cleanroom/lifecycle-summary.json`
- `experiments/m2-33-docker-cleanroom/README.md`

The reproducibility claim is semantic: cold and warm advisor runs must select
the same design, objective, and recommendation digest. Docker layers,
wheel bytes, and PostgreSQL binaries are not claimed to be byte-identical
across hosts.

The runtime containers use non-root users. Passwords are generated only by
the local runner and are not written to evidence files, source files, image
labels, or committed artifacts. The fixture is synthetic and non-sensitive;
it is not a DMV reproduction.
