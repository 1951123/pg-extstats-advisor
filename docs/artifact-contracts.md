# Artifact contracts

## Capture bundle

The supported core input is Bundle v1 with profile
`fixed_t_single_snapshot`.  It binds the effective positive statistics target,
one strong snapshot, relation/schema identity, workload and truth digests,
exact source population, acquisition sample manifest, target-override evidence,
and a root semantic digest.  Historical v1 bundles without this profile are
recognized as compatible legacy artifacts but are not accepted by product
`advise`.

The sample is authoritative for replay of that bundle and is explicitly not
claimed to be native `ANALYZE`-equivalent.

Candidate definition and payload state are separate. `PRESENT` means the
requested native payload was materialized; `ABSENT_NATIVE` means PostgreSQL
registered the definition but legitimately stored no requested payload field;
`UNREGISTERED` is an acquisition or validation failure. Maintenance cost is
charged to the candidate definition, not inferred from payload presence.

## Recommendation bundle

Recommendation format version 1 records the evaluated target, capture digest,
problem/workload/truth/acquisition identities, candidate catalog and
maintenance-model digests, baseline/final objective, selected design and
digest, selected-object metadata (relation, canonical attributes, mechanism,
deterministic object name, maintenance cost, and payload state), search
termination metadata, stock deployment DDL, reverse rollback DDL, scope, and
root digest. Product recommendations additionally bind a portable capture-time
schema identity (relation name/kind, ordered columns, types, typmods,
collations, nullability, and a schema digest); runtime relation OIDs are not
part of this identity. Payload repositories remain derived cache artifacts and
are not embedded in the recommendation.

All validators fail closed on corruption, unsupported profiles, target or
compatibility mismatches, stale identities, unknown candidates, duplicate
object names, and malformed DDL.

Empirical maintenance models are target-specific. A recommendation at a
different target is rejected unless a model calibrated for that target is
supplied; the current core never silently reuses a `T=100` model.

Post-deployment and rollback verification reports are separate immutable
artifacts. They contain only compact catalog presence/definition/materialization
metadata, checks, warnings, failures, and an informational verification
timestamp; they never contain credentials or raw `pg_statistic_ext_data`
payload bytes and never modify the recommendation digest.
