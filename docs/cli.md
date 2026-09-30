# Product CLI

The supported core workflow is fixed-target and offline after capture.  The
default global statistics target is `100`; it is an external configuration,
not a search decision.  The current supported capture profile is
`fixed_t_single_snapshot` on PostgreSQL 16.14, with one base relation and
base-relation selection queries.  Bundle v2 target-grid artifacts remain
experimental and are not accepted by the product `advise` command.

## Capture

```text
pg-extstats-advisor capture \
  --dsn "$PGEXT_CAPTURE_DSN" \
  --relation public.example \
  --workload workload.json \
  --output capture-bundle
```

Capture opens one `REPEATABLE READ READ ONLY` transaction, exports its
snapshot, checks that the role is a non-superuser with schema USAGE and
relation SELECT, and records the exact population, truth, metadata, and a
deterministic reservoir sample.  It does not require (or perform) CREATE,
write, or ANALYZE privileges.  Explicit column or existing-extstats target
overrides fail closed.  The role is never granted privileges automatically.

The sample may contain real production values.  Privacy and encryption are
not solved by this workflow and must be handled by the deployment environment.

## Move the sealed bundle offline

After the bundle is sealed, stop or firewall the production endpoint.  The
`advise` interface accepts only a sealed capture bundle and an advisor-cluster
DSN; it has no production DSN parameter and never connects to production.

```text
pg-extstats-advisor advise capture-bundle \
  --advisor-dsn "$PGEXT_ADVISOR_DSN" \
  --candidate-catalog candidates.json \
  --incidence incidence.json \
  --maintenance-model maintenance-model.json \
  --budget 372.045872636249472 \
  --output recommendation \
  --cache .advisor-cache
```

The advisor evaluates one frozen production-derived statistics realization.
Future PostgreSQL `ANALYZE` runs may produce different statistics
realizations.  The search chooses only the extended-statistics design; it does
not select or change the global target.

## Validate, inspect, and deploy

```text
pg-extstats-advisor validate capture-bundle
pg-extstats-advisor validate recommendation
pg-extstats-advisor inspect recommendation/recommendation.json
```

Review `deploy.sql` and `rollback.sql` before applying them manually.  The
deployment output uses stock PostgreSQL `CREATE STATISTICS`, `ALTER STATISTICS`,
and `ANALYZE`; it does not use the hypothetical extension API or mutate the
database target.  Rollback drops only the recommendation's deterministic
statistics names in reverse order.  There is no automatic deployment.

The product scope is intentionally limited to PostgreSQL 16.14, one base
relation, arity-two MCV/dependency candidates, and selection cardinality
estimation.  Joins, multi-table designs, target optimization, and privacy
redesign are outside the supported scope.
