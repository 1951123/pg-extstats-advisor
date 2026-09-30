# DBA operational runbook

This runbook covers the supported fixed-target MVP: PostgreSQL 16.14, one
base relation, base-relation selection CE, arity-two MCV and functional
dependency statistics, a fixed external `default_statistics_target` (100 by
default), read-only capture, offline advice, and manual deployment. Joins,
automatic target selection, other PostgreSQL major versions, and automatic
deployment are unsupported.

## Trust boundary and permissions

Capture runs inside the production trust zone on a trusted host or jump box.
The capture role needs `CONNECT`, schema `USAGE`, and `SELECT` on the source
relation and must be a non-superuser. It does not need `CREATE`, `INSERT`,
`ANALYZE`, `ALTER`, or grant-management privilege. The capture client exports
a sealed bundle; the advisor does not remotely pull production data.

The post-deployment verification role additionally needs read-only catalog
visibility for `pg_statistic_ext` and `pg_statistic_ext_data` (a DBA may grant
`SELECT` on those catalogs according to local policy). It still needs no
`CREATE`, `DROP`, `ANALYZE`, `ALTER`, `INSERT`, or superuser privilege.

Use `.pgpass`, environment/enterprise secret injection, or client
certificates. Do not put passwords in shell history or commit them. The CLI
does not persist credentials and does not print a complete DSN by default.

Capture bundles may contain sampled real production values, exact truth, SQL,
and constants. Treat them as sensitive: encrypt in transit and at rest, limit
access, define retention, and securely delete them when no longer required.
Recommendation bundles may contain schema metadata, workload SQL, constants,
provenance, and DDL, so they are potentially sensitive as well. This project
does not implement encryption, secret management, or automatic deletion.

The conceptual lifecycle is `CAPTURED -> ADVISED -> PREFLIGHT_PASS ->
DEPLOYED -> VERIFIED`, with an optional `ROLLED_BACK -> ROLLBACK_VERIFIED`
branch. These are audit states, not mutable fields in the immutable
recommendation artifact; each verification result is a separate report.

## Workflow

1. Capture from the production trust zone with the read-only role:

   ```text
   pg-extstats-advisor capture --dsn "$PGEXT_CAPTURE_DSN" \
     --relation public.example --workload workload.json \
     --output capture-bundle --statistics-target 100
   ```

2. Validate the sealed bundle before transfer:

   ```text
   pg-extstats-advisor validate capture-bundle
   ```

3. Transfer the bundle through the approved protected channel. The advisor
   zone does not need production credentials.

4. Run `advise` offline against the disposable patched advisor connection,
   static candidate/incidence artifacts, and the fixed maintenance model. The
   advisor does not select or change the global target.

5. Validate and inspect the recommendation:

   ```text
   pg-extstats-advisor validate recommendation/recommendation.json
   pg-extstats-advisor inspect recommendation/recommendation.json
   ```

6. Return the recommendation bundle to the production trust zone and run the
   read-only deployment preflight immediately before review/deployment:

   ```text
   pg-extstats-advisor preflight recommendation/recommendation.json \
     --production-dsn "$PGEXT_PRODUCTION_DSN"
   ```

   Add `--json` for automation. A `PASS` is compatibility evidence, not
   authorization to deploy. A `FAIL` is fail-closed and must be resolved by a
   DBA; the tool never fixes drift.

7. Review `deploy.sql` manually, using normal change-control and backup
   procedures. Apply it manually as the DBA. Object ownership follows normal
   PostgreSQL execution semantics.

8. Run `ANALYZE` in a DBA-selected maintenance window. `CREATE STATISTICS`
   and `ANALYZE` can consume I/O/CPU and may acquire locks; the advisor does
   not schedule or execute them.

9. If rollback is required, review and apply `rollback.sql` manually. After
   deployment, a DBA can verify that the selected `pg_statistic_ext` rows
   exist and that `ANALYZE` completed.

## After deployment

Run the read-only verifier after applying `deploy.sql` and running `ANALYZE`:

```text
pg-extstats-advisor verify-deployment recommendation/recommendation.json \
  --production-dsn "$PGEXT_PRODUCTION_DSN"
```

It checks the exact PostgreSQL version and target, relation/schema drift,
target overrides, every deterministic definition's relation, columns, kind,
and target semantics, and the presence of its `pg_statistic_ext_data` row. A
definition without a data row is reported as incomplete with guidance to run
`ANALYZE`; the verifier never runs it. A native MCV or dependency field may be
NULL even when the data row is present, so that state is reported as a compact
native-payload warning rather than confused with missing ANALYZE.

Partial deployment is a hard failure listing present and missing objects. A
wrong-kind or wrong-column object under a deterministic name is also a hard
failure. Review the partial state and either complete the manual deployment or
apply the reviewed rollback; the advisor does not continue or repair it.

## After rollback

After manually applying `rollback.sql`, verify that every selected advisor-owned
name is absent:

```text
pg-extstats-advisor verify-rollback recommendation/recommendation.json \
  --production-dsn "$PGEXT_PRODUCTION_DSN"
```

Rollback verification does not claim to restore a previous ANALYZE sample. It
only checks that this recommendation's definitions are gone, the relation and
fixed target contract remain visible, and no generated rollback action is
silently treated as a repair. A partial rollback fails and lists remaining
objects. Preflight is the before-mutation check; these commands are after-
mutation state checks and together do not provide transaction-level atomicity.

Capture and offline advice write into a temporary sibling, validate the
artifact, and atomically install it. An interrupted run therefore leaves no
new apparently-valid sealed bundle or recommendation at the requested output
path; an existing artifact is not overwritten.

## What preflight checks

Preflight validates the recommendation before connecting, then uses only
`SELECT`/`SHOW` statements in a read-only transaction. It checks exact
PostgreSQL 16.14, the effective `default_statistics_target`, role visibility
and read-only permissions, relation existence/kind, portable schema identity
(names, order/attnum, types, typmods, collations, and nullability), column and
existing-extstats target overrides, deterministic statistics-name collisions,
and equivalent extstats under another name. It returns structured checks and
uses compatibility exit code 3 for drift and permission exit code 4 for
insufficient visibility.

The recommendation binds capture-time schema metadata without relying on
runtime relation OIDs. A same-name drop/recreate therefore passes only if its
portable schema identity still matches. Existing equivalent extstats are
reported and fail closed; the tool never deduplicates, overwrites, drops, or
reuses them.

Row/value changes are not schema failures. `reltuples`, when visible, is an
informational diagnostic only. Preflight does not rerun workload truth,
sample acquisition, `COUNT(*)`, `ANALYZE`, or any target-setting command.

## Operational boundaries

The recommendation and preflight are point-in-time artifacts. A read-only
preflight cannot guarantee that production remains unchanged between the
check and manual deployment. Run it immediately before deployment, minimize
the delay, and rerun it if metadata changes. Artifact age is informational;
there is no automatic expiration policy. DBA-owned retention rules apply to
capture bundles, recommendations, and caches.
