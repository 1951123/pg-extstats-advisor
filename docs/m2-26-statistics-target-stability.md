# M2.26: DMV statistics-target stability

The MVP uses one `global_statistics_target` for a complete ANALYZE state.  It
is fixed before acquisition/replay and is shared by ordinary column statistics
and every MCV/FD candidate.  Target tuning is outside the current search
scope.  This avoids introducing sample-size coupling or target-specific
candidate variants into the design space.

M2.26 characterizes T=100, 300, and 1000 with three independent native
PostgreSQL 16.14 ANALYZE realizations per target.  PostgreSQL chooses the
sample capacity; the patched hook persists the exact shared sample consumed by
both ordinary and extended-statistics builders.  The experiment never runs a
budget search and does not change the Production Capture Bundle v1 contract.

The authoritative artifacts are under
`experiments/dmv-m2-26-statistics-target-stability/`.  Large sample/repository
payloads are immutable ignored artifacts under
`.build/artifact-cache/dmv-statistics-target-stability-v1/`; manifests record
target, source row count, sample and repository digests, and candidate-catalog
lineage.  On the observed DMV workload, T300 reduced baseline objective
variance substantially relative to T100; T1000 did not improve it further.
