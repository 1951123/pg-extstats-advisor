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

## Recommendation bundle

Recommendation format version 1 records the evaluated target, capture digest,
problem/workload/truth/acquisition identities, candidate catalog and
maintenance-model digests, baseline/final objective, selected design and
digest, selected-object metadata (relation, canonical attributes, mechanism,
deterministic object name, maintenance cost, and payload state), search
termination metadata, stock deployment DDL, reverse rollback DDL, scope, and
root digest.  Payload repositories remain derived cache artifacts and are not
embedded in the recommendation.

All validators fail closed on corruption, unsupported profiles, target or
compatibility mismatches, stale identities, unknown candidates, duplicate
object names, and malformed DDL.
