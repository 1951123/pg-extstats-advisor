# Scope

## In scope for M0

- PostgreSQL 16.14 only.
- One supplied, fixed workload and explicit truths.
- Base-relation restriction estimates.
- MCV and functional-dependency candidates.
- Native binary payload acquisition from one recorded realization.
- Explicit fixed precedence.
- Conservative candidate-to-query incidence.
- Backend-local hypothetical design switching.
- Native replanning of affected queries.
- Positive-truth q-error aggregation and reuse of unaffected contributions.
- Physical/native reference checks for a small workload slice.

## Explicitly out of scope

- Full CE-Replay migration.
- Query-internal semantic caching or dependency oracles.
- New optimizer formulations, solvers, or neighborhoods.
- Learned CE and join CE models.
- Planner-state caching or a fixed CE skeleton.
- Online tuning, workload monitoring, GUIs, or autonomous operation.
- Multiple PostgreSQL versions.
- Sophisticated candidate-generation heuristics.
- Paper, RQ, contribution, or experimental-claim revision.

## Correctness boundaries

Native payload bytes, acquisition provenance, ordered definition visibility, and
the target query boundary are part of correctness. Decoded JSON is never an
authoritative execution representation. False-negative incidence is forbidden.
Fresh deployment payload drift is distinct from hypothetical/native semantic
fidelity.

## Fixed global statistics target

The PostgreSQL global statistics target is an external production/DBA input,
not an optimization variable. The default is `global_statistics_target = 100`;
an explicitly supplied positive integer is accepted and then remains fixed for
the prepared problem and extstats search. Target grids and target selectors
belong only to the historical M2.27 experimental modules. See
`docs/statistics-target-scope.md` for the correctness, robustness, sampling,
and maintenance-model boundaries.
