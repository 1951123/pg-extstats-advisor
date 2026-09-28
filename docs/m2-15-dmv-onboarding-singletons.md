# M2.15 DMV onboarding and singleton profiling

M2.15 onboards the authoritative DMV workload and profiles the complete raw
candidate universe without running budget search or selecting a screening
fraction. The source is `benchmarks/DMV/queries/dmv.sql` from the frozen
external benchmark repository; its 1,965 query records remain preserved as
raw provenance.

## Explicit objective membership

The current advisor uses the explicit `positive_truth_only` workload policy for
DMV: a query belongs to the effective CE objective iff `truth > 0`. The raw
source contains exactly two zero-truth records, `dmv.173` and `dmv.943`, so the
effective workload has 1,963 queries. Negative truth is invalid and fails
closed. This policy is applied during workload preparation, persisted in the
workload artifact, and is not hidden in q-error, the evaluator, incidence, or
candidate generation. Existing workloads default to `require_all_positive` and
are unchanged.

Historical v3 code appeared in two forms: one retained zero-truth queries and
gave them a zero q-error contribution; another explicitly aggregated only
positive-truth queries. The current advisor adopts the latter as an explicit
preprocessing representation while retaining its existing positive-truth
q-error contract. This does not claim numerical reproduction of every v3
implementation detail.

## Preparation and native realization

The effective workload produces 36 unordered predicate-column pairs, 36 MCV
candidates, and 36 FD candidates. The frozen source-built PostgreSQL 16.14
acquisition uses one fixed statistics target and one relation-grouped
`ANALYZE`; each candidate is retained as `PRESENT` or `ABSENT_NATIVE`. No
candidate is silently dropped and no empty payload is fabricated. Acquisition
statistics are cleaned after evaluator validation.

DMV has no accepted benchmark-specific maintenance model. Singleton profiling
therefore records maintenance cost as unavailable and ranks only by descending
singleton improvement, ascending candidate precedence, and candidate ID. The
Census M2.6 maintenance model is not used or transferred.

## Profile boundary

The empty-design objective and all 72 singleton evaluations are descriptive
evidence about the effective DMV workload. Positive/zero/negative counts,
quantiles, utility concentration, query-side summaries, and repeatability
checks are persisted under `experiments/dmv-m2-15-singletons/`. Utility
concentration is not an additive achievable design-gain claim. DMV onboarding
and singleton profiling are complete; the benchmark-specific development
screening fraction remains a separate next decision.
