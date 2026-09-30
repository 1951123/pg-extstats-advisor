# Release candidate summary

This is a **research-prototype release candidate**, not production-ready
software, a stable 1.0 release, a registry image, or a PyPI publication.

## Identity

- audited source baseline: `22249dbf1ad3e74dddf45983d6658933327748d4`;
- package version: `0.0.1`;
- PostgreSQL: 16.14 exactly;
- Docker is the only release-qualified deployment path;
- default fixed global statistics target: `T=100`;
- supported mechanisms: arity-two MCV and functional dependencies.

The machine-readable contract is `release/rc-manifest.json`. Build hashes and
semantic lifecycle evidence are authoritative in
`experiments/m2-33-docker-cleanroom/build-provenance.json` and
`lifecycle-summary.json`.

## Supported lifecycle

```text
capture -> validate -> offline advise -> inspect -> preflight
        -> DBA review/manual deploy -> ANALYZE -> verify-deployment
        -> optional manual rollback -> verify-rollback
```

The capture process is read-only and exports a sealed bundle. The advisor uses
that bundle offline with a private patched PostgreSQL backend. Production may
remain stock PostgreSQL 16.14. See [the Docker guide](docker.md),
[the DBA runbook](dba-runbook.md), and [supported scope](supported-scope.md).

## Docker reproduction

With Docker and the verified PostgreSQL tarball available locally:

```bash
scripts/docker_cleanroom_m233.sh
```

The runner builds `pg-extstats-advisor/capture:cleanroom`,
`pg-extstats-advisor/advisor:cleanroom`, and
`pg-extstats-advisor/stock-postgres:16.14`, runs the synthetic non-sensitive
fixture, and checks cold/warm semantic equality, target/preflight failures,
manual deployment plus `ANALYZE`, rollback, corruption, and unavailable-source
paths. No image is pushed to a registry.

## Trust, privacy, and security caveats

Capture bundles can contain sampled real production values, exact truth, SQL,
and constants. Recommendation bundles can contain schema metadata, SQL,
provenance, and DDL. Treat both as sensitive: protect transport and storage,
restrict access, define retention, and delete under local policy. The project
does not provide encryption, a secret manager, centralized authentication,
network policy, or automatic retention/deletion. Docker non-root execution is
not a complete security boundary.

## Known limitations

Joins, multi-table execution, target optimization, higher arity, other
mechanisms, automatic deployment/rollback/repair, transactionally atomic
deployment, latency guarantees, global search optimality, and arbitrary
PostgreSQL versions are unsupported. A frozen acquisition realization may
differ from a future production `ANALYZE` realization; this is accepted
realization uncertainty, not a same-realization replay failure.

Historical target-grid, reservoir/native, acquisition-fidelity, Census, and
DMV studies remain under `experiments/` as bounded evidence. They do not
expand the supported core. No tag, GitHub Release, registry image, or PyPI
package is created by this candidate.
